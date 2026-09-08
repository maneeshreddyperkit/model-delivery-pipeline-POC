"""Builds the demo dataset dropped into the pipeline inbox.

A pipeline that only ever sees good input proves nothing. The scenario
deliberately includes the failure modes that actually cause pager traffic
in model delivery work, so the demo can show how each is contained:

  clean            - processes and publishes normally
  corrupt archive  - truncated upload; must fail fast and NOT be retried
  bad manifest     - missing required fields
  bad units        - inch export into a millimetre pipeline
  duplicate tags   - two components claiming the same tag
  thin metadata    - attribute coverage below the publishing gate
  bad tag format   - components that break the project naming standard
  flaky converter  - fails once, succeeds on retry (proves backoff works)
  new revision     - supersedes a live model, exercising rollback
  re-drop          - identical file dropped twice; must be a no-op

Everything is seeded, so the same run produces the same results.
"""

from __future__ import annotations

import json
import random
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import plant

# ---------------------------------------------------------------------
# Projects the POC pretends to serve.
#
# The first three carry the models this pipeline physically converts. The
# rest exist so the platform reads at the scale it would really run at:
# several districts, a mix of project sizes, and a subscriber base in the
# thousands rather than the hundreds. Their run history is backfilled by
# synth/history.py and flagged as simulated everywhere it lands.
#
# `converts_locally` is what tells the backfill which projects to leave
# alone, so simulated rows can never be written over measured ones.
# ---------------------------------------------------------------------
PROJECTS = [
    {
        "project_code": "KNS-NI1",
        "project_name": "Nuclear Island - Unit 1",
        "district": "Kiewit Nuclear Solutions",
        "sla_minutes": 20,
        "subscriber_count": 1450,
        "converts_locally": True,
    },
    {
        "project_code": "KNS-TB2",
        "project_name": "Turbine Building - Unit 2",
        "district": "Kiewit Nuclear Solutions",
        "sla_minutes": 30,
        "subscriber_count": 980,
        "converts_locally": True,
    },
    {
        "project_code": "KNS-BOP",
        "project_name": "Balance of Plant",
        "district": "Kiewit Nuclear Solutions",
        "sla_minutes": 45,
        "subscriber_count": 610,
        "converts_locally": True,
    },
    {
        "project_code": "KNS-RWB",
        "project_name": "Radwaste Building",
        "district": "Kiewit Nuclear Solutions",
        "sla_minutes": 45,
        "subscriber_count": 240,
        "converts_locally": False,
    },
    {
        "project_code": "KNS-FHB",
        "project_name": "Fuel Handling Building",
        "district": "Kiewit Nuclear Solutions",
        "sla_minutes": 30,
        "subscriber_count": 185,
        "converts_locally": False,
    },
    {
        "project_code": "KPD-S41",
        "project_name": "Sherman County 345kV Substation",
        "district": "Kiewit Power Delivery",
        "sla_minutes": 60,
        "subscriber_count": 130,
        "converts_locally": False,
    },
    {
        "project_code": "KIN-H2A",
        "project_name": "Gulf Coast Hydrogen - Train A",
        "district": "Kiewit Industrial",
        "sla_minutes": 60,
        "subscriber_count": 95,
        "converts_locally": False,
    },
    {
        "project_code": "KIE-WTP",
        "project_name": "Regional Water Treatment Expansion",
        "district": "Kiewit Infrastructure Engineers",
        "sla_minutes": 90,
        "subscriber_count": 75,
        "converts_locally": False,
    },
]


@dataclass
class DropSpec:
    project_code: str
    model_key: str
    model_name: str
    discipline: str
    revision: str
    defect: str = "none"
    size: str = "normal"
    note: str = ""


# The dataset. Ordering matters only for readability; the orchestrator
# picks work up by queue order and priority.
DROPS = [
    DropSpec("KNS-NI1", "NI1-PIPE-U10", "Unit 10 Piping", "PIPING", "RevC",
             note="Baseline good model - largest of the set"),
    DropSpec("KNS-NI1", "NI1-STEEL-U10", "Unit 10 Structural Steel", "STRUCTURAL", "RevB",
             note="Baseline good model - heavy instancing"),
    DropSpec("KNS-NI1", "NI1-EQUIP-U10", "Unit 10 Mechanical Equipment", "EQUIPMENT", "RevA",
             note="Baseline good model"),
    DropSpec("KNS-TB2", "TB2-PIPE-U20", "Unit 20 Piping", "PIPING", "RevA", size="small",
             note="Baseline good model"),
    DropSpec("KNS-TB2", "TB2-ELEC-U20", "Unit 20 Cable Trays", "ELECTRICAL", "RevD",
             note="Baseline good model"),
    DropSpec("KNS-BOP", "BOP-STEEL-U30", "BOP Structural Steel", "STRUCTURAL", "RevA",
             size="small", note="Baseline good model"),
    DropSpec("KNS-TB2", "TB2-HVAC-U20", "Unit 20 Ventilation", "HVAC", "RevB",
             note="Baseline good model - ductwork, dampers and diffusers"),

    DropSpec("KNS-BOP", "BOP-PIPE-U30", "BOP Piping", "PIPING", "RevB",
             defect="flaky_converter", size="small",
             note="Converter fails on first attempt, succeeds on retry"),
    DropSpec("KNS-NI1", "NI1-HVAC-U10", "Unit 10 HVAC", "HVAC", "RevA",
             defect="corrupt_archive", size="small",
             note="Truncated upload - permanent failure, must not retry"),
    DropSpec("KNS-TB2", "TB2-EQUIP-U20", "Unit 20 Equipment", "EQUIPMENT", "RevA",
             defect="bad_manifest", size="small",
             note="Manifest missing required fields"),
    DropSpec("KNS-BOP", "BOP-ELEC-U30", "BOP Cable Trays", "ELECTRICAL", "RevA",
             defect="bad_units", size="small",
             note="Exported in inches into a millimetre pipeline"),
    DropSpec("KNS-TB2", "TB2-STEEL-U20", "Unit 20 Structural Steel", "STRUCTURAL", "RevC",
             defect="duplicate_tags", size="small",
             note="Duplicate tags - blocks reliable tag lookup in the viewer"),
    DropSpec("KNS-BOP", "BOP-EQUIP-U30", "BOP Equipment", "EQUIPMENT", "RevA",
             defect="thin_metadata", size="small",
             note="Attribute coverage below the publishing gate"),
    DropSpec("KNS-NI1", "NI1-ELEC-U10", "Unit 10 Cable Trays", "ELECTRICAL", "RevA",
             defect="bad_tag_format", size="small",
             note="Tags breaking the project naming standard - warns, still publishes"),
]

# A second revision of a model that will already be live, so the demo can
# show supersede-and-rollback against real published output.
FOLLOW_UP = DropSpec(
    "KNS-TB2", "TB2-PIPE-U20", "Unit 20 Piping", "PIPING", "RevB",
    size="small", note="New revision superseding a live model",
)

SIZE_PROFILES = {
    "small": {
        "PIPING": dict(lines=5, spools_per_line=5),
        "STRUCTURAL": dict(bays_x=4, bays_y=3, levels=2),
        "EQUIPMENT": dict(count=8),
        "ELECTRICAL": dict(runs=4),
        "HVAC": dict(runs=3, air_handlers=1),
    },
    "normal": {
        "PIPING": dict(lines=34, spools_per_line=16),
        "STRUCTURAL": dict(bays_x=16, bays_y=10, levels=6),
        "EQUIPMENT": dict(count=72),
        "ELECTRICAL": dict(runs=24),
        "HVAC": dict(runs=18, air_handlers=4),
    },
}


# =====================================================================
# Defect injection
# =====================================================================

def _apply_model_defect(model: plant.SynthModel, defect: str, rng: random.Random) -> None:
    """Defects that live inside the container contents."""
    if defect == "bad_units":
        model.unit_system = "in"

    elif defect == "duplicate_tags":
        geometry_bearing = [c for c in model.components if c["geometry_key"]]
        for victim in rng.sample(geometry_bearing, min(6, len(geometry_bearing))):
            twin = dict(victim)
            twin["tag"] = rng.choice(geometry_bearing)["tag"]
            model.components.append(twin)

    elif defect == "thin_metadata":
        for comp in model.components:
            for name in ("Material", "CommissioningSystem", "Area"):
                comp["attributes"].pop(name, None)

    elif defect == "bad_tag_format":
        targets = [c for c in model.components if c["geometry_key"]]
        for comp in rng.sample(targets, max(1, len(targets) // 5)):
            comp["tag"] = f"TEMP_{comp['tag'].replace('-', '_')}"

    elif defect == "flaky_converter":
        model.fault_injection = {"stage": "convert", "fail_attempts": 1,
                                 "error": "ConverterUnavailable"}


def _apply_file_defect(path: Path, defect: str) -> None:
    """Defects that damage the container itself."""
    if defect == "corrupt_archive":
        raw = path.read_bytes()
        path.write_bytes(raw[: int(len(raw) * 0.55)])

    elif defect == "bad_manifest":
        tmp = path.with_suffix(".tmp")
        with zipfile.ZipFile(path, "r") as src, \
             zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
            for item in src.infolist():
                data = src.read(item.filename)
                if item.filename == "manifest.json":
                    man = json.loads(data)
                    for field in ("coordinate_system", "discipline", "unit_system"):
                        man.pop(field, None)
                    data = json.dumps(man, indent=2).encode()
                dst.writestr(item, data)
        path.unlink()
        tmp.rename(path)


# =====================================================================
# Generation
# =====================================================================

def build_drop(spec: DropSpec, out_dir: Path, seed: int) -> Path:
    rng = random.Random(seed)
    random.seed(seed)  # plant.manifest() uses the module-level RNG for timestamps

    builder = plant.BUILDERS[spec.discipline]
    kwargs = SIZE_PROFILES[spec.size][spec.discipline]
    model = builder(rng, spec.project_code, spec.model_key, spec.model_name,
                    spec.revision, **kwargs)

    # Work packaging is applied to the finished model, then defects on top
    # of that. Order matters: thin_metadata strips attributes, and it has
    # to be able to strip packaging attributes too, otherwise the one
    # model meant to demonstrate incomplete data would arrive with a
    # perfect packaging hierarchy.
    plant.assign_work_packaging(model, rng)

    _apply_model_defect(model, spec.defect, rng)
    path = model.write(out_dir)
    _apply_file_defect(path, spec.defect)
    return path


def generate_dataset(inbox: Path, *, seed: int = 20260830,
                     include_follow_up: bool = True,
                     include_redrop: bool = True) -> list[dict]:
    """Write the full demo dataset into the inbox. Returns a summary."""
    inbox.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []

    for i, spec in enumerate(DROPS):
        path = build_drop(spec, inbox, seed + i)
        results.append({
            "file": path.name,
            "project": spec.project_code,
            "model": spec.model_key,
            "revision": spec.revision,
            "defect": spec.defect,
            "bytes": path.stat().st_size,
            "note": spec.note,
        })

    if include_follow_up:
        path = build_drop(FOLLOW_UP, inbox, seed + 500)
        results.append({
            "file": path.name, "project": FOLLOW_UP.project_code,
            "model": FOLLOW_UP.model_key, "revision": FOLLOW_UP.revision,
            "defect": "none", "bytes": path.stat().st_size, "note": FOLLOW_UP.note,
        })

    if include_redrop:
        # Byte-identical copy of an already-present file. Content hashing
        # in the intake stage should recognise it and skip the work.
        original = inbox / f"{DROPS[3].project_code}_{DROPS[3].model_key}_{DROPS[3].revision}.plantx"
        if original.exists():
            copy = inbox / f"{original.stem}__resent_by_engineering.plantx"
            shutil.copyfile(original, copy)
            results.append({
                "file": copy.name, "project": DROPS[3].project_code,
                "model": DROPS[3].model_key, "revision": DROPS[3].revision,
                "defect": "none", "bytes": copy.stat().st_size,
                "note": "Identical re-drop - must be recognised and skipped",
            })

    return results
