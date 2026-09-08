"""Fleet-scale run history, written straight into the catalog.

Why this exists
---------------
The pipeline physically converts about a dozen models, and it does that
in around ten seconds. That is enough to prove the mechanism and far too
small to show what the mechanism is *for*. A dashboard reporting on
fourteen jobs cannot tell you whether a 4% failure rate is normal, which
projects are degrading, or whether last Tuesday was a bad night.

So the platform around those real runs is backfilled: a roster of models
across several districts, re-exported nightly, for a couple of weeks.

What is real and what is not
----------------------------
Every row written here carries ``is_simulated = 1``. Nothing else in the
codebase sets that flag. That gives one honest answer to the obvious
question, expressible as a WHERE clause:

    measured   -> is_simulated = 0   geometry was actually converted,
                                     bytes were actually weighed
    simulated  -> is_simulated = 1   an outcome and a duration, sampled

Two deliberate limits keep the boundary from blurring:

* **No job_steps rows.** Backfill models job *outcomes*, not stage
  internals. Because ``stage_latency()`` reads ``job_steps``, every
  per-stage timing on the dashboard is therefore a real measurement,
  with no filtering required and no way to accidentally mix the two.

* **No components or attributes.** Anything that reads engineering
  content, the viewer, work packages, handover completeness, is looking
  at models this pipeline actually opened and extracted.

The result is that throughput and reliability read at fleet scale, while
every figure describing *geometry* remains something that was measured.
"""

from __future__ import annotations

import hashlib
import random
from datetime import datetime, timedelta, timezone

from ..config import Config
from ..db import Database, utcnow
from .scenario import PROJECTS

# ---------------------------------------------------------------------
# The model roster
#
# Real plant projects are decomposed by building, then by discipline, and
# each combination is a separately exported model. That is what produces
# a hundred-plus models on a single job, and therefore what produces a
# hundred-plus nightly conversions.
# ---------------------------------------------------------------------

AREAS = {
    "KNS-RWB": ["Radwaste Process", "Solid Waste", "Liquid Waste", "Ventilation Stack"],
    "KNS-FHB": ["Fuel Pool", "Cask Loading", "Transfer Canal", "Crane Bay"],
    "KPD-S41": ["345kV Yard", "Control Enclosure", "Transformer Bay"],
    "KIN-H2A": ["Electrolyser Hall", "Compression", "Utilities", "Flare"],
    "KIE-WTP": ["Headworks", "Filtration", "Chemical Feed", "Clearwell"],
}

DISCIPLINES = [
    ("PIPE",  "Piping",            "PIPING",     "SmartPlant Review export"),
    ("STEEL", "Structural Steel",  "STRUCTURAL", "Navisworks export"),
    ("EQUIP", "Mechanical Equipment", "EQUIPMENT", "SmartPlant Review export"),
    ("ELEC",  "Electrical",        "ELECTRICAL", "Navisworks export"),
    ("HVAC",  "HVAC",              "HVAC",       "SmartPlant Review export"),
    ("CIVIL", "Civil and Concrete", "CIVIL",     "Revit export"),
]

# Failure mix. These are the same error classes the real pipeline raises,
# in roughly the proportion a converter fleet actually produces: mostly
# transient infrastructure noise, a minority of genuinely bad exports.
FAILURE_MIX = [
    ("ConverterUnavailable", "TRANSIENT", 0.34, "Converter worker did not respond within the stage timeout."),
    ("SourceUnavailable",    "TRANSIENT", 0.16, "Source share was unreachable while reading the export."),
    ("QaGateFailed",         "PERMANENT", 0.20, "Blocking quality findings; previous revision left live."),
    ("ValidationFailed",     "PERMANENT", 0.12, "Manifest failed validation against the project standard."),
    ("SourceCorrupt",        "PERMANENT", 0.10, "Export archive was truncated in transfer."),
    ("ConverterTimeout",     "TRANSIENT", 0.08, "Conversion exceeded the stage timeout and was abandoned."),
]


def _weighted(rng: random.Random, options):
    roll = rng.random() * sum(o[2] for o in options)
    cumulative = 0.0
    for option in options:
        cumulative += option[2]
        if roll <= cumulative:
            return option
    return options[-1]


def _fleet_models() -> list[dict]:
    """Every model identity the backfill will operate on."""
    out: list[dict] = []
    for project in PROJECTS:
        if project.get("converts_locally"):
            continue
        code = project["project_code"]
        for area_index, area in enumerate(AREAS[code], start=1):
            for short, label, discipline, tool in DISCIPLINES:
                out.append({
                    "project_code": code,
                    "model_key": f"{code.split('-')[1]}-{short}-U{area_index}0",
                    "model_name": f"{area} {label}",
                    "discipline": discipline,
                    "source_tool": tool,
                })
    return out


def backfill(config: Config, db: Database, *, days: int = 14,
             seed: int = 424242) -> dict:
    """Write ``days`` of nightly conversion history for the wider fleet."""
    rng = random.Random(seed)
    fleet = _fleet_models()
    if not fleet:
        return {"models": 0, "jobs": 0, "days": 0}

    sla_by_project = {p["project_code"]: p["sla_minutes"] for p in PROJECTS}
    pipeline_version = config.get("pipeline_version", "1.4.0")

    # -- register the model identities --------------------------------
    model_ids: dict[tuple[str, str], int] = {}
    for spec in fleet:
        key = (spec["project_code"], spec["model_key"])
        existing = db.query_one(
            "SELECT model_id FROM models WHERE project_code = ? AND model_key = ?", key)
        if existing:
            model_ids[key] = existing["model_id"]
            continue
        model_ids[key] = db.execute(
            "INSERT INTO models (project_code, model_key, model_name, discipline, "
            "source_tool, is_simulated, created_at) VALUES (?, ?, ?, ?, ?, 1, ?)",
            (spec["project_code"], spec["model_key"], spec["model_name"],
             spec["discipline"], spec["source_tool"], utcnow()))

    # Each model gets a stable size, so the same model is consistently
    # heavy or light across the whole history rather than jumping about.
    profile = {}
    for spec in fleet:
        key = (spec["project_code"], spec["model_key"])
        components = int(rng.lognormvariate(6.6, 0.62))
        profile[key] = {
            "components": max(120, min(components, 9000)),
            # Larger models take longer, with per-run jitter added later.
            # Calibrated so a small model converts in a couple of minutes
            # and the heaviest take the better part of an hour, which is
            # what puts the tail of the distribution up against the SLA
            # rather than comfortably inside it.
            "base_ms": 0.0,
        }
        profile[key]["base_ms"] = 90_000 + profile[key]["components"] * 260.0

    # -- generate nightly runs ----------------------------------------
    now = datetime.now(timezone.utc)
    today = now.date()

    # One bad night, to give the reliability chart something to explain.
    # A converter host degrades and takes a chunk of that night with it.
    incident_day = max(1, min(days - 2, 4))

    jobs_written = 0
    revisions: list[tuple] = []
    job_rows: list[tuple] = []
    publications: dict[tuple[str, str], tuple] = {}

    for day_offset in range(days, 0, -1):
        day = today - timedelta(days=day_offset)
        # Weekend exports thin out, because engineering is not working.
        weekend = day.weekday() >= 5
        share = rng.uniform(0.28, 0.42) if weekend else rng.uniform(0.86, 1.0)
        incident = (day_offset == incident_day)

        batch = rng.sample(fleet, max(1, int(len(fleet) * share)))
        # The nightly batch kicks off at 22:00 and drains over some hours.
        start = datetime.combine(day, datetime.min.time(),
                                 tzinfo=timezone.utc) + timedelta(hours=22)

        for index, spec in enumerate(batch):
            key = (spec["project_code"], spec["model_key"])
            prof = profile[key]
            queued = start + timedelta(seconds=index * rng.uniform(20, 95))

            failure_rate = 0.32 if incident else 0.045
            failed = rng.random() < failure_rate
            attempt, status = 1, "SUCCEEDED"
            error_class = error_kind = error_message = None

            if failed:
                error_class, error_kind, _, error_message = _weighted(rng, FAILURE_MIX)
                if incident and rng.random() < 0.7:
                    error_class, error_kind = "ConverterUnavailable", "TRANSIENT"
                    error_message = ("Converter worker pool exhausted; "
                                     "no worker accepted the job.")
                if error_kind == "TRANSIENT":
                    # Transient failures retry with backoff. Most recover.
                    attempt = rng.randint(2, 3)
                    if rng.random() < (0.45 if incident else 0.8):
                        status, error_class, error_kind, error_message = (
                            "SUCCEEDED", None, None, None)
                    else:
                        status = "FAILED"
                else:
                    status = "QUARANTINED" if error_class == "SourceCorrupt" else "FAILED"

            duration_ms = int(prof["base_ms"] * rng.uniform(0.72, 1.55) * attempt)
            if incident:
                duration_ms = int(duration_ms * rng.uniform(1.4, 2.6))
            finished = queued + timedelta(milliseconds=duration_ms)

            sla_minutes = sla_by_project.get(spec["project_code"], 30)
            sla_deadline = queued + timedelta(minutes=sla_minutes)
            sla_breached = 1 if finished > sla_deadline else 0

            revision_label = f"Rev{day.strftime('%m%d')}"
            digest = hashlib.sha256(
                f"{key}:{revision_label}:{seed}".encode()).hexdigest()
            source_bytes = int(prof["components"] * rng.uniform(9_000, 26_000))

            revisions.append((
                model_ids[key], revision_label,
                f"{spec['project_code']}_{spec['model_key']}_{revision_label}.plantx",
                f"<nightly-export-share>/{spec['project_code']}/{spec['model_key']}",
                digest, source_bytes, spec["source_tool"], "mm", "PLANT-GRID-NAD83",
                queued.strftime("%Y-%m-%d %H:%M:%S"),
                queued.strftime("%Y-%m-%d %H:%M:%S"),
                "PUBLISHED" if status == "SUCCEEDED" else status,
            ))
            job_rows.append((
                digest, pipeline_version, "AUTO", 100, status, attempt, 3,
                f"worker-{rng.randint(1, 6):02d}",
                queued.strftime("%Y-%m-%d %H:%M:%S"),
                queued.strftime("%Y-%m-%d %H:%M:%S"),
                finished.strftime("%Y-%m-%d %H:%M:%S"),
                duration_ms, sla_deadline.strftime("%Y-%m-%d %H:%M:%S"), sla_breached,
                error_class, error_kind, error_message,
            ))
            jobs_written += 1

            if status == "SUCCEEDED":
                # Latest successful run for a model becomes what is live.
                triangles = int(prof["components"] * rng.uniform(45, 220))
                naive = int(triangles * rng.uniform(52, 78))
                published = int(naive * rng.uniform(0.06, 0.16))
                wire = int(published * rng.uniform(0.55, 0.78))
                publications[key] = (
                    digest, triangles, prof["components"], naive, published, wire,
                    source_bytes, finished.strftime("%Y-%m-%d %H:%M:%S"),
                )

    # -- write it ------------------------------------------------------
    with db.tx() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO model_revisions (model_id, revision_label, "
            "source_filename, source_path, source_sha256, source_bytes, source_tool, "
            "unit_system, coordinate_system, exported_at, received_at, status, "
            "is_simulated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)", revisions)

        # Jobs are matched back to their revision by content hash, which is
        # the same identity rule the real intake stage uses.
        conn.executemany(
            "INSERT INTO jobs (revision_id, pipeline_version, trigger, priority, "
            "status, attempt, max_attempts, worker, queued_at, started_at, "
            "finished_at, duration_ms, sla_deadline, sla_breached, error_class, "
            "error_kind, error_message, is_simulated) "
            "SELECT revision_id, ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1 "
            "FROM model_revisions WHERE source_sha256 = ?",
            [row[1:] + (row[0],) for row in job_rows])

        for key, pub in publications.items():
            (digest, triangles, components, naive, published, wire,
             source_bytes, published_at) = pub
            conn.execute(
                "INSERT INTO publications (revision_id, job_id, delivery_path, "
                "manifest_sha256, published_at, published_bytes, wire_bytes, "
                "naive_bytes, source_bytes, triangle_count, component_count, "
                "lod_levels, is_current, is_simulated) "
                "SELECT r.revision_id, j.job_id, ?, ?, ?, ?, ?, ?, ?, ?, ?, 3, 1, 1 "
                "FROM model_revisions r JOIN jobs j ON j.revision_id = r.revision_id "
                "WHERE r.source_sha256 = ? LIMIT 1",
                (f"<delivery-share>/{key[0]}/{key[1]}", digest[:64], published_at,
                 published, wire, naive, source_bytes, triangles, components, digest))

        # Only the newest publication per model stays current.
        conn.execute(
            "UPDATE publications SET is_current = 0 "
            "WHERE is_simulated = 1 AND publication_id NOT IN ("
            "  SELECT MAX(p.publication_id) FROM publications p "
            "  JOIN model_revisions r ON r.revision_id = p.revision_id "
            "  WHERE p.is_simulated = 1 GROUP BY r.model_id)")
        conn.execute(
            "UPDATE model_revisions SET status = 'SUPERSEDED' "
            "WHERE is_simulated = 1 AND status = 'PUBLISHED' AND revision_id NOT IN ("
            "  SELECT revision_id FROM publications WHERE is_current = 1)")

    db.audit("backfill_history", entity_type="pipeline", entity_id="fleet",
             detail={"days": days, "models": len(fleet), "jobs": jobs_written,
                     "note": "simulated fleet history, is_simulated = 1"})

    return {
        "models": len(fleet),
        "jobs": jobs_written,
        "days": days,
        "projects": len({s["project_code"] for s in fleet}),
        "live": len(publications),
    }
