"""Source-format adapters.

The pipeline never hard-codes a source format. Everything downstream of
intake works against `SourceModel`, and each input format supplies an
adapter that produces one. Onboarding a new export format is then a new
adapter plus a registry entry, not a change to the pipeline.

Only the open `.plantx` container used by this POC is implemented.
Adapters for the real proprietary formats are registered as explicit
stubs rather than omitted, so the extension point and the integration
boundary are both visible: each one names the external converter it
would shell out to, and fails with a clear, classified error instead of
an obscure one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..errors import SourceUnsupported
from ..geometry import Mesh


@dataclass
class SourceComponent:
    tag: str
    parent_tag: str | None
    category: str
    discipline: str
    geometry_key: str | None
    transform: list[float] | None
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass
class SourceModel:
    """Format-neutral representation every adapter must produce."""
    manifest: dict
    components: list[SourceComponent]
    geometry: dict[str, Mesh]
    adapter_name: str

    @property
    def fault_injection(self) -> dict:
        return self.manifest.get("_fault_injection") or {}


class SourceAdapter:
    name: str = "abstract"
    extensions: tuple[str, ...] = ()
    external_tool: str | None = None
    implemented: bool = False

    def read(self, path: Path) -> SourceModel:
        raise NotImplementedError

    def peek(self, path: Path) -> dict:
        """Cheaply read just the manifest, for intake routing.

        Must never raise: intake has to be able to file a damaged model
        against *some* project so the failure is visible and owned,
        rather than silently ignoring the drop. Returns {} if the
        manifest cannot be read.
        """
        return {}


class UnimplementedAdapter(SourceAdapter):
    """Registered but not wired up: names the converter it would drive."""

    def __init__(self, name: str, extensions: tuple[str, ...], external_tool: str):
        self.name = name
        self.extensions = extensions
        self.external_tool = external_tool
        self.implemented = False

    def read(self, path: Path) -> SourceModel:
        raise SourceUnsupported(
            f"No converter configured for {self.name} ({', '.join(self.extensions)}). "
            f"This format is produced by {self.external_tool}; in a production "
            f"deployment this adapter would drive that tool's batch interface "
            f"and hand the result back as a SourceModel.",
            detail={"format": self.name, "file": path.name,
                    "external_tool": self.external_tool},
        )


_REGISTRY: dict[str, SourceAdapter] = {}


def register(adapter: SourceAdapter) -> SourceAdapter:
    for ext in adapter.extensions:
        _REGISTRY[ext.lower()] = adapter
    return adapter


def adapter_for(path: str | Path) -> SourceAdapter:
    ext = Path(path).suffix.lower()
    adapter = _REGISTRY.get(ext)
    if adapter is None:
        raise SourceUnsupported(
            f"Unrecognised model format {ext!r}. "
            f"Registered formats: {', '.join(sorted(_REGISTRY))}",
            detail={"extension": ext, "file": Path(path).name},
        )
    return adapter


def registered() -> dict[str, SourceAdapter]:
    return dict(_REGISTRY)


# --- registration -----------------------------------------------------
from .plantx import PlantXAdapter  # noqa: E402  (import after registry exists)

register(PlantXAdapter())

register(UnimplementedAdapter(
    "Navisworks", (".nwd", ".nwc", ".nwf"),
    "Autodesk Navisworks (file exporters / NWCreate batch utility)"))
register(UnimplementedAdapter(
    "SmartPlant Review", (".vue", ".svf"),
    "Hexagon SmartPlant Review batch publisher"))
register(UnimplementedAdapter(
    "MicroStation", (".dgn",),
    "Bentley MicroStation / iTwin conversion services"))
register(UnimplementedAdapter(
    "IFC", (".ifc", ".ifczip"),
    "buildingSMART IFC toolchain"))
register(UnimplementedAdapter(
    "Revit", (".rvt",),
    "Autodesk Revit export pipeline"))
