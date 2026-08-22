from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any

from .rucam_json import RucamReport, validate_rucam_json


_SECTION_HEADING_RE = re.compile(r"(?im)^##\s+\*{0,2}SECTION\s+([ABC])\b[^\n]*")
_JSON_FENCE_RE = re.compile(r"```json\s*(.*?)\s*```", re.IGNORECASE | re.DOTALL)
_FLOAT_RE = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"


class AnalystReportValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedAnalystReport:
    payload: RucamReport
    canonical_payload: dict[str, Any]


def parse_section_c_payload(
    report_text: str, *, allow_legacy_json: bool = False
) -> dict[str, Any]:
    headings = list(_SECTION_HEADING_RE.finditer(report_text))
    section_c = next(
        (match for match in headings if match.group(1).upper() == "C"), None
    )
    if section_c is None:
        raise AnalystReportValidationError("Unable to locate SECTION C.")
    section_text = report_text[section_c.end() :]
    fence = _JSON_FENCE_RE.search(section_text)
    if fence is not None:
        json_text = fence.group(1)
    elif allow_legacy_json:
        json_text = _extract_balanced_json_object(section_text)
    else:
        raise AnalystReportValidationError("SECTION C must contain fenced JSON.")
    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError as exc:
        recovered = _recover_section_c_payload(json_text) if allow_legacy_json else None
        if recovered is None:
            raise AnalystReportValidationError(f"Invalid SECTION C JSON: {exc}") from exc
        payload = recovered
    if not isinstance(payload, dict):
        raise AnalystReportValidationError("SECTION C JSON must be an object.")
    return payload


def validate_analyst_report(
    report_text: str, *, allow_legacy_json: bool = False
) -> ValidatedAnalystReport:
    stripped = report_text.strip()
    if not stripped:
        raise AnalystReportValidationError("Report is empty.")
    if "see complete sections a, b, and c above." in stripped.lower():
        raise AnalystReportValidationError("Report is a summary placeholder.")
    headings = list(_SECTION_HEADING_RE.finditer(report_text))
    by_name = {match.group(1).upper(): match for match in headings}
    for required in ("A", "B", "C"):
        if required not in by_name:
            raise AnalystReportValidationError(f"Missing SECTION {required}.")
    ordered = sorted(headings, key=lambda match: match.start())
    for required in ("A", "B"):
        match = by_name[required]
        position = ordered.index(match)
        end = (
            ordered[position + 1].start()
            if position + 1 < len(ordered)
            else len(report_text)
        )
        if not report_text[match.end() : end].strip(" \t\r\n-"):
            raise AnalystReportValidationError(f"SECTION {required} is empty.")
    payload = parse_section_c_payload(report_text, allow_legacy_json=allow_legacy_json)
    try:
        typed = validate_rucam_json(payload)
    except ValueError as exc:
        raise AnalystReportValidationError(str(exc)) from exc
    return ValidatedAnalystReport(
        payload=typed,
        canonical_payload=typed.model_dump(),
    )


def _extract_balanced_json_object(section_text: str) -> str:
    first_brace = section_text.find("{")
    if first_brace == -1:
        raise AnalystReportValidationError("Unable to locate a complete JSON object.")
    depth = 0
    in_string = False
    escaped = False
    chars: list[str] = []
    for ch in section_text[first_brace:]:
        chars.append(ch)
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return "".join(chars)
    raise AnalystReportValidationError("Unable to locate a complete JSON object.")


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


__all__ = [
    "AnalystReportValidationError",
    "ValidatedAnalystReport",
    "parse_section_c_payload",
    "validate_analyst_report",
]
