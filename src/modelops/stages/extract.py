"""Stage 3 - extract.

Writes the tag tree and engineering attributes into the catalog. This is
the half of a model delivery that is not geometry: it is what lets a user
search for a tag, filter by system or commissioning package, and what
downstream systems join against.

Rows are written in bulk inside a single transaction. Per-row inserts
across thousands of components are the usual reason a pipeline like this
gets slow as projects grow.
"""

from __future__ import annotations

import numpy as np

from ..db import utcnow
from .base import JobContext, Stage


class ExtractStage(Stage):
    name = "extract"

    def run(self, ctx: JobContext) -> dict:
        self.check_fault_injection(ctx)
        source = ctx.state["source_model"]

        # A re-run of the same revision must not double up rows.
        ctx.db.execute("DELETE FROM component_attributes WHERE revision_id = ?",
                       (ctx.revision_id,))
        ctx.db.execute("DELETE FROM components WHERE revision_id = ?", (ctx.revision_id,))

        # Resolve full hierarchy paths iteratively; a cycle in parent_tag
        # would otherwise hang the walk.
        parent_of = {c.tag: c.parent_tag for c in source.components}
        path_cache: dict[str, str] = {}

        def resolve_path(tag: str) -> str:
            if tag in path_cache:
                return path_cache[tag]
            chain: list[str] = []
            seen: set[str] = set()
            cursor: str | None = tag
            while cursor and cursor not in seen:
                seen.add(cursor)
                chain.append(cursor)
                cursor = parent_of.get(cursor)
            path = " / ".join(reversed(chain))
            path_cache[tag] = path
            return path

        component_rows = []
        for comp in source.components:
            mesh = source.geometry.get(comp.geometry_key) if comp.geometry_key else None
            tri_count = mesh.triangle_count if mesh is not None else 0

            bbox = [None] * 6
            if mesh is not None and mesh.vertex_count:
                matrix = (np.asarray(comp.transform, dtype=np.float64).reshape(4, 4)
                          if comp.transform else np.eye(4))
                placed = mesh.transformed(matrix).bounds()
                bbox = [float(v) for v in placed[0]] + [float(v) for v in placed[1]]

            component_rows.append((
                ctx.revision_id, comp.tag, comp.parent_tag, resolve_path(comp.tag),
                comp.category, comp.discipline, comp.geometry_key,
                1 if mesh is not None else 0, tri_count, *bbox,
            ))

        ctx.db.executemany(
            "INSERT INTO components (revision_id, tag, parent_tag, path, category, "
            "discipline, geometry_key, has_geometry, triangle_count, "
            "bbox_min_x, bbox_min_y, bbox_min_z, bbox_max_x, bbox_max_y, bbox_max_z) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            component_rows,
        )

        # Map tags back to the ids just assigned, then bulk-write attributes.
        id_by_tag: dict[str, int] = {}
        for row in ctx.db.query(
            "SELECT component_id, tag FROM components WHERE revision_id = ? "
            "ORDER BY component_id", (ctx.revision_id,)
        ):
            id_by_tag.setdefault(row["tag"], row["component_id"])

        attribute_rows = []
        for comp in source.components:
            component_id = id_by_tag.get(comp.tag)
            if component_id is None:
                continue
            for name, value in comp.attributes.items():
                attribute_rows.append((component_id, ctx.revision_id, name, value))

        if attribute_rows:
            ctx.db.executemany(
                "INSERT INTO component_attributes (component_id, revision_id, name, value) "
                "VALUES (?, ?, ?, ?)",
                attribute_rows,
            )

        distinct_attrs = len({r[2] for r in attribute_rows})
        ctx.state["component_id_by_tag"] = id_by_tag

        return {
            "components_written": len(component_rows),
            "attributes_written": len(attribute_rows),
            "distinct_attribute_names": distinct_attrs,
            "max_depth": max((p.count(" / ") + 1 for p in path_cache.values()), default=0),
        }
