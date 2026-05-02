from __future__ import annotations

import copy
import json
import re
from typing import Any

from crewai.tools import BaseTool


MASK_TOKEN = "[RUCAM_SCORE_MASKED]"
RUCAM_CATEGORIES = ("Highly probable", "Probable", "Possible", "Unlikely", "Excluded")
RUCAM_METHOD_ALIASES = (
    "rucam",
    "roussel uclaf",
    "roussel-uclaf",
    "roussel uclaf method",
    "roussel uclaf causality assessment method",
)
AUTHOR_OUTCOME_CUES = (
    "our patient",
    "this patient",
    "the patient",
    "case report",
    "article",
    "paper",
    "reported",
    "published",
    "authors",
    "author",
    "source report",
    "original case report",
    "adverse event was applied",
)
GENERIC_METHOD_CUES = (
    "a score from",
    "is used to classify causality",
    "five categories",
    "causality excluded",
    "improbable",
    "possible (3",
    "probable (6",
    "highly probable",
)

_RUCAM_TABLE_HINT_RE = re.compile(r"(?i)\brucam\b.*\b(item|score|scoring|table)\b")
_RUCAM_METHOD_ALIAS_RE = re.compile(
    r"(?i)\b(?:rucam|roussel[\s-]+uclaf(?:\s+(?:method|causality assessment method))?)\b"
)
_RUCAM_SCORE_LINE_RE = re.compile(
    r"(?i)(\brucam\b.*\b(score|points?|total|item|result)\b.*(?<![\d.])[+-]?\d+(?![\d.])"
    r"|"
    r"\b(score|points?|total|item|result)\b.*(?<![\d.])[+-]?\d+(?![\d.]).*\brucam\b)"
)
_RUCAM_ALIAS_SCORE_LINE_RE = re.compile(
    r"(?i)(?:\b(?:roussel[\s-]+uclaf(?:\s+(?:method|causality assessment method))?)\b[^.\n]{0,200}?"
    r"\b(?:score|points?|total|result)\b[^.\n;:]{0,40}?[+-]?\d+\b"
    r"|"
    r"\b(?:score|points?|total|result)\b[^.\n;:]{0,40}?[+-]?\d+\b[^.\n]{0,200}?"
    r"\b(?:roussel[\s-]+uclaf(?:\s+(?:method|causality assessment method))?)\b)"
)
_AUTHOR_REPORTED_SCORE_LINE_RE = re.compile(
    r"(?i)\b(?:author(?:s)?|published|reported|case report|article|paper|source report|original case report)\b[^.\n]{0,160}?"
    r"\b(?:score|points?|total|result)\b[^.\n;:]{0,40}?[+-]?\d+\b"
)
_RUCAM_GRADE_LINE_RE = re.compile(
    r"(?i)\brucam\b.*("
    r"\b(category|causality|grade|grading|assessment|result)\b.*\b(highly probable|probable|possible|unlikely|excluded)\b"
    r"|"
    r"\b(highly probable|probable|possible|unlikely|excluded)\b.*\b(category|causality|grade|grading|assessment|result)\b"
    r")"
)
_INTEGER_TOKEN_RE = re.compile(r"(?<![\d.])[+-]?\d+(?![\d.])")
_OUTCOME_INTEGER_TOKEN_RE = re.compile(r"(?<!\d)[+-]?\d+(?!\d)")
_RUCAM_SCORE_COLUMN_RE = re.compile(r"(?i)\b(score|points?|total|result)\b")
_RUCAM_GRADE_COLUMN_RE = re.compile(
    r"(?i)\b(category|causality|grade|grading|assessment|result)\b"
)
_LIKELY_RUCAM_REVIEW_LINE_RE = re.compile(
    r"(?i)\brucam\b.*((?<![\d.])[+-]?\d+(?![\d.])|(highly probable|probable|possible|unlikely|excluded))"
)
_RUCAM_CATEGORY_VALUE_RE = re.compile(
    r"(?i)\b(highly probable|probable|possible|unlikely|excluded)\b"
)
_RUCAM_SCORE_VALUE_RE = re.compile(
    r"(?i)(\b(?:rucam\s+)?(?:score|points?|total|result)\b[^.\n;:]{0,40}?)([+-]?\d+)(\b)"
)
_RUCAM_SCORE_VALUE_RE_REVERSED = re.compile(
    r"(?i)(\b(?:score|points?|total|result)\b[^.\n;:]{0,40}?)([+-]?\d+)([^.\n]{0,120}?\brucam\b)"
)
_AUTHOR_REPORTED_SCORE_VALUE_RE = re.compile(
    r"(?i)(\b(?:author(?:s)?|published|reported|case report|article|paper|source report|original case report)\b[^.\n]{0,160}?"
    r"\b(?:score|points?|total|result)\b[^.\n;:]{0,40}?)([+-]?\d+)(\b)"
)
_RUCAM_GRADE_CONTEXT_RE = re.compile(
    r"(?i)\brucam\b.*\b(category|causality|grade|grading|assessment|result)\b"
)
_RUCAM_CONTEXT_RE = re.compile(r"(?i)\brucam\b")
_RUCAM_HEADER_RE = re.compile(r"(?i)\brucam\b")
_PATIENT_SPECIFIC_OUTCOME_LINE_RE = re.compile(
    r"(?i)(?:"
    r"\b(?:our patient|this patient|the patient)\b[^.\n]{0,160}?\b(?:score|points?|total|result)\b[^.\n;:]{0,40}?[+-]?\d+\b"
    r"|"
    r"\b(?:author(?:s)?|published|reported|case report|article|paper|source report|original case report)\b[^.\n]{0,160}?\b(?:score|points?|total|result)\b[^.\n;:]{0,40}?[+-]?\d+\b"
    r"|"
    r"\b(?:rucam|roussel[\s-]+uclaf(?:\s+(?:method|causality assessment method))?)\b[^.\n]{0,200}?\b(?:final\s+)?(?:score|points?|total|result)\b[^.\n;:]{0,40}?[+-]?\d+\b"
    r"|"
    r"\b(?:when|after)\b[^.\n]{0,80}?\bapplied\s+to\s+(?:our|this|the)\s+patient\b[^.\n]{0,160}?\b(?:score|points?|total|result)\b[^.\n;:]{0,40}?[+-]?\d+\b"
    r")"
)
_PATIENT_SPECIFIC_GRADE_LINE_RE = re.compile(
    r"(?i)(?:"
    r"\b(?:our patient|this patient|the patient)\b[^.\n]{0,160}?\b(?:category|causality|grade|assessment|result)\b[^.\n]{0,40}?\b(highly probable|probable|possible|unlikely|excluded)\b"
    r"|"
    r"\b(?:author(?:s)?|published|reported|case report|article|paper|source report|original case report)\b[^.\n]{0,160}?\b(?:category|causality|grade|assessment|result)\b[^.\n]{0,40}?\b(highly probable|probable|possible|unlikely|excluded)\b"
    r"|"
    r"\b(?:when|after)\b[^.\n]{0,80}?\bapplied\s+to\s+(?:our|this|the)\s+patient\b[^.\n]{0,160}?\b(?:adr|causality|category|grade|result)\b[^.\n]{0,40}?\b(highly probable|probable|possible|unlikely|excluded)\b"
    r")"
)
_GENERIC_METHOD_LINE_RE = re.compile(
    r"(?i)(?:\b(?:rucam|roussel[\s-]+uclaf(?:\s+(?:method|causality assessment method))?)\b[^.\n]{0,240}?"
    r"(?:a score from|is used to classify causality|five categories)"
    r"|"
    r"\bwhen it is applied,\s+a score from\b)"
)


def mask_case_bundle_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Redact prior RUCAM score values and grades from extracted content."""

    masked_payload = copy.deepcopy(payload)
    masked_payload["normalized_text"] = _mask_text(
        masked_payload.get("normalized_text", "")
    )

    for block in masked_payload.get("blocks", []):
        if isinstance(block, dict):
            block["text"] = _mask_text(block.get("text", ""))

    for table in masked_payload.get("tables", []):
        if not isinstance(table, dict):
            continue
        table["preview"] = _mask_text(table.get("preview", ""))
        raw_rows = table.get("raw_rows", [])
        if _is_rucam_score_table(table):
            score_columns = _find_score_columns(raw_rows)
            grade_columns = _find_grade_columns(raw_rows)
            table["raw_rows"] = [
                _mask_table_row(
                    row,
                    score_columns=score_columns,
                    grade_columns=grade_columns,
                    row_index=row_index,
                )
                for row_index, row in enumerate(raw_rows)
            ]
            continue

        table["raw_rows"] = [
            [_mask_text(str(cell)) for cell in row] for row in raw_rows
        ]

    extraction_notes = list(masked_payload.get("extraction_notes", []))
    extraction_notes.append(
        "RUCAM score and grade masking applied to extracted text before analyst review."
    )
    masked_payload["extraction_notes"] = extraction_notes
    return masked_payload


def _mask_text(text: str) -> str:
    if not text:
        return text

    masked_lines: list[str] = []
    for line in text.splitlines():
        stripped_line = line.strip()
        if stripped_line and _should_mask(stripped_line):
            masked_lines.append(_mask_rucam_tokens(line))
            continue
        masked_lines.append(line)
    return "\n".join(masked_lines)


def _should_mask(text: str) -> bool:
    normalized = text.strip()
    if not normalized:
        return False
    if _is_patient_specific_outcome_line(normalized):
        return True
    if _is_generic_rucam_method_line(normalized):
        return False
    return bool(
        _RUCAM_SCORE_LINE_RE.search(normalized)
        or _RUCAM_GRADE_LINE_RE.search(normalized)
        or (
            _RUCAM_GRADE_CONTEXT_RE.search(normalized)
            and _RUCAM_CATEGORY_VALUE_RE.search(normalized)
        )
    )


def _is_rucam_score_table(table: dict[str, Any]) -> bool:
    preview = table.get("preview", "")
    raw_rows = table.get("raw_rows", [])
    header_row = raw_rows[0] if raw_rows and isinstance(raw_rows[0], list) else []
    header_text = " | ".join(str(cell) for cell in header_row)
    preview_text = str(preview)
    if _RUCAM_HEADER_RE.search(header_text):
        return bool(_find_score_columns(raw_rows) or _find_grade_columns(raw_rows))
    if _RUCAM_TABLE_HINT_RE.search(preview_text) and _RUCAM_HEADER_RE.search(
        preview_text
    ):
        return bool(_find_score_columns(raw_rows) or _find_grade_columns(raw_rows))
    return False


def _mask_table_row(
    row: Any,
    *,
    score_columns: set[int],
    grade_columns: set[int],
    row_index: int,
) -> list[str]:
    if not isinstance(row, list):
        return [_mask_text(str(row))]

    masked_row: list[str] = []
    for column_index, cell in enumerate(row):
        cell_text = str(cell)
        if row_index == 0:
            masked_row.append(cell_text)
            continue
        if column_index in score_columns:
            masked_row.append(_INTEGER_TOKEN_RE.sub(MASK_TOKEN, cell_text))
            continue
        if column_index in grade_columns:
            masked_row.append(_RUCAM_CATEGORY_VALUE_RE.sub(MASK_TOKEN, cell_text))
            continue
        masked_row.append(cell_text)
    return masked_row


def _find_score_columns(raw_rows: list[Any]) -> set[int]:
    if not raw_rows:
        return set()

    header_row = raw_rows[0]
    if not isinstance(header_row, list):
        return set()

    return {
        index
        for index, cell in enumerate(header_row)
        if _RUCAM_SCORE_COLUMN_RE.search(str(cell))
    }


def _find_grade_columns(raw_rows: list[Any]) -> set[int]:
    if not raw_rows:
        return set()

    header_row = raw_rows[0]
    if not isinstance(header_row, list):
        return set()

    return {
        index
        for index, cell in enumerate(header_row)
        if _RUCAM_GRADE_COLUMN_RE.search(str(cell))
    }


def _mask_rucam_tokens(text: str) -> str:
    if _is_patient_specific_outcome_line(text):
        masked_text = _mask_patient_outcome_score_values(text)
        masked_text = _mask_patient_outcome_grade_values(masked_text)
        return masked_text
    masked_text = _mask_score_values(text)
    masked_text = _mask_grade_values(masked_text)
    return masked_text


def _mask_patient_outcome_score_values(text: str) -> str:
    masked_text = _AUTHOR_REPORTED_SCORE_VALUE_RE.sub(
        lambda match: f"{match.group(1)}{MASK_TOKEN}{match.group(3)}",
        text,
    )
    if _RUCAM_METHOD_ALIAS_RE.search(masked_text):
        masked_text = re.sub(
            r"(?i)(\b(?:score|points?|total|result)\b[^.\n;:]{0,40}?)([+-]?\d+)([^.\n]{0,200}?"
            r"\b(?:rucam|roussel[\s-]+uclaf(?:\s+(?:method|causality assessment method))?)\b)",
            lambda match: f"{match.group(1)}{MASK_TOKEN}{match.group(3)}",
            masked_text,
        )
        masked_text = re.sub(
            r"(?i)(\b(?:rucam|roussel[\s-]+uclaf(?:\s+(?:method|causality assessment method))?)\b[^.\n]{0,200}?"
            r"\b(?:score|points?|total|result)\b[^.\n;:]{0,40}?)([+-]?\d+)(\b)",
            lambda match: f"{match.group(1)}{MASK_TOKEN}{match.group(3)}",
            masked_text,
        )
    return _RUCAM_SCORE_VALUE_RE.sub(
        lambda match: f"{match.group(1)}{MASK_TOKEN}{match.group(3)}",
        masked_text,
    )


def _mask_patient_outcome_grade_values(text: str) -> str:
    if not (
        _PATIENT_SPECIFIC_GRADE_LINE_RE.search(text)
        or (
            (
                _RUCAM_METHOD_ALIAS_RE.search(text)
                or any(cue in text.lower() for cue in AUTHOR_OUTCOME_CUES)
            )
            and _RUCAM_CATEGORY_VALUE_RE.search(text)
        )
    ):
        return text
    return _RUCAM_CATEGORY_VALUE_RE.sub(MASK_TOKEN, text)


def _mask_score_values(text: str) -> str:
    masked_text = _AUTHOR_REPORTED_SCORE_VALUE_RE.sub(
        lambda match: f"{match.group(1)}{MASK_TOKEN}{match.group(3)}",
        text,
    )
    if _RUCAM_METHOD_ALIAS_RE.search(masked_text):
        masked_text = re.sub(
            r"(?i)(\b(?:score|points?|total|result)\b[^.\n;:]{0,40}?)([+-]?\d+)([^.\n]{0,200}?"
            r"\b(?:rucam|roussel[\s-]+uclaf(?:\s+(?:method|causality assessment method))?)\b)",
            lambda match: f"{match.group(1)}{MASK_TOKEN}{match.group(3)}",
            masked_text,
        )
        masked_text = re.sub(
            r"(?i)(\b(?:rucam|roussel[\s-]+uclaf(?:\s+(?:method|causality assessment method))?)\b[^.\n]{0,200}?"
            r"\b(?:score|points?|total|result)\b[^.\n;:]{0,40}?)([+-]?\d+)(\b)",
            lambda match: f"{match.group(1)}{MASK_TOKEN}{match.group(3)}",
            masked_text,
        )
    masked_text = _RUCAM_SCORE_VALUE_RE.sub(
        lambda match: f"{match.group(1)}{MASK_TOKEN}{match.group(3)}",
        masked_text,
    )
    if not _RUCAM_CONTEXT_RE.search(masked_text):
        return masked_text
    return _RUCAM_SCORE_VALUE_RE_REVERSED.sub(
        lambda match: f"{match.group(1)}{MASK_TOKEN}{match.group(3)}",
        masked_text,
    )


def _mask_grade_values(text: str) -> str:
    if not (
        _RUCAM_GRADE_CONTEXT_RE.search(text)
        or (
            _RUCAM_METHOD_ALIAS_RE.search(text)
            and _RUCAM_CATEGORY_VALUE_RE.search(text)
        )
    ):
        return text
    return _RUCAM_CATEGORY_VALUE_RE.sub(MASK_TOKEN, text)


def extract_rucam_score_ints(text: str) -> list[int]:
    if not text:
        return []

    normalized = text.strip()
    if not (
        _RUCAM_SCORE_LINE_RE.search(normalized)
        or _RUCAM_TABLE_HINT_RE.search(normalized)
    ):
        return []

    scores: list[int] = []
    for match in _INTEGER_TOKEN_RE.findall(normalized):
        scores.append(int(match))
    return scores


def is_patient_specific_outcome_line(text: str) -> bool:
    return _is_patient_specific_outcome_line(text)


def extract_patient_specific_rucam_scores(text: str) -> list[int]:
    if not text:
        return []
    normalized = text.strip()
    if not _is_patient_specific_outcome_line(normalized):
        return []
    scores: list[int] = []
    for match in _OUTCOME_INTEGER_TOKEN_RE.findall(normalized):
        scores.append(int(match))
    return scores


def extract_rucam_grade_labels(text: str) -> list[str]:
    if not text:
        return []

    normalized = text.strip()
    if not (
        _RUCAM_GRADE_LINE_RE.search(normalized)
        or _RUCAM_TABLE_HINT_RE.search(normalized)
    ):
        return []

    canonical_labels = {category.lower(): category for category in RUCAM_CATEGORIES}
    grades: list[str] = []
    seen: set[str] = set()
    for match in _RUCAM_CATEGORY_VALUE_RE.finditer(normalized):
        canonical = canonical_labels[match.group(1).lower()]
        if canonical.lower() in seen:
            continue
        seen.add(canonical.lower())
        grades.append(canonical)
    return grades


def _is_patient_specific_outcome_line(text: str) -> bool:
    normalized = text.strip()
    if not normalized:
        return False
    if _PATIENT_SPECIFIC_OUTCOME_LINE_RE.search(
        normalized
    ) or _PATIENT_SPECIFIC_GRADE_LINE_RE.search(normalized):
        return True
    lower = normalized.lower()
    has_method = any(alias in lower for alias in RUCAM_METHOD_ALIASES)
    has_outcome_term = any(
        term in lower
        for term in ("final score", "score was", "score of", "total score", "result")
    )
    has_author_cue = any(cue in lower for cue in AUTHOR_OUTCOME_CUES)
    return has_method and has_outcome_term and has_author_cue


def _is_generic_rucam_method_line(text: str) -> bool:
    normalized = text.strip()
    if not normalized:
        return False
    return bool(
        _GENERIC_METHOD_LINE_RE.search(normalized)
    ) and not _is_patient_specific_outcome_line(normalized)


def review_masked_case_bundle(
    raw_payload: dict[str, Any],
    masked_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []

    raw_lines = raw_payload.get("normalized_text", "").splitlines()
    masked_lines = masked_payload.get("normalized_text", "").splitlines()
    for index, raw_line in enumerate(raw_lines):
        if not _LIKELY_RUCAM_REVIEW_LINE_RE.search(raw_line):
            continue
        masked_line = masked_lines[index] if index < len(masked_lines) else ""
        unmasked_scores = [
            score
            for score in extract_rucam_score_ints(raw_line)
            if str(score) in masked_line
        ]
        unmasked_grades = [
            grade
            for grade in extract_rucam_grade_labels(raw_line)
            if re.search(rf"(?i)\b{re.escape(grade)}\b", masked_line)
        ]
        if not unmasked_scores and not unmasked_grades:
            continue
        findings.append(
            {
                "location": f"normalized_text line {index + 1}",
                "reason": _format_review_reason(unmasked_scores, unmasked_grades),
                "original": raw_line,
                "masked": masked_line,
                "likely_missed_scores": unmasked_scores,
                "likely_missed_grades": unmasked_grades,
            }
        )

    raw_tables = {
        (table.get("page_number"), table.get("table_index")): table
        for table in raw_payload.get("tables", [])
    }
    for masked_table in masked_payload.get("tables", []):
        key = (masked_table.get("page_number"), masked_table.get("table_index"))
        raw_table = raw_tables.get(key, {})
        raw_rows = raw_table.get("raw_rows", [])
        masked_rows = masked_table.get("raw_rows", [])
        if not _is_rucam_score_table(raw_table):
            continue
        for row_index, raw_row in enumerate(raw_rows):
            raw_row_text = " | ".join(str(cell) for cell in raw_row)
            if not _LIKELY_RUCAM_REVIEW_LINE_RE.search(raw_row_text):
                continue
            masked_row = masked_rows[row_index] if row_index < len(masked_rows) else []
            masked_row_text = " | ".join(str(cell) for cell in masked_row)
            unmasked_scores = [
                score
                for score in extract_rucam_score_ints(raw_row_text)
                if str(score) in masked_row_text
            ]
            unmasked_grades = [
                grade
                for grade in extract_rucam_grade_labels(raw_row_text)
                if re.search(rf"(?i)\b{re.escape(grade)}\b", masked_row_text)
            ]
            if not unmasked_scores and not unmasked_grades:
                continue
            findings.append(
                {
                    "location": (
                        f"table page {masked_table.get('page_number', '?')} "
                        f"index {masked_table.get('table_index', '?')} row {row_index + 1}"
                    ),
                    "reason": _format_review_reason(unmasked_scores, unmasked_grades),
                    "original": raw_row_text,
                    "masked": masked_row_text,
                    "likely_missed_scores": unmasked_scores,
                    "likely_missed_grades": unmasked_grades,
                }
            )

    return findings


def _format_review_reason(
    unmasked_scores: list[int], unmasked_grades: list[str]
) -> str:
    parts: list[str] = []
    if unmasked_scores:
        parts.append("unmasked score values remain")
    if unmasked_grades:
        parts.append("unmasked RUCAM grades remain")
    return " and ".join(parts)


class ScoreMaskingTool(BaseTool):
    """CrewAI tool wrapper for deterministic RUCAM score redaction."""

    name: str = "score_masker"
    description: str = "Mask prior RUCAM scores, score tables, and causality categories from a case_bundle_json payload."

    def _run(self, case_bundle_json: str) -> str:
        payload = parse_case_bundle_json(case_bundle_json)
        return json.dumps(mask_case_bundle_payload(payload), indent=2)

    async def _arun(
        self, case_bundle_json: str
    ) -> str:  # pragma: no cover - async parity
        return self._run(case_bundle_json)


def parse_case_bundle_json(raw_input: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    candidate = raw_input.strip()

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed

    for start_index, char in enumerate(candidate):
        if char != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(candidate[start_index:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    raise ValueError(
        "Unable to locate a valid case_bundle_json object in score_masker input."
    )


__all__ = [
    "MASK_TOKEN",
    "ScoreMaskingTool",
    "extract_rucam_grade_labels",
    "extract_rucam_score_ints",
    "mask_case_bundle_payload",
    "parse_case_bundle_json",
    "review_masked_case_bundle",
]
