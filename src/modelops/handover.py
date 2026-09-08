"""Structured handover: what a Common Data Environment consumes from here.

Why this exists
---------------
A viewer is a destination, but it is not the only one. The same extracted
attributes that let the pipeline gate a model also make it useful to
every downstream system that needs to know what is in the plant: cost
and progress in InEight, system turnover in Hexagon's completions tools,
and the CDE that aggregates them into one integrated model.

The point this module makes is that once attributes are measured and
enforced at the gate, handover stops being a project-end scramble to
reconcile spreadsheets and becomes a set of feeds that are correct
continuously, because they are generated from the same catalog the
viewer is served from.

Feeds are generated on request rather than cached, so what a consumer
downloads always matches the models that are live right now. They are
small enough that this is cheap, and being able to say "this is not a
stale export" is worth more than the milliseconds.

Scope, stated plainly: these are real files built from the real catalog,
but the consuming systems are not connected. Every outbound connector is
declared with the interface it would use and marked unimplemented, the
same way the inbound source adapters are. The boundary is visible rather
than implied.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .config import project_root
from .db import Database

# Attributes a receiving system needs before a component is any use to
# it. Distinct from the QA gate's required_attributes, which is about
# whether the model is coherent; this is about whether it is usable.
#
# Each entry is (column in v_component_packaging, label, who needs it).
HANDOVER_ATTRIBUTES = [
    ("system_code",           "System",         "Commissioning, P&ID cross-reference"),
    ("material",              "Material",       "Cost, procurement, weight take-off"),
    ("commissioning_system",  "Comm. system",   "System turnover and punch listing"),
    ("cwa",                   "CWA",            "Work packaging, area progress"),
    ("iwp",                   "IWP",            "Field planning and progress claim"),
    ("weight_kg",             "Weight",         "Lift planning, structural loading"),
]


@dataclass
class Consumer:
    """A downstream system that would receive one or more feeds."""
    name: str
    role: str
    interface: str
    implemented: bool = False


CONSUMERS = [
    Consumer("Universal Plant Viewer", "3D delivery to the field and office",
             "Published GLB tree plus model index on a watched share",
             implemented=True),
    Consumer("Hexagon Smart Suite", "Engineering source of record and completions",
             "SmartPlant Foundation / SPO web API, tag and system alignment"),
    Consumer("InEight", "Cost, planning and field progress",
             "InEight Platform API, quantity and work-package endpoints"),
    Consumer("Common Data Environment", "Aggregated project data across sources",
             "Scheduled feed pickup into the CDE landing zone"),
]


@dataclass
class Feed:
    key: str
    name: str
    consumer: str
    fmt: str                      # csv | json
    purpose: str
    build: Callable[[Database, str | None], tuple[list[str], list[dict]]]

    @property
    def filename(self) -> str:
        return f"{self.key}.{self.fmt}"


# --- feed builders ----------------------------------------------------
#
# Every builder reads only from published, current revisions, because a
# feed that includes a model which failed the gate would defeat the
# purpose of having a gate.

def _scope(project_code: str | None, alias: str = "v") -> tuple[str, tuple]:
    if project_code:
        return f"WHERE {alias}.project_code = ?", (project_code,)
    return "", ()


def _component_register(db: Database, project_code: str | None):
    where, params = _scope(project_code)
    rows = [dict(r) for r in db.query(
        f"""SELECT project_code, model_key, tag, category, discipline,
                   system_code AS system, commissioning_system, material,
                   weight_kg, cwa, cwp, ewp, iwp, poc_sequence,
                   planned_date, lifecycle_status, triangle_count
            FROM v_component_packaging v
            {where}
            ORDER BY project_code, model_key, tag""", params)]
    return list(rows[0].keys()) if rows else [], rows


def _commissioning_index(db: Database, project_code: str | None):
    where, params = _scope(project_code)
    clause = (where + " AND " if where else "WHERE ") + \
        "v.commissioning_system IS NOT NULL AND v.commissioning_system <> ''"
    rows = [dict(r) for r in db.query(
        f"""SELECT v.project_code,
                   v.commissioning_system,
                   COUNT(*)                          AS components,
                   COUNT(DISTINCT v.discipline)      AS disciplines,
                   COUNT(DISTINCT v.iwp)             AS work_packages,
                   ROUND(SUM(CAST(COALESCE(v.weight_kg,'0') AS REAL)),1) AS weight_kg,
                   SUM(CASE WHEN v.lifecycle_status IN
                       ('Installed','Tested','Commissioned') THEN 1 ELSE 0 END)
                                                     AS installed,
                   SUM(CASE WHEN v.lifecycle_status IN ('Tested','Commissioned')
                       THEN 1 ELSE 0 END)            AS tested,
                   SUM(CASE WHEN v.lifecycle_status = 'Commissioned'
                       THEN 1 ELSE 0 END)            AS commissioned
            FROM v_component_packaging v
            {clause}
            GROUP BY v.project_code, v.commissioning_system
            ORDER BY v.project_code, v.commissioning_system""", params)]
    for r in rows:
        r["turnover_pct"] = round(
            100.0 * r["commissioned"] / (r["components"] or 1), 1)
    return list(rows[0].keys()) if rows else [], rows


def _work_package_status(db: Database, project_code: str | None):
    from . import awp
    board = awp.package_board(db, project_code=project_code)
    keep = ("project_code", "model_key", "cwa", "cwp", "ewp", "iwp",
            "discipline", "poc_sequence", "planned_date", "crew",
            "components", "weight_kg", "estimated_hours", "installed",
            "materials_on_site", "verdict", "blockers", "summary")
    rows = [{k: p[k] for k in keep} for p in board]
    return list(keep), rows


def _model_index(db: Database, project_code: str | None):
    """What a CDE needs to point a viewer at the current model set."""
    where, params = _scope(project_code, "c")
    clause = (where + " AND " if where else "WHERE ") + \
        "c.live_revision IS NOT NULL AND c.is_simulated = 0"
    rows = [dict(r) for r in db.query(
        f"""SELECT c.project_code, c.model_key, c.model_name, c.discipline,
                   c.live_revision, c.live_published_at, c.component_count,
                   c.triangle_count, c.published_bytes, c.wire_bytes,
                   p.delivery_path, p.manifest_sha256, p.lod_levels
            FROM v_model_currency c
            JOIN model_revisions r ON r.model_id = c.model_id
                                  AND r.revision_label = c.live_revision
            JOIN publications p ON p.revision_id = r.revision_id
                               AND p.is_current = 1
            {clause}
            ORDER BY c.project_code, c.model_key""", params)]

    # Relative to the delivery root, not absolute. A consumer resolves the
    # path against wherever the share is mounted on its side, and the feed
    # does not carry the machine that happened to generate it.
    root = project_root()
    for row in rows:
        try:
            row["delivery_path"] = Path(row["delivery_path"]).relative_to(root).as_posix()
        except ValueError:
            row["delivery_path"] = Path(row["delivery_path"]).as_posix()
    return list(rows[0].keys()) if rows else [], rows


def _attribute_exceptions(db: Database, project_code: str | None):
    """The actionable one: every component a receiving system will reject.

    This is the feed that closes the loop. The gate stops a model that is
    broadly incomplete; this names the individual components that slipped
    under it, with the model and package that own them, so the gap is
    routed to whoever can fix it instead of discovered at turnover.
    """
    where, params = _scope(project_code)
    checks = " OR ".join(
        f"(v.{col} IS NULL OR v.{col} = '')" for col, _, _ in HANDOVER_ATTRIBUTES)
    clause = (where + " AND " if where else "WHERE ") + \
        f"v.has_geometry = 1 AND ({checks})"
    raw = [dict(r) for r in db.query(
        f"""SELECT v.project_code, v.model_key, v.tag, v.category,
                   v.discipline, v.iwp, v.lifecycle_status,
                   {', '.join('v.' + col for col, _, _ in HANDOVER_ATTRIBUTES)}
            FROM v_component_packaging v
            {clause}
            ORDER BY v.project_code, v.model_key, v.tag""", params)]

    rows = []
    for r in raw:
        missing = [label for col, label, _ in HANDOVER_ATTRIBUTES
                   if not r.get(col)]
        rows.append({
            "project_code": r["project_code"],
            "model_key": r["model_key"],
            "tag": r["tag"],
            "category": r["category"],
            "discipline": r["discipline"],
            "iwp": r["iwp"] or "",
            "lifecycle_status": r["lifecycle_status"] or "",
            "missing_attributes": "; ".join(missing),
            "blocks": "; ".join(sorted({
                need for col, label, need in HANDOVER_ATTRIBUTES
                if label in missing})),
        })
    cols = list(rows[0].keys()) if rows else [
        "project_code", "model_key", "tag", "category", "discipline",
        "iwp", "lifecycle_status", "missing_attributes", "blocks"]
    return cols, rows


FEEDS = [
    Feed("component-register", "Component register", "InEight, CDE", "csv",
         "Every published component with its commodity, weight, system and "
         "package. The quantity basis for cost and progress.",
         _component_register),
    Feed("work-package-status", "Work package status", "InEight", "json",
         "Each IWP with its readiness verdict and the constraints holding "
         "it, so planning sees the same blockers the model does.",
         _work_package_status),
    Feed("commissioning-index", "Commissioning system index",
         "Hexagon Smart Suite", "csv",
         "Components rolled up by commissioning system with turnover "
         "progress. The spine of system-based completions.",
         _commissioning_index),
    Feed("model-index", "Model index", "Universal Plant Viewer, CDE", "json",
         "Current published revision per model with delivery path, LOD "
         "count and manifest hash. How a consumer knows what is live.",
         _model_index),
    Feed("attribute-exceptions", "Attribute exceptions", "Data governance",
         "csv",
         "Every component missing an attribute a receiving system needs, "
         "with the model and package that own the fix.",
         _attribute_exceptions),
]

FEEDS_BY_KEY = {f.key: f for f in FEEDS}


def build(db: Database, key: str, project_code: str | None = None):
    """Return (columns, rows) for one feed."""
    feed = FEEDS_BY_KEY[key]
    return feed.build(db, project_code)


def render(key: str, columns: list[str], rows: list[dict]) -> tuple[str, str]:
    """Serialise a feed to text. Returns (body, mimetype)."""
    feed = FEEDS_BY_KEY[key]
    if feed.fmt == "json":
        body = json.dumps({
            "feed": feed.key,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "record_count": len(rows),
            "records": rows,
        }, indent=2)
        return body, "application/json"

    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore",
                            lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue(), "text/csv"


def catalog(db: Database, project_code: str | None = None) -> list[dict]:
    """Every feed with a live record count, for the Handover page."""
    out = []
    for feed in FEEDS:
        _, rows = feed.build(db, project_code)
        out.append({
            "key": feed.key,
            "name": feed.name,
            "consumer": feed.consumer,
            "fmt": feed.fmt.upper(),
            "purpose": feed.purpose,
            "records": len(rows),
        })
    return out


def completeness(db: Database, project_code: str | None = None) -> dict:
    """Attribute coverage per discipline, across published components.

    Counts geometry-bearing components only. A Line or Area node has no
    material and no weight by definition, and including them would
    manufacture a shortfall that nobody can fix.
    """
    where, params = _scope(project_code)
    clause = (where + " AND " if where else "WHERE ") + "v.has_geometry = 1"
    sums = ", ".join(
        f"SUM(CASE WHEN v.{col} IS NOT NULL AND v.{col} <> '' "
        f"THEN 1 ELSE 0 END) AS have_{col}"
        for col, _, _ in HANDOVER_ATTRIBUTES)
    rows = [dict(r) for r in db.query(
        f"""SELECT v.discipline, COUNT(*) AS components, {sums}
            FROM v_component_packaging v
            {clause}
            GROUP BY v.discipline
            ORDER BY v.discipline""", params)]

    disciplines, totals = [], {col: 0 for col, _, _ in HANDOVER_ATTRIBUTES}
    grand = 0
    for row in rows:
        n = row["components"] or 1
        grand += row["components"]
        cells = []
        for col, label, _ in HANDOVER_ATTRIBUTES:
            have = row[f"have_{col}"] or 0
            totals[col] += have
            cells.append({
                "label": label,
                "pct": round(100.0 * have / n, 1),
                "missing": row["components"] - have,
            })
        disciplines.append({"discipline": row["discipline"],
                            "components": row["components"], "cells": cells})

    overall = [{
        "label": label,
        "pct": round(100.0 * totals[col] / (grand or 1), 1),
        "missing": grand - totals[col],
        "needed_by": need,
    } for col, label, need in HANDOVER_ATTRIBUTES]

    return {
        "attributes": [label for _, label, _ in HANDOVER_ATTRIBUTES],
        "disciplines": disciplines,
        "overall": overall,
        "components": grand,
        "complete": sum(1 for a in overall if a["missing"] == 0),
    }


def integration_status(db: Database) -> dict:
    """Inbound adapters and outbound consumers, with what is real."""
    from .adapters import registered

    inbound, seen = [], set()
    for adapter in registered().values():
        if adapter.name in seen:
            continue
        seen.add(adapter.name)
        inbound.append({
            "name": adapter.name,
            "extensions": ", ".join(sorted(adapter.extensions)),
            "implemented": adapter.implemented,
            "detail": adapter.external_tool or "Native to this pipeline",
        })
    inbound.sort(key=lambda a: (not a["implemented"], a["name"]))

    outbound = [{
        "name": c.name, "role": c.role, "interface": c.interface,
        "implemented": c.implemented,
    } for c in CONSUMERS]

    return {"inbound": inbound, "outbound": outbound}
