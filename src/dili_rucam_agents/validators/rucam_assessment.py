"""The JSON object an analyst returns, and the code that scores it.

The model supplies the clinical judgement for each of the seven RUCAM items and
nothing arithmetic: no total, no category and no ``g1``. ``score_assessment``
sums the items and derives the category, ``g1`` is recomputed from the eight
cause flags, and evidence quotes are never marked verified by the model.

``EvidenceRef.verified`` is model-facing: validation always resets it to False,
including on ``model_validate(model_dump())``. Code that certifies a quote must
keep that fact somewhere a reload cannot erase; mutating the field is lost on
the next round trip.
"""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from dili_rucam_agents.validators.rucam_json import (
    CausalityCategory,
    InjuryPattern,
    check_item_score,
    expected_category,
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


def _non_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


Answer = Annotated[str, AfterValidator(_non_blank)]


class DataStatus(StrEnum):
    REPORTED = "reported"
    INFERRED = "inferred"
    NOT_REPORTED = "not_reported"


class EvidenceRef(_Strict):
    quote: str
    verified: bool = False

    @field_validator("verified", mode="after")
    @classmethod
    def _only_code_certifies(cls, _value: bool) -> bool:
        return False


class ItemScore(_Strict):
    score: int
    data_status: DataStatus
    rationale: str
    inference_note: str | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _inference_is_explained(self) -> ItemScore:
        if self.data_status is DataStatus.INFERRED and not (
            self.inference_note and self.inference_note.strip()
        ):
            raise ValueError("inference_note is required when data_status is inferred")
        return self


class PreFlight(_Strict):
    """One answer per pre-flight line of the rules core (section 4)."""

    suspect_drug: Answer
    onset: Answer
    r_ratio_pattern: Answer
    course_basis: Answer
    g1_count: Answer
    item_6_source: Answer
    scores_in_legal_sets: Answer
    total_sum: Answer
    category_match: Answer
    section_b_matches_section_c: Answer
    notes_complete: Answer


class CauseStatus(StrEnum):
    RULED_OUT = "ruled_out"
    NOT_DOCUMENTED = "not_documented"
    PRESENT = "present"


#: The six Group I causes, in the order the sheet prints them.
GROUP_I_CAUSES = (
    "hepatitis_a",
    "hepatitis_b",
    "hepatitis_c",
    "biliary_obstruction",
    "alcoholism",
    "hypotension_or_ischaemia",
)
#: The two Group II categories. Not part of g1; they enter the +2 rung only.
GROUP_II_CAUSES = ("underlying_disease", "acute_viral_infection")


class ItemFiveCauses(_Strict):
    # Group I: exactly six causes.
    hepatitis_a: CauseStatus
    hepatitis_b: CauseStatus
    hepatitis_c: CauseStatus
    biliary_obstruction: CauseStatus
    alcoholism: CauseStatus
    hypotension_or_ischaemia: CauseStatus
    # Group II: exactly two categories.
    underlying_disease: CauseStatus
    acute_viral_infection: CauseStatus
    g1: int

    @property
    def group_ii_ruled_out(self) -> bool:
        return (
            self.underlying_disease is CauseStatus.RULED_OUT
            and self.acute_viral_infection is CauseStatus.RULED_OUT
        )

    @model_validator(mode="after")
    def _g1_matches_flags(self) -> ItemFiveCauses:
        counted = sum(
            getattr(self, name) is CauseStatus.RULED_OUT for name in GROUP_I_CAUSES
        )
        if self.g1 != counted:
            raise ValueError(
                f"g1 is {self.g1} but {counted} Group I causes are ruled_out"
            )
        return self


class RRatioBasis(_Strict):
    alt: float | None = Field(ge=0)
    alp: float | None = Field(gt=0)  # ALP 0 would make R infinite
    alt_uln: float = Field(gt=0)
    alp_uln: float = Field(gt=0)


# The prompt rounds R to 2 dp (error <= 0.005), so a flat 0.01 absorbs the
# rounding. It is the threshold for annotating a disagreement, not for rejecting
# one: see ``r_ratio_disagreement``.
R_RATIO_ABS_TOL = 0.01

_ITEMS = (
    "time_to_onset",
    "course",
    "risk_factors",
    "concomitant_drugs",
    "other_causes_excluded",
    "known_hepatotoxicity",
    "rechallenge",
)


class RucamAssessment(_Strict):
    injury_pattern: InjuryPattern
    r_ratio: float | None = Field(ge=0)
    r_ratio_basis: RRatioBasis
    onset_day: int | None = Field(ge=1)  # numbered from the dose day; no day 0
    onset_anchor_dose: str
    pre_flight: PreFlight
    item_5_causes: ItemFiveCauses
    time_to_onset: ItemScore
    course: ItemScore
    risk_factors: ItemScore
    concomitant_drugs: ItemScore
    other_causes_excluded: ItemScore
    known_hepatotoxicity: ItemScore
    rechallenge: ItemScore
    #: Rules-mandated ``notes`` entries that name no single item, so no item's
    #: ``inference_note`` can hold them: a default ULN was applied (appendix
    #: A.2), or R could not be computed at all. The renderer concatenates these
    #: into SECTION C's ``notes`` ahead of the per-item ones.
    assessment_notes: list[str] = Field(default_factory=list)

    @field_validator(*_ITEMS, mode="after")
    @classmethod
    def _score_is_a_printed_rung(cls, value: ItemScore, info: ValidationInfo):
        check_item_score(info.field_name, value.score)
        return value


class ScoredAssessment(_Strict):
    total_score: int
    category: CausalityCategory


def item_five_rung(causes: ItemFiveCauses) -> int:
    """Steps 2-5 of the printed item-5 procedure, which are pure arithmetic.

    `g1 = 6` with both Group II categories ruled out is +2, `g1 = 6` alone is
    +1, 5 or 4 is 0, and 3 or fewer is -2. Nothing here is a judgement: the
    judgement is each cause's status, which the model supplies.
    """
    if causes.g1 == 6:
        return 2 if causes.group_ii_ruled_out else 1
    if causes.g1 in (5, 4):
        return 0
    return -2


def has_cause_documented_present(causes: ItemFiveCauses) -> bool:
    """Whether step 1's precondition holds: some cause is documented present."""
    return any(
        getattr(causes, name) is CauseStatus.PRESENT
        for name in GROUP_I_CAUSES + GROUP_II_CAUSES
    )


def item_scores(assessment: RucamAssessment) -> dict[str, int]:
    """Every item's score as code holds it, which is not always as stated.

    Six are the model's own. Item 5's is derived from the Group I/II checklist
    the same reply carries, for the reason `total_score` and `category` are
    computed rather than accepted: steps 2-5 of its printed procedure are
    arithmetic over that checklist and nothing else, and arithmetic is not the
    model's to get wrong. A run on 2026-11-12 had one analyst of five mark
    `underlying_disease` as `not_documented` -- so `g1 = 6` without Group II,
    which the sheet scores +1 -- and then claim +2, which every legal-set check
    passed because 2 is a legal item-5 score.

    Step 1's -3 rung is left alone. It needs a cause to be *a highly probable
    explanation* of the injury, which is a reading the eight statuses cannot
    hold, so a claimed -3 stands wherever some cause is documented present. It
    falls through to the arithmetic when none is: that half of step 1 the
    checklist does answer.
    """
    scores = {field: getattr(assessment, field).score for field in _ITEMS}
    if scores["other_causes_excluded"] == -3 and has_cause_documented_present(
        assessment.item_5_causes
    ):
        return scores
    scores["other_causes_excluded"] = item_five_rung(assessment.item_5_causes)
    return scores


def score_assessment(assessment: RucamAssessment) -> ScoredAssessment:
    total = sum(item_scores(assessment).values())
    return ScoredAssessment(total_score=total, category=expected_category(total))


def computed_r_ratio(assessment: RucamAssessment) -> float | None:
    """R from the four numbers of ``r_ratio_basis``, or None when it needs one."""
    basis = assessment.r_ratio_basis
    if basis.alt is None or basis.alp is None:
        return None
    return (basis.alt / basis.alt_uln) / (basis.alp / basis.alp_uln)


def r_ratio_disagreement(assessment: RucamAssessment) -> float | None:
    """The computed R, when it differs from the stated one beyond the tolerance.

    This is reported, never enforced. Rejecting the reply over it would spend a
    validation retry, and the correction turn can change ``r_ratio`` -- which
    selects the injury pattern, which selects the column items 1 and 2 are read
    off. That moves a score for a reason other than removing framework noise,
    which the design's anti-goals forbid, and the straddle the check was
    tightened to catch is inherent at any non-zero tolerance anyway. So the
    renderer prints both numbers and leaves the score alone.
    """
    computed = computed_r_ratio(assessment)
    if assessment.r_ratio is None or computed is None:
        return None
    if math.isclose(
        assessment.r_ratio, computed, rel_tol=0.0, abs_tol=R_RATIO_ABS_TOL
    ):
        return None
    return computed


__all__ = [
    "GROUP_I_CAUSES",
    "GROUP_II_CAUSES",
    "R_RATIO_ABS_TOL",
    "CauseStatus",
    "DataStatus",
    "EvidenceRef",
    "ItemFiveCauses",
    "ItemScore",
    "PreFlight",
    "RRatioBasis",
    "RucamAssessment",
    "ScoredAssessment",
    "computed_r_ratio",
    "has_cause_documented_present",
    "item_five_rung",
    "item_scores",
    "r_ratio_disagreement",
    "score_assessment",
]
