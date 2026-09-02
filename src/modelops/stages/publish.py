"""Stage 7 - publish.

Publication is the only stage users can see, so the failure mode that
matters is a half-written model: a viewer session that opens a directory
mid-copy and gets three of five files.

The approach here is stage-then-swap. Everything is written to a hidden
staging directory, verified, and only then moved into place; the live
revision is selected by a single pointer file replaced with
`os.replace`, which is atomic. A reader either sees the whole previous
revision or the whole new one, never a mixture. The same property makes
rollback a pointer rewrite rather than a restore.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..db import utcnow
from ..errors import PublishConflict, StorageUnavailable
from .base import JobContext, Stage

POINTER_NAME = "current.json"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class PublishStage(Stage):
    name = "publish"

    def run(self, ctx: JobContext) -> dict:
        self.check_fault_injection(ctx)

        source = ctx.state["source_model"]
        published_root = ctx.config.path("published")
        model_root = published_root / ctx.project_code / ctx.model_key
        try:
            model_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageUnavailable(
                f"Delivery root is not writable: {model_root} ({exc})",
                detail={"path": str(model_root)}) from exc

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        revision_dir_name = f"{ctx.revision_label}_{stamp}"
        staging = model_root / f".staging_{uuid.uuid4().hex[:8]}"
        staging.mkdir(parents=True)

        try:
            artifacts = self._stage_artifacts(ctx, source, staging)
            final_dir = model_root / revision_dir_name
            if final_dir.exists():
                shutil.rmtree(final_dir)
            os.replace(staging, final_dir)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        # --- verify what actually landed ---------------------------------
        published_bytes = 0
        for rel in artifacts["files"]:
            f = final_dir / rel
            if not f.exists():
                raise PublishConflict(
                    f"Artifact {rel} is missing from the delivery directory after "
                    f"the move; another writer may have touched it.",
                    detail={"path": str(final_dir)})
            published_bytes += f.stat().st_size

        # --- flip the live pointer (the atomic step) -----------------------
        pointer = {
            "project_code": ctx.project_code,
            "model_key": ctx.model_key,
            "revision_label": ctx.revision_label,
            "revision_dir": revision_dir_name,
            "published_at": utcnow(),
            "job_id": ctx.job_id,
            "lods": artifacts["lods"],
            "manifest_sha256": artifacts["manifest_sha256"],
        }
        pointer_path = model_root / POINTER_NAME
        tmp_pointer = model_root / f".{POINTER_NAME}.{uuid.uuid4().hex[:8]}.tmp"
        tmp_pointer.write_text(json.dumps(pointer, indent=2), encoding="utf-8")
        os.replace(tmp_pointer, pointer_path)

        # --- catalog bookkeeping --------------------------------------------
        publication_id = self._record_publication(
            ctx, final_dir, artifacts, published_bytes)

        retained, removed = self._apply_retention(ctx, model_root, revision_dir_name)
        cde_path = self._emit_cde_manifest(ctx, source, final_dir, artifacts,
                                           publication_id)

        ctx.log(f"Published {ctx.model_key} {ctx.revision_label} to {final_dir.name} "
                f"({published_bytes:,} bytes across {len(artifacts['files'])} files)",
                stage=self.name)

        naive_bytes = ctx.state.get("baseline_stats", {}).get("bytes", 0)
        wire_bytes = sum(r.get("wire_bytes", 0) for r in ctx.state["lod_reports"])
        return {
            "publication_id": publication_id,
            "delivery_path": str(final_dir),
            "files": len(artifacts["files"]),
            "published_bytes": published_bytes,
            "wire_bytes": wire_bytes,
            "naive_conversion_bytes": naive_bytes,
            "reduction_vs_naive_pct": round(
                100.0 * (naive_bytes - published_bytes) / naive_bytes, 1)
                if naive_bytes else 0.0,
            "source_container_bytes": ctx.source_path.stat().st_size,
            "lod_levels": len(artifacts["lods"]),
            "revisions_retained": retained,
            "revisions_pruned": removed,
            "cde_manifest": str(cde_path) if cde_path else None,
        }

    # -----------------------------------------------------------------
    def _stage_artifacts(self, ctx: JobContext, source, staging: Path) -> dict:
        files: list[str] = []
        lods: list[dict] = []

        for report in ctx.state["lod_reports"]:
            src = Path(ctx.state["lod_paths"][report["name"]])
            name = src.name
            shutil.copy2(src, staging / name)
            files.append(name)
            lods.append({
                "name": report["name"],
                "file": name,
                "bytes": (staging / name).stat().st_size,
                "rendered_triangles": report["rendered_triangles"],
                "unique_triangles": report["unique_triangles"],
            })

        # Self-contained tag index, so the published package is usable
        # without the catalog database (a real CDE handoff requirement).
        tag_rows = ctx.db.query(
            "SELECT tag, parent_tag, path, category, discipline, has_geometry, "
            "triangle_count FROM components WHERE revision_id = ? ORDER BY component_id",
            (ctx.revision_id,),
        )
        tags_doc = {
            "revision_id": ctx.revision_id,
            "component_count": len(tag_rows),
            "components": [dict(r) for r in tag_rows],
        }
        (staging / "tags.json").write_text(
            json.dumps(tags_doc, separators=(",", ":")), encoding="utf-8")
        files.append("tags.json")

        manifest = {
            "schema_version": "1.0",
            "project_code": ctx.project_code,
            "model_key": ctx.model_key,
            "model_name": source.manifest.get("model_name"),
            "revision_label": ctx.revision_label,
            "discipline": source.manifest.get("discipline"),
            "source_tool": source.manifest.get("source_tool"),
            "unit_system": source.manifest.get("unit_system"),
            "coordinate_system": source.manifest.get("coordinate_system"),
            "exported_at": source.manifest.get("exported_at"),
            "published_at": utcnow(),
            "pipeline_version": ctx.config.pipeline_version,
            "job_id": ctx.job_id,
            "revision_id": ctx.revision_id,
            "component_count": len(tag_rows),
            "lods": lods,
            "qa": {
                "findings": len(ctx.state.get("qa_findings", [])),
                "warnings": sum(1 for f in ctx.state.get("qa_findings", [])
                                if f["severity"] == "WARN"),
                "errors": sum(1 for f in ctx.state.get("qa_findings", [])
                              if f["severity"] == "ERROR"),
            },
            "files": files + ["manifest.json"],
        }
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        files.append("manifest.json")

        return {"files": files, "lods": lods,
                "manifest_sha256": sha256_file(manifest_path),
                "manifest": manifest}

    # -----------------------------------------------------------------
    def _record_publication(self, ctx: JobContext, final_dir: Path,
                            artifacts: dict, published_bytes: int) -> int:
        # Retire whatever was live for this model before recording the new one.
        ctx.db.execute(
            "UPDATE publications SET is_current = 0, superseded_at = ? "
            "WHERE is_current = 1 AND revision_id IN ("
            "  SELECT r.revision_id FROM model_revisions r"
            "  JOIN models m ON m.model_id = r.model_id"
            "  WHERE m.project_code = ? AND m.model_key = ?)",
            (utcnow(), ctx.project_code, ctx.model_key),
        )
        ctx.db.execute(
            "UPDATE model_revisions SET status = 'SUPERSEDED' "
            "WHERE status = 'PUBLISHED' AND model_id = ("
            "  SELECT model_id FROM model_revisions WHERE revision_id = ?)",
            (ctx.revision_id,),
        )

        lod0 = artifacts["lods"][0]
        publication_id = ctx.db.execute(
            "INSERT INTO publications (revision_id, job_id, delivery_path, "
            "manifest_sha256, published_at, published_bytes, wire_bytes, naive_bytes, "
            "source_bytes, triangle_count, component_count, lod_levels, is_current) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (ctx.revision_id, ctx.job_id, str(final_dir), artifacts["manifest_sha256"],
             utcnow(), published_bytes,
             sum(r.get("wire_bytes", 0) for r in ctx.state["lod_reports"]),
             ctx.state.get("baseline_stats", {}).get("bytes", 0),
             ctx.source_path.stat().st_size,
             lod0["rendered_triangles"], artifacts["manifest"]["component_count"],
             len(artifacts["lods"])),
        )
        ctx.db.execute(
            "UPDATE model_revisions SET status = 'PUBLISHED' WHERE revision_id = ?",
            (ctx.revision_id,),
        )
        ctx.db.audit("model.published", entity_type="revision", entity_id=ctx.revision_id,
                     detail={"publication_id": publication_id,
                             "path": str(final_dir), "bytes": published_bytes})
        return publication_id

    # -----------------------------------------------------------------
    def _apply_retention(self, ctx: JobContext, model_root: Path,
                         current_dir_name: str) -> tuple[int, int]:
        """Keep the configured number of revision directories on disk.

        Old revisions are what make rollback instant, so a few are kept
        rather than deleting on publish; without a cap the delivery share
        grows without limit.
        """
        keep = int(ctx.config.get("delivery.keep_revisions", 5))
        dirs = sorted(
            (d for d in model_root.iterdir() if d.is_dir() and not d.name.startswith(".")),
            key=lambda d: d.stat().st_mtime, reverse=True,
        )
        removed = 0
        for stale in dirs[keep:]:
            if stale.name == current_dir_name:
                continue
            shutil.rmtree(stale, ignore_errors=True)
            removed += 1
            ctx.log(f"Pruned superseded revision directory {stale.name}",
                    stage=self.name)
        return min(len(dirs), keep), removed

    # -----------------------------------------------------------------
    def _emit_cde_manifest(self, ctx: JobContext, source, final_dir: Path,
                           artifacts: dict, publication_id: int) -> Path | None:
        """Hand-off document for downstream systems.

        Written to an outbox directory rather than pushed anywhere. A real
        deployment would have a downstream consumer poll or subscribe to
        this; keeping it as a file keeps the integration boundary explicit
        and the POC entirely local.
        """
        if not ctx.config.get("delivery.emit_cde_manifest", True):
            return None

        outbox = ctx.config.path("outbox") / ctx.project_code
        outbox.mkdir(parents=True, exist_ok=True)

        systems = ctx.db.query(
            "SELECT value AS system, COUNT(*) AS component_count "
            "FROM component_attributes WHERE revision_id = ? AND name = 'CommissioningSystem' "
            "GROUP BY value ORDER BY component_count DESC",
            (ctx.revision_id,),
        )
        categories = ctx.db.query(
            "SELECT category, COUNT(*) AS component_count, SUM(triangle_count) AS triangles "
            "FROM components WHERE revision_id = ? GROUP BY category "
            "ORDER BY component_count DESC",
            (ctx.revision_id,),
        )

        doc = {
            "event": "model.published",
            "emitted_at": utcnow(),
            "publication_id": publication_id,
            "project_code": ctx.project_code,
            "model_key": ctx.model_key,
            "revision_label": ctx.revision_label,
            "delivery_path": str(final_dir),
            "manifest_sha256": artifacts["manifest_sha256"],
            "component_count": artifacts["manifest"]["component_count"],
            "lods": artifacts["lods"],
            "commissioning_systems": [dict(r) for r in systems],
            "category_breakdown": [dict(r) for r in categories],
        }
        path = outbox / f"{ctx.model_key}_{ctx.revision_label}_published.json"
        path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        return path
