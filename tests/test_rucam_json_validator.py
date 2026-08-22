import json

import pytest
from pydantic import ValidationError

from dili_rucam_agents.validators.rucam_json import RucamReport, validate_rucam_json


def valid_payload() -> dict:
    return {
        "injury_pattern": "mixed",
        "R_ratio": 3.2,
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


def test_validator_uses_canonical_other_causes_name():
    report = validate_rucam_json(valid_payload())
    assert report.rucam_scores.other_causes_excluded == 2
    assert "other_causes_excluded" in report.model_dump()["rucam_scores"]


def test_validator_accepts_legacy_alternative_causes_alias():
    payload = valid_payload()
    payload["rucam_scores"]["alternative_causes_excluded"] = payload[
        "rucam_scores"
    ].pop("other_causes_excluded")
    report = validate_rucam_json(payload)
    assert report.rucam_scores.other_causes_excluded == 2


def test_validator_accepts_documented_missing_pattern_and_ratio():
    payload = valid_payload()
    payload["injury_pattern"] = "Not reported"
    payload["R_ratio"] = None
    assert validate_rucam_json(payload).R_ratio is None


def test_validator_rejects_category_inconsistent_with_total():
    payload = valid_payload()
    payload["category"] = "Possible"
    with pytest.raises(ValueError, match="category does not match total_score"):
        validate_rucam_json(payload)


def test_validate_rucam_json_accepts_valid_payload():
    payload = valid_payload()
    report = validate_rucam_json(payload)
    assert report.total_score == 6
    assert report.rucam_scores.time_to_onset == 2


def test_validate_rucam_json_rejects_bad_total():
    payload = valid_payload()
    payload["total_score"] = 8

    with pytest.raises(ValueError):
        validate_rucam_json(payload)


@pytest.mark.parametrize("ratio", (float("nan"), float("inf"), float("-inf")))
def test_validator_rejects_non_finite_ratio_from_direct_dictionary(ratio):
    payload = valid_payload()
    payload["R_ratio"] = ratio

    with pytest.raises(ValueError, match="R_ratio"):
        validate_rucam_json(payload)


@pytest.mark.parametrize("ratio", (float("nan"), float("inf"), float("-inf")))
def test_rucam_model_independently_rejects_non_finite_ratio(ratio):
    payload = valid_payload()
    payload["R_ratio"] = ratio

    with pytest.raises(ValidationError):
        RucamReport.model_validate(payload)


@pytest.mark.parametrize("constant", ("NaN", "Infinity", "-Infinity"))
def test_validator_rejects_non_standard_constants_in_json_text(constant):
    payload = valid_payload()
    json_text = json.dumps(payload).replace("3.2", constant, 1)

    with pytest.raises((ValueError, json.JSONDecodeError)):
        validate_rucam_json(json_text)
