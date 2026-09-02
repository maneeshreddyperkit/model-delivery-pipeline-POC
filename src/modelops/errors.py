"""Error taxonomy for the pipeline.

Every failure is classified two ways:

  error_class - what specifically went wrong, used for the failure
                taxonomy report so recurring problems are visible
                instead of being buried in free-text logs.

  error_kind  - TRANSIENT or PERMANENT, which is what the orchestrator
                uses to decide between retry-with-backoff and
                quarantine. Retrying a corrupt source file forever is
                the classic way an automated pipeline turns one bad
                model into a stuck queue.
"""

from __future__ import annotations

TRANSIENT = "TRANSIENT"
PERMANENT = "PERMANENT"


class PipelineError(Exception):
    """Base class for all classified pipeline failures."""

    error_class = "PipelineError"
    error_kind = PERMANENT

    def __init__(self, message: str, *, detail: dict | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    def as_dict(self) -> dict:
        return {
            "error_class": self.error_class,
            "error_kind": self.error_kind,
            "message": self.message,
            "detail": self.detail,
        }


# --- Permanent: the input itself is wrong. Retrying cannot help. ------------

class SourceCorrupt(PipelineError):
    """Container is unreadable - truncated upload, bad zip, wrong format."""
    error_class = "SourceCorrupt"
    error_kind = PERMANENT


class SourceUnsupported(PipelineError):
    """No adapter registered for this file extension."""
    error_class = "SourceUnsupported"
    error_kind = PERMANENT


class ValidationFailed(PipelineError):
    """Manifest or content failed schema / business rule validation."""
    error_class = "ValidationFailed"
    error_kind = PERMANENT


class GeometryInvalid(PipelineError):
    """Geometry payload could not be parsed into usable meshes."""
    error_class = "GeometryInvalid"
    error_kind = PERMANENT


class QaGateFailed(PipelineError):
    """Model converted, but failed a quality gate. Must not reach users."""
    error_class = "QaGateFailed"
    error_kind = PERMANENT


# --- Transient: environment misbehaved. Retrying is reasonable. -------------

class ConverterTimeout(PipelineError):
    """Conversion exceeded the stage timeout."""
    error_class = "ConverterTimeout"
    error_kind = TRANSIENT


class ConverterUnavailable(PipelineError):
    """External converter / licence server not reachable."""
    error_class = "ConverterUnavailable"
    error_kind = TRANSIENT


class StorageUnavailable(PipelineError):
    """Delivery share or work directory not writable right now."""
    error_class = "StorageUnavailable"
    error_kind = TRANSIENT


class PublishConflict(PipelineError):
    """Another writer touched the delivery root mid-publish."""
    error_class = "PublishConflict"
    error_kind = TRANSIENT


def classify(exc: BaseException) -> tuple[str, str, str]:
    """Return (error_class, error_kind, message) for any exception.

    Unclassified exceptions are treated as TRANSIENT on purpose: an
    unexpected crash is more often an environment hiccup than bad data,
    and the max-attempt ceiling still stops runaway retries.
    """
    if isinstance(exc, PipelineError):
        return exc.error_class, exc.error_kind, exc.message
    if isinstance(exc, (TimeoutError,)):
        return "ConverterTimeout", TRANSIENT, str(exc)
    if isinstance(exc, (PermissionError, OSError)):
        return "StorageUnavailable", TRANSIENT, str(exc)
    return type(exc).__name__, TRANSIENT, str(exc)
