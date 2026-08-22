import json

import pytest

from dili_rucam_agents.validators.analyst_report import (
    AnalystReportValidationError,
    parse_section_c_payload,
    validate_analyst_report,
)


VALID_PAYLOAD = {
    "injury_pattern": "hepatocellular",
    "R_ratio": 6.4,
    "rucam_scores": {
        "time_to_onset": 2,
        "course": 1,
        "risk_factors": 0,
        "concomitant_drugs": 0,
        "other_causes_excluded": 2,
        "known_hepatotoxicity": 1,
        "rechallenge": 0,
    },
    "total_score": 6,
    "category": "Probable",
}


def report_for(payload: dict = VALID_PAYLOAD) -> str:
    return (
        "## SECTION A — HUMAN-READABLE FULL REPORT\n\nClinical summary.\n\n"
        "## SECTION B — RUCAM SCORING TABLE\n\n| Item | Score |\n| --- | --- |\n| Total | 6 |\n\n"
        "## SECTION C — MACHINE-READABLE JSON\n\n"
        f"```json\n{json.dumps(payload)}\n```\n"
    )


def test_validate_analyst_report_returns_typed_payload():
    result = validate_analyst_report(report_for())
    assert result.payload.total_score == 6
    assert result.canonical_payload["rucam_scores"]["other_causes_excluded"] == 2


@pytest.mark.parametrize(
    ("report_text", "message"),
    [
        ("", "empty"),
        ("See complete Sections A, B, and C above.", "summary placeholder"),
        ("## SECTION B\n\nTable\n\n## SECTION C\n\n```json\n{}\n```", "SECTION A"),
        ("## SECTION A\n\nText\n\n## SECTION C\n\n```json\n{}\n```", "SECTION B"),
        ("## SECTION A\n\nText\n\n## SECTION B\n\nTable", "SECTION C"),
    ],
)
def test_validate_analyst_report_rejects_incomplete_sections(report_text, message):
    with pytest.raises(AnalystReportValidationError, match=message):
        validate_analyst_report(report_text)


def test_strict_validation_rejects_unfenced_section_c_json():
    unfenced = report_for().replace("```json\n", "").replace("\n```\n", "\n")
    with pytest.raises(AnalystReportValidationError, match="fenced JSON"):
        validate_analyst_report(unfenced)


def test_legacy_parser_accepts_unfenced_section_c_json():
    unfenced = report_for().replace("```json\n", "").replace("\n```\n", "\n")
    assert parse_section_c_payload(unfenced, allow_legacy_json=True)["total_score"] == 6


def test_validation_diagnostic_omits_model_controlled_values_and_urls():
    secret = "MODEL-CONTROLLED-SECRET"
    payload = {
        **VALID_PAYLOAD,
        "rucam_scores": {
            **VALID_PAYLOAD["rucam_scores"],
            "time_to_onset": secret,
        },
    }

    with pytest.raises(AnalystReportValidationError) as exc_info:
        validate_analyst_report(report_for(payload))

    diagnostic = str(exc_info.value)
    assert "rucam_scores.time_to_onset" in diagnostic
    assert "valid integer" in diagnostic
    assert secret not in diagnostic
    assert "input_value" not in diagnostic
    assert "errors.pydantic.dev" not in diagnostic
