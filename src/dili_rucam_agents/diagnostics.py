from __future__ import annotations

import re


_SAFE_TYPE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")


class SafeExecutionDiagnostic(str):
    """Marker for diagnostics created without reading exception message text."""


def format_execution_error(exc: BaseException) -> SafeExecutionDiagnostic:
    """Return a bounded diagnostic without consulting the exception message."""

    exception_type = type(exc).__name__
    if not _SAFE_TYPE_RE.fullmatch(exception_type):
        exception_type = "Exception"
    markers = [f"type={exception_type}"]
    status_code = _safe_integer_attribute(exc, "status_code", minimum=100, maximum=599)
    if status_code is not None:
        markers.append(f"status_code={status_code}")
    errno = _safe_integer_attribute(exc, "errno", minimum=-9999, maximum=9999)
    if errno is not None:
        markers.append(f"errno={errno}")
    return SafeExecutionDiagnostic(f"Execution error [{'; '.join(markers)}]")


def sanitize_execution_diagnostic(value: str) -> SafeExecutionDiagnostic:
    if isinstance(value, SafeExecutionDiagnostic):
        return value
    return format_execution_error(Exception())


def _safe_integer_attribute(
    exc: BaseException,
    attribute: str,
    *,
    minimum: int,
    maximum: int,
) -> int | None:
    try:
        value = getattr(exc, attribute, None)
    except Exception:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if minimum <= value <= maximum else None


__all__ = [
    "SafeExecutionDiagnostic",
    "format_execution_error",
    "sanitize_execution_diagnostic",
]
