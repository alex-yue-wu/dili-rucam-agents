"""Write the analyst report from a validated assessment.

The model returns one JSON object; this module turns it into the three-section
markdown report that ``validate_analyst_report`` and the workbook writers read.
Total, category and ``rules_version`` are never taken from the model, and
free text is flattened to one line so it cannot open a heading or a fence.
"""

from __future__ import annotations

import json
import re

from dili_rucam_agents.validators.rucam_assessment import (
    ItemScore,
    RucamAssessment,
    ScoredAssessment,
)

RULES_VERSION = "RUCAM_DananBenichou1993_modified_sheet_v3"

NOT_REPORTED = "Not reported"

_ITEM_LABELS = (
    ("time_to_onset", "Time to onset"),
    ("course", "Course"),
    ("risk_factors", "Risk factors"),
    ("concomitant_drugs", "Concomitant drugs"),
    ("other_causes_excluded", "Other causes excluded"),
    ("known_hepatotoxicity", "Known hepatotoxicity"),
    ("rechallenge", "Rechallenge"),
)

_PRE_FLIGHT_LABELS = (
    ("suspect_drug", "Suspect drug"),
    ("onset", "Onset"),
    ("r_ratio_pattern", "R ratio and pattern"),
    ("course_basis", "Course basis"),
    ("g1_count", "g1 count"),
    ("item_6_source", "Item 6 source"),
    ("scores_in_legal_sets", "Scores in legal sets"),
    ("total_sum", "Total sum"),
    ("category_match", "Category match"),
    ("section_b_matches_section_c", "SECTION B matches SECTION C"),
    ("notes_complete", "Notes complete"),
)

_GROUP_I = (
    "hepatitis_a",
    "hepatitis_b",
    "hepatitis_c",
    "biliary_obstruction",
    "alcoholism",
    "hypotension_or_ischaemia",
)
_GROUP_II = ("underlying_disease", "acute_viral_infection")

# validate_analyst_report rejects any report containing this sentence.
_PLACEHOLDER_RE = re.compile(r"(?i)see complete sections")


def render_report(
    *,
    assessment: RucamAssessment,
    scored: ScoredAssessment,
    analyst_label: str,
    model_name: str,
    rules_version: str,
) -> str:
    return "\n".join(
        (
            _section_a(assessment, scored, analyst_label, model_name),
            _section_b(assessment, scored),
            _section_c(assessment, scored, rules_version),
        )
    )


def _text(value: object) -> str:
    """One line of free text: no heading, fence or table cell can start inside it."""

    flat = " ".join(str(value).split())
    return _PLACEHOLDER_RE.sub("see complete section", flat)


def _cell(value: object) -> str:
    return _text(value).replace("|", "\\|")


def _number(value: float | None) -> str:
    return NOT_REPORTED if value is None else f"{value:g}"


def _section_a(
    assessment: RucamAssessment,
    scored: ScoredAssessment,
    analyst_label: str,
    model_name: str,
) -> str:
    basis = assessment.r_ratio_basis
    r_ratio = (
        NOT_REPORTED if assessment.r_ratio is None else f"{assessment.r_ratio:.2f}"
    )
    onset_day = (
        NOT_REPORTED if assessment.onset_day is None else str(assessment.onset_day)
    )
    causes = assessment.item_5_causes
    lines = [
        "## SECTION A -- CASE ANALYSIS",
        "",
        f"Analyst: {_text(analyst_label)} ({_text(model_name)})",
        "",
        "### Injury pattern and R ratio",
        "",
        f"- Injury pattern: {assessment.injury_pattern}",
        f"- R = {r_ratio} (ALT {_number(basis.alt)} (ULN {_number(basis.alt_uln)})"
        f" / ALP {_number(basis.alp)} (ULN {_number(basis.alp_uln)}))",
        "",
        "### Timeline",
        "",
        f"- onset_day = {onset_day}, counted from {_text(assessment.onset_anchor_dose)}",
        "",
        "### Pre-flight check",
        "",
    ]
    for number, (field, label) in enumerate(_PRE_FLIGHT_LABELS, start=1):
        lines.append(f"{number}. {label}: {_text(getattr(assessment.pre_flight, field))}")
    lines += ["", "### Item 5 cause checklist", "", "Group I:"]
    lines += [f"- {name}: {getattr(causes, name).value}" for name in _GROUP_I]
    lines += ["", "Group II:"]
    lines += [f"- {name}: {getattr(causes, name).value}" for name in _GROUP_II]
    lines += ["", f"g1 = {causes.g1}", "", "### Scoring rationale", ""]
    for field, label in _ITEM_LABELS:
        item: ItemScore = getattr(assessment, field)
        lines.append(
            f"- {label} ({_signed(item.score)}, {item.data_status.value}): "
            f"{_text(item.rationale)}"
        )
    lines += [
        "",
        f"Total {scored.total_score}, category {scored.category}.",
        "",
    ]
    return "\n".join(lines)


def _evidence_cell(item: ItemScore) -> str:
    if item.evidence:
        return "; ".join(f'"{_cell(ref.quote)}"' for ref in item.evidence)
    return NOT_REPORTED if item.data_status.value == "not_reported" else "None quoted"


def _signed(score: int) -> str:
    return f"{score:+d}" if score else "0"


def _section_b(assessment: RucamAssessment, scored: ScoredAssessment) -> str:
    lines = [
        "## SECTION B -- RUCAM SCORING TABLE",
        "",
        "| RUCAM Item | Score | Evidence |",
        "| --- | --- | --- |",
    ]
    for field, label in _ITEM_LABELS:
        item: ItemScore = getattr(assessment, field)
        lines.append(f"| {label} | {_signed(item.score)} | {_evidence_cell(item)} |")
    lines.append(
        f"| **Total** | **{scored.total_score}** | "
        f"**Category: {scored.category}** |"
    )
    lines.append("")
    return "\n".join(lines)


def _notes(assessment: RucamAssessment) -> list[str]:
    notes = []
    for field, label in _ITEM_LABELS:
        item: ItemScore = getattr(assessment, field)
        if item.inference_note:
            notes.append(f"{label}: {_text(item.inference_note)}")
    return notes


def _section_c(
    assessment: RucamAssessment, scored: ScoredAssessment, rules_version: str
) -> str:
    payload = {
        "injury_pattern": assessment.injury_pattern,
        "R_ratio": (
            None if assessment.r_ratio is None else round(assessment.r_ratio, 2)
        ),
        "rucam_scores": {
            field: getattr(assessment, field).score for field, _ in _ITEM_LABELS
        },
        "total_score": scored.total_score,
        "category": scored.category,
        "rules_version": rules_version,
        "notes": _notes(assessment),
    }
    return "\n".join(
        (
            "## SECTION C -- MACHINE-READABLE JSON",
            "",
            "```json",
            json.dumps(payload, indent=2, ensure_ascii=False),
            "```",
            "",
        )
    )


__all__ = ["RULES_VERSION", "render_report"]
