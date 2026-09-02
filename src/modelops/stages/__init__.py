"""The delivery pipeline, in order."""

from __future__ import annotations

from .base import JobContext, Stage, StageRunner
from .convert import ConvertStage
from .extract import ExtractStage
from .intake import IntakeStage
from .optimize import OptimizeStage
from .publish import PublishStage
from .qa import QaStage
from .validate import ValidateStage

PIPELINE: list[type[Stage]] = [
    IntakeStage,     # read the container through a format adapter
    ValidateStage,   # is this admissible at all?
    ExtractStage,    # tag tree and attributes into the catalog
    ConvertStage,    # produce the delivery format, unoptimised
    OptimizeStage,   # weld, de-duplicate, quantise, build the LOD ladder
    QaStage,         # is the output fit to publish?
    PublishStage,    # stage, verify, then atomically swap the live pointer
]

STAGE_NAMES = [cls.name for cls in PIPELINE]


def build_pipeline() -> list[Stage]:
    return [cls() for cls in PIPELINE]


__all__ = ["JobContext", "Stage", "StageRunner", "PIPELINE", "STAGE_NAMES",
           "build_pipeline"]
