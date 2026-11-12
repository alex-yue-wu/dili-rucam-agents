from __future__ import annotations

import pytest
from pydantic import ValidationError

from dili_rucam_agents.validators.rucam_assessment import (
    CauseStatus,
    computed_r_ratio,
    EvidenceRef,
    has_cause_documented_present,
    item_five_rung,
    item_scores,
    ItemFiveCauses,
    ItemScore,
    PreFlight,
    RucamAssessment,
    r_ratio_disagreement,
    score_assessment,
)
from dili_rucam_agents.validators.rucam_json import (
    ITEM_LEGAL_SCORES,
    RucamScores,
)

ITEMS = tuple(ITEM_LEGAL_SCORES)
GROUP_I = (
    "hepatitis_a",
    "hepatitis_b",
    "hepatitis_c",
    "biliary_obstruction",
    "alcoholism",
    "hypotension_or_ischaemia",
)
GROUP_II = ("underlying_disease", "acute_viral_infection")

PRE_FLIGHT_FIELDS = (
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


def _item(score: int = 0, **kw) -> dict:
    return {
        "score": score,
        "data_status": kw.pop("data_status", "reported"),
        "rationale": "because",
        **kw,
    }


def _causes(ruled_out: int = 0, **kw) -> dict:
    flags = {name: "not_documented" for name in GROUP_I + GROUP_II}
    for name in (GROUP_I + GROUP_II)[:ruled_out]:
        flags[name] = "ruled_out"
    flags.update(kw)
    flags.setdefault("g1", sum(flags[n] == "ruled_out" for n in GROUP_I))
    return flags


def _pre_flight() -> dict:
    return {name: f"answer {name}" for name in PRE_FLIGHT_FIELDS}


def _assessment(**overrides) -> dict:
    data = {
        "injury_pattern": "hepatocellular",
        "r_ratio": 12.5,
        "r_ratio_basis": {"alt": 500.0, "alp": 120.0, "alt_uln": 40.0, "alp_uln": 120.0},
        "onset_day": 10,
        "onset_anchor_dose": "first dose of amoxicillin",
        "pre_flight": _pre_flight(),
        # Five Group I causes ruled out is the item-5 rung of 0, which is what
        # every item defaults to here. Code derives item 5 from this checklist,
        # so an all-`not_documented` default would have scored -2 and left the
        # fixture describing an assessment the sheet does not allow.
        "item_5_causes": _causes(5),
        **{item: _item(0) for item in ITEMS},
    }
    data.update(overrides)
    return data


@pytest.mark.parametrize(
    ("item", "value"),
    [(i, v) for i, legal in ITEM_LEGAL_SCORES.items() for v in sorted(legal)],
)
def test_each_item_accepts_its_legal_values(item, value):
    assessment = RucamAssessment.model_validate(_assessment(**{item: _item(value)}))
    assert getattr(assessment, item).score == value


@pytest.mark.parametrize(
    ("item", "value"),
    [
        (i, v)
        for i, legal in ITEM_LEGAL_SCORES.items()
        for v in range(-4, 5)
        if v not in legal
    ],
)
def test_each_item_rejects_values_outside_its_legal_set(item, value):
    with pytest.raises(ValidationError):
        RucamAssessment.model_validate(_assessment(**{item: _item(value)}))
    payload = {i: 0 for i in ITEMS}
    payload[item] = value
    with pytest.raises(ValidationError):
        RucamScores.model_validate(payload)


def test_exact_legal_sets():
    assert ITEM_LEGAL_SCORES == {
        "time_to_onset": frozenset({0, 1, 2}),
        "course": frozenset({-2, 0, 1, 2, 3}),
        "risk_factors": frozenset({0, 1, 2}),
        "concomitant_drugs": frozenset({-3, -2, -1, 0}),
        "other_causes_excluded": frozenset({-3, -2, 0, 1, 2}),
        "known_hepatotoxicity": frozenset({0, 1, 2}),
        "rechallenge": frozenset({-2, 0, 1, 3}),
    }


def test_rucam_scores_keeps_its_alias():
    scores = {i: 0 for i in ITEMS if i != "other_causes_excluded"}
    assert RucamScores.model_validate(
        {**scores, "alternative_causes_excluded": 2}
    ).other_causes_excluded == 2


def test_assessment_carries_no_total_and_no_category():
    assert "total_score" not in RucamAssessment.model_fields
    assert "category" not in RucamAssessment.model_fields


def test_score_assessment_sums_the_seven_items_and_derives_the_category():
    assessment = RucamAssessment.model_validate(
        _assessment(
            # +2 is item 5's rung only with all six Group I and both Group II
            # ruled out, and code derives it from this checklist.
            item_5_causes=_causes(8),
            time_to_onset=_item(2),
            course=_item(3),
            risk_factors=_item(1),
            concomitant_drugs=_item(-1),
            other_causes_excluded=_item(2),
            known_hepatotoxicity=_item(2),
            rechallenge=_item(0),
        )
    )
    scored = score_assessment(assessment)
    assert scored.total_score == 9
    assert scored.category == "Highly probable"


def test_score_assessment_negative_total_is_excluded():
    assessment = RucamAssessment.model_validate(
        _assessment(
            # A claimed -3 stands only where a cause is documented present:
            # step 1 of the procedure needs one, and whether it is a highly
            # probable explanation is the judgement the checklist cannot hold.
            item_5_causes=_causes(hypotension_or_ischaemia="present"),
            concomitant_drugs=_item(-3),
            other_causes_excluded=_item(-3),
        )
    )
    scored = score_assessment(assessment)
    assert scored.total_score == -6
    assert scored.category == "Excluded"


def test_g1_mismatch_is_rejected():
    causes = _causes(ruled_out=3)
    causes["g1"] = 4
    with pytest.raises(ValidationError, match="g1"):
        RucamAssessment.model_validate(_assessment(item_5_causes=causes))


def test_g1_counts_only_ruled_out():
    causes = _causes(
        hepatitis_a="ruled_out",
        hepatitis_b="not_documented",
        hepatitis_c="present",
        biliary_obstruction="ruled_out",
        underlying_disease="ruled_out",
        acute_viral_infection="ruled_out",
        g1=2,
    )
    parsed = ItemFiveCauses.model_validate(causes)
    assert parsed.g1 == 2
    assert parsed.hepatitis_c is CauseStatus.PRESENT
    # Group II flags must not inflate g1
    with pytest.raises(ValidationError, match="g1"):
        ItemFiveCauses.model_validate({**causes, "g1": 4})
    # nor may present or not_documented count
    with pytest.raises(ValidationError, match="g1"):
        ItemFiveCauses.model_validate({**causes, "g1": 3})


def test_g1_has_no_default():
    causes = _causes()
    del causes["g1"]
    with pytest.raises(ValidationError):
        ItemFiveCauses.model_validate(causes)


def test_evidence_verified_is_forced_false_even_when_the_model_sends_true():
    ref = EvidenceRef.model_validate({"quote": "ALT 500 U/L", "verified": True})
    assert ref.verified is False
    item = ItemScore.model_validate(
        _item(1, evidence=[{"quote": "q", "verified": True}])
    )
    assert item.evidence[0].verified is False


def test_inference_note_is_required_when_data_status_is_inferred():
    with pytest.raises(ValidationError, match="inference_note"):
        ItemScore.model_validate(_item(1, data_status="inferred"))
    with pytest.raises(ValidationError, match="inference_note"):
        ItemScore.model_validate(
            _item(1, data_status="inferred", inference_note="  ")
        )
    ok = ItemScore.model_validate(
        _item(1, data_status="inferred", inference_note="assumed default ULN")
    )
    assert ok.inference_note == "assumed default ULN"
    # a reported item needs none
    assert ItemScore.model_validate(_item(1)).inference_note is None


def test_unknown_field_is_rejected():
    with pytest.raises(ValidationError):
        RucamAssessment.model_validate(_assessment(total_score=0))
    with pytest.raises(ValidationError):
        RucamAssessment.model_validate(_assessment(category="Possible"))
    with pytest.raises(ValidationError):
        ItemScore.model_validate(_item(0, confidence=0.9))
    with pytest.raises(ValidationError):
        EvidenceRef.model_validate({"quote": "q", "page": 1})


def test_pre_flight_requires_all_eleven_answers():
    assert len(PreFlight.model_fields) == 11
    assert set(PreFlight.model_fields) == set(PRE_FLIGHT_FIELDS)
    PreFlight.model_validate(_pre_flight())
    for name in PRE_FLIGHT_FIELDS:
        partial = _pre_flight()
        del partial[name]
        with pytest.raises(ValidationError):
            PreFlight.model_validate(partial)
        blank = {**_pre_flight(), name: " "}
        with pytest.raises(ValidationError):
            PreFlight.model_validate(blank)


def test_cause_flags_are_the_six_group_i_and_two_group_ii():
    assert set(ItemFiveCauses.model_fields) == set(GROUP_I + GROUP_II) | {"g1"}


def test_r_ratio_basis_requires_positive_uln_and_allows_missing_values():
    base = {"alt": None, "alp": None, "alt_uln": 40.0, "alp_uln": 120.0}
    ok = RucamAssessment.model_validate(_assessment(r_ratio=None, r_ratio_basis=base))
    assert ok.r_ratio_basis.alt is None
    with pytest.raises(ValidationError):
        RucamAssessment.model_validate(
            _assessment(r_ratio_basis={**base, "alt_uln": 0.0})
        )


def test_every_illegal_item_is_reported_with_its_field_location():
    bad = {item: _item(99) for item in ITEMS}
    with pytest.raises(ValidationError) as exc:
        RucamAssessment.model_validate(_assessment(**bad))
    assert {e["loc"][0] for e in exc.value.errors()} == set(ITEMS)
    with pytest.raises(ValidationError) as exc:
        RucamScores.model_validate({item: 99 for item in ITEMS})
    assert {e["loc"] for e in exc.value.errors()} == {
        (("other_causes_excluded" if i == "other_causes_excluded" else i),)
        for i in ITEMS
    }


def test_certified_verified_flag_does_not_survive_a_round_trip():
    # `verified` is model-facing and always False on validation, so a quote
    # certified by mutation is reset by model_validate(model_dump()).
    assessment = RucamAssessment.model_validate(
        _assessment(course=_item(1, evidence=[{"quote": "q"}]))
    )
    assessment.course.evidence[0].verified = True
    reloaded = RucamAssessment.model_validate(assessment.model_dump())
    assert reloaded.course.evidence[0].verified is False


def test_r_ratio_disagreeing_with_its_basis_is_reported_not_rejected():
    """Rejecting it would spend a retry whose correction can move the pattern."""
    # (500/40) / (120/120) = 12.5
    assert r_ratio_disagreement(RucamAssessment.model_validate(_assessment(r_ratio=12.5))) is None
    assert r_ratio_disagreement(RucamAssessment.model_validate(_assessment(r_ratio=12.51))) is None
    disagreeing = RucamAssessment.model_validate(_assessment(r_ratio=8.0))
    assert computed_r_ratio(disagreeing) == pytest.approx(12.5)
    assert r_ratio_disagreement(disagreeing) == pytest.approx(12.5)


def test_r_ratio_cross_check_skipped_when_a_value_is_not_reported():
    basis = {"alt": 500.0, "alp": None, "alt_uln": 40.0, "alp_uln": 120.0}
    missing_alp = RucamAssessment.model_validate(
        _assessment(r_ratio=3.0, r_ratio_basis=basis)
    )
    assert computed_r_ratio(missing_alp) is None
    assert r_ratio_disagreement(missing_alp) is None
    basis = {"alt": None, "alp": 120.0, "alt_uln": 40.0, "alp_uln": 120.0}
    missing_alt = RucamAssessment.model_validate(
        _assessment(r_ratio=3.0, r_ratio_basis=basis)
    )
    assert computed_r_ratio(missing_alt) is None
    assert r_ratio_disagreement(missing_alt) is None
    # Nothing stated: there is nothing to disagree with, even with a full basis.
    assert r_ratio_disagreement(RucamAssessment.model_validate(_assessment(r_ratio=None))) is None


def test_alp_of_zero_is_rejected_by_the_basis_bound():
    """R would be infinite, so the bound on ALP still rejects the reply."""
    zero = {"alt": 500.0, "alp": 0.0, "alt_uln": 40.0, "alp_uln": 120.0}
    with pytest.raises(ValidationError):
        RucamAssessment.model_validate(_assessment(r_ratio=3.0, r_ratio_basis=zero))


@pytest.mark.parametrize(
    ("stated", "alt"),
    [(5.00, 496.0), (2.00, 202.0)],  # basis computes 4.96 and 2.02
)
def test_a_pattern_boundary_straddle_is_annotated_not_rejected(stated, alt):
    """The straddle is real and inherent at any tolerance, so it is reported.

    A stated 5.00 on a basis computing 4.96 crosses the hepatocellular/mixed
    boundary, and 2.00 on 2.02 crosses the cholestatic/mixed one. Rejecting
    either spends a retry that can move the pattern; both are now annotated.
    """
    basis = {"alt": alt, "alp": 100.0, "alt_uln": 100.0, "alp_uln": 100.0}
    straddling = RucamAssessment.model_validate(
        _assessment(r_ratio=stated, r_ratio_basis=basis)
    )
    assert r_ratio_disagreement(straddling) == pytest.approx(alt / 100)
    # Two-decimal rounding of the true value still raises no disagreement.
    rounded = RucamAssessment.model_validate(
        _assessment(r_ratio=round(alt / 100, 2), r_ratio_basis=basis)
    )
    assert r_ratio_disagreement(rounded) is None


def test_assessment_notes_are_optional_and_hold_the_item_free_notes():
    """Appendix A.2 and the R-ratio rule mandate notes that name no item."""
    default = RucamAssessment.model_validate(_assessment())
    assert default.assessment_notes == []
    carried = RucamAssessment.model_validate(
        _assessment(
            assessment_notes=[
                "ULN inferred using default reference values",
                "R not computable from the reported draws",
            ]
        )
    )
    assert carried.assessment_notes == [
        "ULN inferred using default reference values",
        "R not computable from the reported draws",
    ]
    # Still a list of strings: an object here would not survive a round trip.
    with pytest.raises(ValidationError):
        RucamAssessment.model_validate(_assessment(assessment_notes=[{"note": "x"}]))
    with pytest.raises(ValidationError):
        RucamAssessment.model_validate(_assessment(assessment_notes="one note"))


def test_onset_day_has_no_day_zero():
    with pytest.raises(ValidationError):
        RucamAssessment.model_validate(_assessment(onset_day=0))
    RucamAssessment.model_validate(_assessment(onset_day=1))
    RucamAssessment.model_validate(_assessment(onset_day=None))


@pytest.mark.parametrize(
    "ruled_out, group_ii, expected",
    [
        (6, True, 2),  # g1 = 6 and both Group II -> +2
        (6, False, 1),  # g1 = 6 alone -> +1
        (5, False, 0),
        (4, False, 0),
        (3, False, -2),
        (0, False, -2),
    ],
)
def test_item_five_rung_follows_the_printed_procedure(ruled_out, group_ii, expected):
    flags = {name: "not_documented" for name in GROUP_I + GROUP_II}
    for name in GROUP_I[:ruled_out]:
        flags[name] = "ruled_out"
    if group_ii:
        for name in GROUP_II:
            flags[name] = "ruled_out"
    causes = ItemFiveCauses.model_validate(_causes(**flags))

    assert item_five_rung(causes) == expected


def test_a_stated_item_five_score_never_beats_its_own_checklist():
    """The regression this exists for, from the run of 2026-11-12.

    One analyst of five marked `underlying_disease` as `not_documented` -- so
    `g1 = 6` without Group II, which the sheet scores +1 -- and then claimed
    +2. Every check passed, because 2 is a legal item-5 score and nothing
    compared it with the checklist beside it. Its total read 8 where the sheet
    gives 7.
    """
    assessment = RucamAssessment.model_validate(
        _assessment(
            item_5_causes=_causes(
                6, underlying_disease="not_documented", acute_viral_infection="ruled_out"
            ),
            other_causes_excluded=_item(2),
        )
    )

    assert item_scores(assessment)["other_causes_excluded"] == 1
    assert score_assessment(assessment).total_score == 1


def test_the_other_six_items_are_left_as_the_model_scored_them():
    """Only item 5 is derived: the rest are judgements, not arithmetic."""
    assessment = RucamAssessment.model_validate(
        _assessment(
            item_5_causes=_causes(8),
            time_to_onset=_item(2),
            course=_item(3),
            risk_factors=_item(1),
            concomitant_drugs=_item(-1),
            other_causes_excluded=_item(2),
            known_hepatotoxicity=_item(2),
            rechallenge=_item(0),
        )
    )

    scores = item_scores(assessment)

    assert scores == {
        "time_to_onset": 2,
        "course": 3,
        "risk_factors": 1,
        "concomitant_drugs": -1,
        "other_causes_excluded": 2,
        "known_hepatotoxicity": 2,
        "rechallenge": 0,
    }


def test_minus_three_stands_where_a_cause_is_documented_present():
    """Step 1 asks whether a present cause is a highly probable explanation.

    The eight statuses cannot answer that, so the model keeps the call. What
    the checklist does answer is the precondition: some cause must be present.
    """
    assessment = RucamAssessment.model_validate(
        _assessment(
            item_5_causes=_causes(5, hypotension_or_ischaemia="present"),
            other_causes_excluded=_item(-3),
        )
    )

    assert has_cause_documented_present(assessment.item_5_causes) is True
    assert item_scores(assessment)["other_causes_excluded"] == -3


def test_minus_three_falls_through_when_no_cause_is_present():
    """-3 with nothing documented present is unreachable on the sheet."""
    assessment = RucamAssessment.model_validate(
        _assessment(item_5_causes=_causes(5), other_causes_excluded=_item(-3))
    )

    assert has_cause_documented_present(assessment.item_5_causes) is False
    assert item_scores(assessment)["other_causes_excluded"] == 0
