from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import re
from typing import Literal, cast


_SAFE_TYPE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_FAILURE_KINDS = frozenset(("validation", "execution", "truncation", "terminal"))
FailureKind = Literal["validation", "execution", "truncation", "terminal"]

# The first block names the legacy three-section report and its Section C JSON;
# the rest is every path of the RucamAssessment object, with list indices
# dropped (``course.evidence.0.quote`` is ``course.evidence.quote``).
_VALIDATION_FIELD_PATHS = frozenset(
    (
        "report",
        "SECTION_A",
        "SECTION_B",
        "SECTION_C",
        "injury_pattern",
        "R_ratio",
        "rucam_scores",
        "rucam_scores.time_to_onset",
        "rucam_scores.course",
        "rucam_scores.risk_factors",
        "rucam_scores.concomitant_drugs",
        "rucam_scores.other_causes_excluded",
        "rucam_scores.known_hepatotoxicity",
        "rucam_scores.rechallenge",
        "r_ratio",
        "r_ratio_basis",
        "r_ratio_basis.alt",
        "r_ratio_basis.alp",
        "r_ratio_basis.alt_uln",
        "r_ratio_basis.alp_uln",
        "onset_day",
        "onset_anchor_dose",
        "pre_flight",
        "pre_flight.suspect_drug",
        "pre_flight.onset",
        "pre_flight.r_ratio_pattern",
        "pre_flight.course_basis",
        "pre_flight.g1_count",
        "pre_flight.item_6_source",
        "pre_flight.scores_in_legal_sets",
        "pre_flight.total_sum",
        "pre_flight.category_match",
        "pre_flight.section_b_matches_section_c",
        "pre_flight.notes_complete",
        "item_5_causes",
        "item_5_causes.hepatitis_a",
        "item_5_causes.hepatitis_b",
        "item_5_causes.hepatitis_c",
        "item_5_causes.biliary_obstruction",
        "item_5_causes.alcoholism",
        "item_5_causes.hypotension_or_ischaemia",
        "item_5_causes.underlying_disease",
        "item_5_causes.acute_viral_infection",
        "item_5_causes.g1",
        "time_to_onset",
        "time_to_onset.score",
        "time_to_onset.data_status",
        "time_to_onset.rationale",
        "time_to_onset.inference_note",
        "time_to_onset.evidence",
        "time_to_onset.evidence.quote",
        "time_to_onset.evidence.verified",
        "course",
        "course.score",
        "course.data_status",
        "course.rationale",
        "course.inference_note",
        "course.evidence",
        "course.evidence.quote",
        "course.evidence.verified",
        "risk_factors",
        "risk_factors.score",
        "risk_factors.data_status",
        "risk_factors.rationale",
        "risk_factors.inference_note",
        "risk_factors.evidence",
        "risk_factors.evidence.quote",
        "risk_factors.evidence.verified",
        "concomitant_drugs",
        "concomitant_drugs.score",
        "concomitant_drugs.data_status",
        "concomitant_drugs.rationale",
        "concomitant_drugs.inference_note",
        "concomitant_drugs.evidence",
        "concomitant_drugs.evidence.quote",
        "concomitant_drugs.evidence.verified",
        "other_causes_excluded",
        "other_causes_excluded.score",
        "other_causes_excluded.data_status",
        "other_causes_excluded.rationale",
        "other_causes_excluded.inference_note",
        "other_causes_excluded.evidence",
        "other_causes_excluded.evidence.quote",
        "other_causes_excluded.evidence.verified",
        "known_hepatotoxicity",
        "known_hepatotoxicity.score",
        "known_hepatotoxicity.data_status",
        "known_hepatotoxicity.rationale",
        "known_hepatotoxicity.inference_note",
        "known_hepatotoxicity.evidence",
        "known_hepatotoxicity.evidence.quote",
        "known_hepatotoxicity.evidence.verified",
        "rechallenge",
        "rechallenge.score",
        "rechallenge.data_status",
        "rechallenge.rationale",
        "rechallenge.inference_note",
        "rechallenge.evidence",
        "rechallenge.evidence.quote",
        "rechallenge.evidence.verified",
        "total_score",
        "category",
    )
)
_VALIDATION_ISSUE_MESSAGES = {
    "invalid_report": "Report is invalid.",
    "report_empty": "Report is empty.",
    "summary_placeholder": "Report is a summary placeholder.",
    "missing_section": "Missing {field}.",
    "section_heading_level": (
        "{field} was found but its heading is not a level-2 Markdown heading. "
        "Start the line with exactly two hash marks, for example "
        "'## {field} — ...'. Do not use '###', '#', or bold text alone."
    ),
    "duplicate_section": "Report must contain exactly one {field}.",
    "section_order": "Sections must appear in A, B, C order.",
    "empty_section": "{field} is empty.",
    "report_truncated": (
        "Report stops partway through: the completion was cut off before "
        "SECTION A, SECTION B and SECTION C were all finished. Return the "
        "whole report in one reply, and keep SECTION A compact so the "
        "scoring table and the JSON both fit."
    ),
    "json_fence_count": ("SECTION C must contain exactly one fenced JSON object."),
    "invalid_json": "Invalid SECTION C JSON.",
    "json_not_object": "SECTION C JSON must be an object.",
    "missing_json_object": "Unable to locate a complete JSON object.",
    "field_required": "{field}: Field required.",
    "invalid_integer": "{field}: Input should be a valid integer.",
    "integer_too_small": "{field}: Integer is below the allowed minimum.",
    "integer_too_large": "{field}: Integer exceeds the allowed maximum.",
    "invalid_number": "{field}: Input should be a valid number.",
    "number_too_small": "{field}: Number is below the allowed minimum.",
    "invalid_literal": "{field}: Input is not an allowed value.",
    "score_sum_mismatch": "total_score does not match the seven-item score sum.",
    "category_mismatch": "category does not match total_score.",
    "invalid_value": "{field}: Value is invalid.",
}


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


@dataclass(frozen=True)
class SafeValidationDiagnostic:
    """Serializable validation issues containing only allowlisted metadata."""

    issues: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if type(self.issues) is not tuple or not self.issues or len(self.issues) > 16:
            raise ValueError("issues must contain from 1 through 16 validation issues")
        for issue in self.issues:
            if type(issue) is not tuple or len(issue) != 2:
                raise ValueError("each validation issue must be a field/code pair")
            field_path, issue_code = issue
            if type(field_path) is not str or field_path not in _VALIDATION_FIELD_PATHS:
                raise ValueError("validation field path is not allowed")
            if (
                type(issue_code) is not str
                or issue_code not in _VALIDATION_ISSUE_MESSAGES
            ):
                raise ValueError("validation issue code is not allowed")


def validate_validation_diagnostic(diagnostic: object) -> SafeValidationDiagnostic:
    """Independently validate structured validation metadata at a boundary."""

    if type(diagnostic) is not SafeValidationDiagnostic:
        raise ValueError("validation_diagnostic must be a SafeValidationDiagnostic")
    return SafeValidationDiagnostic(diagnostic.issues)


def render_validation_diagnostic(diagnostic: object) -> str:
    """Render only fixed messages selected by validated issue metadata."""

    validated = validate_validation_diagnostic(diagnostic)
    messages = []
    for field_path, issue_code in validated.issues:
        field_label = (
            field_path.replace("_", " ")
            if field_path.startswith("SECTION_")
            else field_path
        )
        messages.append(
            _VALIDATION_ISSUE_MESSAGES[issue_code].format(field=field_label)
        )
    return "; ".join(messages)


#: pydantic error ``type`` -> issue code. Anything unlisted is ``invalid_value``.
_ERROR_TYPE_CODES = {
    "missing": "field_required",
    "int_type": "invalid_integer",
    "int_parsing": "invalid_integer",
    "int_from_float": "invalid_integer",
    "float_type": "invalid_number",
    "float_parsing": "invalid_number",
    "enum": "invalid_literal",
    "literal_error": "invalid_literal",
    "greater_than": "number_too_small",
    "greater_than_equal": "number_too_small",
}


def _allowlisted_path(loc: object) -> str:
    """The longest allowlisted prefix of an error location, else ``report``.

    Integer segments are list indices and are dropped. A segment the schema does
    not define (an ``extra="forbid"`` rejection names the provider's own key)
    ends the walk, so provider text can only ever select an ancestor that the
    allowlist already contains.
    """

    if not isinstance(loc, (tuple, list)):
        return "report"
    parts: list[str] = []
    for segment in loc:
        if type(segment) is int:
            continue
        if type(segment) is not str:
            break
        candidate = ".".join((*parts, segment))
        if candidate not in _VALIDATION_FIELD_PATHS:
            break
        parts.append(segment)
    return ".".join(parts) if parts else "report"


def validation_diagnostic_from_errors(
    errors: Iterable[Mapping[str, object]],
) -> SafeValidationDiagnostic:
    """Map pydantic ``ValidationError.errors()`` onto allowlisted field/code pairs.

    Only each error's ``loc`` and ``type`` are read, never ``msg``, ``input`` or
    ``ctx``, and both are reduced to allowlisted values. Duplicates collapse and
    the list is capped at the diagnostic's own limit.
    """

    issues: list[tuple[str, str]] = []
    for error in errors:
        issue = (
            _allowlisted_path(error.get("loc")),
            _ERROR_TYPE_CODES.get(str(error.get("type")), "invalid_value"),
        )
        if issue not in issues:
            issues.append(issue)
    if not issues:
        issues.append(("report", "invalid_report"))
    return SafeValidationDiagnostic(tuple(issues[:16]))


def validate_failure_kind(value: object) -> FailureKind:
    if not isinstance(value, str) or value not in _FAILURE_KINDS:
        raise ValueError("failure_kind must be 'validation', 'execution', 'truncation' or 'terminal'")
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
    "SafeValidationDiagnostic",
    "build_execution_diagnostic",
    "canonicalize_execution_error",
    "format_execution_error",
    "render_execution_diagnostic",
    "render_validation_diagnostic",
    "validate_execution_diagnostic",
    "validate_failure_kind",
    "validate_validation_diagnostic",
    "validation_diagnostic_from_errors",
]

