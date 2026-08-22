from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)


InjuryPattern = Literal["hepatocellular", "mixed", "cholestatic", "Not reported"]
CausalityCategory = Literal[
    "Excluded", "Unlikely", "Possible", "Probable", "Highly probable"
]


def expected_category(total_score: int) -> CausalityCategory:
    if total_score <= 0:
        return "Excluded"
    if total_score <= 2:
        return "Unlikely"
    if total_score <= 5:
        return "Possible"
    if total_score <= 8:
        return "Probable"
    return "Highly probable"


class RucamScores(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    time_to_onset: int = Field(ge=-3, le=3)
    course: int = Field(ge=-3, le=3)
    risk_factors: int = Field(ge=-2, le=2)
    concomitant_drugs: int = Field(ge=-3, le=3)
    other_causes_excluded: int = Field(
        ge=-3,
        le=3,
        validation_alias=AliasChoices(
            "other_causes_excluded", "alternative_causes_excluded"
        ),
    )
    known_hepatotoxicity: int = Field(ge=-3, le=3)
    rechallenge: int = Field(ge=-3, le=3)

    @property
    def total(self) -> int:
        return sum(
            (
                self.time_to_onset,
                self.course,
                self.risk_factors,
                self.concomitant_drugs,
                self.other_causes_excluded,
                self.known_hepatotoxicity,
                self.rechallenge,
            )
        )


class RucamReport(BaseModel):
    injury_pattern: InjuryPattern
    R_ratio: float | None = Field(ge=0)
    rucam_scores: RucamScores
    total_score: int
    category: CausalityCategory

    @model_validator(mode="after")
    def validate_score_and_category(self) -> RucamReport:
        if self.total_score != self.rucam_scores.total:
            raise ValueError(
                f"total_score {self.total_score} does not match item sum "
                f"{self.rucam_scores.total}"
            )
        expected = expected_category(self.total_score)
        if self.category != expected:
            raise ValueError(
                f"category {self.category} does not match total_score "
                f"{self.total_score} ({expected})"
            )
        return self


def validate_rucam_json(payload: dict[str, Any] | str) -> RucamReport:
    """Validate final SECTION C output and raise helpful errors on mismatch."""

    if isinstance(payload, str):
        payload = json.loads(payload)

    try:
        return RucamReport.model_validate(payload)
    except ValidationError as exc:  # pragma: no cover - formatting
        raise ValueError(f"Invalid RUCAM JSON: {exc}") from exc


__all__ = [
    "CausalityCategory",
    "RucamScores",
    "RucamReport",
    "expected_category",
    "validate_rucam_json",
]
