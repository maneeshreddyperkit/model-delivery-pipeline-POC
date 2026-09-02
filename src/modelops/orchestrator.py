"""Intake scanning and job execution.

The two halves of "run the pipeline unattended":

  IntakeScanner  watches the drop location, decides what is new, and
                 registers work. Its main job is *not* creating work
                 that already exists.

  Orchestrator   runs queued jobs across a worker pool and decides what
                 happens when one fails.

Failure policy is the part worth reading. A transient failure is retried
with exponential backoff up to a ceiling. A permanent failure is not
retried at all - it is quarantined immediately, because re-running a
converter over a truncated file just burns the queue and delays every
model behind it. Either way the source is moved out of the drop location
with a sidecar explaining why, so the inbox reflects only outstanding
work and nothing needs a human to go hunting through logs.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .adapters import adapter_for
from .config import Config
from .db import Database, utcnow
from .errors import PERMANENT, TRANSIENT, PipelineError, classify
from .stages import JobContext, StageRunner, build_pipeline


def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _parse_filename(path: Path) -> tuple[str, str, str]:
    """Fallback routing from the delivery-share naming convention.

    Used when the manifest cannot be read - which is exactly the case
    for a corrupt drop, and precisely when you still want the failure
    attributed to a project instead of vanishing.
    """
    parts = path.stem.split("_")
    project = parts[0] if parts else "UNKNOWN"
    model_key = parts[1] if len(parts) > 1 else path.stem
    revision = parts[2] if len(parts) > 2 and parts[2] else "UNKNOWN"
    return project, model_key, revision


# =====================================================================
# Intake
# =====================================================================

class IntakeScanner:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db

    def scan(self) -> dict:
        inbox = self.config.path("inbox")
        inbox.mkdir(parents=True, exist_ok=True)

        summary = {"seen": 0, "queued": 0, "duplicates": 0, "unsupported": 0}

        for path in sorted(inbox.rglob("*")):
            if not path.is_file() or path.name.startswith("."):
                continue
            summary["seen"] += 1
            try:
                outcome = self.register(path)
            except Exception as exc:
                summary["unsupported"] += 1
                self.db.audit("intake.rejected", entity_type="file",
                              entity_id=path.name, detail=str(exc))
                continue
            summary[outcome] = summary.get(outcome, 0) + 1

        return summary

    def register(self, path: Path) -> str:
        adapter = adapter_for(path)          # raises for unknown extensions
        manifest = adapter.peek(path) if adapter.implemented else {}

        fallback = _parse_filename(path)
        project_code = manifest.get("project_code") or fallback[0]
        model_key = manifest.get("model_key") or fallback[1]
        revision_label = manifest.get("revision_label") or fallback[2]
        model_name = manifest.get("model_name") or model_key
        discipline = manifest.get("discipline")

        digest = sha256_file(path)
        size = path.stat().st_size

        project = self.db.query_one(
            "SELECT project_code FROM projects WHERE project_code = ?", (project_code,))
        if project is None:
            # Unknown projects are auto-registered rather than dropped, so a
            # new project's first delivery is visible instead of silently
            # discarded; onboarding then means setting its SLA, not finding
            # out models were never processed.
            self.db.execute(
                "INSERT INTO projects (project_code, project_name, district, "
                "delivery_root, sla_minutes, subscriber_count, is_active, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
                (project_code, f"{project_code} (auto-registered)", "Unassigned",
                 str(self.config.path("published") / project_code), 30, 0, utcnow()),
            )
            self.db.audit("project.auto_registered", entity_type="project",
                          entity_id=project_code)

        model = self.db.query_one(
            "SELECT model_id FROM models WHERE project_code = ? AND model_key = ?",
            (project_code, model_key))
        if model is None:
            model_id = self.db.execute(
                "INSERT INTO models (project_code, model_key, model_name, discipline, "
                "source_tool, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (project_code, model_key, model_name, discipline,
                 manifest.get("source_tool"), utcnow()),
            )
        else:
            model_id = model["model_id"]

        # Content-hash idempotency: the same export re-sent is not new work.
        existing = self.db.query_one(
            "SELECT revision_id, status FROM model_revisions "
            "WHERE model_id = ? AND source_sha256 = ?", (model_id, digest))
        if existing is not None:
            self.db.audit("intake.duplicate_skipped", entity_type="revision",
                          entity_id=existing["revision_id"],
                          detail={"file": path.name, "sha256": digest[:16]})
            return "duplicates"

        revision_id = self.db.execute(
            "INSERT INTO model_revisions (model_id, revision_label, source_filename, "
            "source_path, source_sha256, source_bytes, source_tool, unit_system, "
            "coordinate_system, exported_at, received_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'RECEIVED')",
            (model_id, revision_label, path.name, str(path), digest, size,
             manifest.get("source_tool"), manifest.get("unit_system"),
             manifest.get("coordinate_system"), manifest.get("exported_at"), utcnow()),
        )

        sla_minutes = self.db.scalar(
            "SELECT sla_minutes FROM projects WHERE project_code = ?",
            (project_code,), default=30)
        deadline = (datetime.now(timezone.utc) + timedelta(minutes=sla_minutes)
                    ).strftime("%Y-%m-%d %H:%M:%S")

        self.db.execute(
            "INSERT INTO jobs (revision_id, pipeline_version, trigger, priority, "
            "status, attempt, max_attempts, queued_at, sla_deadline) "
            "VALUES (?, ?, 'AUTO', ?, 'QUEUED', 0, ?, ?, ?)",
            (revision_id, self.config.pipeline_version,
             100, int(self.config.get("orchestrator.max_attempts", 3)),
             utcnow(), deadline),
        )
        return "queued"


# =====================================================================
# Execution
# =====================================================================

class Orchestrator:
    def __init__(self, config: Config, db: Database, *, verbose: bool = True):
        self.config = config
        self.db = db
        self.verbose = verbose
        self._claim_lock = threading.Lock()
        self._stop = threading.Event()

    # -- job claiming ------------------------------------------------------
    def claim(self, worker: str) -> dict | None:
        """Atomically take the next runnable job.

        The lock plus an immediate transaction is what stops two workers
        picking up the same model and racing each other into the delivery
        directory.
        """
        now = utcnow()
        with self._claim_lock, self.db.tx() as conn:
            row = conn.execute(
                "SELECT job_id, revision_id, attempt, max_attempts FROM jobs "
                "WHERE status IN ('QUEUED', 'RETRYING') "
                "  AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
                "ORDER BY priority ASC, job_id ASC LIMIT 1", (now,)
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE jobs SET status = 'RUNNING', worker = ?, started_at = ?, "
                "attempt = attempt + 1 WHERE job_id = ?",
                (worker, now, row["job_id"]),
            )
            return {"job_id": row["job_id"], "revision_id": row["revision_id"],
                    "attempt": row["attempt"] + 1,
                    "max_attempts": row["max_attempts"]}

    # -- single job --------------------------------------------------------
    def execute(self, claim: dict, worker: str) -> str:
        job_id = claim["job_id"]
        revision_id = claim["revision_id"]
        attempt = claim["attempt"]

        info = self.db.query_one(
            "SELECT r.source_path, r.revision_label, m.model_key, m.project_code "
            "FROM model_revisions r JOIN models m ON m.model_id = r.model_id "
            "WHERE r.revision_id = ?", (revision_id,))

        work_dir = self.config.path("work") / f"job_{job_id}_attempt_{attempt}"
        work_dir.mkdir(parents=True, exist_ok=True)

        ctx = JobContext(
            job_id=job_id, revision_id=revision_id, attempt=attempt,
            config=self.config, db=self.db,
            source_path=Path(info["source_path"]), work_dir=work_dir,
            project_code=info["project_code"], model_key=info["model_key"],
            revision_label=info["revision_label"],
        )
        ctx.log(f"Attempt {attempt} started on worker {worker}", stage="orchestrator")

        started = time.perf_counter()
        runner = StageRunner(build_pipeline(), self.db)

        try:
            runner.run(ctx, on_stage=lambda s: self._trace(f"  job {job_id}: {s}"))
        except Exception as exc:
            duration = int((time.perf_counter() - started) * 1000)
            return self._handle_failure(ctx, claim, exc, duration, work_dir)

        duration = int((time.perf_counter() - started) * 1000)
        self._finish_success(ctx, duration)
        shutil.rmtree(work_dir, ignore_errors=True)
        return "SUCCEEDED"

    # -- outcomes ----------------------------------------------------------
    def _finish_success(self, ctx: JobContext, duration_ms: int) -> None:
        finished = utcnow()
        deadline = self.db.scalar(
            "SELECT sla_deadline FROM jobs WHERE job_id = ?", (ctx.job_id,))
        breached = 1 if (deadline and finished > deadline) else 0

        self.db.execute(
            "UPDATE jobs SET status = 'SUCCEEDED', finished_at = ?, duration_ms = ?, "
            "sla_breached = ?, error_class = NULL, error_kind = NULL, "
            "error_message = NULL, next_attempt_at = NULL WHERE job_id = ?",
            (finished, duration_ms, breached, ctx.job_id),
        )
        ctx.log(f"Job succeeded in {duration_ms} ms"
                + (" (SLA BREACHED)" if breached else ""), stage="orchestrator")
        self._trace(f"  job {ctx.job_id}: SUCCEEDED in {duration_ms} ms")

    def _handle_failure(self, ctx: JobContext, claim: dict, exc: BaseException,
                        duration_ms: int, work_dir: Path) -> str:
        error_class, error_kind, message = classify(exc)
        attempt, max_attempts = claim["attempt"], claim["max_attempts"]
        finished = utcnow()

        detail = exc.detail if isinstance(exc, PipelineError) else {}
        ctx.log(f"Attempt {attempt} failed [{error_kind} / {error_class}]: {message}",
                level="ERROR", stage="orchestrator")
        if not isinstance(exc, PipelineError):
            ctx.log(traceback.format_exc()[-1800:], level="ERROR", stage="orchestrator")

        can_retry = error_kind == TRANSIENT and attempt < max_attempts
        if can_retry:
            base = float(self.config.get("orchestrator.backoff_base_seconds", 2))
            cap = float(self.config.get("orchestrator.backoff_max_seconds", 60))
            delay = min(cap, base * (2 ** (attempt - 1)))
            next_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)
                       ).strftime("%Y-%m-%d %H:%M:%S")
            self.db.execute(
                "UPDATE jobs SET status = 'RETRYING', finished_at = NULL, "
                "duration_ms = ?, error_class = ?, error_kind = ?, error_message = ?, "
                "next_attempt_at = ? WHERE job_id = ?",
                (duration_ms, error_class, error_kind, message[:2000], next_at, ctx.job_id),
            )
            ctx.log(f"Transient failure; retrying in {delay:.0f}s "
                    f"(attempt {attempt + 1} of {max_attempts})", stage="orchestrator")
            self._trace(f"  job {ctx.job_id}: RETRY in {delay:.0f}s ({error_class})")
            shutil.rmtree(work_dir, ignore_errors=True)
            return "RETRYING"

        # Terminal. Permanent failures never reached the retry branch at all.
        status = "QUARANTINED" if error_kind == PERMANENT else "FAILED"
        deadline = self.db.scalar(
            "SELECT sla_deadline FROM jobs WHERE job_id = ?", (ctx.job_id,))
        breached = 1 if (deadline and finished > deadline) else 0

        self.db.execute(
            "UPDATE jobs SET status = ?, finished_at = ?, duration_ms = ?, "
            "sla_breached = ?, error_class = ?, error_kind = ?, error_message = ?, "
            "next_attempt_at = NULL WHERE job_id = ?",
            (status, finished, duration_ms, breached, error_class, error_kind,
             message[:2000], ctx.job_id),
        )
        self.db.execute(
            "UPDATE model_revisions SET status = ? WHERE revision_id = ?",
            ("QUARANTINED" if status == "QUARANTINED" else "FAILED", ctx.revision_id),
        )
        self._quarantine_source(ctx, error_class, error_kind, message, detail)
        self._trace(f"  job {ctx.job_id}: {status} ({error_class})")
        shutil.rmtree(work_dir, ignore_errors=True)
        return status

    def _quarantine_source(self, ctx: JobContext, error_class: str, error_kind: str,
                           message: str, detail: dict) -> None:
        """Move the source out of the inbox with an explanation beside it."""
        quarantine = self.config.path("quarantine") / ctx.project_code
        quarantine.mkdir(parents=True, exist_ok=True)
        source = ctx.source_path

        target = quarantine / f"job{ctx.job_id}_{source.name}"
        try:
            if source.exists():
                os.replace(source, target)
                self.db.execute(
                    "UPDATE model_revisions SET source_path = ? WHERE revision_id = ?",
                    (str(target), ctx.revision_id),
                )
        except OSError as exc:
            ctx.log(f"Could not move source to quarantine: {exc}",
                    level="WARN", stage="orchestrator")
            target = source

        sidecar = target.with_suffix(target.suffix + ".failure.json")
        sidecar.write_text(json.dumps({
            "job_id": ctx.job_id,
            "revision_id": ctx.revision_id,
            "project_code": ctx.project_code,
            "model_key": ctx.model_key,
            "revision_label": ctx.revision_label,
            "error_class": error_class,
            "error_kind": error_kind,
            "message": message,
            "detail": detail,
            "quarantined_at": utcnow(),
            "replay_command": f"python -m modelops replay --job {ctx.job_id}",
        }, indent=2, default=str), encoding="utf-8")

        self.db.audit("model.quarantined", entity_type="revision",
                      entity_id=ctx.revision_id,
                      detail={"error_class": error_class, "path": str(target)})

    # -- drain / watch -----------------------------------------------------
    def run_until_idle(self, *, max_seconds: float = 300.0) -> dict:
        """Process everything runnable, including scheduled retries."""
        workers = int(self.config.get("orchestrator.worker_count", 4))
        counts: dict[str, int] = {}
        counts_lock = threading.Lock()
        deadline = time.monotonic() + max_seconds

        def loop(index: int) -> None:
            name = f"worker-{index}"
            while time.monotonic() < deadline and not self._stop.is_set():
                claim = self.claim(name)
                if claim is None:
                    # Nothing runnable now; there may still be a retry pending.
                    pending = self.db.scalar(
                        "SELECT COUNT(*) FROM jobs WHERE status IN ('QUEUED','RETRYING')",
                        default=0)
                    if not pending:
                        return
                    time.sleep(0.25)
                    continue
                try:
                    outcome = self.execute(claim, name)
                except Exception:
                    # A crash in failure handling itself must not kill the
                    # worker and strand the rest of the queue.
                    outcome = "WORKER_ERROR"
                    self.db.audit("worker.error", entity_type="job",
                                  entity_id=claim["job_id"],
                                  detail=traceback.format_exc()[-1500:])
                with counts_lock:
                    counts[outcome] = counts.get(outcome, 0) + 1

        threads = [threading.Thread(target=loop, args=(i,), name=f"worker-{i}",
                                    daemon=True) for i in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=max_seconds + 5)

        return counts

    def watch(self, interval: float | None = None) -> None:
        """Continuous mode: scan the inbox, drain the queue, repeat."""
        scanner = IntakeScanner(self.config, self.db)
        interval = interval or float(self.config.get("orchestrator.poll_interval_seconds", 2))
        self._trace(f"Watching {self.config.path('inbox')} (Ctrl+C to stop)")
        try:
            while not self._stop.is_set():
                found = scanner.scan()
                if found.get("queued"):
                    self._trace(f"Queued {found['queued']} new model(s)")
                self.run_until_idle(max_seconds=600)
                time.sleep(interval)
        except KeyboardInterrupt:
            self._trace("Stopped.")

    def stop(self) -> None:
        self._stop.set()

    def _trace(self, message: str) -> None:
        if self.verbose:
            print(message, flush=True)


# =====================================================================
# Operator actions
# =====================================================================

def replay(config: Config, db: Database, job_id: int) -> int:
    """Queue a fresh job for the same revision, keeping the original history."""
    row = db.query_one(
        "SELECT j.revision_id, r.source_path, m.project_code "
        "FROM jobs j JOIN model_revisions r ON r.revision_id = j.revision_id "
        "JOIN models m ON m.model_id = r.model_id WHERE j.job_id = ?", (job_id,))
    if row is None:
        raise ValueError(f"No such job: {job_id}")
    if not Path(row["source_path"]).exists():
        raise FileNotFoundError(
            f"Source for job {job_id} is no longer on disk: {row['source_path']}")

    sla = db.scalar("SELECT sla_minutes FROM projects WHERE project_code = ?",
                    (row["project_code"],), default=30)
    deadline = (datetime.now(timezone.utc) + timedelta(minutes=sla)
                ).strftime("%Y-%m-%d %H:%M:%S")

    new_id = db.execute(
        "INSERT INTO jobs (revision_id, pipeline_version, trigger, priority, status, "
        "attempt, max_attempts, queued_at, sla_deadline, replay_of_job_id) "
        "VALUES (?, ?, 'REPLAY', 50, 'QUEUED', 0, ?, ?, ?, ?)",
        (row["revision_id"], config.pipeline_version,
         int(config.get("orchestrator.max_attempts", 3)), utcnow(), deadline, job_id),
    )
    db.execute("UPDATE model_revisions SET status = 'RECEIVED' WHERE revision_id = ?",
               (row["revision_id"],))
    db.audit("job.replayed", entity_type="job", entity_id=new_id,
             detail={"replay_of": job_id})
    return new_id


def rollback(config: Config, db: Database, project_code: str, model_key: str) -> dict:
    """Point the live delivery back at the previous published revision.

    Because publication is a pointer swap over retained revision
    directories, this is a metadata operation plus one atomic file
    replace - no reprocessing, and it completes in milliseconds.
    """
    current = db.query_one(
        "SELECT p.publication_id, p.delivery_path, r.revision_id, r.revision_label "
        "FROM publications p "
        "JOIN model_revisions r ON r.revision_id = p.revision_id "
        "JOIN models m ON m.model_id = r.model_id "
        "WHERE m.project_code = ? AND m.model_key = ? AND p.is_current = 1",
        (project_code, model_key))
    if current is None:
        raise ValueError(f"{project_code}/{model_key} has no live publication.")

    previous = db.query_one(
        "SELECT p.publication_id, p.delivery_path, p.manifest_sha256, "
        "r.revision_id, r.revision_label "
        "FROM publications p "
        "JOIN model_revisions r ON r.revision_id = p.revision_id "
        "JOIN models m ON m.model_id = r.model_id "
        "WHERE m.project_code = ? AND m.model_key = ? AND p.is_current = 0 "
        "  AND p.rolled_back_at IS NULL AND p.publication_id <> ? "
        "ORDER BY p.published_at DESC LIMIT 1",
        (project_code, model_key, current["publication_id"]))
    if previous is None:
        raise ValueError(f"{project_code}/{model_key} has no earlier revision to roll back to.")

    target_dir = Path(previous["delivery_path"])
    if not target_dir.exists():
        raise FileNotFoundError(
            f"Previous revision directory was pruned by retention and cannot be "
            f"restored: {target_dir}")

    model_root = target_dir.parent
    pointer_path = model_root / "current.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8")) if pointer_path.exists() else {}
    pointer.update({
        "revision_label": previous["revision_label"],
        "revision_dir": target_dir.name,
        "published_at": utcnow(),
        "rolled_back_from": current["revision_label"],
        "manifest_sha256": previous["manifest_sha256"],
    })
    tmp = model_root / ".current.rollback.tmp"
    tmp.write_text(json.dumps(pointer, indent=2), encoding="utf-8")
    os.replace(tmp, pointer_path)

    db.execute("UPDATE publications SET is_current = 0, rolled_back_at = ? "
               "WHERE publication_id = ?", (utcnow(), current["publication_id"]))
    db.execute("UPDATE publications SET is_current = 1, superseded_at = NULL "
               "WHERE publication_id = ?", (previous["publication_id"],))
    db.execute("UPDATE model_revisions SET status = 'SUPERSEDED' WHERE revision_id = ?",
               (current["revision_id"],))
    db.execute("UPDATE model_revisions SET status = 'PUBLISHED' WHERE revision_id = ?",
               (previous["revision_id"],))
    db.audit("model.rolled_back", entity_type="model", entity_id=model_key,
             detail={"from": current["revision_label"], "to": previous["revision_label"]})

    return {"project_code": project_code, "model_key": model_key,
            "from_revision": current["revision_label"],
            "to_revision": previous["revision_label"],
            "delivery_path": str(target_dir)}
