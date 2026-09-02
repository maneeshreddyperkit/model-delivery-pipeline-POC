"""Command line interface.

    python -m modelops <command>

Run `python -m modelops demo` for the full scripted run.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import load_config
from .db import Database, utcnow
from .monitoring import AlertEngine, overview, stage_latency, failure_taxonomy, qa_summary
from .orchestrator import IntakeScanner, Orchestrator, replay, rollback
from .synth import scenario

BAR = "=" * 78
RULE = "-" * 78


def _fmt_bytes(n: float | int | None) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024.0:
            return f"{n:,.1f} {unit}" if unit != "B" else f"{n:,.0f} B"
        n /= 1024.0
    return f"{n:,.1f} TB"


def _open(args) -> tuple:
    config = load_config(getattr(args, "config", None))
    config.ensure_directories()
    return config, Database(config.path("catalog_db"))


# =====================================================================
# Commands
# =====================================================================

def cmd_init(args) -> int:
    config, db = _open(args)
    schema = config.root / "sql" / "schema_sqlite.sql"

    if args.force:
        db.reset(schema)
        print("Catalog reset.")
    else:
        db.initialise(schema)
        print("Catalog ready.")

    for project in scenario.PROJECTS:
        existing = db.query_one("SELECT project_code FROM projects WHERE project_code = ?",
                                (project["project_code"],))
        if existing:
            continue
        db.execute(
            "INSERT INTO projects (project_code, project_name, district, delivery_root, "
            "sla_minutes, subscriber_count, is_active, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
            (project["project_code"], project["project_name"], project["district"],
             str(config.path("published") / project["project_code"]),
             project["sla_minutes"], project["subscriber_count"], utcnow()))
        print(f"  registered project {project['project_code']} "
              f"({project['subscriber_count']:,} subscribers)")

    print(f"Database: {config.path('catalog_db')}")
    return 0


def cmd_generate(args) -> int:
    config, db = _open(args)
    inbox = config.path("inbox")
    print(f"Generating synthetic engineering models into {inbox}\n")

    rows = scenario.generate_dataset(inbox, seed=args.seed)
    total = sum(r["bytes"] for r in rows)

    print(f"{'FILE':58s} {'SIZE':>10s}  DEFECT")
    print(RULE)
    for r in rows:
        print(f"{r['file'][:58]:58s} {_fmt_bytes(r['bytes']):>10s}  "
              f"{'-' if r['defect'] == 'none' else r['defect']}")
    print(RULE)
    print(f"{len(rows)} files, {_fmt_bytes(total)} total\n")
    print("Deliberate defects are included so failure handling is demonstrable;")
    print("see docs/DEMO_SCRIPT.md for what each one proves.")
    return 0


def cmd_scan(args) -> int:
    config, db = _open(args)
    result = IntakeScanner(config, db).scan()
    print(f"Scanned {result['seen']} file(s): {result.get('queued', 0)} queued, "
          f"{result.get('duplicates', 0)} duplicate(s) skipped, "
          f"{result.get('unsupported', 0)} unsupported.")
    return 0


def cmd_run(args) -> int:
    config, db = _open(args)
    pending = db.scalar("SELECT COUNT(*) FROM jobs WHERE status IN ('QUEUED','RETRYING')",
                        default=0)
    if not pending:
        print("No queued work. Run `scan` first.")
        return 0

    workers = config.get("orchestrator.worker_count", 4)
    print(f"Draining {pending} job(s) across {workers} worker(s)...\n")
    started = time.perf_counter()
    counts = Orchestrator(config, db, verbose=not args.quiet).run_until_idle(
        max_seconds=args.max_seconds)
    elapsed = time.perf_counter() - started

    print(f"\nFinished in {elapsed:.1f}s: " +
          ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))

    engine = AlertEngine(config, db)
    result = engine.run()
    print(f"Alerting: {result['raised']} raised, {result['suppressed']} suppressed "
          f"(cooldown).")
    return 0


def cmd_watch(args) -> int:
    config, db = _open(args)
    Orchestrator(config, db).watch()
    return 0


def cmd_status(args) -> int:
    config, db = _open(args)
    stats = overview(db)

    print(BAR)
    print("MODEL DELIVERY PIPELINE - STATUS")
    print(BAR)
    print(f"  Jobs            {stats['jobs_total']:>8,}  "
          f"({stats['jobs_succeeded']:,} ok / {stats['jobs_failed']:,} failed / "
          f"{stats['jobs_quarantined']:,} quarantined / {stats['jobs_pending']:,} pending)")
    print(f"  Success rate    {stats['success_rate_pct']:>7.1f}%  "
          f"[{stats['jobs_retried']:,} job(s) needed a retry]")
    print(f"  Duration        {stats['avg_duration_s']:>7.2f}s avg, "
          f"{stats['p95_duration_s']:.2f}s p95")
    print(f"  SLA breaches    {stats['sla_breaches']:>8,}")
    print()
    print(f"  Models live     {stats['models_live']:>8,}  "
          f"serving {stats['subscribers']:,} subscribers")
    print(f"  Components      {stats['components_live']:>8,}")
    print(f"  Triangles       {stats['triangles_live']:>8,}")
    print()
    print("  Delivery payload (measured, like-for-like on the same models)")
    print(f"    Naive conversion   {_fmt_bytes(stats['naive_bytes']):>10s}   "
          f"no instancing, no optimisation")
    print(f"    Delivered on disk  {_fmt_bytes(stats['published_bytes']):>10s}   "
          f"{stats['payload_reduction_pct']:.1f}% smaller")
    print(f"    Transferred (gzip) {_fmt_bytes(stats['wire_bytes']):>10s}   "
          f"{stats['wire_reduction_pct']:.1f}% smaller")
    print(f"    Source containers  {_fmt_bytes(stats['source_bytes']):>10s}   "
          f"(compressed archives, not comparable)")
    print()
    print(f"  Open alerts     {stats['open_alerts']:>8,}  "
          f"({stats['critical_alerts']} critical)")
    print(f"  In quarantine   {stats['quarantined_files']:>8,}")
    print(BAR)
    return 0


def cmd_report(args) -> int:
    config, db = _open(args)
    cmd_status(args)

    print("\nSTAGE LATENCY")
    print(RULE)
    print(f"{'STAGE':12s} {'RUNS':>6s} {'FAIL':>5s} {'AVG ms':>8s} "
          f"{'P50 ms':>8s} {'P95 ms':>8s} {'MAX ms':>8s}")
    for s in stage_latency(db):
        print(f"{s['stage']:12s} {s['executions']:>6,} {s['failures']:>5,} "
              f"{s['avg_ms']:>8,} {s['p50_ms']:>8,} {s['p95_ms']:>8,} {s['max_ms']:>8,}")

    print("\nFAILURE TAXONOMY")
    print(RULE)
    taxonomy = failure_taxonomy(db)
    if not taxonomy:
        print("  (no failures)")
    for f in taxonomy:
        print(f"  {f['error_class']:24s} {f['error_kind']:10s} "
              f"{f['occurrences']:>3,} occurrence(s)")

    print("\nQUALITY FINDINGS")
    print(RULE)
    findings = qa_summary(db)
    if not findings:
        print("  (none)")
    for f in findings:
        print(f"  [{f['severity']:5s}] {f['rule_code']:22s} "
              f"{f['finding_count']:>4,} finding(s) across "
              f"{f['revisions_affected']} revision(s)")

    print("\nLIVE MODELS")
    print(RULE)
    print(f"{'PROJECT':10s} {'MODEL':16s} {'REV':6s} {'COMPONENTS':>11s} "
          f"{'TRIANGLES':>10s} {'NAIVE':>10s} {'DELIVERED':>10s} {'WIRE':>10s} {'SAVED':>7s}")
    rows = db.query(
        "SELECT * FROM v_model_currency WHERE live_revision IS NOT NULL "
        "ORDER BY project_code, model_key")
    for r in rows:
        print(f"{r['project_code']:10s} {r['model_key'][:16]:16s} "
              f"{(r['live_revision'] or '-'):6s} {r['component_count'] or 0:>11,} "
              f"{r['triangle_count'] or 0:>10,} "
              f"{_fmt_bytes(r['naive_bytes']):>10s} "
              f"{_fmt_bytes(r['published_bytes']):>10s} "
              f"{_fmt_bytes(r['wire_bytes']):>10s} "
              f"{(r['size_reduction_pct'] or 0):>6.1f}%")

    unpublished = db.query(
        "SELECT m.project_code, m.model_key, r.revision_label, r.status, "
        "j.error_class, j.error_message FROM model_revisions r "
        "JOIN models m ON m.model_id = r.model_id "
        "LEFT JOIN jobs j ON j.revision_id = r.revision_id "
        "WHERE r.status IN ('FAILED','QUARANTINED') GROUP BY r.revision_id "
        "ORDER BY m.project_code")
    if unpublished:
        print("\nNOT DELIVERED")
        print(RULE)
        for r in unpublished:
            print(f"  {r['project_code']}/{r['model_key']} {r['revision_label']} "
                  f"[{r['status']}] {r['error_class']}")
            print(f"      {(r['error_message'] or '')[:150]}")

    print("\nOPEN ALERTS")
    print(RULE)
    alerts = db.query(
        "SELECT severity, rule_code, subject FROM alerts WHERE resolved_at IS NULL "
        "ORDER BY CASE severity WHEN 'CRITICAL' THEN 0 WHEN 'WARNING' THEN 1 ELSE 2 END, "
        "created_at DESC")
    if not alerts:
        print("  (none)")
    for a in alerts:
        print(f"  [{a['severity']:8s}] {a['rule_code']:22s} {a['subject']}")
    print()
    return 0


def cmd_alerts(args) -> int:
    config, db = _open(args)
    result = AlertEngine(config, db).run()
    print(f"Evaluated {result['evaluated']} condition(s): {result['raised']} raised, "
          f"{result['suppressed']} suppressed by cooldown.")
    print(f"Webhook sink: {result['sink']}")
    for a in db.query("SELECT severity, rule_code, subject, created_at FROM alerts "
                      "ORDER BY alert_id DESC LIMIT 20"):
        print(f"  {a['created_at']}  [{a['severity']:8s}] {a['rule_code']:22s} "
              f"{a['subject']}")
    return 0


def cmd_replay(args) -> int:
    config, db = _open(args)
    try:
        new_id = replay(config, db, args.job)
    except (ValueError, FileNotFoundError) as exc:
        print(f"Cannot replay job {args.job}: {exc}")
        return 1
    print(f"Queued job {new_id} as a replay of job {args.job}.")
    if args.run:
        Orchestrator(config, db).run_until_idle(max_seconds=args.max_seconds)
    return 0


def cmd_rollback(args) -> int:
    config, db = _open(args)
    try:
        result = rollback(config, db, args.project, args.model)
    except (ValueError, FileNotFoundError) as exc:
        print(f"Rollback failed: {exc}")
        return 1
    print(f"Rolled {result['project_code']}/{result['model_key']} back from "
          f"{result['from_revision']} to {result['to_revision']}.")
    print(f"Live delivery path is now {result['delivery_path']}")
    return 0


def cmd_adapters(args) -> int:
    from .adapters import registered
    print(f"{'EXTENSION':12s} {'STATUS':14s} FORMAT / EXTERNAL TOOL")
    print(RULE)
    for ext, adapter in sorted(registered().items()):
        status = "implemented" if adapter.implemented else "not configured"
        detail = adapter.name if adapter.implemented else f"{adapter.name} - requires {adapter.external_tool}"
        print(f"{ext:12s} {status:14s} {detail}")
    return 0


def cmd_simulate_usage(args) -> int:
    """Populate viewer-consumption stats so the dashboard shows who is served."""
    config, db = _open(args)
    rng = random.Random(args.seed)
    models = db.query(
        "SELECT m.model_id, m.project_code, p.subscriber_count FROM models m "
        "JOIN projects p ON p.project_code = m.project_code "
        "WHERE m.model_id IN (SELECT r.model_id FROM model_revisions r "
        "JOIN publications pub ON pub.revision_id = r.revision_id AND pub.is_current = 1)")
    if not models:
        print("No published models yet.")
        return 0

    db.execute("DELETE FROM delivery_stats")
    rows = []
    today = datetime.now(timezone.utc).date()
    for day_offset in range(args.days):
        day = (today - timedelta(days=day_offset)).isoformat()
        weekday = (today - timedelta(days=day_offset)).weekday()
        activity = 0.25 if weekday >= 5 else 1.0
        for m in models:
            share = rng.uniform(0.05, 0.22)
            users = int((m["subscriber_count"] or 0) * share * activity)
            sessions = int(users * rng.uniform(1.1, 2.4))
            rows.append((day, m["project_code"], m["model_id"], sessions, users,
                         sessions * rng.randint(200_000, 900_000)))
    db.executemany(
        "INSERT OR REPLACE INTO delivery_stats (stat_date, project_code, model_id, "
        "viewer_sessions, unique_users, bytes_served) VALUES (?, ?, ?, ?, ?, ?)", rows)
    print(f"Simulated {args.days} days of viewer consumption across {len(models)} models "
          f"({len(rows):,} rows).")
    return 0


def cmd_serve(args) -> int:
    from .web.app import create_app
    config, db = _open(args)
    app = create_app(config, db)
    url = f"http://127.0.0.1:{args.port}"
    print(BAR)
    print(f"  Operations dashboard : {url}")
    print(f"  Catalog              : {config.path('catalog_db')}")
    print(f"  Delivery root        : {config.path('published')}")
    print(BAR)
    print("  Bound to 127.0.0.1 (this machine only). Ctrl+C to stop.\n")
    app.run(host="127.0.0.1", port=args.port, debug=False, threaded=True)
    return 0


def cmd_demo(args) -> int:
    """Full scripted run: build the catalog, generate models, process, report."""
    steps = [
        ("Initialising catalog", lambda a: cmd_init(a)),
        ("Generating engineering models", lambda a: cmd_generate(a)),
        ("Scanning inbox", lambda a: cmd_scan(a)),
        ("Processing delivery queue", lambda a: cmd_run(a)),
        ("Simulating viewer consumption", lambda a: cmd_simulate_usage(a)),
    ]
    args.force = True
    args.quiet = False

    for i, (title, fn) in enumerate(steps, start=1):
        print(f"\n{BAR}\n  STEP {i}/{len(steps)}  {title}\n{BAR}")
        rc = fn(args)
        if rc:
            return rc

    print(f"\n{BAR}\n  RESULTS\n{BAR}")
    cmd_report(args)

    print(BAR)
    print("  Next:  python -m modelops serve       (dashboard + 3D viewer)")
    print("         python -m modelops replay --job <id> --run")
    print("         python -m modelops rollback --project KNS-TB2 --model TB2-PIPE-U20")
    print(BAR)
    return 0


# =====================================================================
# Parser
# =====================================================================

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="modelops",
        description="Local engineering-model processing and delivery pipeline (POC).")
    p.add_argument("--config", help="Path to pipeline.json")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("init", help="Create the catalog and register projects")
    sp.add_argument("--force", action="store_true", help="Drop and rebuild the catalog")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("generate", help="Write synthetic source models into the inbox")
    sp.add_argument("--seed", type=int, default=20260830)
    sp.set_defaults(func=cmd_generate)

    sp = sub.add_parser("scan", help="Register new inbox files as delivery jobs")
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("run", help="Process the queue until idle")
    sp.add_argument("--max-seconds", type=float, default=600.0)
    sp.add_argument("--quiet", action="store_true")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("watch", help="Continuously scan and process")
    sp.set_defaults(func=cmd_watch)

    sp = sub.add_parser("status", help="Headline pipeline status")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("report", help="Full operational report")
    sp.set_defaults(func=cmd_report)

    sp = sub.add_parser("alerts", help="Evaluate alert rules and show alerts")
    sp.set_defaults(func=cmd_alerts)

    sp = sub.add_parser("adapters", help="List registered source-format adapters")
    sp.set_defaults(func=cmd_adapters)

    sp = sub.add_parser("replay", help="Re-queue a failed job")
    sp.add_argument("--job", type=int, required=True)
    sp.add_argument("--run", action="store_true", help="Process it immediately")
    sp.add_argument("--max-seconds", type=float, default=300.0)
    sp.set_defaults(func=cmd_replay)

    sp = sub.add_parser("rollback", help="Revert a model to its previous revision")
    sp.add_argument("--project", required=True)
    sp.add_argument("--model", required=True)
    sp.set_defaults(func=cmd_rollback)

    sp = sub.add_parser("simulate-usage", help="Generate viewer consumption statistics")
    sp.add_argument("--days", type=int, default=14)
    sp.add_argument("--seed", type=int, default=7)
    sp.set_defaults(func=cmd_simulate_usage)

    sp = sub.add_parser("serve", help="Run the operations dashboard and 3D viewer")
    sp.add_argument("--port", type=int, default=8765)
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("demo", help="Run the entire scripted demonstration")
    sp.add_argument("--seed", type=int, default=20260830)
    sp.add_argument("--max-seconds", type=float, default=600.0)
    sp.add_argument("--days", type=int, default=14)
    sp.set_defaults(func=cmd_demo)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
