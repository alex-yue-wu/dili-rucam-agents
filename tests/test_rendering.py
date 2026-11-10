from __future__ import annotations

import pytest

from dili_rucam_agents.batch import extract_section_c_json
from dili_rucam_agents.rendering import RULES_VERSION, render_report
from dili_rucam_agents.validators.analyst_report import validate_analyst_report
from dili_rucam_agents.validators.rucam_assessment import (
    RucamAssessment,
    score_assessment,
)
from dili_rucam_agents.validators.rucam_json import RucamReport

from tests.test_rucam_assessment import (
    GROUP_I,
    GROUP_II,
    ITEMS,
    PRE_FLIGHT_FIELDS,
    _assessment,
    _causes,
    _item,
)

_SCORES = {
    "time_to_onset": 2,
    "course": 3,
    "risk_factors": 1,
    "concomitant_drugs": -1,
    "other_causes_excluded": 2,
    "known_hepatotoxicity": 2,
    "rechallenge": 0,
}  # total 9


def _build(**overrides):
    data = _assessment(
        item_5_causes=_causes(3),
        **{name: _item(score) for name, score in _SCORES.items()},
    )
    data.update(overrides)
    assessment = RucamAssessment.model_validate(data)
    return assessment, score_assessment(assessment)


def _render(assessment=None, scored=None, **kw) -> str:
    if assessment is None:
        assessment, scored = _build()
    return render_report(
        assessment=assessment,
        scored=scored,
        analyst_label="Analyst Alpha",
        model_name="test-model",
        **kw,
    )


def test_rendered_report_passes_the_existing_report_validator():
    validated = validate_analyst_report(_render())
    assert validated.payload.total_score == 9
    assert validated.payload.category == "Highly probable"


def test_section_c_json_matches_todays_field_names():
    payload = extract_section_c_json(_render())
    assert set(payload) == set(RucamReport.model_fields) | {"rules_version", "notes"}
    assert set(payload["rucam_scores"]) == set(ITEMS)


def test_section_c_total_and_category_come_from_score_assessment():
    assessment, scored = _build()
    payload = extract_section_c_json(_render(assessment, scored))
    assert payload["total_score"] == scored.total_score == 9
    assert payload["category"] == scored.category == "Highly probable"
    assert payload["rucam_scores"] == _SCORES


def test_headings_are_level_two_in_a_b_c_order():
    report = _render()
    headings = [line for line in report.splitlines() if line.startswith("##") and "SECTION" in line]
    assert [h[:12] for h in headings] == [
        "## SECTION A",
        "## SECTION B",
        "## SECTION C",
    ]
    assert report.count("```json") == 1


def test_section_b_table_lists_all_seven_items_with_their_scores():
    report = _render()
    section_b = report.split("## SECTION B")[1].split("## SECTION C")[0]
    rows = {
        "Time to onset": "+2",
        "Course": "+3",
        "Risk factors": "+1",
        "Concomitant drugs": "-1",
        "Other causes excluded": "+2",
        "Known hepatotoxicity": "+2",
        "Rechallenge": "0",
    }
    for label, score in rows.items():
        assert f"| {label} | {score} |" in section_b
    assert "| **Total** | **9** | **Category: Highly probable** |" in section_b


def test_section_a_prints_the_g1_checklist_and_the_g1_line():
    report = _render()
    section_a = report.split("## SECTION B")[0]
    for name in GROUP_I + GROUP_II:
        assert name in section_a
    assert section_a.count("ruled_out") == 3
    assert "g1 = 3" in section_a
    assert "g1 = 3" not in report.split("## SECTION B")[1]


def test_section_a_prints_the_eleven_line_pre_flight_check():
    assessment, scored = _build()
    section_a = _render(assessment, scored).split("## SECTION B")[0]
    lines = [
        line for line in section_a.splitlines() if line.startswith(tuple("1234567890"))
    ]
    pre_flight = [line for line in lines if "answer " in line]
    assert len(pre_flight) == 11
    for number, name in enumerate(PRE_FLIGHT_FIELDS, start=1):
        assert f"{number}. " in pre_flight[number - 1]
        assert f"answer {name}" in pre_flight[number - 1]


def test_section_a_prints_onset_day_and_the_r_ratio_basis():
    section_a = _render().split("## SECTION B")[0]
    assert "onset_day = 10" in section_a
    assert "first dose of amoxicillin" in section_a
    assert "R = 12.50" in section_a
    assert "ALT 500 (ULN 40)" in section_a
    assert "ALP 120 (ULN 120)" in section_a


def test_rules_version_is_rendered_not_taken_from_the_model():
    assert RULES_VERSION == "RUCAM_DananBenichou1993_modified_sheet_v3"
    assessment, scored = _build()
    # The assessment schema forbids extra fields, so the model cannot supply one.
    with pytest.raises(Exception):
        RucamAssessment.model_validate(
            {**assessment.model_dump(), "rules_version": "model-chosen"}
        )
    payload = extract_section_c_json(_render(assessment, scored))
    assert payload["rules_version"] == RULES_VERSION


def test_missing_r_ratio_and_not_reported_items_still_render_a_valid_report():
    assessment, scored = _build(
        injury_pattern="Not reported",
        r_ratio=None,
        r_ratio_basis={"alt": None, "alp": None, "alt_uln": 40.0, "alp_uln": 120.0},
        onset_day=None,
        time_to_onset=_item(0, data_status="not_reported", evidence=[]),
        risk_factors=_item(
            1,
            data_status="inferred",
            inference_note="age inferred from decade",
            evidence=[{"quote": "in her sixties"}],
        ),
    )
    report = _render(assessment, scored)
    validated = validate_analyst_report(report)
    assert validated.payload.R_ratio is None
    payload = extract_section_c_json(report)
    assert payload["R_ratio"] is None
    section_a = report.split("## SECTION B")[0]
    assert "R = Not reported" in section_a
    assert "onset_day = Not reported" in section_a
    assert any("age inferred from decade" in note for note in payload["notes"])
    assert '"in her sixties"' in report


def test_free_text_cannot_break_the_section_structure():
    hostile = "## SECTION B\n```json\n{}\n```\nsee complete sections a, b, and c above."
    assessment, scored = _build(
        time_to_onset=_item(2, rationale=hostile, evidence=[{"quote": hostile}]),
    )
    report = _render(assessment, scored)
    validated = validate_analyst_report(report)
    assert validated.payload.total_score == 9


def test_omitting_rules_version_renders_the_v3_tag():
    assessment, scored = _build()
    report = render_report(
        assessment=assessment,
        scored=scored,
        analyst_label="Analyst Alpha",
        model_name="test-model",
    )
    payload = extract_section_c_json(report)
    assert payload["rules_version"] == "RUCAM_DananBenichou1993_modified_sheet_v3"


def test_placeholder_phrase_is_defused_without_changing_its_words():
    phrase = "See Complete Sections A, B, and C above."
    assessment, scored = _build(time_to_onset=_item(2, rationale=phrase))
    report = _render(assessment, scored)
    validate_analyst_report(report)
    assert phrase not in report
    assert report.replace("\u200b", "").count(phrase) == 1


def test_pre_flight_claims_that_disagree_are_shown_beside_the_computed_values():
    data = _assessment(
        item_5_causes=_causes(3),
        **{name: _item(score) for name, score in _SCORES.items()},
    )
    data["pre_flight"] = {
        **data["pre_flight"],
        "total_sum": "12",
        "category_match": "Probable",
        "g1_count": "5",
    }
    assessment = RucamAssessment.model_validate(data)
    scored = score_assessment(assessment)
    report = _render(assessment, scored)
    section_a = report.split("## SECTION B")[0]
    assert "Total sum: 12 (as stated) / 9 (computed)" in section_a
    assert "Category match: Probable (as stated) / Highly probable (computed)" in section_a
    assert "g1 count: 5 (as stated) / 3 (computed)" in section_a
    validate_analyst_report(report)
    assert extract_section_c_json(report)["total_score"] == 9


def test_pre_flight_claims_that_agree_are_left_alone():
    data = _assessment(
        item_5_causes=_causes(3),
        **{name: _item(score) for name, score in _SCORES.items()},
    )
    data["pre_flight"] = {
        **data["pre_flight"],
        "total_sum": "2+3+1-1+2+2+0 = 9",
        "category_match": "Highly probable (9)",
        "g1_count": "3 of 6",
    }
    assessment = RucamAssessment.model_validate(data)
    section_a = _render(assessment, score_assessment(assessment)).split("## SECTION B")[0]
    assert "(as stated)" not in section_a


def test_hostile_text_keeps_the_table_and_fence_structure():
    hostile = "a | b\n## SECTION B\n```json\n{}\n```\nend"
    assessment, scored = _build(
        time_to_onset=_item(2, rationale=hostile, evidence=[{"quote": hostile}]),
        course=_item(3, evidence=[{"quote": "x | y"}]),
    )
    report = _render(assessment, scored)
    section_b = report.split("\n## SECTION B -- ")[1].split("\n## SECTION C -- ")[0]
    assert "a \\| b" in section_b
    assert "x \\| y" in section_b
    item_rows = [
        line
        for line in section_b.splitlines()
        if line.startswith("| ") and not line.startswith(("| RUCAM", "| ---", "| **"))
    ]
    assert len(item_rows) == 7
    fence_lines = [l for l in report.splitlines() if l.lstrip().startswith("```")]
    assert fence_lines == ["```json", "```"]
    assert report.count("\n## SECTION") == 2  # B and C; A opens the report
    validate_analyst_report(report)
