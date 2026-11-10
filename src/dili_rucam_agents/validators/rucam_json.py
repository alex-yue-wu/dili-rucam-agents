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


ITEM_LEGAL_SCORES: dict[str, frozenset[int]] = {
    "time_to_onset": frozenset({0, 1, 2}),
    "course": frozenset({-2, 0, 1, 2, 3}),
    "risk_factors": frozenset({0, 1, 2}),
    "concomitant_drugs": frozenset({-3, -2, -1, 0}),
    "other_causes_excluded": frozenset({-3, -2, 0, 1, 2}),
    "known_hepatotoxicity": frozenset({0, 1, 2}),
    "rechallenge": frozenset({-2, 0, 1, 3}),
}


def check_item_score(item: str, value: int) -> int:
    """Return ``value`` when it is a printed rung of ``item``, else raise."""

    if value not in ITEM_LEGAL_SCORES[item]:
        legal = sorted(ITEM_LEGAL_SCORES[item])
        raise ValueError(f"{item} score {value} is not one of {legal}")
    return value


class RucamScores(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    time_to_onset: int
    course: int
    risk_factors: int
    concomitant_drugs: int
    other_causes_excluded: int = Field(
        validation_alias=AliasChoices(
            "other_causes_excluded", "alternative_causes_excluded"
        ),
    )
    known_hepatotoxicity: int
    rechallenge: int

    @model_validator(mode="after")
    def validate_legal_sets(self) -> RucamScores:
        for item in ITEM_LEGAL_SCORES:
            check_item_score(item, getattr(self, item))
        return self

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
    model_config = ConfigDict(allow_inf_nan=False)

    injury_pattern: InjuryPattern
    R_ratio: float | None = Field(ge=0)
    rucam_scores: RucamScores
    total_score: int
    category: CausalityCategory

    @model_validator(mode="after")
    def validate_score_and_category(self) -> RucamReport:
        if self.total_score != self.rucam_scores.total:
            raise ValueError("total_score does not match the seven-item score sum")
        expected = expected_category(self.total_score)
        if self.category != expected:
            raise ValueError("category does not match total_score")
        return self


def validate_rucam_json(payload: dict[str, Any] | str) -> RucamReport:
    """Validate final SECTION C output and raise helpful errors on mismatch."""

    if isinstance(payload, str):
        payload = json.loads(
            payload,
            parse_constant=_reject_non_standard_json_constant,
        )

    try:
        return RucamReport.model_validate(payload)
    except ValidationError as exc:  # pragma: no cover - formatting
        diagnostics = []
        for error in exc.errors(
            include_url=False,
            include_context=False,
            include_input=False,
        ):
            location = ".".join(str(part) for part in error["loc"]) or "report"
            diagnostics.append(f"{location}: {error['msg']}")
        raise ValueError(f"Invalid RUCAM JSON: {'; '.join(diagnostics)}") from exc


def _reject_non_standard_json_constant(_constant: str) -> None:
    raise ValueError("non-standard JSON numeric constants are not allowed")


__all__ = [
    "CausalityCategory",
    "ITEM_LEGAL_SCORES",
    "check_item_score",
    "RucamScores",
    "RucamReport",
    "expected_category",
    "validate_rucam_json",
]
