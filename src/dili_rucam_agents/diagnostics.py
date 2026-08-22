from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal, cast


_SAFE_TYPE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_FAILURE_KINDS = frozenset(("validation", "execution"))
FailureKind = Literal["validation", "execution"]


@dataclass(frozen=True)
class SafeExecutionDiagnostic:
    """Serializable execution metadata with no provider-controlled text."""

    exception_type: str
    status_code: int | None = None
    errno: int | None = None

    def __post_init__(self) -> None:
        if type(self.exception_type) is not str or not _SAFE_TYPE_RE.fullmatch(
            self.exception_type
        ):
            raise ValueError("exception_type must be a safe Python identifier")
        _validate_optional_integer(
            self.status_code,
            name="status_code",
            minimum=100,
            maximum=599,
        )
        _validate_optional_integer(
            self.errno,
            name="errno",
            minimum=-9999,
            maximum=9999,
        )


def validate_failure_kind(value: object) -> FailureKind:
    if not isinstance(value, str) or value not in _FAILURE_KINDS:
        raise ValueError("failure_kind must be 'validation' or 'execution'")
    return cast(FailureKind, value)


def format_execution_error(exc: BaseException) -> str:
    """Return a bounded diagnostic without consulting the exception message."""

    return render_execution_diagnostic(build_execution_diagnostic(exc))


def build_execution_diagnostic(exc: BaseException) -> SafeExecutionDiagnostic:
    """Extract allowlisted execution metadata without consulting exception text."""

    try:
        exception_type = type(exc).__name__
        if type(exception_type) is not str or not _SAFE_TYPE_RE.fullmatch(
            exception_type
        ):
            return SafeExecutionDiagnostic("Exception")
        return SafeExecutionDiagnostic(
            exception_type=exception_type,
            status_code=_safe_integer_attribute(
                exc, "status_code", minimum=100, maximum=599
            ),
            errno=_safe_integer_attribute(exc, "errno", minimum=-9999, maximum=9999),
        )
    except BaseException:
        return SafeExecutionDiagnostic("Exception")


def validate_execution_diagnostic(
    diagnostic: object | None,
) -> SafeExecutionDiagnostic:
    """Independently validate structured metadata at a trust boundary."""

    if diagnostic is None:
        return SafeExecutionDiagnostic(exception_type="Exception")
    if type(diagnostic) is not SafeExecutionDiagnostic:
        raise ValueError("execution_diagnostic must be a SafeExecutionDiagnostic")
    return SafeExecutionDiagnostic(
        exception_type=diagnostic.exception_type,
        status_code=diagnostic.status_code,
        errno=diagnostic.errno,
    )


def render_execution_diagnostic(diagnostic: object | None) -> str:
    """Validate and render structured metadata using fixed field labels."""

    validated = validate_execution_diagnostic(diagnostic)
    markers = [f"type={validated.exception_type}"]
    if validated.status_code is not None:
        markers.append(f"status_code={validated.status_code}")
    if validated.errno is not None:
        markers.append(f"errno={validated.errno}")
    return f"Execution error [{'; '.join(markers)}]"


def canonicalize_execution_error(exc: object | None) -> str:
    source = exc if isinstance(exc, BaseException) else Exception()
    return format_execution_error(source)


def _validate_optional_integer(
    value: object,
    *,
    name: str,
    minimum: int,
    maximum: int,
) -> None:
    if value is None:
        return
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} through {maximum}")


def _safe_integer_attribute(
    exc: BaseException,
    attribute: str,
    *,
    minimum: int,
    maximum: int,
) -> int | None:
    value = getattr(exc, attribute, None)
    if value is None:
        return None
    if type(value) is not int:
        raise ValueError("unsafe numeric diagnostic value")
    return value if minimum <= value <= maximum else None


__all__ = [
    "FailureKind",
    "SafeExecutionDiagnostic",
    "build_execution_diagnostic",
    "canonicalize_execution_error",
    "format_execution_error",
    "render_execution_diagnostic",
    "validate_execution_diagnostic",
    "validate_failure_kind",
]
