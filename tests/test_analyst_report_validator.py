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


class HostileValidationIssues:
    def __init__(self, secret: str) -> None:
        self.secret = secret
        self.invoked: list[str] = []

    def _raise(self, hook: str):
        self.invoked.append(hook)
        raise RuntimeError(f"{hook}: {self.secret}")

    def __iter__(self):
        return self._raise("__iter__")

    def __len__(self):
        return self._raise("__len__")

    def __getitem__(self, key):
        return self._raise("__getitem__")

    def __bool__(self):
        return self._raise("__bool__")

    def __str__(self):
        return self._raise("__str__")

    def __repr__(self):
        return self._raise("__repr__")


def forged_diagnostic_with_issues(issues: object) -> SafeValidationDiagnostic:
    forged = object.__new__(SafeValidationDiagnostic)
    object.__setattr__(forged, "issues", issues)
    return forged


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


@pytest.mark.parametrize("constant", ("NaN", "Infinity", "-Infinity"))
@pytest.mark.parametrize("allow_legacy_json", (False, True))
def test_section_c_parser_rejects_non_standard_numeric_constants_at_parse_time(
    constant, allow_legacy_json
):
    report = report_for().replace("6.4", constant, 1)

    with pytest.raises(AnalystReportValidationError) as exc_info:
        parse_section_c_payload(report, allow_legacy_json=allow_legacy_json)

    assert exc_info.value.diagnostic == SafeValidationDiagnostic(
        (("SECTION_C", "invalid_json"),)
    )


def test_schema_reports_overflowed_standard_json_ratio_as_invalid_number():
    report = report_for().replace("6.4", "1e999", 1)

    with pytest.raises(AnalystReportValidationError) as exc_info:
        validate_analyst_report(report)

    assert exc_info.value.diagnostic == SafeValidationDiagnostic(
        (("R_ratio", "invalid_number"),)
    )


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


def test_validation_diagnostic_revalidation_never_executes_hostile_issues_hooks():
    secret = "HOSTILE-VALIDATION-ITERABLE-SECRET"
    issues = HostileValidationIssues(secret)
    forged = forged_diagnostic_with_issues(issues)

    with pytest.raises(ValueError) as exc_info:
        validate_validation_diagnostic(forged)

    assert issues.invoked == []
    assert secret not in str(exc_info.value)


def test_validation_error_constructor_canonicalizes_hostile_issues_without_hooks():
    secret = "HOSTILE-VALIDATOR-ERROR-SECRET"
    issues = HostileValidationIssues(secret)
    forged = forged_diagnostic_with_issues(issues)

    error = AnalystReportValidationError(forged)

    assert issues.invoked == []
    assert error.diagnostic == SafeValidationDiagnostic((("report", "invalid_report"),))
    assert secret not in str(error)
