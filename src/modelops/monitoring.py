"""Metrics and alerting.

Unattended automation is only trustworthy if it tells you when it stops
working. This module is the "how would I know?" half of the pipeline.

Two design choices worth calling out:

* Alerts are de-duplicated by a stable key with a cooldown window. An
  alerting system that fires once per failed job trains people to ignore
  it; one that fires once per *problem* and then goes quiet does not.

* Delivery is a local append-only JSONL sink. Swapping that for a real
  notification channel is one function, and keeping it a file means the
  POC has no outbound dependency.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config
from .db import Database, utcnow

CRITICAL = "CRITICAL"
WARNING = "WARNING"
INFO = "INFO"


@dataclass
class Alert:
    rule_code: str
    severity: str
    subject: str
    body: str
    dedupe_key: str
    entity_type: str | None = None
    entity_id: str | None = None


# =====================================================================
# Metrics
# =====================================================================

def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * pct
    low, high = int(rank), min(int(rank) + 1, len(ordered) - 1)
    return float(ordered[low] + (ordered[high] - ordered[low]) * (rank - low))


def stage_latency(db: Database) -> list[dict]:
    """Per-stage timing, including true percentiles.

    Averages hide the tail, and the tail is what breaches an SLA, so p95
    is computed here rather than only reporting the mean.
    """
    rows = db.query(
        "SELECT stage, duration_ms, status FROM job_steps WHERE duration_ms IS NOT NULL")
    buckets: dict[str, list[float]] = {}
    failures: dict[str, int] = {}
    for row in rows:
        buckets.setdefault(row["stage"], []).append(float(row["duration_ms"]))
        if row["status"] == "FAILED":
            failures[row["stage"]] = failures.get(row["stage"], 0) + 1

    from .stages import STAGE_NAMES
    order = {name: i for i, name in enumerate(STAGE_NAMES)}

    out = []
    for stage, values in buckets.items():
        out.append({
            "stage": stage,
            "executions": len(values),
            "failures": failures.get(stage, 0),
            "avg_ms": round(statistics.fmean(values)),
            "p50_ms": round(percentile(values, 0.50)),
            "p95_ms": round(percentile(values, 0.95)),
            "max_ms": round(max(values)),
            "total_s": round(sum(values) / 1000.0, 1),
        })
    out.sort(key=lambda r: order.get(r["stage"], 99))
    return out


def overview(db: Database) -> dict:
    """Headline numbers for the dashboard."""
    totals = db.query_one(
        "SELECT COUNT(*) AS total,"
        " SUM(CASE WHEN status='SUCCEEDED' THEN 1 ELSE 0 END) AS succeeded,"
        " SUM(CASE WHEN status='FAILED' THEN 1 ELSE 0 END) AS failed,"
        " SUM(CASE WHEN status='QUARANTINED' THEN 1 ELSE 0 END) AS quarantined,"
        " SUM(CASE WHEN status IN ('QUEUED','RETRYING') THEN 1 ELSE 0 END) AS pending,"
        " SUM(CASE WHEN status='RUNNING' THEN 1 ELSE 0 END) AS running,"
        " SUM(CASE WHEN attempt>1 THEN 1 ELSE 0 END) AS retried,"
        " SUM(sla_breached) AS sla_breaches"
        " FROM jobs") or {}

    total = totals["total"] or 0
    terminal = (totals["succeeded"] or 0) + (totals["failed"] or 0) + (totals["quarantined"] or 0)
    durations = [float(r["duration_ms"]) for r in db.query(
        "SELECT duration_ms FROM jobs WHERE status='SUCCEEDED' AND duration_ms IS NOT NULL")]

    published = db.query_one(
        "SELECT COUNT(*) AS models, COALESCE(SUM(component_count),0) AS components,"
        " COALESCE(SUM(triangle_count),0) AS triangles,"
        " COALESCE(SUM(published_bytes),0) AS published_bytes,"
        " COALESCE(SUM(wire_bytes),0) AS wire_bytes,"
        " COALESCE(SUM(naive_bytes),0) AS naive_bytes,"
        " COALESCE(SUM(source_bytes),0) AS source_bytes"
        " FROM publications WHERE is_current = 1") or {}

    subscribers = db.scalar(
        "SELECT COALESCE(SUM(subscriber_count),0) FROM projects WHERE is_active = 1",
        default=0)
    open_alerts = db.scalar(
        "SELECT COUNT(*) FROM alerts WHERE resolved_at IS NULL", default=0)
    critical_alerts = db.scalar(
        "SELECT COUNT(*) FROM alerts WHERE resolved_at IS NULL AND severity = 'CRITICAL'",
        default=0)

    source_bytes = published["source_bytes"] or 0
    published_bytes = published["published_bytes"] or 0
    wire_bytes = published["wire_bytes"] or 0
    naive_bytes = published["naive_bytes"] or 0

    return {
        "jobs_total": total,
        "jobs_succeeded": totals["succeeded"] or 0,
        "jobs_failed": totals["failed"] or 0,
        "jobs_quarantined": totals["quarantined"] or 0,
        "jobs_pending": totals["pending"] or 0,
        "jobs_running": totals["running"] or 0,
        "jobs_retried": totals["retried"] or 0,
        "sla_breaches": totals["sla_breaches"] or 0,
        "success_rate_pct": round(100.0 * (totals["succeeded"] or 0) / terminal, 1)
                            if terminal else 0.0,
        "avg_duration_s": round(statistics.fmean(durations) / 1000.0, 2) if durations else 0.0,
        "p95_duration_s": round(percentile(durations, 0.95) / 1000.0, 2) if durations else 0.0,
        "models_live": published["models"] or 0,
        "components_live": published["components"] or 0,
        "triangles_live": published["triangles"] or 0,
        "published_bytes": published_bytes,
        "wire_bytes": wire_bytes,
        "naive_bytes": naive_bytes,
        "source_bytes": source_bytes,
        "payload_reduction_pct": round(100.0 * (naive_bytes - published_bytes) / naive_bytes, 1)
                                 if naive_bytes else 0.0,
        "wire_reduction_pct": round(100.0 * (naive_bytes - wire_bytes) / naive_bytes, 1)
                              if naive_bytes else 0.0,
        "subscribers": subscribers,
        "open_alerts": open_alerts,
        "critical_alerts": critical_alerts,
        "quarantined_files": len(list(_quarantine_files(db))),
    }


def _quarantine_files(db: Database):
    rows = db.query(
        "SELECT r.source_path FROM model_revisions r WHERE r.status = 'QUARANTINED'")
    for row in rows:
        p = Path(row["source_path"])
        if p.exists():
            yield p


def project_health(db: Database) -> list[dict]:
    return [dict(r) for r in db.query(
        "SELECT * FROM v_job_health ORDER BY run_date DESC, project_code")]


def failure_taxonomy(db: Database) -> list[dict]:
    return [dict(r) for r in db.query(
        "SELECT * FROM v_failure_taxonomy ORDER BY occurrences DESC")]


def qa_summary(db: Database) -> list[dict]:
    return [dict(r) for r in db.query(
        "SELECT * FROM v_qa_summary ORDER BY "
        "CASE severity WHEN 'ERROR' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END, "
        "finding_count DESC")]


# =====================================================================
# Alert rules
# =====================================================================

class AlertEngine:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.rules_cfg = config.get("alerting.rules", {})

    def evaluate(self) -> list[Alert]:
        alerts: list[Alert] = []
        alerts += self._consecutive_failures()
        alerts += self._low_success_rate()
        alerts += self._sla_breaches()
        alerts += self._qa_blocked()
        alerts += self._slow_stages()
        alerts += self._stale_models()
        alerts += self._quarantine_backlog()
        return alerts

    # -- individual rules ---------------------------------------------
    def _consecutive_failures(self) -> list[Alert]:
        threshold = int(self.rules_cfg.get("consecutive_failures_threshold", 2))
        out: list[Alert] = []
        projects = self.db.query("SELECT project_code FROM projects WHERE is_active = 1")

        for project in projects:
            rows = self.db.query(
                "SELECT j.status, j.error_class, m.model_key FROM jobs j "
                "JOIN model_revisions r ON r.revision_id = j.revision_id "
                "JOIN models m ON m.model_id = r.model_id "
                "WHERE m.project_code = ? AND j.status IN "
                "('SUCCEEDED','FAILED','QUARANTINED') "
                "ORDER BY j.finished_at DESC, j.job_id DESC LIMIT 20",
                (project["project_code"],))
            streak, classes, models = 0, [], []
            for row in rows:
                if row["status"] == "SUCCEEDED":
                    break
                streak += 1
                classes.append(row["error_class"] or "Unknown")
                models.append(row["model_key"])

            if streak >= threshold:
                out.append(Alert(
                    rule_code="CONSECUTIVE_FAILURES",
                    severity=CRITICAL if streak >= threshold * 2 else WARNING,
                    subject=f"{project['project_code']}: {streak} consecutive model "
                            f"deliveries failed",
                    body=f"The last {streak} completed jobs for "
                         f"{project['project_code']} all failed.\n"
                         f"Affected models: {', '.join(dict.fromkeys(models))}\n"
                         f"Error classes: {', '.join(dict.fromkeys(classes))}\n"
                         f"Deliveries for this project are not reaching users.",
                    dedupe_key=f"consecutive:{project['project_code']}:{streak // 2}",
                    entity_type="project", entity_id=project["project_code"]))
        return out

    def _low_success_rate(self) -> list[Alert]:
        window_hours = int(self.rules_cfg.get("success_rate_window_hours", 24))
        minimum = float(self.rules_cfg.get("min_success_rate_pct", 90.0))
        since = (datetime.now(timezone.utc) - timedelta(hours=window_hours)
                 ).strftime("%Y-%m-%d %H:%M:%S")

        row = self.db.query_one(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN status='SUCCEEDED' THEN 1 ELSE 0 END) AS ok "
            "FROM jobs WHERE status IN ('SUCCEEDED','FAILED','QUARANTINED') "
            "AND queued_at >= ?", (since,))
        total = (row["total"] if row else 0) or 0
        if total < 5:
            return []          # too small a sample to be meaningful
        rate = 100.0 * (row["ok"] or 0) / total
        if rate >= minimum:
            return []
        return [Alert(
            rule_code="LOW_SUCCESS_RATE", severity=CRITICAL,
            subject=f"Pipeline success rate {rate:.0f}% over the last {window_hours}h",
            body=f"{row['ok']} of {total} jobs succeeded in the last {window_hours} "
                 f"hours, below the {minimum:.0f}% threshold.",
            dedupe_key=f"success_rate:{int(rate // 10)}",
            entity_type="pipeline", entity_id="global")]

    def _sla_breaches(self) -> list[Alert]:
        rows = self.db.query(
            "SELECT j.job_id, j.duration_ms, m.model_key, m.project_code, p.sla_minutes "
            "FROM jobs j JOIN model_revisions r ON r.revision_id = j.revision_id "
            "JOIN models m ON m.model_id = r.model_id "
            "JOIN projects p ON p.project_code = m.project_code "
            "WHERE j.sla_breached = 1 AND j.finished_at IS NOT NULL")
        return [Alert(
            rule_code="SLA_BREACH", severity=WARNING,
            subject=f"{r['project_code']}/{r['model_key']} missed its "
                    f"{r['sla_minutes']}-minute delivery SLA",
            body=f"Job {r['job_id']} finished outside the SLA window "
                 f"({(r['duration_ms'] or 0) / 1000.0:.1f}s of processing).",
            dedupe_key=f"sla:{r['job_id']}",
            entity_type="job", entity_id=str(r["job_id"])) for r in rows]

    def _qa_blocked(self) -> list[Alert]:
        rows = self.db.query(
            "SELECT j.job_id, m.project_code, m.model_key, r.revision_label, "
            "COUNT(f.finding_id) AS errors, "
            "GROUP_CONCAT(DISTINCT f.rule_code) AS codes "
            "FROM jobs j "
            "JOIN model_revisions r ON r.revision_id = j.revision_id "
            "JOIN models m ON m.model_id = r.model_id "
            "JOIN qa_findings f ON f.job_id = j.job_id AND f.severity = 'ERROR' "
            "WHERE j.error_class = 'QaGateFailed' "
            "GROUP BY j.job_id")
        return [Alert(
            rule_code="QA_GATE_BLOCKED", severity=WARNING,
            subject=f"{r['project_code']}/{r['model_key']} {r['revision_label']} "
                    f"blocked by quality gates",
            body=f"{r['errors']} blocking finding(s): {r['codes']}.\n"
                 f"The previous revision remains live; this one was not published. "
                 f"The model owner needs to correct the source export.",
            dedupe_key=f"qa:{r['job_id']}",
            entity_type="job", entity_id=str(r["job_id"])) for r in rows]

    def _slow_stages(self) -> list[Alert]:
        threshold_s = float(self.rules_cfg.get("stage_p95_seconds_threshold", 45))
        out = []
        for stage in stage_latency(self.db):
            if stage["executions"] < 3:
                continue
            p95_s = stage["p95_ms"] / 1000.0
            if p95_s > threshold_s:
                out.append(Alert(
                    rule_code="STAGE_LATENCY", severity=WARNING,
                    subject=f"Stage '{stage['stage']}' p95 is {p95_s:.1f}s",
                    body=f"p95 {p95_s:.1f}s over {stage['executions']} executions, "
                         f"above the {threshold_s:.0f}s threshold. This stage is the "
                         f"current constraint on throughput.",
                    dedupe_key=f"latency:{stage['stage']}:{int(p95_s // 10)}",
                    entity_type="stage", entity_id=stage["stage"]))
        return out

    def _stale_models(self) -> list[Alert]:
        max_hours = float(self.rules_cfg.get("stale_model_hours", 168))
        rows = self.db.query(
            "SELECT project_code, model_key, live_revision, age_hours "
            "FROM v_model_currency WHERE age_hours IS NOT NULL AND age_hours > ?",
            (max_hours,))
        return [Alert(
            rule_code="STALE_MODEL", severity=INFO,
            subject=f"{r['project_code']}/{r['model_key']} has not been refreshed "
                    f"in {r['age_hours']:.0f}h",
            body=f"Live revision {r['live_revision']} is "
                 f"{r['age_hours'] / 24.0:.1f} days old. Users may be working "
                 f"against stale geometry.",
            dedupe_key=f"stale:{r['project_code']}:{r['model_key']}",
            entity_type="model", entity_id=r["model_key"]) for r in rows]

    def _quarantine_backlog(self) -> list[Alert]:
        rows = self.db.query(
            "SELECT m.project_code, COUNT(*) AS n FROM model_revisions r "
            "JOIN models m ON m.model_id = r.model_id "
            "WHERE r.status = 'QUARANTINED' GROUP BY m.project_code")
        return [Alert(
            rule_code="QUARANTINE_BACKLOG", severity=WARNING,
            subject=f"{r['project_code']}: {r['n']} model(s) awaiting triage in quarantine",
            body=f"{r['n']} source export(s) could not be processed and are waiting "
                 f"for someone to correct and re-drop them. Each has a .failure.json "
                 f"sidecar explaining the cause.",
            dedupe_key=f"quarantine:{r['project_code']}:{r['n']}",
            entity_type="project", entity_id=r["project_code"]) for r in rows]

    # -- dispatch -------------------------------------------------------
    def dispatch(self, alerts: list[Alert]) -> dict:
        cooldown = int(self.config.get("alerting.cooldown_minutes", 15))
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=cooldown)
                  ).strftime("%Y-%m-%d %H:%M:%S")
        sink_path = self.config.root / self.config.get(
            "alerting.sink", "data/logs/alert_webhook.jsonl")
        sink_path.parent.mkdir(parents=True, exist_ok=True)

        raised, suppressed = 0, 0
        with open(sink_path, "a", encoding="utf-8") as sink:
            for alert in alerts:
                recent = self.db.query_one(
                    "SELECT alert_id FROM alerts WHERE dedupe_key = ? AND created_at >= ?",
                    (alert.dedupe_key, cutoff))
                if recent is not None:
                    suppressed += 1
                    continue

                alert_id = self.db.execute(
                    "INSERT INTO alerts (rule_code, severity, subject, body, entity_type, "
                    "entity_id, dedupe_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (alert.rule_code, alert.severity, alert.subject, alert.body,
                     alert.entity_type, alert.entity_id, alert.dedupe_key, utcnow()))
                sink.write(json.dumps({
                    "alert_id": alert_id, "ts": utcnow(), "rule": alert.rule_code,
                    "severity": alert.severity, "subject": alert.subject,
                    "body": alert.body, "entity": f"{alert.entity_type}:{alert.entity_id}",
                }) + "\n")
                raised += 1

        return {"evaluated": len(alerts), "raised": raised, "suppressed": suppressed,
                "sink": str(sink_path)}

    def run(self) -> dict:
        return self.dispatch(self.evaluate())
