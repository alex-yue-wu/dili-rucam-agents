"""The declared analyst roster: who scores a case, on which vendor, and how much.

The roster is a JSON file, so the number of analysts is whatever the file lists
(one or more) rather than a fixed tuple in code. This module owns only the
declaration and its validation; it imports nothing from the provider layer,
which depends on ``Provider`` and ``AnalystConfig`` from here.
"""

from __future__ import annotations

from enum import StrEnum
import json
from pathlib import Path
import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

#: Repository root, three levels above this file (src/dili_rucam_agents/roster.py).
DEFAULT_ROSTER_PATH = Path(__file__).resolve().parents[2] / "config" / "analysts.json"

_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


class RosterError(ValueError):
    """The roster file is missing, unreadable, malformed or inconsistent."""


class Provider(StrEnum):
    openai = "openai"
    anthropic = "anthropic"
    google = "google"
    openrouter = "openrouter"
    deepseek = "deepseek"


class AnalystConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(pattern=_KEY_PATTERN.pattern)
    label: str = Field(min_length=1)
    provider: Provider
    model: str = Field(min_length=1)
    max_output_tokens: int = Field(default=32000, gt=0)

    @property
    def report_filename(self) -> str:
        return f"{self.key.replace('_', '-')}_report.md"


class GroundTruthConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: Provider
    model: str = Field(min_length=1)
    max_output_tokens: int = Field(default=8000, gt=0)


class AnalystRoster(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    analysts: list[AnalystConfig] = Field(min_length=1)
    ground_truth: GroundTruthConfig

    @model_validator(mode="after")
    def _keys_are_unique(self) -> AnalystRoster:
        # Each key names a report file and a workbook column; a repeat would
        # make one analyst silently overwrite another.
        seen: set[str] = set()
        for analyst in self.analysts:
            if analyst.key in seen:
                raise ValueError(f"duplicate analyst key {analyst.key!r}")
            seen.add(analyst.key)
        return self


def load_roster(path: Path | str | None = None) -> AnalystRoster:
    """Load and validate a roster file, naming the path in every failure."""
    resolved = Path(path) if path is not None else DEFAULT_ROSTER_PATH
    try:
        raw = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise RosterError(f"cannot read analyst roster {resolved}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RosterError(f"analyst roster {resolved} is not valid JSON: {exc}") from exc
    try:
        return AnalystRoster.model_validate(data)
    except ValidationError as exc:
        raise RosterError(f"invalid analyst roster {resolved}: {exc}") from exc
