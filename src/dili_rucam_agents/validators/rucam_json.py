from __future__ import annotations

import json
from typing import Any, Dict, Literal

from pydantic import BaseModel, Field, ValidationError, validator


InjuryPattern = Literal["hepatocellular", "mixed", "cholestatic"]
CausalityCategory = Literal["Excluded", "Unlikely", "Possible", "Probable", "Highly probable"]


class RucamScores(BaseModel):
    time_to_onset: int = Field(..., ge=-3, le=3)
    course: int = Field(..., ge=-3, le=3)
    risk_factors: int = Field(..., ge=-2, le=2)
    concomitant_drugs: int = Field(..., ge=-3, le=3)
    alternative_causes_excluded: int = Field(..., ge=-3, le=3)
    known_hepatotoxicity: int = Field(..., ge=-3, le=3)
    rechallenge: int = Field(..., ge=-3, le=3)

    @property
    def total(self) -> int:
        return sum(
            [
                self.time_to_onset,
                self.course,
                self.risk_factors,
                self.concomitant_drugs,
                self.alternative_causes_excluded,
                self.known_hepatotoxicity,
                self.rechallenge,
            ]
        )


class RucamReport(BaseModel):
    injury_pattern: InjuryPattern
    R_ratio: float = Field(..., ge=0)
    rucam_scores: RucamScores
    total_score: int
    category: CausalityCategory

    @validator("total_score")
    def validate_total_score(cls, value: int, values: Dict[str, Any]) -> int:
        scores: RucamScores | None = values.get("rucam_scores")
        if scores and value != scores.total:
            raise ValueError(f"total_score {value} does not match item sum {scores.total}")
        return value


def validate_rucam_json(payload: Dict[str, Any] | str) -> RucamReport:
    """Validate final SECTION C output and raise with helpful errors on mismatch."""

    if isinstance(payload, str):
        payload = json.loads(payload)

    try:
        return RucamReport.model_validate(payload)
    except ValidationError as exc:  # pragma: no cover - formatting
        raise ValueError(f"Invalid RUCAM JSON: {exc}") from exc


__all__ = ["RucamScores", "RucamReport", "validate_rucam_json"]
