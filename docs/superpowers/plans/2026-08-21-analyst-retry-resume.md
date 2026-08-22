# Analyst Retry and Resume Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restart an incomplete analyst at most twice, persist only validated outputs, and resume later batch invocations from the first analyst without a compatible successful checkpoint.

**Architecture:** A shared report validator becomes the definition of analyst success. The isolated analyst runner emits typed attempt events while creating a fresh CrewAI agent/task/crew for each attempt; a pipeline-owned checkpoint store atomically persists validated reports, attempt diagnostics, and per-analyst fingerprints. Batch completion and force-rerun behavior use those same checkpoint rules.

**Tech Stack:** Python 3.12, CrewAI, Pydantic 2, pytest, openpyxl, uv

**Spec:** `docs/superpowers/specs/2026-08-21-analyst-retry-resume-design.md`

## Global Constraints

- Permit `max_restarts` values from `0` through `2`; default to `2`, meaning three total attempts.
- Keep analyst execution sequential and preserve fail-fast behavior after retry exhaustion.
- Do not retry deterministic PDF ingestion failures.
- Do not change the configured analyst model lineup or RUCAM scoring prompts.
- Persist canonical analyst reports only after shared validation succeeds.
- Reuse a report only when its checkpoint fingerprint matches and shared validation succeeds.
- Keep current canonical report filenames unchanged.
- Use `uv` for every test command.
- All tests must run without live model API calls.

## File Structure

- Create `src/dili_rucam_agents/validators/analyst_report.py`: extract strict or legacy Section C JSON and validate complete analyst markdown.
- Modify `src/dili_rucam_agents/validators/rucam_json.py`: align field names, missing values, totals, and category consistency with the production contract.
- Create `src/dili_rucam_agents/checkpoints.py`: compute analyst fingerprints, read/write the versioned manifest, adopt legacy reports, and perform atomic writes.
- Modify `src/dili_rucam_agents/crew/tasks.py`: accept a retry instruction when constructing an analyst task.
- Modify `src/dili_rucam_agents/crew/crew.py`: create fresh attempt objects, validate every result, emit attempt events, and enforce restart limits.
- Modify `src/dili_rucam_agents/crew/agents.py`: expose the effective max-output-token resolver used by checkpoint fingerprints.
- Modify `src/dili_rucam_agents/pipeline.py`: integrate checkpoint loading, atomic persistence, attempt artifacts, resume control, and single-PDF CLI arguments.
- Modify `src/dili_rucam_agents/batch.py`: use compatible checkpoint completion, propagate retries/resume, and expose the batch CLI argument.
- Create `tests/test_analyst_report_validator.py`: report and schema boundary coverage.
- Create `tests/test_checkpoints.py`: manifest, fingerprint, legacy adoption, and atomic persistence coverage.
- Modify `tests/test_rucam_json_validator.py`, `tests/test_crew_topology.py`, and `tests/test_batch.py`: update fixtures and add retry/resume integration coverage.
- Modify `README.md` and `docs/exec-plans/active/agent-redesign.md`: document behavior and verification.

---

### Task 1: Establish the Canonical Analyst Report Contract

**Files:**
- Create: `src/dili_rucam_agents/validators/analyst_report.py`
- Modify: `src/dili_rucam_agents/validators/rucam_json.py`
- Create: `tests/test_analyst_report_validator.py`
- Modify: `tests/test_rucam_json_validator.py`

**Interfaces:**
- Produces: `ValidatedAnalystReport`, `AnalystReportValidationError`, `parse_section_c_payload(report_text: str, *, allow_legacy_json: bool = False) -> dict[str, Any]`, and `validate_analyst_report(report_text: str, *, allow_legacy_json: bool = False) -> ValidatedAnalystReport`.
- Produces: `expected_category(total_score: int) -> CausalityCategory` and a `RucamReport` whose canonical score key is `other_causes_excluded`.
- Consumes: no new interfaces.

- [ ] **Step 1: Write schema tests for canonical names, legacy aliases, missing pattern/R-ratio values, total sums, and category mapping**

Add these cases to `tests/test_rucam_json_validator.py`:

```python
import pytest


def valid_payload() -> dict:
    return {
        "injury_pattern": "hepatocellular",
        "R_ratio": 6.4,
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
    payload["rucam_scores"]["alternative_causes_excluded"] = payload["rucam_scores"].pop(
        "other_causes_excluded"
    )
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
    with pytest.raises(ValueError, match="category Possible does not match total_score 6"):
        validate_rucam_json(payload)
```

- [ ] **Step 2: Write complete-report validation tests**

Create `tests/test_analyst_report_validator.py` with a canonical fixture and focused failures:

```python
import json
import pytest

from dili_rucam_agents.validators.analyst_report import (
    AnalystReportValidationError,
    parse_section_c_payload,
    validate_analyst_report,
)


VALID_PAYLOAD = {
    "injury_pattern": "hepatocellular",
    "R_ratio": 6.4,
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


def report_for(payload: dict = VALID_PAYLOAD) -> str:
    return (
        "## SECTION A — HUMAN-READABLE FULL REPORT\n\nClinical summary.\n\n"
        "## SECTION B — RUCAM SCORING TABLE\n\n| Item | Score |\n| --- | --- |\n| Total | 6 |\n\n"
        "## SECTION C — MACHINE-READABLE JSON\n\n"
        f"```json\n{json.dumps(payload)}\n```\n"
    )


def test_validate_analyst_report_returns_typed_payload():
    result = validate_analyst_report(report_for())
    assert result.payload.total_score == 6
    assert result.canonical_payload["rucam_scores"]["other_causes_excluded"] == 2


@pytest.mark.parametrize(
    ("report_text", "message"),
    [
        ("", "empty"),
        ("See complete Sections A, B, and C above.", "summary placeholder"),
        ("## SECTION B\n\nTable\n\n## SECTION C\n\n```json\n{}\n```", "SECTION A"),
        ("## SECTION A\n\nText\n\n## SECTION C\n\n```json\n{}\n```", "SECTION B"),
        ("## SECTION A\n\nText\n\n## SECTION B\n\nTable", "SECTION C"),
    ],
)
def test_validate_analyst_report_rejects_incomplete_sections(report_text, message):
    with pytest.raises(AnalystReportValidationError, match=message):
        validate_analyst_report(report_text)


def test_strict_validation_rejects_unfenced_section_c_json():
    unfenced = report_for().replace("```json\n", "").replace("\n```\n", "\n")
    with pytest.raises(AnalystReportValidationError, match="fenced JSON"):
        validate_analyst_report(unfenced)


def test_legacy_parser_accepts_unfenced_section_c_json():
    unfenced = report_for().replace("```json\n", "").replace("\n```\n", "\n")
    assert parse_section_c_payload(unfenced, allow_legacy_json=True)["total_score"] == 6
```

- [ ] **Step 3: Run the new tests and confirm they fail for the missing interfaces and old field contract**

Run:

```bash
uv run pytest tests/test_rucam_json_validator.py tests/test_analyst_report_validator.py -q
```

Expected: failures show that `analyst_report` does not exist, `other_causes_excluded` is not accepted, and category consistency is not enforced.

- [ ] **Step 4: Align the Pydantic RUCAM schema**

Replace the legacy field and validators in `rucam_json.py` with Pydantic 2 constructs equivalent to:

```python
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError, model_validator

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
    def validate_score_and_category(self) -> "RucamReport":
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
```

Keep `validate_rucam_json` wrapping Pydantic errors as `ValueError`, and export `expected_category`.

- [ ] **Step 5: Implement strict and legacy report parsing**

Implement `analyst_report.py` with:

```python
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any

from .rucam_json import RucamReport, validate_rucam_json

_SECTION_HEADING_RE = re.compile(
    r"(?im)^##\s+\*{0,2}SECTION\s+([ABC])\b[^\n]*"
)
_JSON_FENCE_RE = re.compile(r"```json\s*(.*?)\s*```", re.IGNORECASE | re.DOTALL)


class AnalystReportValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedAnalystReport:
    payload: RucamReport
    canonical_payload: dict[str, Any]


def parse_section_c_payload(
    report_text: str, *, allow_legacy_json: bool = False
) -> dict[str, Any]:
    headings = list(_SECTION_HEADING_RE.finditer(report_text))
    section_c = next((match for match in headings if match.group(1).upper() == "C"), None)
    if section_c is None:
        raise AnalystReportValidationError("Unable to locate SECTION C.")
    section_text = report_text[section_c.end() :]
    fence = _JSON_FENCE_RE.search(section_text)
    if fence is not None:
        json_text = fence.group(1)
    elif allow_legacy_json:
        json_text = _extract_balanced_json_object(section_text)
    else:
        raise AnalystReportValidationError("SECTION C must contain fenced JSON.")
    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError as exc:
        recovered = _recover_section_c_payload(json_text) if allow_legacy_json else None
        if recovered is None:
            raise AnalystReportValidationError(f"Invalid SECTION C JSON: {exc}") from exc
        payload = recovered
    if not isinstance(payload, dict):
        raise AnalystReportValidationError("SECTION C JSON must be an object.")
    return payload


def validate_analyst_report(
    report_text: str, *, allow_legacy_json: bool = False
) -> ValidatedAnalystReport:
    stripped = report_text.strip()
    if not stripped:
        raise AnalystReportValidationError("Report is empty.")
    if "see complete sections a, b, and c above." in stripped.lower():
        raise AnalystReportValidationError("Report is a summary placeholder.")
    headings = list(_SECTION_HEADING_RE.finditer(report_text))
    by_name = {match.group(1).upper(): match for match in headings}
    for required in ("A", "B", "C"):
        if required not in by_name:
            raise AnalystReportValidationError(f"Missing SECTION {required}.")
    ordered = sorted(headings, key=lambda match: match.start())
    for required in ("A", "B"):
        match = by_name[required]
        position = ordered.index(match)
        end = ordered[position + 1].start() if position + 1 < len(ordered) else len(report_text)
        if not report_text[match.end() : end].strip(" \t\r\n-"):
            raise AnalystReportValidationError(f"SECTION {required} is empty.")
    payload = parse_section_c_payload(
        report_text, allow_legacy_json=allow_legacy_json
    )
    try:
        typed = validate_rucam_json(payload)
    except ValueError as exc:
        raise AnalystReportValidationError(str(exc)) from exc
    return ValidatedAnalystReport(
        payload=typed,
        canonical_payload=typed.model_dump(),
    )
```

Implement `_extract_balanced_json_object(section_text: str) -> str` by moving the existing balanced-brace scanner from `batch.py`; raise `AnalystReportValidationError` when no complete object is found. Move `_recover_section_c_payload` and its field-extraction helpers from `batch.py` into this module as well. Call recovery only when `allow_legacy_json=True` and `json.loads` fails; strict validation of new attempts must never call recovery.

- [ ] **Step 6: Run validator tests and the existing parser tests**

Run:

```bash
uv run pytest tests/test_rucam_json_validator.py tests/test_analyst_report_validator.py tests/test_batch.py -q
```

Expected: validator tests pass. Existing batch parser tests may still use their current tolerant helper until Task 5 rewires the compatibility wrapper.

- [ ] **Step 7: Commit the canonical validation boundary**

```bash
git add src/dili_rucam_agents/validators/rucam_json.py src/dili_rucam_agents/validators/analyst_report.py tests/test_rucam_json_validator.py tests/test_analyst_report_validator.py
git commit -m "feat: validate complete analyst reports"
```

---

### Task 2: Restart Only the Invalid Analyst

**Files:**
- Modify: `src/dili_rucam_agents/crew/tasks.py`
- Modify: `src/dili_rucam_agents/crew/crew.py`
- Modify: `tests/test_crew_topology.py`

**Interfaces:**
- Consumes: `validate_analyst_report(report_text: str) -> ValidatedAnalystReport` from Task 1.
- Produces: `AnalystAttemptEvent`, `AnalystExecutionError`, and `validate_max_restarts(max_restarts: int) -> int`.
- Changes `run_crew` by adding keyword-only `max_restarts: int = 2` and `on_attempt: Callable[[AnalystAttemptEvent], None] | None = None` parameters.
- Changes `create_analysis_task` by adding keyword-only `retry_instruction: str | None = None`.

- [ ] **Step 1: Add test helpers that always generate contract-valid analyst reports**

In `tests/test_crew_topology.py`, add:

```python
import json
import pytest


def complete_report(total_score: int = 6, narrative: str = "Clinical summary") -> str:
    payload = {
        "injury_pattern": "hepatocellular",
        "R_ratio": 6.4,
        "rucam_scores": {
            "time_to_onset": 2,
            "course": 1,
            "risk_factors": 0,
            "concomitant_drugs": 0,
            "other_causes_excluded": 2,
            "known_hepatotoxicity": 1,
            "rechallenge": 0,
        },
        "total_score": total_score,
        "category": "Probable",
    }
    return (
        f"## SECTION A\n\n{narrative}\n\n"
        "## SECTION B\n\n| Item | Score |\n| --- | --- |\n| Total | 6 |\n\n"
        f"## SECTION C\n\n```json\n{json.dumps(payload)}\n```\n"
    )
```

Replace dummy successful outputs such as `"alpha"` or Section A-only text with `complete_report(narrative="alpha")`. Leave explicitly invalid outputs unchanged.

- [ ] **Step 2: Write retry, exhaustion, fresh-instance, and event tests**

Add this reusable fake installer and tests. The factory returns a new crew/task pair on every construction and records analyst order:

```python
def install_retry_scenario(monkeypatch, outputs_by_key):
    constructed_keys = []

    class DummyBundle:
        def to_dict(self):
            return {
                "pdf_path": "dummy.pdf",
                "extraction_notes": [],
                "blocks": [],
                "normalized_text": "ALT 650",
                "tables": [],
                "unknowns": [],
                "quality": {
                    "unstructured_total_score": 1,
                    "fallback_pages": [],
                    "fallback_total_score": 0,
                },
            }

    class DummyTask:
        output = None

    class DummyCrew:
        def __init__(self, result):
            self.result = result

        def kickoff(self, inputs):
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    configs = [
        {"key": key, "label": key.replace("_", " ").title()}
        for key in outputs_by_key
    ]

    def fake_build_run(*, config, **kwargs):
        constructed_keys.append(config["key"])
        return DummyCrew(next(outputs_by_key[config["key"]])), DummyTask()

    monkeypatch.setattr(
        "dili_rucam_agents.crew.crew.build_case_bundle", lambda path: DummyBundle()
    )
    monkeypatch.setattr(
        "dili_rucam_agents.crew.crew.get_enabled_analyst_configs",
        lambda **flags: configs,
    )
    monkeypatch.setattr(
        "dili_rucam_agents.crew.crew._build_isolated_analyst_run", fake_build_run
    )
    return constructed_keys


def test_run_crew_restarts_only_invalid_analyst_until_third_attempt(monkeypatch):
    outputs_by_key = {
        "analyst_alpha": iter(["incomplete", "still incomplete", complete_report()])
    }
    constructed_keys = install_retry_scenario(monkeypatch, outputs_by_key)
    events = []

    _, reports = run_crew(
        "dummy.pdf", capture_reports=True, max_restarts=2, on_attempt=events.append
    )

    assert constructed_keys == ["analyst_alpha"] * 3
    assert reports["analyst_alpha"].startswith("## SECTION A")
    assert [event.status for event in events] == [
        "running",
        "validation_failed",
        "running",
        "validation_failed",
        "running",
        "completed",
    ]


def test_run_crew_raises_after_three_invalid_attempts(monkeypatch):
    constructed_keys = install_retry_scenario(
        monkeypatch,
        {"analyst_alpha": iter(["incomplete", "incomplete", "incomplete"])},
    )
    with pytest.raises(AnalystExecutionError) as exc_info:
        run_crew("dummy.pdf", capture_reports=True, max_restarts=2)
    assert constructed_keys == ["analyst_alpha"] * 3
    assert exc_info.value.analyst_key == "analyst_alpha"
    assert exc_info.value.attempts == 3
    assert exc_info.value.failure_kind == "validation"


def test_run_crew_skips_completed_alpha_and_retries_beta(monkeypatch):
    constructed_keys = install_retry_scenario(
        monkeypatch,
        {
            "analyst_alpha": iter(()),
            "analyst_beta": iter(["incomplete", complete_report(narrative="beta")]),
            "analyst_gamma": iter([complete_report(narrative="gamma")]),
        },
    )
    _, reports = run_crew(
        "dummy.pdf",
        capture_reports=True,
        completed_reports={"analyst_alpha": complete_report(narrative="saved alpha")},
        max_restarts=2,
    )
    assert constructed_keys == ["analyst_beta", "analyst_beta", "analyst_gamma"]
    assert "saved alpha" in reports["analyst_alpha"]


def test_run_crew_restarts_after_execution_exception(monkeypatch):
    install_retry_scenario(
        monkeypatch,
        {"analyst_alpha": iter([RuntimeError("provider unavailable"), complete_report()])},
    )
    events = []
    run_crew("dummy.pdf", capture_reports=True, on_attempt=events.append)
    assert [event.status for event in events] == [
        "running",
        "execution_failed",
        "running",
        "completed",
    ]
```

- [ ] **Step 3: Run the new crew tests and confirm retry interfaces are missing**

Run:

```bash
uv run pytest tests/test_crew_topology.py -q
```

Expected: failures identify the missing event/error classes, missing fresh-run builder, and missing `max_restarts` behavior.

- [ ] **Step 4: Add an optional retry instruction to analyst task creation**

Change `create_analysis_task` to accept `retry_instruction: str | None = None`. Append this exact block only for attempts after a validation failure:

```python
retry_block = ""
if retry_instruction:
    retry_block = dedent(
        f"""

        --- RETRY REQUIREMENT ---
        The previous attempt was rejected: {retry_instruction}
        Return a fresh, complete report with non-empty SECTION A and SECTION B,
        followed by strict fenced SECTION C JSON.
        --- END RETRY REQUIREMENT ---
        """
    ).rstrip()
```

Place `{retry_block}` after the production prompt in the task description. Do not include the invalid prior output.

- [ ] **Step 5: Implement attempt events, bounded restarts, and typed exhaustion**

Add these types to `crew.py`:

```python
from dataclasses import dataclass
from typing import Literal

AttemptStatus = Literal[
    "running", "validation_failed", "execution_failed", "completed"
]


@dataclass(frozen=True)
class AnalystAttemptEvent:
    analyst_key: str
    attempt: int
    max_attempts: int
    status: AttemptStatus
    error: str | None = None
    report_text: str | None = None


class AnalystExecutionError(RuntimeError):
    def __init__(
        self,
        *,
        analyst_key: str,
        attempts: int,
        failure_kind: Literal["execution", "validation"],
        last_error: str,
    ) -> None:
        self.analyst_key = analyst_key
        self.attempts = attempts
        self.failure_kind = failure_kind
        self.last_error = last_error
        super().__init__(
            f"{analyst_key} failed after {attempts} attempts "
            f"({failure_kind}): {last_error}"
        )


def validate_max_restarts(max_restarts: int) -> int:
    if isinstance(max_restarts, bool) or not 0 <= max_restarts <= 2:
        raise ValueError("max_restarts must be an integer from 0 through 2")
    return max_restarts
```

Replace `_build_isolated_analyst_runs` with `_build_isolated_analyst_run(*, config: dict[str, Any], bundle_input_name: str, prompt_path: Path | None, strict_scoring: bool, retry_instruction: str | None) -> tuple[Crew, Task]`.

In `run_crew`, validate `max_restarts` before `_prepare_case_bundle_json`. For each enabled config not present in `completed_reports`, execute `max_restarts + 1` attempts. Create a fresh run inside the attempt loop, print `f"{key}: attempt {attempt}/{max_attempts}"`, emit `running`, call kickoff, extract output, and call `validate_analyst_report`. Emit `validation_failed` with report text when validation fails, `execution_failed` without report text when kickoff raises `Exception`, and `completed` with report text before adding the report. Preserve `KeyboardInterrupt` and `SystemExit` by catching only `Exception`. Invoke callbacks outside the kickoff/validation `try` block so a checkpoint persistence error propagates immediately instead of causing another model call.

Track the most recent validation error as the next attempt's `retry_instruction`. Execution errors use `None`, so a provider exception does not alter the production prompt.

- [ ] **Step 6: Run crew and task tests**

Run:

```bash
uv run pytest tests/test_crew_topology.py -q
```

Expected: all retry cases pass, successful analysts run once, and later analysts do not run after exhaustion.

- [ ] **Step 7: Commit isolated analyst restart behavior**

```bash
git add src/dili_rucam_agents/crew/tasks.py src/dili_rucam_agents/crew/crew.py tests/test_crew_topology.py
git commit -m "feat: restart incomplete analysts"
```

---

### Task 3: Add Versioned Per-Analyst Checkpoints

**Files:**
- Create: `src/dili_rucam_agents/checkpoints.py`
- Modify: `src/dili_rucam_agents/crew/agents.py`
- Create: `tests/test_checkpoints.py`
- Modify: `tests/test_agents.py`

**Interfaces:**
- Consumes: `validate_analyst_report` from Task 1.
- Produces: `AnalystIdentity`, `LegacyRunContext`, `build_analyst_identities`, `AnalystCheckpointStore`, `atomic_write_text`, and `atomic_write_json`.
- Produces: `resolve_analyst_max_output_tokens(max_tokens_env: str) -> int | None` as the public spelling of the existing token resolver.

- [ ] **Step 1: Expose and test the effective max-output-token resolver**

Rename `_resolve_analyst_max_output_tokens` to `resolve_analyst_max_output_tokens`, preserve a private compatibility alias for current imports, and export the public name. Update `tests/test_agents.py` to import the public resolver and retain the existing precedence tests.

Run:

```bash
uv run pytest tests/test_agents.py -q
```

Expected: all existing model and token-routing tests pass under the public name.

- [ ] **Step 2: Write checkpoint identity and compatibility tests**

Create `tests/test_checkpoints.py` with tests for deterministic fingerprints and selective invalidation:

```python
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from dili_rucam_agents.checkpoints import (
    AnalystCheckpointStore,
    AnalystIdentity,
    LegacyRunContext,
    atomic_write_text,
    build_analyst_fingerprint,
)


def complete_report() -> str:
    payload = {
        "injury_pattern": "hepatocellular",
        "R_ratio": 6.4,
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
    return (
        "## SECTION A\n\nClinical summary\n\n"
        "## SECTION B\n\n| Item | Score |\n| --- | --- |\n| Total | 6 |\n\n"
        f"## SECTION C\n\n```json\n{json.dumps(payload)}\n```\n"
    )


def identity(key: str = "analyst_alpha", fingerprint: str = "fingerprint-a"):
    return AnalystIdentity(
        key=key,
        report_filename=f"{key.replace('_', '-')}_report.md",
        fingerprint=fingerprint,
    )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("pdf_sha256", "pdf-b"),
        ("prompt_sha256", "prompt-b"),
        ("enable_score_masking", True),
        ("strict_scoring", True),
        ("model", "model-b"),
        ("max_output_tokens", 16000),
    ],
)
def test_fingerprint_changes_with_each_compatibility_input(field, replacement):
    base = dict(
        pdf_sha256="pdf-a",
        prompt_sha256="prompt-a",
        enable_score_masking=False,
        strict_scoring=False,
        model="model-a",
        max_output_tokens=12000,
    )
    first = build_analyst_fingerprint(**base)
    assert build_analyst_fingerprint(**{**base, field: replacement}) != first


def test_store_reuses_only_matching_completed_valid_report(tmp_path: Path):
    store = AnalystCheckpointStore(
        tmp_path,
        pdf_filename="case.pdf",
        pdf_sha256="pdf",
        enabled_analysts=("analyst_alpha",),
    )
    report = complete_report()
    store.record_completed(identity(), attempt=1, report_text=report)
    assert store.load_compatible_reports([identity()], resume=True) == {
        "analyst_alpha": report
    }
    assert store.load_compatible_reports(
        [identity(fingerprint="changed")], resume=True
    ) == {}


def test_store_rejects_invalid_report_even_when_manifest_says_completed(tmp_path: Path):
    store = AnalystCheckpointStore(
        tmp_path,
        pdf_filename="case.pdf",
        pdf_sha256="pdf",
        enabled_analysts=("analyst_alpha",),
    )
    store.record_completed(identity(), attempt=1, report_text=complete_report())
    (tmp_path / "analyst-alpha_report.md").write_text("incomplete", encoding="utf-8")
    assert store.load_compatible_reports([identity()], resume=True) == {}


def test_resume_false_bypasses_completed_checkpoints(tmp_path: Path):
    store = AnalystCheckpointStore(
        tmp_path,
        pdf_filename="case.pdf",
        pdf_sha256="pdf",
        enabled_analysts=("analyst_alpha",),
    )
    store.record_completed(identity(), attempt=1, report_text=complete_report())
    assert store.load_compatible_reports([identity()], resume=False) == {}


def test_running_or_corrupt_checkpoint_is_not_reused(tmp_path: Path):
    store = AnalystCheckpointStore(
        tmp_path,
        pdf_filename="case.pdf",
        pdf_sha256="pdf",
        enabled_analysts=("analyst_alpha",),
    )
    store.record_running(identity(), attempt=1)
    assert store.load_compatible_reports([identity()], resume=True) == {}
    (tmp_path / "analyst_checkpoints.json").write_text("{", encoding="utf-8")
    assert store.load_compatible_reports([identity()], resume=True) == {}


def test_adding_delta_preserves_alpha_checkpoint(tmp_path: Path):
    store = AnalystCheckpointStore(
        tmp_path,
        pdf_filename="case.pdf",
        pdf_sha256="pdf",
        enabled_analysts=("analyst_alpha", "analyst_delta"),
    )
    store.record_completed(identity(), attempt=1, report_text=complete_report())
    reports = store.load_compatible_reports(
        [identity(), identity("analyst_delta", "fingerprint-d")], resume=True
    )
    assert set(reports) == {"analyst_alpha"}


def test_total_attempts_accumulate_across_invocations(tmp_path: Path):
    store = AnalystCheckpointStore(
        tmp_path,
        pdf_filename="case.pdf",
        pdf_sha256="pdf",
        enabled_analysts=("analyst_alpha",),
    )
    store.record_running(identity(), attempt=1)
    store.record_failure(
        identity(), attempt=1, failure_kind="validation", error="missing SECTION C"
    )
    store.record_running(identity(), attempt=1)
    manifest = json.loads((tmp_path / "analyst_checkpoints.json").read_text())
    entry = manifest["analysts"]["analyst_alpha"]
    assert entry["total_attempts"] == 2
    assert entry["attempts_in_last_invocation"] == 1
```

- [ ] **Step 3: Write legacy adoption and atomic-write tests**

Add:

```python
def test_store_adopts_valid_legacy_report(tmp_path: Path):
    report_path = tmp_path / "analyst-alpha_report.md"
    report_path.write_text(complete_report(), encoding="utf-8")
    store = AnalystCheckpointStore(
        tmp_path,
        pdf_filename="case.pdf",
        pdf_sha256="pdf",
        enabled_analysts=("analyst_alpha",),
    )
    reports = store.load_compatible_reports(
        [identity()],
        resume=True,
        legacy_context=LegacyRunContext(
            pdf_filename="case.pdf",
            masking_enabled=False,
            strict_scoring=False,
        ),
    )
    assert reports == {"analyst_alpha": complete_report()}
    assert (tmp_path / "analyst_checkpoints.json").exists()


def test_atomic_write_does_not_replace_destination_when_replace_fails(tmp_path, monkeypatch):
    destination = tmp_path / "report.md"
    destination.write_text("old", encoding="utf-8")
    monkeypatch.setattr("dili_rucam_agents.checkpoints.os.replace", Mock(side_effect=OSError("stop")))
    with pytest.raises(OSError, match="stop"):
        atomic_write_text(destination, "new")
    assert destination.read_text(encoding="utf-8") == "old"
```

- [ ] **Step 4: Run checkpoint tests and confirm the module is missing**

Run:

```bash
uv run pytest tests/test_checkpoints.py tests/test_agents.py -q
```

Expected: `test_agents.py` passes after the resolver rename; checkpoint tests fail because `checkpoints.py` is not implemented.

- [ ] **Step 5: Implement atomic writes, fingerprints, and the manifest store**

Import `Mapping` and `Sequence` from `collections.abc`, `dataclass` from `dataclasses`, `datetime` and `timezone`, `hashlib`, `json`, `os`, `Path`, `Any`, and `uuid4`.

Implement these public types in `checkpoints.py`:

```python
CHECKPOINT_SCHEMA_VERSION = 1
CHECKPOINT_FILENAME = "analyst_checkpoints.json"


@dataclass(frozen=True)
class AnalystIdentity:
    key: str
    report_filename: str
    fingerprint: str


@dataclass(frozen=True)
class LegacyRunContext:
    pdf_filename: str
    masking_enabled: bool
    strict_scoring: bool


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def build_analyst_fingerprint(
    *,
    pdf_sha256: str,
    prompt_sha256: str,
    enable_score_masking: bool,
    strict_scoring: bool,
    model: str,
    max_output_tokens: int | None,
) -> str:
    payload = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "pdf_sha256": pdf_sha256,
        "prompt_sha256": prompt_sha256,
        "enable_score_masking": enable_score_masking,
        "strict_scoring": strict_scoring,
        "model": model,
        "max_output_tokens": max_output_tokens,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
```

Add `build_analyst_identities(*, configs: list[dict[str, Any]], report_filename_map: Mapping[str, str], pdf_sha256: str, prompt_sha256: str, enable_score_masking: bool, strict_scoring: bool) -> dict[str, AnalystIdentity]`, resolving each enabled config's model and effective max tokens and using the supplied report filename map rather than importing `pipeline.py`.

Implement `AnalystCheckpointStore` with these exact methods:

- `__init__(self, output_dir: Path, *, pdf_filename: str, pdf_sha256: str, enabled_analysts: Sequence[str]) -> None`
- `load_compatible_reports(self, identities: list[AnalystIdentity], *, resume: bool, legacy_context: LegacyRunContext | None = None, adopt_legacy: bool = True) -> dict[str, str]`
- `record_running(self, identity: AnalystIdentity, *, attempt: int) -> None`
- `record_failure(self, identity: AnalystIdentity, *, attempt: int, failure_kind: str, error: str) -> None`
- `record_completed(self, identity: AnalystIdentity, *, attempt: int, report_text: str) -> None`
- `all_completed(self, identities: list[AnalystIdentity], *, legacy_context: LegacyRunContext | None = None) -> bool`

The manifest root stores `enabled_analysts` from the constructor for observability, but that list is not part of any analyst fingerprint. `record_running` sets status `running`, increments `total_attempts`, sets `attempts_in_last_invocation`, and writes an ISO-8601 UTC `updated_at`. `record_failure` sets status `failed`, `failure_kind`, `last_error`, and `failed_at` without incrementing again. `record_completed` validates the report again, atomically writes the canonical report, then sets status `completed`, `report_file`, `fingerprint`, `completed_at`, and `last_error: null` before atomically writing the manifest. `load_compatible_reports` validates each file, ignores corrupt/stale entries, and adopts legacy files only when the provided legacy context matches an existing `run_status.json`'s PDF filename, masking, and strict settings. If `run_status.json` is absent, allow validation-only adoption as specified. When `adopt_legacy=False`, return compatible legacy reports without writing a manifest; `all_completed` uses this read-only mode.

- [ ] **Step 6: Run checkpoint, schema, and report tests**

Run:

```bash
uv run pytest tests/test_checkpoints.py tests/test_agents.py tests/test_analyst_report_validator.py tests/test_rucam_json_validator.py -q
```

Expected: all tests pass, including selective fingerprint invalidation and legacy adoption.

- [ ] **Step 7: Commit checkpoint infrastructure**

```bash
git add src/dili_rucam_agents/checkpoints.py src/dili_rucam_agents/crew/agents.py tests/test_checkpoints.py tests/test_agents.py
git commit -m "feat: add analyst checkpoint manifest"
```

---

### Task 4: Integrate Checkpoints with the Pipeline

**Files:**
- Modify: `src/dili_rucam_agents/pipeline.py`
- Modify: `tests/test_batch.py`

**Interfaces:**
- Consumes: `AnalystAttemptEvent` from Task 2 and checkpoint identities/store from Task 3.
- Changes `run_end_to_end` by adding keyword-only `max_restarts: int = 2` and `resume: bool = True` parameters.
- Produces: `is_end_to_end_complete(pdf_path: str, output_dir: str, prompt_path: str | None = None, *, enable_score_masking: bool = False, strict_scoring: bool = False, use_analyst_delta: bool = False, use_analyst_epsilon: bool = False, use_analyst_zeta: bool = False, use_analyst_eta: bool = False) -> bool`.
- Preserves: canonical report filenames and `_persist_reports` behavior for masking and ground-truth artifacts.

- [ ] **Step 1: Replace minimal report fixtures in pipeline tests with complete reports**

Add this helper to `tests/test_batch.py` and replace successful two-field JSON and Section A-only fixtures in pipeline/resume tests. Leave explicitly incomplete placeholder fixtures unchanged.

```python
def complete_report() -> str:
    payload = {
        "injury_pattern": "hepatocellular",
        "R_ratio": 6.4,
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
    return (
        "## SECTION A\n\nClinical summary\n\n"
        "## SECTION B\n\n| Item | Score |\n| --- | --- |\n| Total | 6 |\n\n"
        f"## SECTION C\n\n```json\n{json.dumps(payload)}\n```\n"
    )
```

- [ ] **Step 2: Write pipeline event-persistence and resume tests**

Add tests that capture the arguments passed to `run_crew` and emit actual `AnalystAttemptEvent` objects:

```python
def test_run_end_to_end_persists_invalid_attempt_then_valid_report(tmp_path, monkeypatch):
    pdf_path = tmp_path / "example.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    output_dir = tmp_path / "case"

    def fake_run_crew(pdf_path, prompt_path=None, **kwargs):
        callback = kwargs["on_attempt"]
        callback(AnalystAttemptEvent("analyst_alpha", 1, 3, "running"))
        callback(
            AnalystAttemptEvent(
                "analyst_alpha",
                1,
                3,
                "validation_failed",
                error="Missing SECTION C",
                report_text="incomplete",
            )
        )
        callback(AnalystAttemptEvent("analyst_alpha", 2, 3, "running"))
        callback(
            AnalystAttemptEvent(
                "analyst_alpha", 2, 3, "completed", report_text=complete_report()
            )
        )
        return "ok", {"analyst_alpha": complete_report()}

    monkeypatch.setattr("dili_rucam_agents.pipeline.run_crew", fake_run_crew)
    run_end_to_end(str(pdf_path), output_dir=str(output_dir))

    assert (output_dir / "attempts/analyst-alpha_attempt-1.invalid.md").read_text() == "incomplete"
    assert validate_analyst_report(
        (output_dir / "analyst-alpha_report.md").read_text()
    ).payload.total_score == 6


def test_run_end_to_end_resume_false_supplies_no_completed_reports(tmp_path, monkeypatch):
    pdf_path = tmp_path / "example.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    output_dir = tmp_path / "case"
    output_dir.mkdir()
    (output_dir / "analyst-alpha_report.md").write_text(
        complete_report(), encoding="utf-8"
    )
    captured_kwargs = {}

    def fake_run_crew(pdf_path, prompt_path=None, **kwargs):
        captured_kwargs.update(kwargs)
        return "ok", {}

    monkeypatch.setattr("dili_rucam_agents.pipeline.run_crew", fake_run_crew)
    run_end_to_end(
        str(pdf_path), output_dir=str(output_dir), resume=False
    )
    assert captured_kwargs["completed_reports"] == {}


def test_run_end_to_end_forwards_max_restarts(monkeypatch):
    captured_kwargs = {}

    def fake_run_crew(pdf_path, prompt_path=None, **kwargs):
        captured_kwargs.update(kwargs)
        return "ok"

    monkeypatch.setattr("dili_rucam_agents.pipeline.run_crew", fake_run_crew)
    run_end_to_end("example.pdf", max_restarts=1)
    assert captured_kwargs["max_restarts"] == 1


def test_later_failure_preserves_completed_alpha_checkpoint(tmp_path, monkeypatch):
    pdf_path = tmp_path / "example.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    output_dir = tmp_path / "case"

    def fake_run_crew(pdf_path, prompt_path=None, **kwargs):
        callback = kwargs["on_attempt"]
        callback(AnalystAttemptEvent("analyst_alpha", 1, 3, "running"))
        callback(
            AnalystAttemptEvent(
                "analyst_alpha", 1, 3, "completed", report_text=complete_report()
            )
        )
        callback(AnalystAttemptEvent("analyst_beta", 1, 3, "running"))
        callback(
            AnalystAttemptEvent(
                "analyst_beta",
                1,
                3,
                "validation_failed",
                error="Missing SECTION C",
                report_text="incomplete",
            )
        )
        raise AnalystExecutionError(
            analyst_key="analyst_beta",
            attempts=3,
            failure_kind="validation",
            last_error="Missing SECTION C",
        )

    monkeypatch.setattr("dili_rucam_agents.pipeline.run_crew", fake_run_crew)
    with pytest.raises(AnalystExecutionError):
        run_end_to_end(str(pdf_path), output_dir=str(output_dir))
    assert validate_analyst_report(
        (output_dir / "analyst-alpha_report.md").read_text()
    ).payload.total_score == 6


def test_second_invocation_supplies_all_checkpointed_reports(tmp_path, monkeypatch):
    pdf_path = tmp_path / "example.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    output_dir = tmp_path / "case"
    analyst_keys = ("analyst_alpha", "analyst_beta", "analyst_gamma")

    def seed_run(pdf_path, prompt_path=None, **kwargs):
        for key in analyst_keys:
            kwargs["on_attempt"](AnalystAttemptEvent(key, 1, 3, "running"))
            kwargs["on_attempt"](
                AnalystAttemptEvent(
                    key, 1, 3, "completed", report_text=complete_report()
                )
            )
        return "ok", {key: complete_report() for key in analyst_keys}

    monkeypatch.setattr("dili_rucam_agents.pipeline.run_crew", seed_run)
    run_end_to_end(str(pdf_path), output_dir=str(output_dir))
    captured_kwargs = {}

    def resume_run(pdf_path, prompt_path=None, **kwargs):
        captured_kwargs.update(kwargs)
        return "ok", dict(kwargs["completed_reports"])

    monkeypatch.setattr("dili_rucam_agents.pipeline.run_crew", resume_run)
    run_end_to_end(str(pdf_path), output_dir=str(output_dir))
    assert set(captured_kwargs["completed_reports"]) == set(analyst_keys)
```

- [ ] **Step 3: Run the pipeline-focused tests and confirm integration failures**

Run:

```bash
uv run pytest tests/test_batch.py -k "run_end_to_end" -q
```

Expected: failures show `run_end_to_end` lacks `resume`, `max_restarts`, and event-backed persistence.

- [ ] **Step 4: Build the pipeline execution context and event handler**

In `pipeline.py`, add a helper that resolves the prompt content, PDF hash, enabled analyst configs, and identities:

```python
@dataclass(frozen=True)
class PipelineCheckpointContext:
    store: AnalystCheckpointStore
    identities: dict[str, AnalystIdentity]
    legacy_context: LegacyRunContext


def _build_checkpoint_context(
    *,
    pdf_path: Path,
    output_dir: Path,
    prompt_path: Path | None,
    enable_score_masking: bool,
    strict_scoring: bool,
    analyst_flags: dict[str, bool],
) -> PipelineCheckpointContext:
    prompt_text = load_rucam_prompt(prompt_path, strict_scoring=strict_scoring)
    pdf_sha256 = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    configs = get_enabled_analyst_configs(**analyst_flags)
    identities = build_analyst_identities(
        configs=configs,
        report_filename_map=_REPORT_FILENAME_MAP,
        pdf_sha256=pdf_sha256,
        prompt_sha256=hashlib.sha256(prompt_text.encode()).hexdigest(),
        enable_score_masking=enable_score_masking,
        strict_scoring=strict_scoring,
    )
    return PipelineCheckpointContext(
        store=AnalystCheckpointStore(
            output_dir,
            pdf_filename=pdf_path.name,
            pdf_sha256=pdf_sha256,
            enabled_analysts=tuple(identities),
        ),
        identities=identities,
        legacy_context=LegacyRunContext(
            pdf_filename=pdf_path.name,
            masking_enabled=enable_score_masking,
            strict_scoring=strict_scoring,
        ),
    )
```

Implement `_handle_attempt_event(context, event)`:

- `running`: call `record_running`.
- `validation_failed`: atomically write `attempts/{analyst-key-with-dashes}_attempt-{attempt}.invalid.md` when report text exists, then call `record_failure` with kind `validation`.
- `execution_failed`: call `record_failure` with kind `execution`.
- `completed`: call `record_completed`, which validates and atomically persists the canonical report.

- [ ] **Step 5: Integrate resume and bounded retries into `run_end_to_end`**

Add keyword parameters `max_restarts: int = 2` and `resume: bool = True`. Validate the restart bound before creating output directories or reading the PDF.

When an output directory exists, build the checkpoint context and load compatible reports with:

```python
completed_reports = context.store.load_compatible_reports(
    list(context.identities.values()),
    resume=resume,
    legacy_context=context.legacy_context,
)
```

Pass `completed_reports`, `max_restarts`, and an event-handler closure to `run_crew`. Continue bulk persistence for masking and ground-truth artifacts, but exclude analyst keys from bulk persistence because `record_completed` already wrote them atomically.

When no output directory is supplied, pass an empty completed-report map and no event handler; `run_crew` still validates and retries in memory.

Change `_persist_reports` to use `atomic_write_text` for all artifacts.

- [ ] **Step 6: Implement read-only completion checking**

Add:

```python
def is_end_to_end_complete(
    pdf_path: str,
    output_dir: str,
    prompt_path: str | None = None,
    *,
    enable_score_masking: bool = False,
    strict_scoring: bool = False,
    use_analyst_delta: bool = False,
    use_analyst_epsilon: bool = False,
    use_analyst_zeta: bool = False,
    use_analyst_eta: bool = False,
) -> bool:
```

Build the same checkpoint context and call `load_compatible_reports` with `adopt_legacy=False`. Return false unless every enabled identity is present in that validated result. When masking is enabled, also require `masked-case-bundle_report.md` and `ground-truth-rucam-score_report.md`. Do not mutate the manifest during this read-only check.

- [ ] **Step 7: Add the single-PDF CLI restart option**

In `pipeline._main`, add:

```python
parser.add_argument(
    "--analyst-restarts",
    type=int,
    choices=range(0, 3),
    default=2,
    help="Number of restarts per incomplete analyst (0-2; default: 2).",
)
```

Forward it as `max_restarts=args.analyst_restarts`.

- [ ] **Step 8: Run pipeline, checkpoint, and crew tests**

Run:

```bash
uv run pytest tests/test_batch.py tests/test_checkpoints.py tests/test_crew_topology.py -q
```

Expected: pipeline event artifacts, compatible report loading, and forced fresh execution tests pass.

- [ ] **Step 9: Commit pipeline checkpoint integration**

```bash
git add src/dili_rucam_agents/pipeline.py tests/test_batch.py
git commit -m "feat: resume validated analyst checkpoints"
```

---

### Task 5: Make Batch Resume and Force Rerun Use Checkpoint Semantics

**Files:**
- Modify: `src/dili_rucam_agents/batch.py`
- Modify: `tests/test_batch.py`

**Interfaces:**
- Consumes: `is_end_to_end_complete` and `run_end_to_end` from Task 4; `validate_analyst_report` and `parse_section_c_payload` from Task 1.
- Changes `run_batch_folder` by adding keyword-only `max_restarts: int = 2` and produces batch CLI `--analyst-restarts {0,1,2}`.
- Preserves: `_is_pdf_run_complete` as a compatibility wrapper used by existing tests.

- [ ] **Step 1: Write batch-level restart forwarding, force-rerun, and resume tests**

Add these tests in `tests/test_batch.py`:

```python
def write_complete_reports(result_dir: Path) -> None:
    result_dir.mkdir(parents=True, exist_ok=True)
    for report_name in (
        "analyst-alpha_report.md",
        "analyst-beta_report.md",
        "analyst-gamma_report.md",
    ):
        (result_dir / report_name).write_text(complete_report(), encoding="utf-8")


def test_run_batch_folder_forwards_restart_limit_and_resume(tmp_path, monkeypatch):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    (input_dir / "case.pdf").write_bytes(b"%PDF-1.4")
    captured_kwargs = {}

    monkeypatch.setattr(
        "dili_rucam_agents.batch.is_end_to_end_complete", lambda *args, **kwargs: False
    )

    def fake_run_end_to_end(pdf_path, output_dir=None, **kwargs):
        captured_kwargs.update(kwargs)
        write_complete_reports(Path(output_dir))
        return "ok"

    monkeypatch.setattr(
        "dili_rucam_agents.batch.run_end_to_end", fake_run_end_to_end
    )
    run_batch_folder(
        input_dir=str(input_dir), output_dir=str(output_dir), max_restarts=1
    )
    assert captured_kwargs["max_restarts"] == 1
    assert captured_kwargs["resume"] is True


def test_force_rerun_disables_pipeline_resume(tmp_path, monkeypatch):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    (input_dir / "case.pdf").write_bytes(b"%PDF-1.4")
    captured_kwargs = {}

    def fake_run_end_to_end(pdf_path, output_dir=None, **kwargs):
        captured_kwargs.update(kwargs)
        write_complete_reports(Path(output_dir))
        return "ok"

    monkeypatch.setattr(
        "dili_rucam_agents.batch.run_end_to_end", fake_run_end_to_end
    )
    run_batch_folder(
        input_dir=str(input_dir), output_dir=str(output_dir), force_rerun=True
    )
    assert captured_kwargs["resume"] is False


def test_rerun_skips_completed_pdf_then_runs_failed_pdf(tmp_path, monkeypatch):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()
    for name in ("case-a.pdf", "case-b.pdf"):
        (input_dir / name).write_bytes(b"%PDF-1.4")
    case_a_dir = output_dir / "case-a"
    write_complete_reports(case_a_dir)
    (case_a_dir / "run_status.json").write_text(
        json.dumps(
            {
                "pdf_filename": "case-a.pdf",
                "status": "completed",
                "masking_enabled": False,
                "strict_scoring": False,
                "enabled_analysts": [
                    "analyst_alpha",
                    "analyst_beta",
                    "analyst_gamma",
                ],
            }
        ),
        encoding="utf-8",
    )
    calls = []

    monkeypatch.setattr(
        "dili_rucam_agents.batch.is_end_to_end_complete",
        lambda pdf_path, *args, **kwargs: Path(pdf_path).name == "case-a.pdf",
    )

    def fake_run_end_to_end(pdf_path, output_dir=None, **kwargs):
        calls.append(Path(pdf_path).name)
        write_complete_reports(Path(output_dir))
        return "ok"

    monkeypatch.setattr(
        "dili_rucam_agents.batch.run_end_to_end", fake_run_end_to_end
    )
    summary_path = run_batch_folder(
        input_dir=str(input_dir), output_dir=str(output_dir)
    )
    workbook = load_workbook(summary_path)
    summary_workbook_rows = [
        workbook.active.cell(row=index, column=1).value for index in (2, 3)
    ]
    assert calls == ["case-b.pdf"]
    assert summary_workbook_rows == ["case-a.pdf", "case-b.pdf"]


def test_run_batch_folder_rejects_more_than_two_restarts(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "dili_rucam_agents.batch.run_end_to_end",
        lambda *args, **kwargs: calls.append("called"),
    )
    with pytest.raises(ValueError, match="0 through 2"):
        run_batch_folder(
            input_dir=str(tmp_path / "input"),
            output_dir=str(tmp_path / "output"),
            max_restarts=3,
        )
    assert calls == []
```

- [ ] **Step 2: Run the batch tests and confirm forwarding/completion failures**

Run:

```bash
uv run pytest tests/test_batch.py -q
```

Expected: failures identify missing `max_restarts`, missing `resume=not force_rerun`, and old PDF completion semantics.

- [ ] **Step 3: Delegate PDF completion to the pipeline compatibility check**

Change `_is_pdf_run_complete` to accept `pdf_path`, `prompt_path`, `strict_scoring`, and analyst flags, verify that `run_status.json` says `completed` with matching common settings, and then call `is_end_to_end_complete` with current arguments.

Remove `_expected_report_paths` after `is_end_to_end_complete` becomes the sole artifact and analyst-completeness check.

Update the main batch loop's skip call with the full current invocation settings.

- [ ] **Step 4: Forward restart and resume controls**

Add `max_restarts: int = 2` to `run_batch_folder`, call `validate_max_restarts` before making output directories, and pass:

```python
run_end_to_end(
    str(pdf_path),
    prompt_path=prompt_path,
    output_dir=str(pdf_output_dir),
    enable_score_masking=enable_score_masking,
    strict_scoring=strict_scoring,
    use_analyst_delta=use_analyst_delta,
    use_analyst_epsilon=use_analyst_epsilon,
    use_analyst_zeta=use_analyst_zeta,
    use_analyst_eta=use_analyst_eta,
    max_restarts=max_restarts,
    resume=not force_rerun,
)
```

When catching `AnalystExecutionError`, add `failed_analyst`, `attempts`, and `failure_kind` to `run_status.json` before raising the existing batch-stopped error. Preserve the generic exception path for non-analyst failures.

- [ ] **Step 5: Use shared validation for summary extraction**

For `_populate_row_from_reports`, call `validate_analyst_report(report_text, allow_legacy_json=True)` and read `validated.payload.total_score`. Include the report filename in raised validation errors as today.

Move the existing tolerant Section C extraction implementation, including malformed-payload recovery, into `validators/analyst_report.py` as the legacy branch, then keep this compatibility wrapper in `batch.py` so external imports and existing parser tests do not break:

```python
def extract_section_c_json(report_text: str) -> dict[str, Any]:
    return parse_section_c_payload(report_text, allow_legacy_json=True)
```

Keep the existing malformed-recovery tests for `extract_section_c_json`. Add a separate assertion that `validate_analyst_report` rejects the same malformed JSON when `allow_legacy_json` is left at its strict default.

- [ ] **Step 6: Add the batch CLI restart option**

Add the same `--analyst-restarts` definition used by the single-PDF CLI and forward `args.analyst_restarts` to `run_batch_folder`.

- [ ] **Step 7: Run all batch, pipeline, validator, and checkpoint tests**

Run:

```bash
uv run pytest tests/test_batch.py tests/test_crew_topology.py tests/test_checkpoints.py tests/test_analyst_report_validator.py tests/test_rucam_json_validator.py -q
```

Expected: failed PDFs resume at the failed analyst, completed PDFs are skipped only when compatible, force rerun bypasses reports, and summary extraction shares the report contract.

- [ ] **Step 8: Commit batch resume semantics**

```bash
git add src/dili_rucam_agents/batch.py tests/test_batch.py
git commit -m "feat: resume batches from failed analysts"
```

---

### Task 6: Document and Verify the Production Workflow

**Files:**
- Modify: `README.md`
- Modify: `docs/exec-plans/active/agent-redesign.md`

**Interfaces:**
- Consumes: final CLI and artifact behavior from Tasks 1–5.
- Produces: operator documentation and verification evidence.

- [ ] **Step 1: Update batch usage documentation**

Add this content under `README.md`'s Batch Runs section, adapting surrounding command paths without changing semantics:

```markdown
### Analyst retries and resume

Each analyst receives one initial attempt and, by default, up to two restarts when execution fails or the report is incomplete. Set `--analyst-restarts` to `0`, `1`, or `2`; the value counts restarts after the initial attempt.

Validated analyst reports are checkpointed independently in each PDF result directory. Re-running the same batch skips compatible completed PDFs and, for a failed PDF, skips compatible successful analysts before restarting the failed analyst. PDF content, prompt content, masking mode, strict-scoring mode, model, or max-output-token changes invalidate only affected analyst checkpoints.

`--force-rerun` bypasses every analyst checkpoint. Invalid model outputs are retained under `attempts/`, and `analyst_checkpoints.json` records per-analyst status and attempt diagnostics.
```

Add `--analyst-restarts 2` to one batch command example and list both new artifacts.

- [ ] **Step 2: Update the active execution plan**

Add a completed resilience extension section to `docs/exec-plans/active/agent-redesign.md` listing shared validation, two restarts, per-analyst manifests, atomic persistence, force-rerun semantics, and the final test commands actually run.

- [ ] **Step 3: Run formatting and static source checks**

Run:

```bash
uv run ruff check src tests
uv run ruff format --check src tests
```

Expected: both commands exit successfully. If Ruff is not installed in the project environment, record that fact and run the syntax compilation command below instead of installing a new dependency:

```bash
uv run python -m compileall -q src tests
```

- [ ] **Step 4: Run the targeted regression suite**

Run:

```bash
uv run pytest tests/test_analyst_report_validator.py tests/test_rucam_json_validator.py tests/test_checkpoints.py tests/test_crew_topology.py tests/test_batch.py tests/test_agents.py -q
```

Expected: all tests pass without live API calls.

- [ ] **Step 5: Run the full offline suite**

Run:

```bash
uv run pytest -q
```

Expected: the entire repository test suite passes.

- [ ] **Step 6: Inspect the final diff for accidental scope expansion**

Run:

```bash
git diff --check
git status --short
git diff --stat HEAD~5..HEAD
```

Expected: no whitespace errors, only files named in this plan are changed, and no generated caches or clinical inputs are tracked.

- [ ] **Step 7: Commit documentation and final verification notes**

```bash
git add README.md docs/exec-plans/active/agent-redesign.md
git commit -m "docs: explain analyst retry and resume"
```

- [ ] **Step 8: Record the handoff summary**

Report the implemented retry limit, resume behavior, force-rerun behavior, invalid-attempt artifact location, checkpoint manifest location, targeted/full test counts, and any environment-only verification limitation. Do not claim live-provider behavior was tested unless a live API run was explicitly authorized and performed.
