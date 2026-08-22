import json
from dataclasses import asdict
import pickle

import pytest

from dili_rucam_agents.diagnostics import (
    SafeValidationDiagnostic,
    render_validation_diagnostic,
    validate_validation_diagnostic,
)
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


@pytest.mark.parametrize(
    ("report_text", "message"),
    [
        (
            report_for() + "\n## SECTION A\n\nDuplicate narrative.\n",
            "exactly one SECTION A",
        ),
        (
            report_for() + "\n## SECTION B\n\nDuplicate table.\n",
            "exactly one SECTION B",
        ),
        (
            report_for()
            + "\n## SECTION C\n\n"
            + f"```json\n{json.dumps(VALID_PAYLOAD)}\n```\n",
            "exactly one SECTION C",
        ),
        (
            report_for().replace(
                "## SECTION C — MACHINE-READABLE JSON\n\n",
                "## SECTION C — MACHINE-READABLE JSON\n\n```json\n{}\n```\n\n",
            ),
            "exactly one fenced JSON object",
        ),
        (
            "## section c\n\n"
            f"```JSON\n{json.dumps(VALID_PAYLOAD)}\n```\n\n"
            "## **SECTION A**\n\nClinical summary.\n\n"
            "## Section B\n\n| Item | Score |\n| --- | --- |\n| Total | 6 |\n",
            "A, B, C order",
        ),
    ],
)
def test_strict_validation_rejects_ambiguous_or_out_of_order_structure(
    report_text, message
):
    with pytest.raises(AnalystReportValidationError, match=message):
        validate_analyst_report(report_text)


def test_strict_validation_ignores_headings_and_json_examples_inside_code_fences():
    report = report_for().replace(
        "Clinical summary.",
        "Clinical summary.\n\n"
        "````text\n"
        "## SECTION C\n"
        "```json\n"
        '{"decoy": true}\n'
        "```\n"
        "````",
    )

    assert validate_analyst_report(report).payload.total_score == 6


def test_legacy_parser_accepts_unfenced_section_c_json():
    unfenced = report_for().replace("```json\n", "").replace("\n```\n", "\n")
    assert parse_section_c_payload(unfenced, allow_legacy_json=True)["total_score"] == 6


def test_legacy_parser_selects_last_json_block_from_malformed_historical_report():
    report = (
        "## SECTION C\n\n"
        '```json\n{"total_score": 1, "category": "Unlikely"}\n```\n\n'
        f"```JSON\n{json.dumps(VALID_PAYLOAD)}\n```\n"
    )

    assert parse_section_c_payload(report, allow_legacy_json=True) == VALID_PAYLOAD


def test_legacy_parser_selects_last_unfenced_json_object():
    report = (
        "## SECTION C\n\n"
        '{"total_score": 1, "category": "Unlikely"}\n\n'
        f"{json.dumps(VALID_PAYLOAD)}\n"
    )

    assert parse_section_c_payload(report, allow_legacy_json=True) == VALID_PAYLOAD


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


def test_validation_diagnostic_is_structured_allowlisted_and_forge_resistant():
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

    diagnostic = exc_info.value.diagnostic
    assert diagnostic == SafeValidationDiagnostic(
        (("rucam_scores.time_to_onset", "invalid_integer"),)
    )
    serialized = (
        repr(diagnostic),
        repr(asdict(diagnostic)),
        repr(vars(diagnostic)),
        pickle.dumps(diagnostic),
        render_validation_diagnostic(diagnostic),
    )
    for value in serialized:
        encoded = value if isinstance(value, bytes) else value.encode()
        assert secret.encode() not in encoded

    with pytest.raises(ValueError):
        SafeValidationDiagnostic(((f"patient.{secret}", "invalid_integer"),))
    with pytest.raises(ValueError):
        SafeValidationDiagnostic((("total_score", secret),))

    forged = object.__new__(SafeValidationDiagnostic)
    object.__setattr__(forged, "issues", (("total_score", secret),))
    with pytest.raises(ValueError):
        validate_validation_diagnostic(forged)
