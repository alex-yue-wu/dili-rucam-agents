from __future__ import annotations

import re
from typing import Literal, cast


_SAFE_TYPE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_FAILURE_KINDS = frozenset(("validation", "execution"))
FailureKind = Literal["validation", "execution"]


def validate_failure_kind(value: object) -> FailureKind:
    if not isinstance(value, str) or value not in _FAILURE_KINDS:
        raise ValueError("failure_kind must be 'validation' or 'execution'")
    return cast(FailureKind, value)


def format_execution_error(exc: BaseException) -> str:
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
    return f"Execution error [{'; '.join(markers)}]"


def canonicalize_execution_error(exc: object | None) -> str:
    source = exc if isinstance(exc, BaseException) else Exception()
    return format_execution_error(source)


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
    return int(value) if minimum <= value <= maximum else None


__all__ = [
    "FailureKind",
    "canonicalize_execution_error",
    "format_execution_error",
    "validate_failure_kind",
]
