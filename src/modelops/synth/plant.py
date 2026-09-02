"""Synthetic plant-model generator.

Why this exists
---------------
The real inputs to this kind of pipeline are proprietary design-tool
exports that cannot be shipped in a demo repository. To keep the POC
runnable end to end offline, it generates its own source exports in a
documented open container (`.plantx`) that carries the same three things
a real plant export carries:

  1. a manifest    - project, model, revision, units, coordinate system
  2. a tag tree    - hierarchy plus engineering attributes per component
  3. geometry      - unique shape definitions, instanced by transform

Point 3 is the one that matters technically. Real plant models repeat
the same valve, flange, bolt and beam thousands of times, carrying each
as an instance of a shared definition. Generating the data that way is
what makes the instancing work in the optimise stage a genuine
measurement rather than a staged number.

Models are generated from a fixed seed, so every run of the demo
produces byte-identical inputs and therefore comparable metrics.
"""

from __future__ import annotations

import json
import math
import random
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from .. import geometry as g

SCHEMA_VERSION = "1.0"
CONTAINER_EXT = ".plantx"

MATERIALS = ["CS-A106-B", "SS-316L", "CS-A53", "ALLOY-625", "CS-A234"]
SPECS = ["1CA1", "2CB3", "3SS1", "1CD2"]
SERVICES = ["Feedwater", "Main Steam", "Component Cooling", "Chilled Water",
            "Service Air", "Condensate", "Borated Water", "Fire Protection"]


# =====================================================================
# In-memory model being assembled
# =====================================================================

@dataclass
class SynthModel:
    project_code: str
    model_key: str
    model_name: str
    discipline: str
    source_tool: str
    revision_label: str
    unit_system: str = "mm"
    coordinate_system: str = "PLANT-GRID-NAD83"
    geometry: dict[str, g.Mesh] = field(default_factory=dict)
    components: list[dict] = field(default_factory=list)
    fault_injection: dict = field(default_factory=dict)

    # -- authoring helpers -------------------------------------------------
    def define(self, key: str, mesh: g.Mesh) -> str:
        """Register a unique geometry definition once; reuse by key."""
        if key not in self.geometry:
            self.geometry[key] = mesh
        return key

    def add(self, tag: str, *, parent_tag: str | None = None, category: str,
            geometry_key: str | None = None, transform: np.ndarray | None = None,
            attributes: dict | None = None, discipline: str | None = None) -> str:
        self.components.append({
            "tag": tag,
            "parent_tag": parent_tag,
            "category": category,
            "discipline": discipline or self.discipline,
            "geometry_key": geometry_key,
            "transform": (None if transform is None
                          else [round(float(x), 6) for x in np.asarray(transform).reshape(-1)]),
            "attributes": attributes or {},
        })
        return tag

    # -- statistics used by the demo report --------------------------------
    @property
    def instance_count(self) -> int:
        return sum(1 for c in self.components if c["geometry_key"])

    @property
    def instanced_triangles(self) -> int:
        return sum(self.geometry[c["geometry_key"]].triangle_count
                   for c in self.components if c["geometry_key"])

    @property
    def unique_triangles(self) -> int:
        return sum(m.triangle_count for m in self.geometry.values())

    # -- serialisation -----------------------------------------------------
    def manifest(self) -> dict:
        exported = datetime.now(timezone.utc) - timedelta(minutes=random.randint(5, 240))
        man = {
            "schema_version": SCHEMA_VERSION,
            "project_code": self.project_code,
            "model_key": self.model_key,
            "model_name": self.model_name,
            "discipline": self.discipline,
            "source_tool": self.source_tool,
            "revision_label": self.revision_label,
            "unit_system": self.unit_system,
            "coordinate_system": self.coordinate_system,
            "exported_at": exported.strftime("%Y-%m-%d %H:%M:%S"),
            "component_count": len(self.components),
            "geometry_definition_count": len(self.geometry),
        }
        if self.fault_injection:
            # Explicit, visible fault-injection hook. The pipeline honours
            # this so failure handling can be demonstrated on demand; it is
            # not hidden behaviour.
            man["_fault_injection"] = self.fault_injection
        return man

    def export_geometry(self) -> dict[str, g.Mesh]:
        """Degrade clean geometry into what a real export actually looks like.

        Two properties are reproduced deliberately, because a pipeline
        tuned against idealised input is tuned against nothing:

        * **Unwelded triangle soup.** Tessellating exporters emit three
          fresh vertices per triangle with no shared topology. This is
          the normal case, and it is why a welding step exists at all -
          a decimator has no edges to collapse until connectivity is
          rebuilt.

        * **Duplicated shells.** In a federated model the same part
          routinely appears twice, because two disciplines modelled it
          or an export covered an overlapping range. It is invisible on
          screen and paid for on every byte and every triangle.

        Roughly one definition in five gets a duplicate shell here.
        """
        out: dict[str, g.Mesh] = {}
        for index, (key, mesh) in enumerate(self.geometry.items()):
            soup_vertices = mesh.vertices[mesh.faces].reshape(-1, 3)
            soup_faces = np.arange(soup_vertices.shape[0]).reshape(-1, 3)
            if index % 5 == 0:
                soup_vertices = np.vstack([soup_vertices, soup_vertices])
                soup_faces = np.vstack([soup_faces, soup_faces + len(soup_faces) * 3])
            out[key] = g.Mesh(soup_vertices, soup_faces)
        return out

    def write(self, out_dir: Path) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{self.project_code}_{self.model_key}_{self.revision_label}{CONTAINER_EXT}"
        path = out_dir / filename

        obj_path = out_dir / f".{filename}.obj.tmp"
        g.write_obj(obj_path, self.export_geometry())
        obj_text = obj_path.read_text(encoding="utf-8")
        obj_path.unlink()

        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            z.writestr("manifest.json", json.dumps(self.manifest(), indent=2))
            z.writestr("components.json", json.dumps(
                {"schema_version": SCHEMA_VERSION, "components": self.components}, indent=1))
            z.writestr("geometry.obj", obj_text)
        return path


# =====================================================================
# Attribute authoring
# =====================================================================

def _piping_attrs(rng: random.Random, unit: str, area: str, system: str,
                  line: str, nps: int) -> dict:
    return {
        "System": system,
        "Area": area,
        "Unit": unit,
        "Material": rng.choice(MATERIALS),
        "CommissioningSystem": f"CS-{system}",
        "LineNumber": line,
        "PipingSpec": rng.choice(SPECS),
        "NPS": f'{nps}"',
        "Service": rng.choice(SERVICES),
        "Insulation": rng.choice(["NONE", "HC-50", "PP-25", "CC-40"]),
        "DesignPressure_barg": str(rng.choice([10, 16, 25, 40, 63])),
        "DesignTemp_C": str(rng.choice([60, 120, 200, 280, 350])),
        "WBS": f"{unit}.{area}.PIP",
        "ISOSheet": f"ISO-{line}-{rng.randint(1, 4):02d}",
        "WeightKg": f"{rng.uniform(5, 400):.1f}",
    }


def _structural_attrs(rng: random.Random, unit: str, area: str, section: str) -> dict:
    return {
        "System": "STRUCT",
        "Area": area,
        "Unit": unit,
        "Material": rng.choice(["A992-GR50", "A36", "A572-GR50"]),
        "CommissioningSystem": "CS-STRUCT",
        "Section": section,
        "SurfaceTreatment": rng.choice(["GALV", "PAINT-S1", "FIREPROOF-2H"]),
        "WBS": f"{unit}.{area}.STL",
        "WeightKg": f"{rng.uniform(80, 2400):.1f}",
        "ErectionSequence": str(rng.randint(1, 12)),
    }


def _equipment_attrs(rng: random.Random, unit: str, area: str,
                     system: str, eq_type: str) -> dict:
    return {
        "System": system,
        "Area": area,
        "Unit": unit,
        "Material": rng.choice(["SS-316L", "CS-A516-70", "ALLOY-625"]),
        "CommissioningSystem": f"CS-{system}",
        "EquipmentType": eq_type,
        "Manufacturer": rng.choice(["Flowserve", "Sulzer", "KSB", "Alfa Laval"]),
        "TagClass": "MAJOR",
        "MaintenanceStrategy": rng.choice(["RCM", "TIME-BASED", "RUN-TO-FAIL"]),
        "WBS": f"{unit}.{area}.EQP",
        "WeightKg": f"{rng.uniform(500, 18000):.1f}",
        "DesignCode": rng.choice(["ASME VIII Div 1", "ASME III Class 2", "API 610"]),
    }


def _electrical_attrs(rng: random.Random, unit: str, area: str) -> dict:
    return {
        "System": "ELEC",
        "Area": area,
        "Unit": unit,
        "Material": rng.choice(["GALV-STEEL", "ALUMINIUM", "SS-304"]),
        "CommissioningSystem": "CS-ELEC",
        "VoltageClass": rng.choice(["LV-400V", "MV-6.6kV", "CONTROL-110V"]),
        "TrayWidth_mm": str(rng.choice([300, 450, 600])),
        "FillPercent": f"{rng.uniform(15, 65):.0f}",
        "WBS": f"{unit}.{area}.ELE",
        "WeightKg": f"{rng.uniform(20, 180):.1f}",
    }


# =====================================================================
# Discipline builders
# =====================================================================

def build_piping_model(rng: random.Random, project: str, key: str, name: str,
                       revision: str, *, lines: int = 14, spools_per_line: int = 9) -> SynthModel:
    m = SynthModel(project, key, name, "PIPING", "SmartPlant Review export", revision)
    unit = key.split("-")[0] if "-" in key else "10"
    unit_no = "10"

    # --- shared geometry definitions (this is the instancing payoff) ----
    for nps in (4, 6, 10):
        r = nps * 12.7
        m.define(f"geom_pipe_{nps}in", g.cylinder(r, 3000, sections=20, capped=False))
        m.define(f"geom_elbow_{nps}in_90",
                 g.torus(r * 3, r, major_segments=14, minor_segments=12, sweep_degrees=90))
        m.define(f"geom_flange_{nps}in", g.cylinder(r * 1.9, 60, sections=24))
        m.define(f"geom_gatevalve_{nps}in", g.concat([
            g.box(r * 3, r * 3, r * 3.4),
            g.cylinder(r * 0.35, r * 4).transformed(g.translation(0, 0, r * 3)),
            g.torus(r * 1.5, r * 0.22, 18, 8).transformed(g.translation(0, 0, r * 5)),
        ]))
        m.define(f"geom_reducer_{nps}in", g.cone(r, 180, sections=20))
    m.define("geom_pipesupport", g.concat([
        g.box(160, 60, 400),
        g.box(300, 80, 40).transformed(g.translation(0, 0, 220)),
    ]))

    area_tag = m.add(f"{unit_no}-A-100", category="Area",
                     attributes={"System": "N/A", "Area": "100", "Unit": unit_no,
                                 "Material": "N/A", "CommissioningSystem": "N/A"})

    for li in range(lines):
        nps = rng.choice([4, 6, 10])
        r = nps * 12.7
        sys_no = 1200 + li
        system = f"SYS{sys_no}"
        line_no = f"{unit_no}-L-{sys_no}-{nps}IN-CS"

        sys_tag = m.add(f"{unit_no}-SYS-{sys_no}", parent_tag=area_tag, category="System",
                        attributes={"System": system, "Area": "100", "Unit": unit_no,
                                    "Material": "N/A", "CommissioningSystem": f"CS-{system}"})
        line_tag = m.add(line_no, parent_tag=sys_tag, category="Line",
                         attributes=_piping_attrs(rng, unit_no, "100", system, line_no, nps))

        # Route the line along +X at a per-line elevation, with a couple of
        # elbow direction changes, so the delivered model reads as a plant.
        x = rng.uniform(-2000, 2000)
        y = li * 2400.0 - lines * 1200.0
        z = rng.choice([2500.0, 5200.0, 7900.0, 10600.0])
        heading = 0.0

        for si in range(spools_per_line):
            item = 10 * (si + 1)
            attrs = _piping_attrs(rng, unit_no, "100", system, line_no, nps)

            # straight spool
            m.add(f"{unit_no}-P-{sys_no}{item}", parent_tag=line_tag, category="Pipe",
                  geometry_key=f"geom_pipe_{nps}in",
                  transform=g.compose(g.translation(x, y, z),
                                      g.rotation("z", heading),
                                      g.rotation("y", 90)),
                  attributes=attrs)
            step = 3000.0
            x += step * math.cos(math.radians(heading))
            y += step * math.sin(math.radians(heading))

            roll = rng.random()
            if roll < 0.30:
                m.add(f"{unit_no}-V-{sys_no}{item + 1}", parent_tag=line_tag, category="Valve",
                      geometry_key=f"geom_gatevalve_{nps}in",
                      transform=g.compose(g.translation(x, y, z), g.rotation("z", heading)),
                      attributes={**attrs, "ValveType": rng.choice(["GATE", "GLOBE", "CHECK"]),
                                  "FailPosition": rng.choice(["FC", "FO", "FL"])})
                for side in (-1, 1):
                    m.add(f"{unit_no}-F-{sys_no}{item + (2 if side < 0 else 3)}",
                          parent_tag=line_tag, category="Flange",
                          geometry_key=f"geom_flange_{nps}in",
                          transform=g.compose(
                              g.translation(x + side * r * 2.2 * math.cos(math.radians(heading)),
                                            y + side * r * 2.2 * math.sin(math.radians(heading)),
                                            z),
                              g.rotation("z", heading), g.rotation("y", 90)),
                          attributes={**attrs, "Rating": rng.choice(["150#", "300#", "600#"])})
            elif roll < 0.45:
                turn = rng.choice([-90.0, 90.0])
                m.add(f"{unit_no}-EL-{sys_no}{item + 4}", parent_tag=line_tag, category="Elbow",
                      geometry_key=f"geom_elbow_{nps}in_90",
                      transform=g.compose(g.translation(x, y, z),
                                          g.rotation("z", heading),
                                          g.rotation("x", 90 if turn > 0 else -90)),
                      attributes={**attrs, "BendRadius": "3D"})
                heading += turn
            elif roll < 0.55:
                m.add(f"{unit_no}-RD-{sys_no}{item + 5}", parent_tag=line_tag, category="Reducer",
                      geometry_key=f"geom_reducer_{nps}in",
                      transform=g.compose(g.translation(x, y, z),
                                          g.rotation("z", heading), g.rotation("y", 90)),
                      attributes=attrs)

            if si % 3 == 0:
                m.add(f"{unit_no}-PS-{sys_no}{item + 6}", parent_tag=line_tag,
                      category="PipeSupport", geometry_key="geom_pipesupport",
                      transform=g.translation(x, y, z - r - 220),
                      attributes={**attrs, "SupportType": rng.choice(["REST", "GUIDE", "ANCHOR"])})
    return m


def build_structural_model(rng: random.Random, project: str, key: str, name: str,
                           revision: str, *, bays_x: int = 9, bays_y: int = 6,
                           levels: int = 4) -> SynthModel:
    m = SynthModel(project, key, name, "STRUCTURAL", "Navisworks export", revision)
    unit_no = "20"

    m.define("geom_col_w14", g.i_beam(360, 360, 18, 28, 4200).transformed(g.rotation("y", 90)))
    m.define("geom_beam_w18", g.i_beam(460, 190, 12, 20, 7500))
    m.define("geom_beam_w12", g.i_beam(310, 165, 10, 16, 6000))
    m.define("geom_brace", g.cylinder(60, 5200, sections=12))
    m.define("geom_baseplate", g.box(600, 600, 45))
    m.define("geom_grating", g.box(7500, 6000, 40))

    area_tag = m.add(f"{unit_no}-A-200", category="Area",
                     attributes={"System": "STRUCT", "Area": "200", "Unit": unit_no,
                                 "Material": "N/A", "CommissioningSystem": "CS-STRUCT"})
    spacing_x, spacing_y, storey = 7500.0, 6000.0, 4200.0
    seq = 0

    for lv in range(levels):
        z0 = lv * storey
        lvl_tag = m.add(f"{unit_no}-SYS-{300 + lv}", parent_tag=area_tag, category="Level",
                        attributes={"System": "STRUCT", "Area": "200", "Unit": unit_no,
                                    "Material": "N/A", "CommissioningSystem": "CS-STRUCT",
                                    "Elevation_mm": f"{z0:.0f}"})
        for ix in range(bays_x):
            for iy in range(bays_y):
                x, y = ix * spacing_x, iy * spacing_y
                seq += 1
                m.add(f"{unit_no}-C-{1000 + seq}", parent_tag=lvl_tag, category="Column",
                      geometry_key="geom_col_w14",
                      transform=g.translation(x, y, z0 + storey / 2),
                      attributes=_structural_attrs(rng, unit_no, "200", "W14x90"))
                if lv == 0:
                    m.add(f"{unit_no}-BP-{1000 + seq}", parent_tag=lvl_tag, category="BasePlate",
                          geometry_key="geom_baseplate", transform=g.translation(x, y, 0),
                          attributes=_structural_attrs(rng, unit_no, "200", "PL600x600x45"))
                if ix < bays_x - 1:
                    m.add(f"{unit_no}-B-{2000 + seq}", parent_tag=lvl_tag, category="Beam",
                          geometry_key="geom_beam_w18",
                          transform=g.translation(x + spacing_x / 2, y, z0 + storey),
                          attributes=_structural_attrs(rng, unit_no, "200", "W18x50"))
                if iy < bays_y - 1:
                    m.add(f"{unit_no}-B-{3000 + seq}", parent_tag=lvl_tag, category="Beam",
                          geometry_key="geom_beam_w12",
                          transform=g.compose(
                              g.translation(x, y + spacing_y / 2, z0 + storey),
                              g.rotation("z", 90)),
                          attributes=_structural_attrs(rng, unit_no, "200", "W12x40"))
                if ix < bays_x - 1 and iy < bays_y - 1 and (ix + iy) % 4 == 0:
                    m.add(f"{unit_no}-GR-{4000 + seq}", parent_tag=lvl_tag, category="Grating",
                          geometry_key="geom_grating",
                          transform=g.translation(x + spacing_x / 2, y + spacing_y / 2,
                                                  z0 + storey - 30),
                          attributes=_structural_attrs(rng, unit_no, "200", "GRAT-40"))
                if lv < levels - 1 and (ix % 3 == 0) and (iy % 3 == 0):
                    m.add(f"{unit_no}-BR-{5000 + seq}", parent_tag=lvl_tag, category="Brace",
                          geometry_key="geom_brace",
                          transform=g.compose(g.translation(x + spacing_x / 2, y,
                                                            z0 + storey / 2),
                                              g.rotation("y", 55)),
                          attributes=_structural_attrs(rng, unit_no, "200", "HSS6x6"))
    return m


def build_equipment_model(rng: random.Random, project: str, key: str, name: str,
                          revision: str, *, count: int = 26) -> SynthModel:
    m = SynthModel(project, key, name, "EQUIPMENT", "SmartPlant Review export", revision)
    unit_no = "10"

    m.define("geom_vessel_shell", g.cylinder(1400, 6000, sections=32))
    m.define("geom_vessel_head", g.uv_sphere(1400, segments=32, rings=16))
    m.define("geom_pump_body", g.cylinder(420, 900, sections=24))
    m.define("geom_pump_motor", g.cylinder(320, 1100, sections=20))
    m.define("geom_skid", g.box(2600, 1600, 220))
    m.define("geom_hx_shell", g.cylinder(700, 4500, sections=24))
    m.define("geom_nozzle", g.cylinder(160, 500, sections=14))
    m.define("geom_saddle", g.box(400, 1800, 700))

    area_tag = m.add(f"{unit_no}-A-100", category="Area",
                     attributes={"System": "MECH", "Area": "100", "Unit": unit_no,
                                 "Material": "N/A", "CommissioningSystem": "CS-MECH"})

    for i in range(count):
        x, y = (i % 6) * 9000.0, (i // 6) * 11000.0
        kind = ["VESSEL", "PUMP", "HEAT_EXCHANGER"][i % 3]
        eq_no = 4000 + i
        system = f"SYS{1200 + (i % 8)}"
        attrs = _equipment_attrs(rng, unit_no, "100", system, kind)

        if kind == "VESSEL":
            tag = m.add(f"{unit_no}-VES-{eq_no}", parent_tag=area_tag, category="Vessel",
                        geometry_key="geom_vessel_shell",
                        transform=g.translation(x, y, 4000), attributes=attrs)
            for sign in (-1, 1):
                m.add(f"{unit_no}-VH-{eq_no}{1 if sign < 0 else 2}", parent_tag=tag,
                      category="VesselHead", geometry_key="geom_vessel_head",
                      transform=g.translation(x, y, 4000 + sign * 3000), attributes=attrs)
            for n in range(4):
                ang = n * 90.0
                m.add(f"{unit_no}-NZ-{eq_no}{3 + n}", parent_tag=tag, category="Nozzle",
                      geometry_key="geom_nozzle",
                      transform=g.compose(
                          g.translation(x + 1500 * math.cos(math.radians(ang)),
                                        y + 1500 * math.sin(math.radians(ang)), 4000 + n * 900),
                          g.rotation("y", 90), g.rotation("z", ang)),
                      attributes={**attrs, "NozzleSize": f'{rng.choice([4, 6, 8])}"'})
            for sign in (-1, 1):
                m.add(f"{unit_no}-SD-{eq_no}{7 if sign < 0 else 8}", parent_tag=tag,
                      category="Saddle", geometry_key="geom_saddle",
                      transform=g.translation(x + sign * 1800, y, 350), attributes=attrs)

        elif kind == "PUMP":
            tag = m.add(f"{unit_no}-PMP-{eq_no}", parent_tag=area_tag, category="Pump",
                        geometry_key="geom_pump_body",
                        transform=g.compose(g.translation(x, y, 700), g.rotation("y", 90)),
                        attributes={**attrs, "Duty": rng.choice(["DUTY", "STANDBY"]),
                                    "Flow_m3h": f"{rng.uniform(20, 900):.0f}"})
            m.add(f"{unit_no}-MTR-{eq_no}1", parent_tag=tag, category="Motor",
                  geometry_key="geom_pump_motor",
                  transform=g.compose(g.translation(x + 1200, y, 700), g.rotation("y", 90)),
                  attributes={**attrs, "Power_kW": f"{rng.choice([15, 37, 75, 160])}"})
            m.add(f"{unit_no}-SK-{eq_no}2", parent_tag=tag, category="Skid",
                  geometry_key="geom_skid", transform=g.translation(x + 500, y, 110),
                  attributes=attrs)

        else:
            tag = m.add(f"{unit_no}-HX-{eq_no}", parent_tag=area_tag, category="HeatExchanger",
                        geometry_key="geom_hx_shell",
                        transform=g.compose(g.translation(x, y, 2200), g.rotation("y", 90)),
                        attributes={**attrs, "Duty_kW": f"{rng.uniform(200, 4000):.0f}"})
            for n in range(4):
                m.add(f"{unit_no}-NZ-{eq_no}{n + 1}", parent_tag=tag, category="Nozzle",
                      geometry_key="geom_nozzle",
                      transform=g.translation(x - 1800 + n * 1200, y, 3000),
                      attributes=attrs)
            for sign in (-1, 1):
                m.add(f"{unit_no}-SD-{eq_no}{5 if sign < 0 else 6}", parent_tag=tag,
                      category="Saddle", geometry_key="geom_saddle",
                      transform=g.compose(g.translation(x + sign * 1600, y, 350),
                                          g.rotation("z", 90)),
                      attributes=attrs)
    return m


def build_electrical_model(rng: random.Random, project: str, key: str, name: str,
                           revision: str, *, runs: int = 10) -> SynthModel:
    m = SynthModel(project, key, name, "ELECTRICAL", "Navisworks export", revision)
    unit_no = "30"

    m.define("geom_tray_600", g.channel(600, 150, 8, 6000))
    m.define("geom_tray_300", g.channel(300, 100, 6, 6000))
    m.define("geom_tray_bend", g.torus(900, 90, 12, 8, sweep_degrees=90))
    m.define("geom_jbox", g.box(400, 300, 200))
    m.define("geom_conduit", g.cylinder(40, 3000, sections=10, capped=False))
    m.define("geom_tray_support", g.concat([
        g.box(80, 80, 900), g.box(700, 80, 60).transformed(g.translation(0, 0, 470)),
    ]))

    area_tag = m.add(f"{unit_no}-A-300", category="Area",
                     attributes={"System": "ELEC", "Area": "300", "Unit": unit_no,
                                 "Material": "N/A", "CommissioningSystem": "CS-ELEC"})

    for r_i in range(runs):
        y = r_i * 3500.0
        z = 6500.0 if r_i % 2 else 8200.0
        width = 600 if r_i % 3 else 300
        run_tag = m.add(f"{unit_no}-SYS-{500 + r_i}", parent_tag=area_tag, category="CableRun",
                        attributes=_electrical_attrs(rng, unit_no, "300"))
        for s in range(12):
            x = s * 6000.0
            attrs = _electrical_attrs(rng, unit_no, "300")
            m.add(f"{unit_no}-CT-{5000 + r_i * 100 + s}", parent_tag=run_tag,
                  category="CableTray", geometry_key=f"geom_tray_{width}",
                  transform=g.translation(x, y, z), attributes=attrs)
            if s % 2 == 0:
                m.add(f"{unit_no}-TS-{6000 + r_i * 100 + s}", parent_tag=run_tag,
                      category="TraySupport", geometry_key="geom_tray_support",
                      transform=g.translation(x, y, z - 500), attributes=attrs)
            if s % 5 == 4:
                m.add(f"{unit_no}-JB-{7000 + r_i * 100 + s}", parent_tag=run_tag,
                      category="JunctionBox", geometry_key="geom_jbox",
                      transform=g.translation(x, y + 500, z - 200), attributes=attrs)
                m.add(f"{unit_no}-CD-{8000 + r_i * 100 + s}", parent_tag=run_tag,
                      category="Conduit", geometry_key="geom_conduit",
                      transform=g.compose(g.translation(x, y + 500, z - 1700),
                                          g.rotation("x", 0)),
                      attributes=attrs)
    return m


BUILDERS = {
    "PIPING": build_piping_model,
    "STRUCTURAL": build_structural_model,
    "EQUIPMENT": build_equipment_model,
    "ELECTRICAL": build_electrical_model,
}
