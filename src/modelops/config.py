"""Configuration loading and path resolution.

Config lives in config/pipeline.json so that thresholds, SLAs, QA gates
and LOD targets are operational settings rather than code changes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_DEFAULT_CONFIG_NAME = "pipeline.json"


def project_root() -> Path:
    """Repository root (the directory containing config/ and src/)."""
    return Path(__file__).resolve().parents[2]


class Config:
    def __init__(self, data: dict[str, Any], root: Path, source: Path):
        self._data = data
        self.root = root
        self.source = source

    # -- generic access ----------------------------------------------------
    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def get(self, dotted: str, default: Any = None) -> Any:
        """Fetch a nested value with a dotted path, e.g. 'orchestrator.workers'."""
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def as_dict(self) -> dict[str, Any]:
        return self._data

    # -- paths -------------------------------------------------------------
    def path(self, key: str) -> Path:
        """Resolve a configured path relative to the project root."""
        raw = self.get(f"paths.{key}")
        if raw is None:
            raise KeyError(f"No configured path named {key!r}")
        p = Path(raw)
        return p if p.is_absolute() else (self.root / p)

    @property
    def pipeline_version(self) -> str:
        return self._data.get("pipeline_version", "0.0.0")

    def ensure_directories(self) -> None:
        for key in ("inbox", "work", "published", "quarantine", "outbox", "logs"):
            self.path(key).mkdir(parents=True, exist_ok=True)
        self.path("catalog_db").parent.mkdir(parents=True, exist_ok=True)


_cached: Config | None = None


def load_config(path: str | os.PathLike | None = None, *, reload: bool = False) -> Config:
    global _cached
    if _cached is not None and not reload and path is None:
        return _cached

    root = project_root()
    cfg_path = Path(path) if path else (root / "config" / _DEFAULT_CONFIG_NAME)
    if not cfg_path.exists():
        raise FileNotFoundError(f"Pipeline config not found: {cfg_path}")

    with cfg_path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    cfg = Config(data, root, cfg_path)
    if path is None:
        _cached = cfg
    return cfg
