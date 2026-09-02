"""Stage framework.

A job is an ordered list of stages sharing one `JobContext`. Each stage
returns a dict of metrics that is persisted to `job_steps.metrics_json`,
which is what makes the pipeline debuggable after the fact: for any run
you can see what each stage saw and how long it took, without re-running
anything.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..config import Config
from ..db import Database, utcnow
from ..errors import PipelineError


@dataclass
class JobContext:
    job_id: int
    revision_id: int
    attempt: int
    config: Config
    db: Database
    source_path: Path
    work_dir: Path
    project_code: str
    model_key: str
    revision_label: str

    # Carried between stages within one attempt.
    state: dict[str, Any] = field(default_factory=dict)

    def log(self, message: str, *, level: str = "INFO", stage: str | None = None) -> None:
        self.db.execute(
            "INSERT INTO job_log (job_id, ts, level, stage, message) VALUES (?, ?, ?, ?, ?)",
            (self.job_id, utcnow(), level, stage, message[:2000]),
        )

    def artifact_dir(self, name: str) -> Path:
        d = self.work_dir / name
        d.mkdir(parents=True, exist_ok=True)
        return d


class Stage:
    """One unit of pipeline work."""

    name: str = "stage"

    def run(self, ctx: JobContext) -> dict:
        raise NotImplementedError

    # -- fault injection ---------------------------------------------------
    def check_fault_injection(self, ctx: JobContext) -> None:
        """Honour the scenario's explicit fault-injection hook.

        Only fires for models generated with `_fault_injection` in their
        manifest. It exists so retry/backoff behaviour can be demonstrated
        on demand instead of waiting for a real transient failure.
        """
        source = ctx.state.get("source_model")
        fault = getattr(source, "fault_injection", None) or {}
        if fault.get("stage") != self.name:
            return
        if ctx.attempt > int(fault.get("fail_attempts", 1)):
            ctx.log(f"Fault injection satisfied after {ctx.attempt - 1} failed "
                    f"attempt(s); proceeding.", stage=self.name)
            return

        from .. import errors as err
        error_cls = getattr(err, fault.get("error", "ConverterUnavailable"),
                            err.ConverterUnavailable)
        raise error_cls(
            f"[fault injection] {self.name} stage failed on attempt {ctx.attempt} "
            f"as configured by the demo scenario.",
            detail={"injected": True, "stage": self.name, "attempt": ctx.attempt},
        )


class StageRunner:
    """Executes stages in order, recording timing and metrics for each."""

    def __init__(self, stages: list[Stage], db: Database):
        self.stages = stages
        self.db = db

    def run(self, ctx: JobContext, on_stage: Callable[[str], None] | None = None) -> dict:
        all_metrics: dict[str, dict] = {}

        for seq, stage in enumerate(self.stages, start=1):
            if on_stage:
                on_stage(stage.name)

            step_id = self.db.execute(
                "INSERT INTO job_steps (job_id, seq, stage, attempt, status, started_at) "
                "VALUES (?, ?, ?, ?, 'RUNNING', ?)",
                (ctx.job_id, seq, stage.name, ctx.attempt, utcnow()),
            )
            started = time.perf_counter()

            try:
                metrics = stage.run(ctx) or {}
            except Exception as exc:
                elapsed = int((time.perf_counter() - started) * 1000)
                from ..errors import classify
                error_class, _kind, message = classify(exc)
                self.db.execute(
                    "UPDATE job_steps SET status = 'FAILED', finished_at = ?, "
                    "duration_ms = ?, error_class = ?, error_message = ? WHERE step_id = ?",
                    (utcnow(), elapsed, error_class, message[:2000], step_id),
                )
                ctx.log(f"{stage.name} failed after {elapsed} ms: {message}",
                        level="ERROR", stage=stage.name)
                raise

            elapsed = int((time.perf_counter() - started) * 1000)
            self.db.execute(
                "UPDATE job_steps SET status = 'SUCCEEDED', finished_at = ?, "
                "duration_ms = ?, metrics_json = ? WHERE step_id = ?",
                (utcnow(), elapsed, json.dumps(metrics, default=str), step_id),
            )
            all_metrics[stage.name] = metrics

            summary = ", ".join(f"{k}={v}" for k, v in list(metrics.items())[:6])
            ctx.log(f"{stage.name} completed in {elapsed} ms ({summary})",
                    stage=stage.name)

        return all_metrics
