"""Stage 5 - optimise.

Four operations, applied to the *unique* geometry definitions rather
than to every placed instance, which is why this is cheap even on large
models:

  1. vertex welding      - CAD exports emit unshared vertices; welding
                           rebuilds the connectivity that step 3 needs
  2. duplicate-face drop  - the same shell exported twice is invisible
                           on screen and pure cost everywhere else
  3. quantisation         - coordinates snapped below the precision any
                           plant model actually carries
  4. LOD generation       - quadric decimation into progressively lighter
                           levels, so a user opening a 3,000-component
                           model on a laptop is not sent the full detail

Welding is deliberately ordered before decimation. On an unwelded
triangle soup there are no shared edges to collapse, so a decimator
either does nothing or shreds the surface; this ordering is the
difference between LOD generation working and appearing to work.

The stage emits one GLB per LOD level and reports measured sizes.
"""

from __future__ import annotations

import gzip
from pathlib import Path

from .. import geometry as g
from .. import gltf
from .base import JobContext, Stage


def _gzip_size(path: Path) -> int:
    """Bytes actually crossing the network.

    Viewers are served over HTTP with compression on, and glTF's JSON
    scene graph is extremely repetitive, so the transferred size is far
    below the file size. Measuring it rather than quoting the on-disk
    number is the difference between an honest delivery figure and a
    flattering one.
    """
    raw = Path(path).read_bytes()
    return len(gzip.compress(raw, compresslevel=6))


class OptimizeStage(Stage):
    name = "optimize"

    def run(self, ctx: JobContext) -> dict:
        self.check_fault_injection(ctx)

        settings = ctx.config["optimize"]
        geometry: dict[str, g.Mesh] = ctx.state["geometry"]
        instances = ctx.state["instances"]
        out_dir = ctx.artifact_dir("optimize")

        # --- clean the unique definitions --------------------------------
        cleaned: dict[str, g.Mesh] = {}
        welded_away = 0
        duplicate_faces = 0
        triangles_before = 0

        for key, mesh in geometry.items():
            triangles_before += mesh.triangle_count
            welded, removed = g.weld_vertices(mesh, settings["weld_tolerance"])
            welded_away += removed
            deduped, dropped = g.drop_duplicate_faces(welded)
            duplicate_faces += dropped
            cleaned[key] = g.quantize(deduped, decimals=3)

        triangles_after = sum(m.triangle_count for m in cleaned.values())

        # --- per-instance rendered triangle counts ------------------------
        def rendered_triangles(defs: dict[str, g.Mesh]) -> int:
            return sum(defs[i.geometry_key].triangle_count
                       for i in instances if i.geometry_key in defs)

        # --- LOD ladder -----------------------------------------------------
        # Extra levels are only worth their own copy of the scene graph on
        # models big enough for a client to struggle with. Shipping three
        # LODs of a 3,000-triangle model triples the payload to solve a
        # problem that model does not have.
        rendered_at_full = rendered_triangles(cleaned)
        lod_threshold = int(settings.get("lod_min_rendered_triangles", 20000))
        levels = settings["lod_levels"]
        if rendered_at_full < lod_threshold:
            levels = levels[:1]
            ctx.log(f"Model renders {rendered_at_full:,} triangles, below the "
                    f"{lod_threshold:,} threshold; shipping full detail only.",
                    stage=self.name)

        lod_reports: list[dict] = []
        lod_paths: dict[str, object] = {}
        min_faces = settings["min_faces_to_decimate"]

        for level in levels:
            name, ratio = level["name"], float(level["target_ratio"])
            if ratio >= 1.0:
                defs = cleaned
            else:
                defs = {k: g.decimate(m, ratio, min_faces=min_faces)
                        for k, m in cleaned.items()}

            path = out_dir / f"{name.lower()}.glb"
            stats = gltf.write_glb(
                path, defs, instances,
                generator=f"modelops {ctx.config.pipeline_version} ({name})",
                extras={"lod": name, "target_ratio": ratio},
            )
            lod_paths[name] = path
            lod_reports.append({
                "name": name,
                "target_ratio": ratio,
                "bytes": stats["bytes"],
                "wire_bytes": _gzip_size(path),
                "unique_triangles": stats["unique_triangles"],
                "rendered_triangles": stats["instanced_triangles"],
                "mesh_definitions": stats["mesh_definitions"],
            })
            ctx.state.setdefault("lod_geometry", {})[name] = defs
            ctx.log(f"{name}: {stats['unique_triangles']:,} unique triangles, "
                    f"{stats['instanced_triangles']:,} rendered, "
                    f"{stats['bytes']:,} bytes", stage=self.name)

        ctx.state["optimized_geometry"] = cleaned
        ctx.state["lod_paths"] = lod_paths
        ctx.state["lod_reports"] = lod_reports

        baseline = ctx.state["baseline_stats"]
        lod0 = lod_reports[0]
        finest_rendered = lod0["rendered_triangles"] or 1
        coarsest = lod_reports[-1]

        delivered_bytes = sum(r["bytes"] for r in lod_reports)
        delivered_wire = sum(r["wire_bytes"] for r in lod_reports)
        baseline_bytes = baseline["bytes"] or 1

        return {
            "vertices_welded_away": welded_away,
            "duplicate_faces_removed": duplicate_faces,
            "unique_triangles_before": triangles_before,
            "unique_triangles_after": triangles_after,
            "naive_baseline_bytes": baseline["bytes"],
            "delivered_bytes": delivered_bytes,
            "delivered_wire_bytes": delivered_wire,
            "payload_reduction_vs_naive_pct": round(
                100.0 * (baseline_bytes - delivered_bytes) / baseline_bytes, 1),
            "wire_reduction_vs_naive_pct": round(
                100.0 * (baseline_bytes - delivered_wire) / baseline_bytes, 1),
            "instancing_factor": round(
                lod0["rendered_triangles"] / lod0["unique_triangles"], 1)
                if lod0["unique_triangles"] else 0,
            "lod_levels": len(lod_reports),
            "coarsest_rendered_triangle_reduction_pct": round(
                100.0 * (finest_rendered - coarsest["rendered_triangles"])
                / finest_rendered, 1),
            "lods": lod_reports,
        }
