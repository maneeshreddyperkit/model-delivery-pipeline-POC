"""Stage 6 - quality gates.

The model converted. The question this stage answers is whether the
result is fit to put in front of 3,000 people.

Two classes of check:

  fidelity  - did processing change the model? Bounding box and volume
              are compared between what arrived and what will ship. A
              silent scale error or a decimator that shredded a surface
              shows up here rather than in a user's screenshot.

  usability - is the delivered model actually usable? Geometry with no
              resolvable tag, ambiguous duplicate tags, or missing
              engineering attributes all render fine and are still a
              failed delivery, because the model cannot be searched,
              filtered or joined to anything.

Findings are persisted with severity. ERROR blocks publication; WARN is
recorded, surfaced on the dashboard, and shipped anyway.
"""

from __future__ import annotations

import json
import re

import numpy as np

from ..db import utcnow
from ..errors import QaGateFailed
from .. import geometry as g
from .. import gltf
from .base import JobContext, Stage


def _model_bounds(defs: dict[str, g.Mesh], instances) -> np.ndarray | None:
    """World-space bounds of all placed instances.

    Each definition's eight bbox corners are transformed rather than its
    full vertex set. For rigid placements this is exact for axis-aligned
    parts and conservative for rotated ones, and both sides of the
    comparison use the same method, so the drift figure is meaningful.
    """
    lo = np.full(3, np.inf)
    hi = np.full(3, -np.inf)
    found = False

    corner_cache: dict[str, np.ndarray] = {}
    for inst in instances:
        mesh = defs.get(inst.geometry_key)
        if mesh is None or mesh.vertex_count == 0:
            continue
        corners = corner_cache.get(inst.geometry_key)
        if corners is None:
            b = mesh.bounds()
            corners = np.array([[x, y, z] for x in b[:, 0] for y in b[:, 1] for z in b[:, 2]])
            corner_cache[inst.geometry_key] = corners
        matrix = np.asarray(inst.matrix, dtype=np.float64).reshape(4, 4)
        placed = (np.hstack([corners, np.ones((8, 1))]) @ matrix.T)[:, :3]
        lo = np.minimum(lo, placed.min(axis=0))
        hi = np.maximum(hi, placed.max(axis=0))
        found = True

    return np.vstack([lo, hi]) if found else None


def _total_volume(defs: dict[str, g.Mesh], instances) -> float:
    """Summed volume of placed instances (rigid transforms preserve volume)."""
    cache = {k: m.signed_volume() for k, m in defs.items()}
    return sum(cache.get(i.geometry_key, 0.0) for i in instances)


class QaStage(Stage):
    name = "qa"

    def run(self, ctx: JobContext) -> dict:
        self.check_fault_injection(ctx)

        gates = ctx.config["qa_gates"]
        source = ctx.state["source_model"]
        instances = ctx.state["instances"]
        original = ctx.state["geometry"]
        optimized = ctx.state["optimized_geometry"]
        findings: list[dict] = []

        def add(rule_code: str, severity: str, message: str,
                tag: str | None = None, detail: dict | None = None) -> None:
            findings.append({"rule_code": rule_code, "severity": severity,
                             "message": message, "component_tag": tag,
                             "detail": detail or {}})

        # ---- carry forward validation findings ---------------------------
        for w in ctx.state.get("validation_warnings", []):
            add(w["rule_code"], w["severity"], w["message"],
                detail={"sample": w.get("sample")})

        # ---- fidelity: bounding box --------------------------------------
        src_bounds = _model_bounds(original, instances)
        out_bounds = _model_bounds(optimized, instances)
        bbox_drift_pct = 0.0
        if src_bounds is not None and out_bounds is not None:
            diagonal = float(np.linalg.norm(src_bounds[1] - src_bounds[0])) or 1.0
            drift = float(np.abs(out_bounds - src_bounds).max())
            bbox_drift_pct = 100.0 * drift / diagonal
            if bbox_drift_pct > gates["bbox_drift_tolerance_pct"]:
                add("BBOX_DRIFT", "ERROR",
                    f"Delivered model bounding box moved {bbox_drift_pct:.2f}% of the "
                    f"model diagonal during processing, above the "
                    f"{gates['bbox_drift_tolerance_pct']}% tolerance. This usually "
                    f"means a unit or transform error.",
                    detail={"drift_pct": round(bbox_drift_pct, 3),
                            "source_bounds": src_bounds.tolist(),
                            "delivered_bounds": out_bounds.tolist()})

        # ---- fidelity: volume --------------------------------------------
        # The baseline is the source with its exact duplicate shells
        # removed. Dropping a shell that is byte-identical to one already
        # present is lossless by definition, so counting it as "volume
        # lost" would flag every federated model as broken and hide the
        # drift that actually matters.
        weld_tolerance = ctx.config.get("optimize.weld_tolerance", 1e-5)
        reference = {}
        for key, mesh in original.items():
            welded, _ = g.weld_vertices(mesh, weld_tolerance)
            reference[key], _ = g.drop_duplicate_faces(welded)

        src_volume = _total_volume(reference, instances)
        out_volume = _total_volume(optimized, instances)
        volume_drift_pct = (abs(out_volume - src_volume) / src_volume * 100.0
                            if src_volume else 0.0)
        if volume_drift_pct > gates["volume_drift_tolerance_pct"]:
            add("VOLUME_DRIFT", "ERROR",
                f"Delivered geometry volume differs from source by "
                f"{volume_drift_pct:.2f}%, above the "
                f"{gates['volume_drift_tolerance_pct']}% tolerance.",
                detail={"source_volume": src_volume, "delivered_volume": out_volume})

        # Each LOD is checked against full detail as well; a decimator that
        # shredded a surface shows up as volume loss at the coarse levels
        # long before anyone notices it visually.
        for name, defs in (ctx.state.get("lod_geometry") or {}).items():
            if name == "LOD0":
                continue
            lod_volume = _total_volume(defs, instances)
            lod_drift = (abs(lod_volume - out_volume) / out_volume * 100.0
                         if out_volume else 0.0)
            if lod_drift > gates.get("lod_volume_drift_tolerance_pct", 15.0):
                add("LOD_DISTORTION", "WARN",
                    f"{name} volume differs from full detail by {lod_drift:.1f}%, "
                    f"which suggests decimation is damaging shape rather than "
                    f"just reducing triangles.",
                    detail={"lod": name, "drift_pct": round(lod_drift, 2)})

        # ---- fidelity: nothing vanished ------------------------------------
        empty_defs = [k for k, m in optimized.items() if m.triangle_count == 0]
        if empty_defs:
            add("GEOMETRY_LOST", "ERROR",
                f"{len(empty_defs)} geometry definition(s) ended up empty after "
                f"optimisation; every instance of them would be invisible.",
                detail={"definitions": empty_defs[:20]})

        # ---- usability: tag coverage -----------------------------------------
        pattern = re.compile(ctx.config.get("validation.tag_pattern"))
        physical = [c for c in source.components if c.geometry_key]
        conforming = sum(1 for c in physical if pattern.match(c.tag))
        tag_coverage = 100.0 * conforming / len(physical) if physical else 100.0
        if tag_coverage < gates["min_tag_coverage_pct"]:
            add("TAG_COVERAGE", "WARN",
                f"Only {tag_coverage:.1f}% of geometry-bearing components carry a "
                f"conforming tag, below the {gates['min_tag_coverage_pct']}% target. "
                f"Non-conforming components cannot be found by tag search.",
                detail={"coverage_pct": round(tag_coverage, 1),
                        "conforming": conforming, "total": len(physical)})

        # ---- usability: attribute coverage ------------------------------------
        coverage = ctx.state.get("attribute_coverage", {})
        weakest = min(coverage.values()) if coverage else 100.0
        for attr, pct in sorted(coverage.items(), key=lambda kv: kv[1]):
            if pct < gates["min_attribute_coverage_pct"]:
                add("ATTRIBUTE_COVERAGE", "ERROR",
                    f"Attribute '{attr}' is present on only {pct:.1f}% of "
                    f"geometry-bearing components, below the "
                    f"{gates['min_attribute_coverage_pct']}% publishing gate. "
                    f"Downstream filtering and reporting on this field would be "
                    f"incomplete and misleading.",
                    detail={"attribute": attr, "coverage_pct": pct})

        # ---- usability: untagged geometry --------------------------------------
        untagged = sum(1 for c in physical if not c.tag or c.tag.strip() == "")
        untagged_pct = 100.0 * untagged / len(physical) if physical else 0.0
        if untagged_pct > gates["max_untagged_geometry_pct"]:
            add("UNTAGGED_GEOMETRY", "ERROR",
                f"{untagged_pct:.1f}% of geometry has no tag at all.",
                detail={"untagged": untagged})

        # ---- deliverable self-check ----------------------------------------------
        # Read every artifact back the way a viewer would. A file that was
        # written but cannot be parsed is the worst possible outcome,
        # because everything upstream reports success.
        lod_checks = []
        for report in ctx.state["lod_reports"]:
            path = ctx.state["lod_paths"][report["name"]]
            try:
                doc = gltf.read_glb_json(path)
                node_count = len(doc.get("nodes", []))
                mesh_count = len(doc.get("meshes", []))
                if mesh_count == 0 or node_count <= 1:
                    add("EMPTY_DELIVERABLE", "ERROR",
                        f"{report['name']} parsed but contains no drawable content.",
                        detail={"file": str(path), "meshes": mesh_count})
                lod_checks.append({"lod": report["name"], "readable": True,
                                   "nodes": node_count, "meshes": mesh_count})
            except Exception as exc:
                add("UNREADABLE_DELIVERABLE", "ERROR",
                    f"{report['name']} could not be parsed back after writing: {exc}",
                    detail={"file": str(path)})
                lod_checks.append({"lod": report["name"], "readable": False})

        # ---- persist ---------------------------------------------------------------
        ctx.db.execute("DELETE FROM qa_findings WHERE job_id = ?", (ctx.job_id,))
        if findings:
            ctx.db.executemany(
                "INSERT INTO qa_findings (job_id, revision_id, rule_code, severity, "
                "component_tag, message, detail_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(ctx.job_id, ctx.revision_id, f["rule_code"], f["severity"],
                  f["component_tag"], f["message"],
                  json.dumps(f["detail"], default=str), utcnow())
                 for f in findings],
            )

        errors = [f for f in findings if f["severity"] == "ERROR"]
        warnings = [f for f in findings if f["severity"] == "WARN"]
        ctx.state["qa_findings"] = findings

        summary = {
            "findings": len(findings),
            "errors": len(errors),
            "warnings": len(warnings),
            "bbox_drift_pct": round(bbox_drift_pct, 4),
            "volume_drift_pct": round(volume_drift_pct, 4),
            "tag_coverage_pct": round(tag_coverage, 1),
            "weakest_attribute_coverage_pct": round(weakest, 1),
            "lod_checks": lod_checks,
        }

        if errors and gates["fail_on_severity"] == "ERROR":
            codes = sorted({f["rule_code"] for f in errors})
            raise QaGateFailed(
                f"Model failed {len(errors)} quality gate(s) and was not published: "
                f"{', '.join(codes)}. First: {errors[0]['message']}",
                detail={"rule_codes": codes, "summary": summary},
            )

        return summary
