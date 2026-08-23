from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any

from pydantic import ValidationError

from dili_rucam_agents.diagnostics import (
    SafeValidationDiagnostic,
    render_validation_diagnostic,
    validate_validation_diagnostic,
)

from .rucam_json import RucamReport


_SECTION_HEADING_RE = re.compile(
    r"(?im)^[ \t]{0,3}##(?!#)[ \t]+\*{0,2}SECTION[ \t]+([ABC])\b[^\n]*"
)
# Recognizes a section label rendered at any other heading level, or emphasized
# instead of headed, so a wrong-level heading is reported as such rather than as
# a missing section. Never widens what validates - only what the failure says.
_MISLEVELED_SECTION_HEADING_RE = re.compile(
    r"(?im)^[ \t]{0,3}(?:#{1,6}[ \t]*\*{0,2}|\*{1,2})[ \t]*SECTION[ \t]+([ABC])\b"
)
_FENCE_OPEN_RE = re.compile(r"^[ \t]{0,3}(?P<fence>`{3,}|~{3,})(?P<info>[^\r\n]*)$")
_FLOAT_RE = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"


class AnalystReportValidationError(ValueError):
    def __init__(self, diagnostic: object) -> None:
        try:
            validated = validate_validation_diagnostic(diagnostic)
        except (TypeError, ValueError):
            validated = SafeValidationDiagnostic((("report", "invalid_report"),))
        self.diagnostic = validated
        super().__init__(render_validation_diagnostic(validated))


@dataclass(frozen=True)
class ValidatedAnalystReport:
    payload: RucamReport
    canonical_payload: dict[str, Any]


@dataclass(frozen=True)
class _MarkdownFence:
    start: int
    content_start: int
    content_end: int
    end: int
    info: str


def parse_section_c_payload(
    report_text: str, *, allow_legacy_json: bool = False
) -> dict[str, Any]:
    headings, fences, masked_text = _markdown_structure(report_text)
    section_c_headings = [match for match in headings if match.group(1).upper() == "C"]
    if not section_c_headings:
        raise _validation_error("SECTION_C", _absent_section_code(masked_text, "C"))
    if not allow_legacy_json and len(section_c_headings) != 1:
        raise _validation_error("SECTION_C", "duplicate_section")
    section_c = section_c_headings[-1] if allow_legacy_json else section_c_headings[0]
    section_end = next(
        (
            heading.start()
            for heading in headings
            if heading.start() > section_c.start()
        ),
        len(report_text),
    )
    json_fences = [
        fence
        for fence in fences
        if section_c.end() <= fence.start
        and fence.end <= section_end
        and _fence_language(fence.info) == "json"
    ]
    if json_fences:
        if not allow_legacy_json and len(json_fences) != 1:
            raise _validation_error("SECTION_C", "json_fence_count")
        fence = json_fences[-1] if allow_legacy_json else json_fences[0]
        json_text = report_text[fence.content_start : fence.content_end].strip()
    elif allow_legacy_json:
        section_text = report_text[section_c.end() : section_end]
        json_text = _extract_balanced_json_object(section_text)
    else:
        raise _validation_error("SECTION_C", "json_fence_count")
    invalid_json = False
    try:
        payload = json.loads(
            json_text,
            parse_constant=_reject_non_standard_json_constant,
        )
    except json.JSONDecodeError:
        recovered = _recover_section_c_payload(json_text) if allow_legacy_json else None
        if recovered is None:
            invalid_json = True
            payload = {}
        else:
            payload = recovered
    except ValueError:
        invalid_json = True
        payload = {}
    if invalid_json:
        raise _validation_error("SECTION_C", "invalid_json")
    if not isinstance(payload, dict):
        raise _validation_error("SECTION_C", "json_not_object")
    return payload


def validate_analyst_report(
    report_text: str, *, allow_legacy_json: bool = False
) -> ValidatedAnalystReport:
    if type(report_text) is not str:
        raise _validation_error("report", "invalid_report")
    stripped = report_text.strip()
    if not stripped:
        raise _validation_error("report", "report_empty")
    if "see complete sections a, b, and c above." in stripped.lower():
        raise _validation_error("report", "summary_placeholder")
    headings, _, masked_text = _markdown_structure(report_text)
    by_name = {
        name: [match for match in headings if match.group(1).upper() == name]
        for name in ("A", "B", "C")
    }
    for required in ("A", "B", "C"):
        if not by_name[required]:
            raise _validation_error(
                f"SECTION_{required}", _absent_section_code(masked_text, required)
            )
        if len(by_name[required]) != 1:
            raise _validation_error(f"SECTION_{required}", "duplicate_section")
    if tuple(match.group(1).upper() for match in headings) != ("A", "B", "C"):
        raise _validation_error("report", "section_order")
    for required in ("A", "B"):
        match = by_name[required][0]
        position = headings.index(match)
        end = headings[position + 1].start()
        if not report_text[match.end() : end].strip(" \t\r\n-"):
            raise _validation_error(f"SECTION_{required}", "empty_section")
    payload = parse_section_c_payload(report_text, allow_legacy_json=allow_legacy_json)
    validation_diagnostic = None
    try:
        typed = RucamReport.model_validate(payload)
    except ValidationError as exc:
        validation_diagnostic = _pydantic_diagnostic(exc)
        typed = None
    if validation_diagnostic is not None:
        raise AnalystReportValidationError(validation_diagnostic)
    assert typed is not None
    return ValidatedAnalystReport(
        payload=typed,
        canonical_payload=typed.model_dump(),
    )


def _markdown_structure(
    report_text: str,
) -> tuple[list[re.Match[str]], list[_MarkdownFence], str]:
    fences: list[_MarkdownFence] = []
    masked_ranges: list[tuple[int, int]] = []
    active: tuple[str, int, int, int, str] | None = None
    offset = 0
    for line in report_text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        if active is None:
            opener = _FENCE_OPEN_RE.fullmatch(body)
            if opener is not None:
                marker = opener.group("fence")
                active = (
                    marker[0],
                    len(marker),
                    offset,
                    offset + len(line),
                    opener.group("info").strip(),
                )
        else:
            marker_char, marker_length, start, content_start, info = active
            candidate = body.lstrip(" \t")
            indent = len(body) - len(candidate)
            if (
                indent <= 3
                and candidate.rstrip(" \t")
                == marker_char * len(candidate.rstrip(" \t"))
                and len(candidate.rstrip(" \t")) >= marker_length
            ):
                fences.append(
                    _MarkdownFence(
                        start=start,
                        content_start=content_start,
                        content_end=offset,
                        end=offset + len(line),
                        info=info,
                    )
                )
                masked_ranges.append((start, offset + len(line)))
                active = None
        offset += len(line)
    if active is not None:
        masked_ranges.append((active[2], len(report_text)))

    masked = list(report_text)
    for start, end in masked_ranges:
        for index in range(start, end):
            if masked[index] not in "\r\n":
                masked[index] = " "
    masked_text = "".join(masked)
    headings = list(_SECTION_HEADING_RE.finditer(masked_text))
    return headings, fences, masked_text


def _absent_section_code(masked_text: str, name: str) -> str:
    """Distinguish a mislevelled section heading from a genuinely absent section."""

    for match in _MISLEVELED_SECTION_HEADING_RE.finditer(masked_text):
        if match.group(1).upper() == name.upper():
            return "section_heading_level"
    return "missing_section"


def _fence_language(info: str) -> str:
    return info.casefold().split(maxsplit=1)[0] if info else ""


def _extract_balanced_json_object(section_text: str) -> str:
    depth = 0
    in_string = False
    escaped = False
    object_start: int | None = None
    objects: list[str] = []
    for index, ch in enumerate(section_text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"' and depth:
            in_string = True
        elif ch == "{":
            if depth == 0:
                object_start = index
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and object_start is not None:
                objects.append(section_text[object_start : index + 1])
                object_start = None
    if objects:
        return objects[-1]
    raise _validation_error("SECTION_C", "missing_json_object")


def _validation_error(field_path: str, issue_code: str) -> AnalystReportValidationError:
    return AnalystReportValidationError(
        SafeValidationDiagnostic(((field_path, issue_code),))
    )


def _pydantic_diagnostic(exc: ValidationError) -> SafeValidationDiagnostic:
    issues: list[tuple[str, str]] = []
    for error in exc.errors(
        include_url=False,
        include_context=False,
        include_input=False,
    ):
        location = ".".join(str(part) for part in error["loc"])
        if location == "rucam_scores.alternative_causes_excluded":
            location = "rucam_scores.other_causes_excluded"
        issue_type = error["type"]
        if not location:
            message = error.get("msg")
            if (
                message
                == "Value error, total_score does not match the seven-item score sum"
            ):
                location, issue_code = "total_score", "score_sum_mismatch"
            elif message == "Value error, category does not match total_score":
                location, issue_code = "category", "category_mismatch"
            else:
                location, issue_code = "report", "invalid_value"
        else:
            issue_code = {
                "missing": "field_required",
                "int_parsing": "invalid_integer",
                "int_type": "invalid_integer",
                "greater_than_equal": (
                    "number_too_small" if location == "R_ratio" else "integer_too_small"
                ),
                "less_than_equal": "integer_too_large",
                "float_parsing": "invalid_number",
                "float_type": "invalid_number",
                "finite_number": "invalid_number",
                "literal_error": "invalid_literal",
            }.get(issue_type, "invalid_value")
        try:
            issue = SafeValidationDiagnostic(((location, issue_code),)).issues[0]
        except ValueError:
            issue = ("report", "invalid_value")
        if issue not in issues:
            issues.append(issue)
    return SafeValidationDiagnostic(
        tuple(issues[:16]) or (("report", "invalid_value"),)
    )


def _recover_section_c_payload(json_block: str) -> dict[str, Any] | None:
    payload: dict[str, Any] = {}
    for key in ("injury_pattern", "category", "rules_version"):
        value = _extract_string_field(json_block, key)
        if value is not None:
            payload[key] = value
    r_ratio = _extract_number_field(json_block, "R_ratio")
    if r_ratio is not None:
        payload["R_ratio"] = r_ratio
    total_score = _extract_number_field(json_block, "total_score")
    if total_score is not None:
        payload["total_score"] = int(total_score)
    rucam_scores: dict[str, int] = {}
    for key in (
        "time_to_onset",
        "course",
        "risk_factors",
        "concomitant_drugs",
        "other_causes_excluded",
        "known_hepatotoxicity",
        "rechallenge",
    ):
        value = _extract_number_field(json_block, key)
        if value is not None:
            rucam_scores[key] = int(value)
    if rucam_scores:
        payload["rucam_scores"] = rucam_scores
    notes = _extract_notes(json_block)
    if notes:
        payload["notes"] = notes
    if "total_score" not in payload and "category" not in payload:
        return None
    return payload


def _extract_string_field(text: str, key: str) -> str | None:
    match = re.search(rf'"{re.escape(key)}"\s*:\s*"([^"\n]+)"', text)
    if not match:
        return None
    return match.group(1).strip()


def _extract_number_field(text: str, key: str) -> float | None:
    match = re.search(rf'"{re.escape(key)}"\s*:\s*({_FLOAT_RE})', text)
    if not match:
        return None
    return float(match.group(1))


def _extract_notes(json_block: str) -> list[str]:
    notes_match = re.search(r'"notes"\s*:\s*\[(.*?)\]', json_block, re.DOTALL)
    if not notes_match:
        return []
    notes = re.findall(r'"([^"\n]+)"', notes_match.group(1))
    return [note.strip().rstrip(",)") for note in notes if note.strip().rstrip(",)")]


def _reject_non_standard_json_constant(_constant: str) -> None:
    raise ValueError("non-standard JSON numeric constants are not allowed")


__all__ = [
    "AnalystReportValidationError",
    "ValidatedAnalystReport",
    "parse_section_c_payload",
    "validate_analyst_report",
]
