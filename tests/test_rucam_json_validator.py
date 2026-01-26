import pytest

from dili_rucam_agents.validators.rucam_json import validate_rucam_json


def test_validate_rucam_json_accepts_valid_payload():
    payload = {
        "injury_pattern": "mixed",
        "R_ratio": 3.2,
        "rucam_scores": {
            "time_to_onset": 2,
            "course": 1,
            "risk_factors": 0,
            "concomitant_drugs": 0,
            "alternative_causes_excluded": 2,
            "known_hepatotoxicity": 2,
            "rechallenge": 0,
        },
        "total_score": 7,
        "category": "Probable",
    }

    report = validate_rucam_json(payload)
    assert report.total_score == 7
    assert report.rucam_scores.time_to_onset == 2


def test_validate_rucam_json_rejects_bad_total():
    payload = {
        "injury_pattern": "mixed",
        "R_ratio": 3.2,
        "rucam_scores": {
            "time_to_onset": 2,
            "course": 1,
            "risk_factors": 0,
            "concomitant_drugs": 0,
            "alternative_causes_excluded": 2,
            "known_hepatotoxicity": 2,
            "rechallenge": 0,
        },
        "total_score": 8,  # mismatch
        "category": "Probable",
    }

    with pytest.raises(ValueError):
        validate_rucam_json(payload)
