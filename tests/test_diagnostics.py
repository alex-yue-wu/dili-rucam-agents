from __future__ import annotations

import asyncio
import json
import typing

from pydantic import BaseModel, ValidationError
import pytest

from dili_rucam_agents import diagnostics as diagnostics_module
from dili_rucam_agents.diagnostics import (
    SafeValidationDiagnostic,
    render_validation_diagnostic,
    validate_failure_kind,
    validation_diagnostic_from_errors,
)
from dili_rucam_agents.providers import ModelSpec
from dili_rucam_agents.roster import Provider
from dili_rucam_agents.runner import RunnerError, run_contract
from dili_rucam_agents.validators.rucam_assessment import RucamAssessment
from dili_rucam_agents.validators.rucam_json import ITEM_LEGAL_SCORES
from fakes import FakeProvider

ITEMS = tuple(ITEM_LEGAL_SCORES)
SPEC = ModelSpec(provider=Provider.openai, model="gpt-5.5")
SECRET = "PROVIDER-CONTROLLED-FIELD-NAME"


def _item(score: int = 0) -> dict:
    return {"score": score, "data_status": "reported", "rationale": "because"}


def _assessment(**overrides) -> dict:
    flags = {
        name: "not_documented"
        for name in (
            "hepatitis_a",
            "hepatitis_b",
            "hepatitis_c",
            "biliary_obstruction",
            "alcoholism",
            "hypotension_or_ischaemia",
            "underlying_disease",
            "acute_viral_infection",
        )
    }
    data = {
        "injury_pattern": "hepatocellular",
        "r_ratio": 12.5,
        "r_ratio_basis": {
            "alt": 500.0,
            "alp": 120.0,
            "alt_uln": 40.0,
            "alp_uln": 120.0,
        },
        "onset_day": 10,
        "onset_anchor_dose": "first dose",
        "pre_flight": {
            name: "x"
            for name in (
                "suspect_drug",
                "onset",
                "r_ratio_pattern",
                "course_basis",
                "g1_count",
                "item_6_source",
                "scores_in_legal_sets",
                "total_sum",
                "category_match",
                "section_b_matches_section_c",
                "notes_complete",
            )
        },
        "item_5_causes": {**flags, "g1": 0},
        **{item: _item(0) for item in ITEMS},
    }
    data.update(overrides)
    return data


def _issues(payload: object) -> tuple[tuple[str, str], ...]:
    with pytest.raises(ValidationError) as info:
        RucamAssessment.model_validate(payload)
    return validation_diagnostic_from_errors(info.value.errors()).issues


def test_failure_kind_accepts_truncation_and_terminal():
    for kind in ("validation", "execution", "truncation", "terminal"):
        assert validate_failure_kind(kind) == kind
    with pytest.raises(ValueError):
        validate_failure_kind("timeout")


def _schema_paths(model: type[BaseModel], prefix: str = "") -> set[str]:
    paths: set[str] = set()
    for name, field in model.model_fields.items():
        path = f"{prefix}{name}"
        paths.add(path)
        annotation = field.annotation
        # list[EvidenceRef] -> EvidenceRef: list indices are not part of a path.
        for candidate in (annotation, *typing.get_args(annotation)):
            if isinstance(candidate, type) and issubclass(candidate, BaseModel):
                paths |= _schema_paths(candidate, f"{path}.")
    return paths


def test_validation_field_paths_cover_the_assessment_schema():
    schema_paths = _schema_paths(RucamAssessment)
    # Guard the walker: it must reach a nested leaf, not just the top level.
    assert {
        "pre_flight.notes_complete",
        "item_5_causes.g1",
        "r_ratio_basis.alp_uln",
        "known_hepatotoxicity.score",
        "rechallenge.evidence.quote",
    } <= schema_paths
    missing = schema_paths - diagnostics_module._VALIDATION_FIELD_PATHS
    assert missing == set()


def test_runner_validation_diagnostic_carries_assessment_field_paths():
    # +3 is not a printed rung of item 6, and onset_anchor_dose is absent.
    bad = _assessment(known_hepatotoxicity=_item(3))
    del bad["onset_anchor_dose"]
    provider = FakeProvider(json.dumps(bad))

    with pytest.raises(RunnerError) as info:
        asyncio.run(
            run_contract(
                instructions="Score.",
                model_class=RucamAssessment,
                spec=SPEC,
                provider=provider,
                max_retries=0,
            )
        )

    assert info.value.kind == "validation"
    assert set(info.value.validation_diagnostic.issues) == {
        ("known_hepatotoxicity", "invalid_value"),
        ("onset_anchor_dose", "field_required"),
    }
    assert ("report", "invalid_value") not in info.value.validation_diagnostic.issues


def test_nested_failures_name_the_nested_field():
    payload = _assessment()
    payload["r_ratio_basis"]["alp"] = 0  # gt=0
    payload["onset_day"] = 0  # ge=1
    del payload["pre_flight"]["total_sum"]
    payload["rechallenge"]["data_status"] = "guessed"
    assert set(_issues(payload)) == {
        ("r_ratio_basis.alp", "number_too_small"),
        ("onset_day", "number_too_small"),
        ("pre_flight.total_sum", "field_required"),
        ("rechallenge.data_status", "invalid_literal"),
    }


def test_evidence_list_index_is_dropped_from_the_path():
    payload = _assessment(course={**_item(0), "evidence": [{"quote": "ok"}, {}]})
    assert _issues(payload) == (("course.evidence.quote", "field_required"),)


def test_unknown_field_names_never_reach_the_diagnostic():
    payload = _assessment()
    payload[SECRET] = 1
    payload["pre_flight"][SECRET] = "x"
    issues = _issues(payload)

    # Each unknown key collapses onto its nearest allowlisted ancestor.
    assert set(issues) == {
        ("report", "unexpected_field"),
        ("pre_flight", "unexpected_field"),
    }
    rendered = render_validation_diagnostic(SafeValidationDiagnostic(issues))
    assert SECRET not in repr(issues)
    assert SECRET not in rendered


def test_model_level_failure_falls_back_to_the_report():
    # A reply that parsed as JSON but is not an object: raised on the model, so
    # loc == () and there is no field to name. ``_parse`` can return a list, so
    # this is a reply the runner really can be handed.
    assert _issues([1, 2, 3]) == (("report", "invalid_value"),)


def test_item_five_g1_mismatch_names_its_group():
    payload = _assessment()
    payload["item_5_causes"]["g1"] = 4
    assert _issues(payload) == (("item_5_causes", "invalid_value"),)


def test_wrong_types_map_to_distinct_codes():
    payload = _assessment(onset_day="ten", r_ratio="high")
    assert set(_issues(payload)) == {
        ("onset_day", "invalid_integer"),
        ("r_ratio", "invalid_number"),
    }


def test_duplicate_issues_collapse_and_the_list_is_bounded():
    errors = [{"loc": ("course", "score"), "type": "missing"}] * 3
    assert validation_diagnostic_from_errors(errors).issues == (
        ("course.score", "field_required"),
    )
    many = [{"loc": (item, "score"), "type": "missing"} for item in ITEMS] * 4
    many += [{"loc": ("pre_flight", name), "type": "missing"} for name in ("onset", "total_sum")]
    assert len(validation_diagnostic_from_errors(many).issues) <= 16


def test_no_errors_still_yields_a_valid_diagnostic():
    assert validation_diagnostic_from_errors([]).issues == (("report", "invalid_report"),)


def test_type_and_bound_errors_use_their_precise_codes():
    payload = _assessment()
    payload["course"]["rationale"] = 5
    payload["course"]["evidence"] = [{"quote": "q", "verified": "maybe"}]
    assert set(_issues(payload)) == {
        ("course.rationale", "invalid_string"),
        ("course.evidence.verified", "invalid_boolean"),
    }
    for kind in ("less_than", "less_than_equal"):
        assert validation_diagnostic_from_errors(
            [{"loc": ("onset_day",), "type": kind}]
        ).issues == (("onset_day", "number_too_large"),)
    assert validation_diagnostic_from_errors(
        [{"loc": ("onset_day",), "type": "some_unmapped_type"}]
    ).issues == (("onset_day", "invalid_value"),)
