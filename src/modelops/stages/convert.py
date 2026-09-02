"""Stage 4 - convert.

Produces the delivery format (glTF binary) the naive way: every placed
component written out as its own geometry with its transform baked in,
exactly as a straightforward "load it all and export" conversion does.

This file is never published. It exists to be the *measured* baseline
that the optimise stage is compared against, so the reduction reported
later is the difference between two files that were both actually
written, rather than an estimate chosen to look good. It is deleted with
the job's working directory.
"""

from __future__ import annotations

import numpy as np

from .. import gltf
from ..errors import GeometryInvalid
from ..geometry import Mesh
from .base import JobContext, Stage

IDENTITY = tuple(np.eye(4).flatten())


class ConvertStage(Stage):
    name = "convert"

    def run(self, ctx: JobContext) -> dict:
        self.check_fault_injection(ctx)
        source = ctx.state["source_model"]

        instances: list[gltf.Instance] = []
        referenced: set[str] = set()
        skipped_missing = 0

        for comp in source.components:
            if not comp.geometry_key:
                continue
            if comp.geometry_key not in source.geometry:
                skipped_missing += 1
                continue
            referenced.add(comp.geometry_key)
            instances.append(gltf.Instance(
                name=comp.tag,
                geometry_key=comp.geometry_key,
                matrix=tuple(comp.transform) if comp.transform else IDENTITY,
                material=gltf.material_for(comp.category),
            ))

        if not instances:
            raise GeometryInvalid(
                "Model contains no placeable geometry; nothing would be visible "
                "in the viewer.",
                detail={"components": len(source.components),
                        "geometry_definitions": len(source.geometry)},
            )

        geometry = {k: v for k, v in source.geometry.items() if k in referenced}
        unreferenced = len(source.geometry) - len(geometry)

        # --- naive baseline: bake every transform, share nothing --------
        naive_geometry: dict[str, Mesh] = {}
        naive_instances: list[gltf.Instance] = []
        for i, inst in enumerate(instances):
            key = f"baked_{i}"
            matrix = np.asarray(inst.matrix, dtype=np.float64).reshape(4, 4)
            naive_geometry[key] = geometry[inst.geometry_key].transformed(matrix)
            naive_instances.append(gltf.Instance(
                name=inst.name, geometry_key=key, matrix=IDENTITY,
                material=inst.material))

        out_dir = ctx.artifact_dir("convert")
        baseline_path = out_dir / "baseline.glb"
        stats = gltf.write_glb(
            baseline_path, naive_geometry, naive_instances,
            generator=f"modelops {ctx.config.pipeline_version} (naive conversion)",
        )

        ctx.state["instances"] = instances
        ctx.state["geometry"] = geometry
        ctx.state["baseline_stats"] = stats
        ctx.state["baseline_path"] = baseline_path

        # Release the baked copies; on a large model these are the biggest
        # thing in memory and nothing downstream needs them.
        naive_geometry.clear()
        naive_instances.clear()

        return {
            "instances": len(instances),
            "geometry_definitions": len(geometry),
            "unreferenced_definitions_dropped": unreferenced,
            "components_missing_geometry": skipped_missing,
            "baseline_bytes": stats["bytes"],
            "baseline_mesh_definitions": stats["mesh_definitions"],
            "rendered_triangles": stats["instanced_triangles"],
        }
