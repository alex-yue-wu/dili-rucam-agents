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
            status is CauseStatus.RULED_OUT
            for status in (
                self.hepatitis_a,
                self.hepatitis_b,
                self.hepatitis_c,
                self.biliary_obstruction,
                self.alcoholism,
                self.hypotension_or_ischaemia,
            )
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


# The prompt rounds R to 2 dp (error <= 0.005). A flat 0.01 absorbs that without
# letting a stated R straddle the 2 or 5 boundary that picks the scoring table.
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

    @field_validator(*_ITEMS, mode="after")
    @classmethod
    def _score_is_a_printed_rung(cls, value: ItemScore, info: ValidationInfo):
        check_item_score(info.field_name, value.score)
        return value

    @model_validator(mode="after")
    def _r_ratio_matches_basis(self) -> RucamAssessment:
        basis = self.r_ratio_basis
        if (
            self.r_ratio is None
            or basis.alt is None
            or basis.alp is None
        ):
            return self
        computed = (basis.alt / basis.alt_uln) / (basis.alp / basis.alp_uln)
        if not math.isclose(
            self.r_ratio, computed, rel_tol=0.0, abs_tol=R_RATIO_ABS_TOL
        ):
            raise ValueError(
                f"r_ratio {self.r_ratio} disagrees with r_ratio_basis "
                f"(computes to {computed:.2f})"
            )
        return self


class ScoredAssessment(_Strict):
    total_score: int
    category: CausalityCategory


def score_assessment(assessment: RucamAssessment) -> ScoredAssessment:
    total = sum(getattr(assessment, item).score for item in _ITEMS)
    return ScoredAssessment(total_score=total, category=expected_category(total))


__all__ = [
    "CauseStatus",
    "DataStatus",
    "EvidenceRef",
    "ItemFiveCauses",
    "ItemScore",
    "PreFlight",
    "RRatioBasis",
    "RucamAssessment",
    "ScoredAssessment",
    "score_assessment",
]
