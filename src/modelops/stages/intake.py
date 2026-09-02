"""Stage 1 - intake.

Reads the source container through the format adapter and records what
arrived. Nothing is trusted yet; this stage only turns bytes into a
`SourceModel` and measures it.
"""

from __future__ import annotations

from ..adapters import adapter_for
from ..db import utcnow
from .base import JobContext, Stage


class IntakeStage(Stage):
    name = "intake"

    def run(self, ctx: JobContext) -> dict:
        self.check_fault_injection(ctx)

        if not ctx.source_path.exists():
            from ..errors import StorageUnavailable
            raise StorageUnavailable(
                f"Source file disappeared before processing: {ctx.source_path.name}",
                detail={"path": str(ctx.source_path)},
            )

        adapter = adapter_for(ctx.source_path)
        ctx.log(f"Reading {ctx.source_path.name} with adapter '{adapter.name}'",
                stage=self.name)

        source = adapter.read(ctx.source_path)
        ctx.state["source_model"] = source

        instanced_triangles = 0
        geometry_bearing = 0
        for comp in source.components:
            mesh = source.geometry.get(comp.geometry_key) if comp.geometry_key else None
            if mesh is not None:
                instanced_triangles += mesh.triangle_count
                geometry_bearing += 1

        unique_triangles = sum(m.triangle_count for m in source.geometry.values())
        ctx.state["source_stats"] = {
            "components": len(source.components),
            "geometry_bearing": geometry_bearing,
            "instanced_triangles": instanced_triangles,
            "unique_triangles": unique_triangles,
        }

        # Fill in details the intake scan could not read from the filename.
        ctx.db.execute(
            "UPDATE model_revisions SET source_tool = ?, unit_system = ?, "
            "coordinate_system = ?, exported_at = ?, status = 'PROCESSING' "
            "WHERE revision_id = ?",
            (source.manifest.get("source_tool"),
             source.manifest.get("unit_system"),
             source.manifest.get("coordinate_system"),
             source.manifest.get("exported_at"),
             ctx.revision_id),
        )

        return {
            "adapter": adapter.name,
            "components": len(source.components),
            "geometry_definitions": len(source.geometry),
            "geometry_bearing_components": geometry_bearing,
            "unique_triangles": unique_triangles,
            "instanced_triangles": instanced_triangles,
            "instancing_factor": round(instanced_triangles / unique_triangles, 1)
                                 if unique_triangles else 0,
            "source_bytes": ctx.source_path.stat().st_size,
        }
