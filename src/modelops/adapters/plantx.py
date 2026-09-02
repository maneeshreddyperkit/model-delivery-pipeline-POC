"""Adapter for the open `.plantx` container used by this POC.

Container layout (a plain zip):

    manifest.json    project / model / revision / units / coordinate system
    components.json  tag hierarchy, engineering attributes, geometry refs
    geometry.obj     unique geometry definitions as named OBJ groups

Failure handling is the point of most of this file. A model-delivery
pipeline is judged on what it does with bad input, so every way this can
go wrong is turned into a classified `PipelineError` with enough context
to act on, rather than a raw zipfile or JSON traceback.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from ..errors import GeometryInvalid, SourceCorrupt, ValidationFailed
from ..geometry import parse_obj
from . import SourceAdapter, SourceComponent, SourceModel

REQUIRED_MEMBERS = ("manifest.json", "components.json", "geometry.obj")


class PlantXAdapter(SourceAdapter):
    name = "PlantX (open POC container)"
    extensions = (".plantx",)
    external_tool = None
    implemented = True

    def read(self, path: Path) -> SourceModel:
        path = Path(path)

        try:
            archive = zipfile.ZipFile(path, "r")
        except zipfile.BadZipFile as exc:
            raise SourceCorrupt(
                f"{path.name} is not a readable archive - most likely a "
                f"truncated or interrupted upload.",
                detail={"file": path.name, "bytes": path.stat().st_size,
                        "underlying": str(exc)},
            ) from exc

        with archive:
            names = set(archive.namelist())
            missing = [m for m in REQUIRED_MEMBERS if m not in names]
            if missing:
                raise SourceCorrupt(
                    f"{path.name} is missing required container members: "
                    f"{', '.join(missing)}",
                    detail={"file": path.name, "missing": missing,
                            "present": sorted(names)},
                )

            # A damaged member can survive the directory listing and only
            # fail on read, so surface that as corruption too.
            try:
                bad = archive.testzip()
            except Exception as exc:
                raise SourceCorrupt(
                    f"{path.name} failed integrity verification: {exc}",
                    detail={"file": path.name},
                ) from exc
            if bad is not None:
                raise SourceCorrupt(
                    f"{path.name} contains a corrupt member: {bad}",
                    detail={"file": path.name, "member": bad},
                )

            manifest = self._read_json(archive, "manifest.json", path)
            components_doc = self._read_json(archive, "components.json", path)

            try:
                obj_text = archive.read("geometry.obj").decode("utf-8")
            except (UnicodeDecodeError, zipfile.BadZipFile, EOFError) as exc:
                raise SourceCorrupt(
                    f"geometry.obj in {path.name} could not be read: {exc}",
                    detail={"file": path.name},
                ) from exc

        try:
            geometry = parse_obj(obj_text)
        except ValueError as exc:
            raise GeometryInvalid(
                f"geometry.obj in {path.name} is malformed: {exc}",
                detail={"file": path.name},
            ) from exc

        raw_components = components_doc.get("components")
        if not isinstance(raw_components, list):
            raise ValidationFailed(
                f"components.json in {path.name} has no 'components' array.",
                detail={"file": path.name, "keys": sorted(components_doc)},
            )

        components: list[SourceComponent] = []
        for i, raw in enumerate(raw_components):
            if not isinstance(raw, dict) or not raw.get("tag"):
                raise ValidationFailed(
                    f"components.json entry {i} in {path.name} has no tag.",
                    detail={"file": path.name, "index": i},
                )
            transform = raw.get("transform")
            if transform is not None and len(transform) != 16:
                raise ValidationFailed(
                    f"Component {raw['tag']} has a {len(transform)}-element "
                    f"transform; a 4x4 matrix has 16.",
                    detail={"file": path.name, "tag": raw["tag"]},
                )
            components.append(SourceComponent(
                tag=str(raw["tag"]),
                parent_tag=raw.get("parent_tag"),
                category=raw.get("category") or "Unknown",
                discipline=raw.get("discipline") or manifest.get("discipline") or "UNKNOWN",
                geometry_key=raw.get("geometry_key"),
                transform=transform,
                attributes={str(k): ("" if v is None else str(v))
                            for k, v in (raw.get("attributes") or {}).items()},
            ))

        return SourceModel(manifest=manifest, components=components,
                           geometry=geometry, adapter_name=self.name)

    def peek(self, path: Path) -> dict:
        try:
            with zipfile.ZipFile(path, "r") as archive:
                doc = json.loads(archive.read("manifest.json"))
            return doc if isinstance(doc, dict) else {}
        except Exception:
            # Intake falls back to the filename convention. The real read
            # will raise a properly classified error moments later.
            return {}

    @staticmethod
    def _read_json(archive: zipfile.ZipFile, member: str, path: Path) -> dict:
        try:
            raw = archive.read(member)
        except (zipfile.BadZipFile, EOFError, RuntimeError) as exc:
            raise SourceCorrupt(
                f"{member} in {path.name} could not be extracted: {exc}",
                detail={"file": path.name, "member": member},
            ) from exc
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SourceCorrupt(
                f"{member} in {path.name} is not valid JSON "
                f"(line {exc.lineno}, column {exc.colno}).",
                detail={"file": path.name, "member": member, "error": exc.msg},
            ) from exc
        if not isinstance(doc, dict):
            raise ValidationFailed(
                f"{member} in {path.name} must contain a JSON object.",
                detail={"file": path.name, "member": member},
            )
        return doc
