# Batched Reproducibility Analysis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an explicit, resumable folder-level reproducibility mode that runs the unchanged full RUCAM analysis a configurable number of times per PDF and writes isolated repeat artifacts plus per-PDF run and aggregate summaries.

**Architecture:** Extract the current per-PDF batch behavior into one shared execution boundary, preserving regular batch output and failure semantics. A new `reproducibility.py` orchestrator calls that boundary sequentially for `<pdf-stem>_<repeat>` directories and atomically refreshes a two-sheet workbook after every visited repeat.

**Tech Stack:** Python 3.12, `argparse`, `pathlib`, standard-library `dataclasses`/`statistics`/`tempfile`, `openpyxl`, `pytest`, `uv`

**Spec:** `docs/superpowers/specs/2026-08-22-batched-reproducibility-analysis-design.md`

## Global Constraints

- The clinical pipeline remains the existing `run_end_to_end`; do not change ingestion, masking, prompts, scoring, analyst topology, report validation, or retry rules.
- Reproducibility mode is selected by `--reproducibility`; its repeat count defaults to `5`, accepts integers `>= 1`, and `--repeats` is invalid without the mode flag.
- `INPUT_DIR` and `OUTPUT_DIR` remain the first two positional batch arguments.
- PDFs and repeats execute sequentially in sorted PDF-name and ascending repeat-index order.
- Repeat outputs are `OUTPUT_DIR/<pdf-stem>/<pdf-stem>_<repeat>/`; no source PDF copy, nested `batch_summary.xlsx`, or root reproducibility workbook is created.
- Repeat checkpoints are isolated; reuse is allowed only within the same repeat directory.
- A failed repeat is persisted to the current PDF's `summary.xlsx` and then stops all later work.
- Failed rows never contribute to aggregate statistics, even when they show partial validated scores on `Runs`.
- No raw provider message, credential, request body, invalid model output, or clinical text may cross into public exceptions, status JSON, logs, or workbook cells.
- Regular batch mode and its root `batch_summary.xlsx` remain backward compatible.
- No new runtime dependency is introduced.

---

### Task 1: Extract a shared, repeat-aware single-PDF execution boundary

**Files:**
- Modify: `src/dili_rucam_agents/batch.py:1-424`
- Modify: `tests/test_batch.py:224-1625`

**Interfaces:**
- Produces: `PdfRunContext(mode: Literal["batch", "reproducibility"] = "batch", repeat_index: int | None = None)`
- Produces: `PdfRunResult(row: dict[str, Any], reused: bool)`
- Produces: `PdfRunFailure(pdf_path: Path, row: dict[str, Any], diagnostic: str)`
- Produces: `_get_resolved_analyst_configs` with the four optional-analyst booleans shown in Step 3, returning `list[dict[str, Any]]`
- Produces: `_run_pdf_analysis` with the complete keyword-only signature shown in Step 4, returning `PdfRunResult`
- Changes internal row keys from resolved model names to stable analyst keys while preserving regular workbook headers and values.
- Consumed later by: `src/dili_rucam_agents/reproducibility.py`

- [ ] **Step 1: Add failing tests for stable score keys and repeat status metadata**

Add imports for `PdfRunContext`, `_run_pdf_analysis`, and `get_enabled_analyst_configs` to `tests/test_batch.py`, then add:

```python
def test_shared_pdf_runner_returns_stable_analyst_keys_and_repeat_status(
    tmp_path: Path, monkeypatch
):
    pdf_path = tmp_path / "case.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    result_dir = tmp_path / "case" / "case_2"
    configs = get_enabled_analyst_configs()
    for config in configs:
        config["resolved_model_name"] = config["default_model"]

    monkeypatch.setattr(
        "dili_rucam_agents.batch.is_end_to_end_complete",
        lambda *args, **kwargs: False,
    )

    def fake_run_end_to_end(pdf_path, output_dir=None, **kwargs):
        write_complete_reports(Path(output_dir))
        return "ok"

    monkeypatch.setattr(
        "dili_rucam_agents.batch.run_end_to_end", fake_run_end_to_end
    )

    result = _run_pdf_analysis(
        pdf_path=pdf_path,
        pdf_output_dir=result_dir,
        prompt_path=None,
        enabled_analyst_configs=configs,
        enable_score_masking=False,
        strict_scoring=False,
        use_analyst_delta=False,
        use_analyst_epsilon=False,
        use_analyst_zeta=False,
        use_analyst_eta=False,
        debug=False,
        force_rerun=False,
        max_restarts=2,
        run_context=PdfRunContext(mode="reproducibility", repeat_index=2),
    )

    assert result.reused is False
    assert result.row["analyst_alpha"] == 6
    assert result.row["analyst_beta"] == 6
    assert result.row["analyst_gamma"] == 6
    assert "gpt-5.5" not in result.row
    status = json.loads((result_dir / "run_status.json").read_text())
    assert status["mode"] == "reproducibility"
    assert status["repeat_index"] == 2
    assert status["status"] == "completed"
```

Add a focused test proving a copied status from repeat 1 cannot fast-skip repeat 2:

```python
def test_repeat_completion_requires_matching_repeat_metadata(tmp_path, monkeypatch):
    pdf_path = tmp_path / "case.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    result_dir = tmp_path / "case_2"
    result_dir.mkdir()
    (result_dir / "run_status.json").write_text(
        json.dumps(
            {
                "pdf_filename": "case.pdf",
                "status": "completed",
                "masking_enabled": False,
                "strict_scoring": False,
                "mode": "reproducibility",
                "repeat_index": 1,
            }
        )
    )
    validator = Mock(return_value=True)
    monkeypatch.setattr(
        "dili_rucam_agents.batch.is_end_to_end_complete", validator
    )

    assert not _is_pdf_run_complete(
        pdf_path=pdf_path,
        pdf_output_dir=result_dir,
        prompt_path=None,
        enabled_analyst_configs=get_enabled_analyst_configs(),
        enable_score_masking=False,
        strict_scoring=False,
        use_analyst_delta=False,
        use_analyst_epsilon=False,
        use_analyst_zeta=False,
        use_analyst_eta=False,
        run_context=PdfRunContext(mode="reproducibility", repeat_index=2),
    )
    validator.assert_not_called()
```

- [ ] **Step 2: Run the focused tests and confirm the new interfaces are missing**

```bash
uv run pytest tests/test_batch.py::test_shared_pdf_runner_returns_stable_analyst_keys_and_repeat_status tests/test_batch.py::test_repeat_completion_requires_matching_repeat_metadata -q
```

Expected: collection fails because `PdfRunContext` and `_run_pdf_analysis` do not yet exist.

- [ ] **Step 3: Add the shared value types and resolved-config helper**

In `src/dili_rucam_agents/batch.py`, import `dataclass` and `Literal`, then define:

```python
@dataclass(frozen=True)
class PdfRunContext:
    mode: Literal["batch", "reproducibility"] = "batch"
    repeat_index: int | None = None

    def status_fields(self) -> dict[str, str | int]:
        if self.mode == "batch":
            if self.repeat_index is not None:
                raise ValueError("batch mode cannot have a repeat index")
            return {}
        if type(self.repeat_index) is not int or self.repeat_index < 1:
            raise ValueError("reproducibility repeat index must be at least 1")
        return {"mode": self.mode, "repeat_index": self.repeat_index}


@dataclass(frozen=True)
class PdfRunResult:
    row: dict[str, Any]
    reused: bool


class PdfRunFailure(RuntimeError):
    def __init__(
        self, *, pdf_path: Path, row: dict[str, Any], diagnostic: str
    ) -> None:
        self.pdf_path = pdf_path
        self.row = dict(row)
        self.diagnostic = diagnostic
        super().__init__(f"{pdf_path.name}: {diagnostic}")
```

Add:

```python
def _get_resolved_analyst_configs(
    *,
    use_analyst_delta: bool,
    use_analyst_epsilon: bool,
    use_analyst_zeta: bool,
    use_analyst_eta: bool,
) -> list[dict[str, Any]]:
    configs = get_enabled_analyst_configs(
        use_analyst_delta=use_analyst_delta,
        use_analyst_epsilon=use_analyst_epsilon,
        use_analyst_zeta=use_analyst_zeta,
        use_analyst_eta=use_analyst_eta,
    )
    for config in configs:
        config["resolved_model_name"] = resolve_rucam_model(
            model_env=config["model_env"],
            fallback_envs=config["fallback_envs"],
            default_model=config["default_model"],
        )
    return configs
```

Remove the unused `pdf_output_dir` parameter from `_initialize_summary_row`, initialize `row[config["key"]]`, and change `_populate_row_from_reports` to store each validated total at `row[config["key"]]`. Add a keyword-only `tolerate_invalid: bool = False`; on an analyst report validation error, skip that report only when it is `True`.

- [ ] **Step 4: Extract `_run_pdf_analysis` without changing regular batch behavior**

Move the current completion check, debug redirect, status transitions, `run_end_to_end` call, and report parsing from `run_batch_folder` into this exact boundary:

```python
def _run_pdf_analysis(
    *,
    pdf_path: Path,
    pdf_output_dir: Path,
    prompt_path: str | None,
    enabled_analyst_configs: list[dict[str, Any]],
    enable_score_masking: bool,
    strict_scoring: bool,
    use_analyst_delta: bool,
    use_analyst_epsilon: bool,
    use_analyst_zeta: bool,
    use_analyst_eta: bool,
    debug: bool,
    force_rerun: bool,
    max_restarts: int,
    run_context: PdfRunContext = PdfRunContext(),
) -> PdfRunResult:
    pdf_output_dir.mkdir(parents=True, exist_ok=True)
    row = _initialize_summary_row(pdf_path, enabled_analyst_configs)
    if not force_rerun and _is_pdf_run_complete(
        pdf_path=pdf_path,
        pdf_output_dir=pdf_output_dir,
        prompt_path=prompt_path,
        enabled_analyst_configs=enabled_analyst_configs,
        enable_score_masking=enable_score_masking,
        strict_scoring=strict_scoring,
        use_analyst_delta=use_analyst_delta,
        use_analyst_epsilon=use_analyst_epsilon,
        use_analyst_zeta=use_analyst_zeta,
        use_analyst_eta=use_analyst_eta,
        run_context=run_context,
    ):
        _populate_row_from_reports(
            row,
            pdf_output_dir,
            enabled_analyst_configs,
            enable_score_masking,
        )
        return PdfRunResult(row=row, reused=True)

    base_status = {
        "pdf_filename": pdf_path.name,
        "masking_enabled": enable_score_masking,
        "strict_scoring": strict_scoring,
        "enabled_analysts": [
            config["key"] for config in enabled_analyst_configs
        ],
        **run_context.status_fields(),
    }
    log_file = None
    stdout_cm: contextlib.AbstractContextManager[object] = contextlib.nullcontext()
    stderr_cm: contextlib.AbstractContextManager[object] = contextlib.nullcontext()
    if debug:
        log_file = (pdf_output_dir / f"{pdf_path.stem}.log").open(
            "w", encoding="utf-8"
        )
        stdout_cm = contextlib.redirect_stdout(log_file)
        stderr_cm = contextlib.redirect_stderr(log_file)

    with contextlib.ExitStack() as stack:
        if log_file is not None:
            stack.callback(log_file.close)
        stack.enter_context(stdout_cm)
        stack.enter_context(stderr_cm)
        try:
            _write_pdf_run_status(
                pdf_output_dir=pdf_output_dir,
                payload={
                    **base_status,
                    "status": "running",
                    "started_at": _utc_now_isoformat(),
                },
            )
            if debug:
                print(f"Running PDF: {pdf_path}")
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
            _populate_row_from_reports(
                row,
                pdf_output_dir,
                enabled_analyst_configs,
                enable_score_masking,
            )
            _write_pdf_run_status(
                pdf_output_dir=pdf_output_dir,
                payload={
                    **base_status,
                    "status": "completed",
                    "completed_at": _utc_now_isoformat(),
                },
            )
            if debug:
                print("Completed successfully.")
            return PdfRunResult(row=row, reused=False)
        except Exception as exc:
            diagnostic = _safe_batch_failure_diagnostic(exc)
            _populate_row_from_reports(
                row,
                pdf_output_dir,
                enabled_analyst_configs,
                enable_score_masking,
                tolerate_invalid=True,
            )
            analyst_failure = (
                {
                    "failed_analyst": exc.analyst_key,
                    "attempts": exc.attempts,
                    "failure_kind": exc.failure_kind,
                }
                if isinstance(exc, AnalystExecutionError)
                else {}
            )
            _write_pdf_run_status(
                pdf_output_dir=pdf_output_dir,
                payload={
                    **base_status,
                    "status": "failed",
                    "failed_at": _utc_now_isoformat(),
                    "error": diagnostic,
                    **analyst_failure,
                },
            )
            if debug:
                print(f"Run failed: {diagnostic}")
            raise PdfRunFailure(
                pdf_path=pdf_path,
                row=row,
                diagnostic=diagnostic,
            ) from exc
```

Inside the boundary:

- Create `pdf_output_dir` before opening the debug log.
- Build every running/completed/failed status payload from the existing fields plus `run_context.status_fields()`.
- Pass `resume=not force_rerun` and every existing analysis option to `run_end_to_end` unchanged.
- Return `PdfRunResult(row=row, reused=True)` after a compatible completion skip.
- Return `PdfRunResult(row=row, reused=False)` after successful execution and parsing.
- On any exception, derive the diagnostic with `_safe_batch_failure_diagnostic`, call `_populate_row_from_reports` with `tolerate_invalid=True` to recover only canonical validated scores, write the failed status including existing `AnalystExecutionError` metadata, and raise `PdfRunFailure` from the original exception.
- Print only the safe diagnostic to the debug log.

Add `run_context: PdfRunContext = PdfRunContext()` to `_is_pdf_run_complete` and require every key/value from `run_context.status_fields()` to match before delegating to `is_end_to_end_complete`.

Refactor `run_batch_folder` to resolve configs through `_get_resolved_analyst_configs`, call `_run_pdf_analysis` once per PDF, append successful rows, and preserve the current failure workbook contract:

```python
except PdfRunFailure as exc:
    row = dict(exc.row)
    row["masked_rucam_score"] = f"ERROR: {exc.diagnostic}"
    row["masked_rucam_category"] = f"ERROR: {exc.diagnostic}"
    summary_rows.append(row)
    _write_summary_workbook(
        summary_rows,
        enabled_analyst_configs,
        results_dir / "batch_summary.xlsx",
    )
    raise RuntimeError(
        f"Batch stopped at {pdf_path.name}: {exc.diagnostic}"
    ) from exc
```

Delete `_stop_batch_after_failure` only after all of its status fields and safe failure behavior exist in the shared boundary.

- [ ] **Step 5: Preserve model-name headers in the regular workbook**

Update `_write_summary_workbook` so display headers and internal row keys are separate:

```python
headers = ["pdf_filename"] + [
    config["resolved_model_name"] for config in enabled_analyst_configs
] + ["masked_rucam_score", "masked_rucam_category"]
row_keys = ["pdf_filename"] + [
    config["key"] for config in enabled_analyst_configs
] + ["masked_rucam_score", "masked_rucam_category"]

sheet.append(headers)
for row in rows:
    sheet.append([row.get(key, "") for key in row_keys])
```

Keep every existing `test_run_batch_folder_writes_summary_workbook` assertion unchanged.

- [ ] **Step 6: Run regular batch regression tests**

```bash
uv run pytest tests/test_batch.py -q
```

Expected: all batch tests pass with the same public output and diagnostics.

- [ ] **Step 7: Commit the shared execution refactor**

```bash
git add src/dili_rucam_agents/batch.py tests/test_batch.py
git commit -m "refactor: share per-pdf batch execution"
```

---

### Task 2: Implement reproducibility records, statistics, and atomic workbooks

**Files:**
- Create: `src/dili_rucam_agents/reproducibility.py`
- Create: `tests/test_reproducibility.py`
- Modify: `src/dili_rucam_agents/crew/agents.py:238-266`
- Modify: `tests/test_agents.py:4-52`

**Interfaces:**
- Consumes: resolved analyst configs from `_get_resolved_analyst_configs`
- Produces: `resolve_ground_truth_score_finder_model(model: str | None = None) -> str`
- Produces: `ReproducibilityRunRecord`
- Produces: `ScoreStatistics`
- Produces: `_score_statistics(values: Sequence[int]) -> ScoreStatistics`
- Produces: `_write_reproducibility_workbook` with the complete signature in Step 6, returning `Path`
- Consumed later by: `run_reproducibility_folder`

- [ ] **Step 1: Add failing tests for the shared ground-truth model resolver**

In `tests/test_agents.py`, import `resolve_ground_truth_score_finder_model` and add:

```python
def test_ground_truth_model_resolution_matches_agent_fallbacks(monkeypatch):
    monkeypatch.delenv("GROUND_TRUTH_SCORE_FINDER_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    assert resolve_ground_truth_score_finder_model() == "gpt-5.4"

    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.5")
    assert resolve_ground_truth_score_finder_model() == "gpt-5.5"

    monkeypatch.setenv("GROUND_TRUTH_SCORE_FINDER_MODEL", "gpt-5.6")
    assert resolve_ground_truth_score_finder_model() == "gpt-5.6"
    assert resolve_ground_truth_score_finder_model("explicit-model") == "explicit-model"
```

```bash
uv run pytest tests/test_agents.py::test_ground_truth_model_resolution_matches_agent_fallbacks -q
```

Expected: import fails because the resolver does not exist.

- [ ] **Step 2: Extract and use the ground-truth model resolver**

Add to `src/dili_rucam_agents/crew/agents.py`:

```python
def resolve_ground_truth_score_finder_model(model: Optional[str] = None) -> str:
    return (
        model
        or os.getenv("GROUND_TRUTH_SCORE_FINDER_MODEL")
        or os.getenv("OPENAI_MODEL", "gpt-5.4")
    )
```

Make `build_ground_truth_rucam_score_finder_agent` call this helper and export it in `__all__`. Run the focused test and the existing ground-truth agent routing test.

- [ ] **Step 3: Add failing pure-statistics tests**

Create `tests/test_reproducibility.py` with:

```python
import json
from pathlib import Path
from unittest.mock import Mock

from openpyxl import load_workbook
import pytest

from dili_rucam_agents.reproducibility import (
    ReproducibilityRunRecord,
    _score_statistics,
    _write_reproducibility_workbook,
)


def _complete_report(total_score: int = 6) -> str:
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
        "## SECTION A\n\nClinical summary\n\n"
        f"## SECTION B\n\n| Item | Score |\n| --- | --- |\n| Total | {total_score} |\n\n"
        f"## SECTION C\n\n```json\n{json.dumps(payload)}\n```\n"
    )


def _write_complete_reports(result_dir: Path) -> None:
    result_dir.mkdir(parents=True, exist_ok=True)
    for report_name in (
        "analyst-alpha_report.md",
        "analyst-beta_report.md",
        "analyst-gamma_report.md",
    ):
        (result_dir / report_name).write_text(
            _complete_report(), encoding="utf-8"
        )


def test_score_statistics_computes_sample_spread_and_agreement():
    result = _score_statistics([6, 6, 8, 8, 8])

    assert result.valid_repeats == 5
    assert result.mean == pytest.approx(7.2)
    assert result.sample_standard_deviation == pytest.approx(1.095445115)
    assert result.minimum == 6
    assert result.maximum == 8
    assert result.score_range == 2
    assert result.mode == 8
    assert result.exact_mode_agreement == pytest.approx(0.6)


def test_score_statistics_reports_tied_modes_and_small_samples():
    tied = _score_statistics([6, 6, 8, 8])
    assert tied.mode == "6, 8"
    assert tied.exact_mode_agreement == pytest.approx(0.5)

    singleton = _score_statistics([7])
    assert singleton.sample_standard_deviation is None
    assert singleton.mode == 7

    empty = _score_statistics([])
    assert empty.valid_repeats == 0
    assert empty.mean is None
    assert empty.exact_mode_agreement is None
```

```bash
uv run pytest tests/test_reproducibility.py -q
```

Expected: collection fails because the new module does not exist.

- [ ] **Step 4: Implement immutable run records and score statistics**

Create `src/dili_rucam_agents/reproducibility.py` with:

```python
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import os
from pathlib import Path
from statistics import mean, stdev
from tempfile import NamedTemporaryFile
from typing import Any, Literal, Sequence

from openpyxl import Workbook
from openpyxl.styles import Font

from dili_rucam_agents.crew.agents import resolve_ground_truth_score_finder_model


@dataclass(frozen=True)
class ReproducibilityRunRecord:
    repeat: int
    repeat_directory: str
    status: Literal["completed", "failed"]
    pdf_filename: str
    analyst_scores: dict[str, int | None]
    masked_rucam_score: int | None = None
    masked_rucam_category: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class ScoreStatistics:
    valid_repeats: int
    mean: float | None
    sample_standard_deviation: float | None
    minimum: int | None
    maximum: int | None
    score_range: int | None
    mode: int | str | None
    exact_mode_agreement: float | None


def _score_statistics(values: Sequence[int]) -> ScoreStatistics:
    scores = list(values)
    if not scores:
        return ScoreStatistics(0, None, None, None, None, None, None, None)
    counts = Counter(scores)
    highest_frequency = max(counts.values())
    modes = sorted(score for score, count in counts.items() if count == highest_frequency)
    rendered_mode: int | str = (
        modes[0] if len(modes) == 1 else ", ".join(str(score) for score in modes)
    )
    return ScoreStatistics(
        valid_repeats=len(scores),
        mean=mean(scores),
        sample_standard_deviation=stdev(scores) if len(scores) >= 2 else None,
        minimum=min(scores),
        maximum=max(scores),
        score_range=max(scores) - min(scores),
        mode=rendered_mode,
        exact_mode_agreement=highest_frequency / len(scores),
    )
```

Run the statistics tests and expect them to pass.

- [ ] **Step 5: Add failing workbook-contract tests**

Add:

```python
def test_reproducibility_workbook_has_runs_and_completed_only_statistics(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("GROUND_TRUTH_SCORE_FINDER_MODEL", "ground-truth-model")
    configs = [
        {"key": "analyst_alpha", "resolved_model_name": "alpha-model"},
        {"key": "analyst_beta", "resolved_model_name": "beta-model"},
    ]
    records = [
        ReproducibilityRunRecord(
            repeat=1,
            repeat_directory="case_1",
            status="completed",
            pdf_filename="case.pdf",
            analyst_scores={"analyst_alpha": 6, "analyst_beta": 7},
            masked_rucam_score=8,
            masked_rucam_category="Probable",
        ),
        ReproducibilityRunRecord(
            repeat=2,
            repeat_directory="case_2",
            status="completed",
            pdf_filename="case.pdf",
            analyst_scores={"analyst_alpha": 8, "analyst_beta": 7},
            masked_rucam_score=9,
            masked_rucam_category="Highly probable",
        ),
        ReproducibilityRunRecord(
            repeat=3,
            repeat_directory="case_3",
            status="failed",
            pdf_filename="case.pdf",
            analyst_scores={"analyst_alpha": 10, "analyst_beta": None},
            error="ProviderRequestError (status_code=502)",
        ),
    ]
    output_path = tmp_path / "case" / "summary.xlsx"

    written = _write_reproducibility_workbook(
        records=records,
        enabled_analyst_configs=configs,
        enable_score_masking=True,
        output_path=output_path,
    )

    assert written == output_path
    workbook = load_workbook(output_path, data_only=True)
    assert workbook.sheetnames == ["Runs", "Reproducibility"]
    runs = list(workbook["Runs"].values)
    assert runs[0] == (
        "repeat",
        "repeat_directory",
        "status",
        "pdf_filename",
        "analyst_alpha_score",
        "analyst_beta_score",
        "masked_rucam_score",
        "masked_rucam_category",
        "error",
    )
    assert runs[3][2] == "failed"
    assert runs[3][4] == 10

    aggregate_rows = list(workbook["Reproducibility"].values)
    aggregate = {row[0]: row for row in aggregate_rows[1:]}
    alpha = aggregate["analyst_alpha"]
    assert alpha[1] == "alpha-model"
    assert alpha[2] == 2
    assert alpha[3] == 7
    assert alpha[5:8] == (6, 8, 2)
    assert aggregate["ground_truth_rucam"][1] == "ground-truth-model"
    assert aggregate["ground_truth_rucam"][2] == 2
```

- [ ] **Step 6: Implement the atomic two-sheet workbook writer**

Implement:

```python
def _write_reproducibility_workbook(
    *,
    records: list[ReproducibilityRunRecord],
    enabled_analyst_configs: list[dict[str, Any]],
    enable_score_masking: bool,
    output_path: Path,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    runs_sheet = workbook.active
    runs_sheet.title = "Runs"
    aggregate_sheet = workbook.create_sheet("Reproducibility")

    run_headers = [
        "repeat",
        "repeat_directory",
        "status",
        "pdf_filename",
        *[
            f"{config['key']}_score"
            for config in enabled_analyst_configs
        ],
    ]
    if enable_score_masking:
        run_headers.extend(["masked_rucam_score", "masked_rucam_category"])
    run_headers.append("error")
    runs_sheet.append(run_headers)

    for record in records:
        values: list[Any] = [
            record.repeat,
            record.repeat_directory,
            record.status,
            record.pdf_filename,
            *[
                record.analyst_scores.get(config["key"])
                for config in enabled_analyst_configs
            ],
        ]
        if enable_score_masking:
            values.extend(
                [record.masked_rucam_score, record.masked_rucam_category]
            )
        values.append(record.error)
        runs_sheet.append(values)

    aggregate_headers = [
        "scorer",
        "model",
        "valid_repeats",
        "mean",
        "sample_standard_deviation",
        "minimum",
        "maximum",
        "range",
        "mode",
        "exact_mode_agreement",
    ]
    aggregate_sheet.append(aggregate_headers)
    completed_records = [
        record for record in records if record.status == "completed"
    ]

    def append_statistics(scorer: str, model: str, values: list[int]) -> None:
        statistics = _score_statistics(values)
        aggregate_sheet.append(
            [
                scorer,
                model,
                statistics.valid_repeats,
                statistics.mean,
                statistics.sample_standard_deviation,
                statistics.minimum,
                statistics.maximum,
                statistics.score_range,
                statistics.mode,
                statistics.exact_mode_agreement,
            ]
        )

    for config in enabled_analyst_configs:
        key = config["key"]
        append_statistics(
            key,
            config["resolved_model_name"],
            [
                score
                for record in completed_records
                if (score := record.analyst_scores.get(key)) is not None
            ],
        )
    if enable_score_masking:
        append_statistics(
            "ground_truth_rucam",
            resolve_ground_truth_score_finder_model(),
            [
                record.masked_rucam_score
                for record in completed_records
                if record.masked_rucam_score is not None
            ],
        )

    for sheet in workbook.worksheets:
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for column_cells in sheet.columns:
            max_length = max(len(str(cell.value or "")) for cell in column_cells)
            sheet.column_dimensions[column_cells[0].column_letter].width = min(
                max(max_length + 2, 12), 60
            )
    for cell in aggregate_sheet["D"][1:]:
        cell.number_format = "0.00"
    for cell in aggregate_sheet["E"][1:]:
        cell.number_format = "0.00"
    for cell in aggregate_sheet["J"][1:]:
        cell.number_format = "0.0%"

    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            prefix=f".{output_path.stem}.",
            suffix=".tmp.xlsx",
            dir=output_path.parent,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
        workbook.save(temporary_path)
        os.replace(temporary_path, output_path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return output_path
```

Requirements:

- Build `Runs` headers exactly as asserted, omitting both masking columns when masking is disabled.
- Write numeric scores as numbers and every missing value as `None`.
- Build one aggregate row per enabled analyst and a final `ground_truth_rucam` row only when masking is enabled.
- Filter to `record.status == "completed"` before collecting any aggregate values.
- Use `_score_statistics` for every aggregate row.
- Format agreement as `0.0%` and mean/standard deviation as `0.00`.
- Bold each header, freeze at `A2`, add an auto-filter, and cap widths at 60 characters.
- Resolve the ground-truth model with `resolve_ground_truth_score_finder_model()`.
- Save with a closed `NamedTemporaryFile` in `output_path.parent`, `os.replace(temp_path, output_path)`, and `temp_path.unlink(missing_ok=True)` in `finally`.
- Return `output_path`.

```bash
uv run pytest tests/test_reproducibility.py tests/test_agents.py::test_ground_truth_model_resolution_matches_agent_fallbacks tests/test_agents.py::test_ground_truth_score_finder_agent_uses_tool_and_default_routing -q
```

Expected: all focused summary and model-resolution tests pass.

- [ ] **Step 7: Commit the summary layer**

```bash
git add src/dili_rucam_agents/reproducibility.py src/dili_rucam_agents/crew/agents.py tests/test_reproducibility.py tests/test_agents.py
git commit -m "feat: add reproducibility summaries"
```

---

### Task 3: Orchestrate independent repeats with resume and fail-fast behavior

**Files:**
- Modify: `src/dili_rucam_agents/reproducibility.py`
- Modify: `tests/test_reproducibility.py`

**Interfaces:**
- Consumes: `PdfRunContext`, `PdfRunResult`, `PdfRunFailure`, `_get_resolved_analyst_configs`, and `_run_pdf_analysis` from `batch.py`
- Consumes: `_write_reproducibility_workbook` from Task 2
- Produces: `validate_repeats(repeats: int) -> int`
- Produces: `run_reproducibility_folder` with the complete signature in Step 3, returning `list[Path]`
- Consumed later by: batch CLI dispatch

- [ ] **Step 1: Add failing orchestration tests for layout, order, and forwarding**

Append imports for `PdfRunFailure`, `PdfRunResult`, `run_reproducibility_folder`, and `validate_repeats`, then add:

```python
def test_reproducibility_runs_sorted_pdfs_and_repeats_in_isolated_directories(
    tmp_path: Path, monkeypatch
):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    for name in ("case-b.pdf", "case-a.pdf"):
        (input_dir / name).write_bytes(b"%PDF-1.4")
    calls = []

    def fake_run_pdf_analysis(**kwargs):
        pdf_path = kwargs["pdf_path"]
        result_dir = kwargs["pdf_output_dir"]
        context = kwargs["run_context"]
        calls.append(
            (
                pdf_path.name,
                result_dir.relative_to(output_dir).as_posix(),
                context.repeat_index,
                kwargs["enable_score_masking"],
                kwargs["strict_scoring"],
                kwargs["max_restarts"],
            )
        )
        return PdfRunResult(
            row={
                "pdf_filename": pdf_path.name,
                "analyst_alpha": 6,
                "analyst_beta": 7,
                "analyst_gamma": 8,
                "masked_rucam_score": None,
                "masked_rucam_category": None,
            },
            reused=False,
        )

    monkeypatch.setattr(
        "dili_rucam_agents.reproducibility._run_pdf_analysis",
        fake_run_pdf_analysis,
    )

    summaries = run_reproducibility_folder(
        input_dir=str(input_dir),
        output_dir=str(output_dir),
        repeats=2,
        strict_scoring=True,
        max_restarts=1,
    )

    assert [path.relative_to(output_dir).as_posix() for path in summaries] == [
        "case-a/summary.xlsx",
        "case-b/summary.xlsx",
    ]
    assert calls == [
        ("case-a.pdf", "case-a/case-a_1", 1, False, True, 1),
        ("case-a.pdf", "case-a/case-a_2", 2, False, True, 1),
        ("case-b.pdf", "case-b/case-b_1", 1, False, True, 1),
        ("case-b.pdf", "case-b/case-b_2", 2, False, True, 1),
    ]
    assert not list(output_dir.rglob("batch_summary.xlsx"))


@pytest.mark.parametrize("value", (0, -1, True, 1.5, "5"))
def test_validate_repeats_rejects_non_positive_integers(value):
    with pytest.raises(ValueError, match="positive integer"):
        validate_repeats(value)


def test_reproducibility_empty_input_returns_no_summaries(tmp_path: Path):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    assert run_reproducibility_folder(
        input_dir=str(input_dir), output_dir=str(tmp_path / "output")
    ) == []
```

- [ ] **Step 2: Run orchestration tests and confirm the public helper is missing**

```bash
uv run pytest tests/test_reproducibility.py::test_reproducibility_runs_sorted_pdfs_and_repeats_in_isolated_directories tests/test_reproducibility.py::test_reproducibility_empty_input_returns_no_summaries -q
```

Expected: import or attribute failure for `run_reproducibility_folder`.

- [ ] **Step 3: Implement repeat validation and the public orchestration API**

Add these imports to `reproducibility.py`:

```python
from dili_rucam_agents.batch import (
    PdfRunContext,
    PdfRunFailure,
    _get_resolved_analyst_configs,
    _run_pdf_analysis,
)
from dili_rucam_agents.crew.crew import validate_max_restarts
```

Then add:

```python
def validate_repeats(repeats: int) -> int:
    if type(repeats) is not int or repeats < 1:
        raise ValueError("repeats must be a positive integer")
    return repeats
```

Implement:

```python
def run_reproducibility_folder(
    *,
    input_dir: str,
    output_dir: str,
    repeats: int = 5,
    prompt_path: str | None = None,
    enable_score_masking: bool = False,
    strict_scoring: bool = False,
    use_analyst_delta: bool = False,
    use_analyst_epsilon: bool = False,
    use_analyst_zeta: bool = False,
    use_analyst_eta: bool = False,
    debug: bool = False,
    force_rerun: bool = False,
    max_restarts: int = 2,
) -> list[Path]:
    repeats = validate_repeats(repeats)
    max_restarts = validate_max_restarts(max_restarts)
    source_dir = Path(input_dir).expanduser().resolve()
    results_dir = Path(output_dir).expanduser().resolve()
    results_dir.mkdir(parents=True, exist_ok=True)
    enabled_analyst_configs = _get_resolved_analyst_configs(
        use_analyst_delta=use_analyst_delta,
        use_analyst_epsilon=use_analyst_epsilon,
        use_analyst_zeta=use_analyst_zeta,
        use_analyst_eta=use_analyst_eta,
    )
    summary_paths: list[Path] = []

    for pdf_path in sorted(source_dir.glob("*.pdf")):
        pdf_parent = results_dir / pdf_path.stem
        pdf_parent.mkdir(parents=True, exist_ok=True)
        summary_path = pdf_parent / "summary.xlsx"
        records: list[ReproducibilityRunRecord] = []
        for repeat_index in range(1, repeats + 1):
            repeat_dir = pdf_parent / f"{pdf_path.stem}_{repeat_index}"
            try:
                result = _run_pdf_analysis(
                    pdf_path=pdf_path,
                    pdf_output_dir=repeat_dir,
                    prompt_path=prompt_path,
                    enabled_analyst_configs=enabled_analyst_configs,
                    enable_score_masking=enable_score_masking,
                    strict_scoring=strict_scoring,
                    use_analyst_delta=use_analyst_delta,
                    use_analyst_epsilon=use_analyst_epsilon,
                    use_analyst_zeta=use_analyst_zeta,
                    use_analyst_eta=use_analyst_eta,
                    debug=debug,
                    force_rerun=force_rerun,
                    max_restarts=max_restarts,
                    run_context=PdfRunContext(
                        mode="reproducibility",
                        repeat_index=repeat_index,
                    ),
                )
            except PdfRunFailure as exc:
                records.append(
                    ReproducibilityRunRecord(
                        repeat=repeat_index,
                        repeat_directory=repeat_dir.name,
                        status="failed",
                        pdf_filename=pdf_path.name,
                        analyst_scores={
                            config["key"]: exc.row.get(config["key"])
                            for config in enabled_analyst_configs
                        },
                        masked_rucam_score=exc.row.get("masked_rucam_score"),
                        masked_rucam_category=exc.row.get(
                            "masked_rucam_category"
                        ),
                        error=exc.diagnostic,
                    )
                )
                _write_reproducibility_workbook(
                    records=records,
                    enabled_analyst_configs=enabled_analyst_configs,
                    enable_score_masking=enable_score_masking,
                    output_path=summary_path,
                )
                raise RuntimeError(
                    f"Reproducibility batch stopped at {pdf_path.name} "
                    f"repeat {repeat_index}: {exc.diagnostic}"
                ) from exc

            records.append(
                ReproducibilityRunRecord(
                    repeat=repeat_index,
                    repeat_directory=repeat_dir.name,
                    status="completed",
                    pdf_filename=pdf_path.name,
                    analyst_scores={
                        config["key"]: result.row.get(config["key"])
                        for config in enabled_analyst_configs
                    },
                    masked_rucam_score=result.row.get("masked_rucam_score"),
                    masked_rucam_category=result.row.get(
                        "masked_rucam_category"
                    ),
                )
            )
            _write_reproducibility_workbook(
                records=records,
                enabled_analyst_configs=enabled_analyst_configs,
                enable_score_masking=enable_score_masking,
                output_path=summary_path,
            )
        summary_paths.append(summary_path)
    return summary_paths
```

Implementation sequence:

- Validate `repeats` and `max_restarts` before creating output.
- Resolve paths and analyst configs once.
- Iterate `sorted(source_dir.glob("*.pdf"))` and `range(1, repeats + 1)`.
- Derive `pdf_parent = results_dir / pdf_path.stem` and `repeat_dir = pdf_parent / f"{pdf_path.stem}_{repeat_index}"`.
- Pass every analysis option to `_run_pdf_analysis`, with `PdfRunContext(mode="reproducibility", repeat_index=repeat_index)`.
- Convert each success to a completed `ReproducibilityRunRecord` using stable analyst keys, exactly as shown in the reference body.
- Append the record and call `_write_reproducibility_workbook` immediately.
- Append a PDF's summary path to the return value only after all its repeats finish.

- [ ] **Step 4: Add and satisfy fail-fast persistence tests**

Add:

```python
def test_reproducibility_persists_failed_repeat_then_stops(tmp_path: Path, monkeypatch):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    for name in ("case-a.pdf", "case-b.pdf"):
        (input_dir / name).write_bytes(b"%PDF-1.4")
    calls = []

    def fake_run_pdf_analysis(**kwargs):
        pdf_path = kwargs["pdf_path"]
        repeat = kwargs["run_context"].repeat_index
        calls.append((pdf_path.name, repeat))
        row = {
            "pdf_filename": pdf_path.name,
            "analyst_alpha": 6,
            "analyst_beta": None,
            "analyst_gamma": None,
            "masked_rucam_score": None,
            "masked_rucam_category": None,
        }
        if repeat == 2:
            raise PdfRunFailure(
                pdf_path=pdf_path,
                row=row,
                diagnostic="ProviderRequestError (status_code=502)",
            )
        return PdfRunResult(row=row, reused=False)

    monkeypatch.setattr(
        "dili_rucam_agents.reproducibility._run_pdf_analysis",
        fake_run_pdf_analysis,
    )

    with pytest.raises(RuntimeError, match="case-a.pdf repeat 2"):
        run_reproducibility_folder(
            input_dir=str(input_dir), output_dir=str(output_dir), repeats=3
        )

    assert calls == [("case-a.pdf", 1), ("case-a.pdf", 2)]
    workbook = load_workbook(output_dir / "case-a" / "summary.xlsx", data_only=True)
    runs = list(workbook["Runs"].values)
    assert [row[2] for row in runs[1:]] == ["completed", "failed"]
    aggregate = list(workbook["Reproducibility"].values)
    alpha = next(row for row in aggregate[1:] if row[0] == "analyst_alpha")
    assert alpha[2] == 1
```

In the `PdfRunFailure` branch, create a failed record from `exc.row`, write the workbook, and raise:

```python
raise RuntimeError(
    f"Reproducibility batch stopped at {pdf_path.name} "
    f"repeat {repeat_index}: {exc.diagnostic}"
) from exc
```

- [ ] **Step 5: Add repeat-local skip, resume, and force-rerun integration tests**

Add:

```python
def test_reproducibility_skips_only_compatible_completed_repeats(
    tmp_path: Path, monkeypatch
):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    (input_dir / "case.pdf").write_bytes(b"%PDF-1.4")
    for repeat in (1, 2):
        repeat_dir = output_dir / "case" / f"case_{repeat}"
        _write_complete_reports(repeat_dir)
        (repeat_dir / "run_status.json").write_text(
            json.dumps(
                {
                    "pdf_filename": "case.pdf",
                    "status": "completed",
                    "masking_enabled": False,
                    "strict_scoring": False,
                    "enabled_analysts": [
                        "analyst_alpha",
                        "analyst_beta",
                        "analyst_gamma",
                    ],
                    "mode": "reproducibility",
                    "repeat_index": repeat,
                }
            ),
            encoding="utf-8",
        )
    monkeypatch.setattr(
        "dili_rucam_agents.batch.is_end_to_end_complete",
        lambda *args, **kwargs: True,
    )
    guard = Mock(side_effect=AssertionError("completed repeat reran"))
    monkeypatch.setattr("dili_rucam_agents.batch.run_end_to_end", guard)

    summaries = run_reproducibility_folder(
        input_dir=str(input_dir), output_dir=str(output_dir), repeats=2
    )

    guard.assert_not_called()
    assert summaries == [output_dir / "case" / "summary.xlsx"]
    workbook = load_workbook(summaries[0], data_only=True)
    assert [row[0] for row in list(workbook["Runs"].values)[1:]] == [1, 2]


def test_reproducibility_force_rerun_disables_skip_and_resume(
    tmp_path: Path, monkeypatch
):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    (input_dir / "case.pdf").write_bytes(b"%PDF-1.4")
    completion_guard = Mock(side_effect=AssertionError("force checked completion"))
    monkeypatch.setattr(
        "dili_rucam_agents.batch.is_end_to_end_complete", completion_guard
    )
    resumes = []

    def fake_run_end_to_end(pdf_path, output_dir=None, **kwargs):
        resumes.append(kwargs["resume"])
        _write_complete_reports(Path(output_dir))
        return "ok"

    monkeypatch.setattr(
        "dili_rucam_agents.batch.run_end_to_end", fake_run_end_to_end
    )

    run_reproducibility_folder(
        input_dir=str(input_dir),
        output_dir=str(output_dir),
        repeats=2,
        force_rerun=True,
    )

    completion_guard.assert_not_called()
    assert resumes == [False, False]


def test_reproducibility_expansion_runs_only_the_new_repeat(
    tmp_path: Path, monkeypatch
):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    (input_dir / "case.pdf").write_bytes(b"%PDF-1.4")
    for repeat in (1, 2):
        repeat_dir = output_dir / "case" / f"case_{repeat}"
        _write_complete_reports(repeat_dir)
        (repeat_dir / "run_status.json").write_text(
            json.dumps(
                {
                    "pdf_filename": "case.pdf",
                    "status": "completed",
                    "masking_enabled": False,
                    "strict_scoring": False,
                    "mode": "reproducibility",
                    "repeat_index": repeat,
                }
            ),
            encoding="utf-8",
        )
    monkeypatch.setattr(
        "dili_rucam_agents.batch.is_end_to_end_complete",
        lambda pdf_path, output_dir, **kwargs: Path(output_dir).name
        in {"case_1", "case_2"},
    )
    executed = []

    def fake_run_end_to_end(pdf_path, output_dir=None, **kwargs):
        executed.append(Path(output_dir).name)
        _write_complete_reports(Path(output_dir))
        return "ok"

    monkeypatch.setattr(
        "dili_rucam_agents.batch.run_end_to_end", fake_run_end_to_end
    )

    run_reproducibility_folder(
        input_dir=str(input_dir), output_dir=str(output_dir), repeats=3
    )

    assert executed == ["case_3"]


def test_reproducibility_contraction_ignores_and_preserves_higher_directories(
    tmp_path: Path, monkeypatch
):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    (input_dir / "case.pdf").write_bytes(b"%PDF-1.4")
    higher_dir = output_dir / "case" / "case_3"
    higher_dir.mkdir(parents=True)
    sentinel = higher_dir / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    def fake_run_pdf_analysis(**kwargs):
        pdf_path = kwargs["pdf_path"]
        return PdfRunResult(
            row={
                "pdf_filename": pdf_path.name,
                "analyst_alpha": 6,
                "analyst_beta": 6,
                "analyst_gamma": 6,
                "masked_rucam_score": None,
                "masked_rucam_category": None,
            },
            reused=False,
        )

    monkeypatch.setattr(
        "dili_rucam_agents.reproducibility._run_pdf_analysis",
        fake_run_pdf_analysis,
    )

    run_reproducibility_folder(
        input_dir=str(input_dir), output_dir=str(output_dir), repeats=2
    )

    assert sentinel.read_text(encoding="utf-8") == "keep"
    workbook = load_workbook(
        output_dir / "case" / "summary.xlsx", data_only=True
    )
    assert [row[0] for row in list(workbook["Runs"].values)[1:]] == [1, 2]
```

- [ ] **Step 6: Add diagnostic-safety and debug-log integration coverage**

Add:

```python
def test_reproducibility_failure_sinks_keep_raw_provider_text_private(
    tmp_path: Path, monkeypatch
):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    (input_dir / "case.pdf").write_bytes(b"%PDF-1.4")
    secret = "sk-live-REPRODUCIBILITY-SECRET"
    clinical_text = "Patient Jane Doe ALT 980 after Drug Q"

    class ProviderRequestError(RuntimeError):
        status_code = 502

    monkeypatch.setattr(
        "dili_rucam_agents.batch.is_end_to_end_complete",
        lambda *args, **kwargs: False,
    )

    def fail_run(*args, **kwargs):
        raise ProviderRequestError(
            f"Authorization: Bearer {secret}; request body: {clinical_text}"
        )

    monkeypatch.setattr("dili_rucam_agents.batch.run_end_to_end", fail_run)

    with pytest.raises(RuntimeError) as exc_info:
        run_reproducibility_folder(
            input_dir=str(input_dir),
            output_dir=str(output_dir),
            repeats=1,
            debug=True,
        )

    repeat_dir = output_dir / "case" / "case_1"
    status_text = (repeat_dir / "run_status.json").read_text()
    log_text = (repeat_dir / "case.log").read_text()
    workbook = load_workbook(
        output_dir / "case" / "summary.xlsx", data_only=True
    )
    workbook_text = " ".join(
        str(cell.value)
        for sheet in workbook.worksheets
        for row in sheet.iter_rows()
        for cell in row
        if cell.value is not None
    )
    diagnostics = (str(exc_info.value), status_text, log_text, workbook_text)
    for diagnostic in diagnostics:
        assert secret not in diagnostic
        assert clinical_text not in diagnostic
        assert "Authorization" not in diagnostic
        assert "request body" not in diagnostic
        assert "ProviderRequestError" in diagnostic
        assert "status_code=502" in diagnostic


def test_reproducibility_success_has_no_log_without_debug(tmp_path: Path, monkeypatch):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    (input_dir / "case.pdf").write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(
        "dili_rucam_agents.batch.is_end_to_end_complete",
        lambda *args, **kwargs: False,
    )

    def fake_run_end_to_end(pdf_path, output_dir=None, **kwargs):
        _write_complete_reports(Path(output_dir))
        return "ok"

    monkeypatch.setattr(
        "dili_rucam_agents.batch.run_end_to_end", fake_run_end_to_end
    )

    run_reproducibility_folder(
        input_dir=str(input_dir), output_dir=str(output_dir), repeats=1
    )

    assert not (output_dir / "case" / "case_1" / "case.log").exists()
```

- [ ] **Step 7: Run focused orchestration and safety tests**

```bash
uv run pytest tests/test_reproducibility.py tests/test_batch.py -q
```

Expected: all reproducibility tests and all regular batch regressions pass without live API calls.

- [ ] **Step 8: Commit repeat orchestration**

```bash
git add src/dili_rucam_agents/reproducibility.py tests/test_reproducibility.py
git commit -m "feat: orchestrate reproducibility repeats"
```

---

### Task 4: Wire the existing batch CLI and document the mode

**Files:**
- Modify: `src/dili_rucam_agents/batch.py:427-500`
- Modify: `tests/test_reproducibility.py`
- Modify: `README.md:57-89`
- Modify: `README.md:209-272`

**Interfaces:**
- Consumes: `run_reproducibility_folder` from Task 3
- Changes: `_main(argv: Sequence[str] | None = None) -> None`
- Preserves: `scripts/run_batch.py INPUT_DIR OUTPUT_DIR [existing flags]`
- Adds: `--reproducibility` and `--repeats N`

- [ ] **Step 1: Add failing CLI dispatch and validation tests**

In `tests/test_reproducibility.py`, import `_main` and add:

```python
def test_batch_cli_dispatches_reproducibility_with_default_five(
    tmp_path: Path, monkeypatch, capsys
):
    captured = {}

    def fake_run_reproducibility_folder(**kwargs):
        captured.update(kwargs)
        return [Path(kwargs["output_dir"]) / "case" / "summary.xlsx"]

    monkeypatch.setattr(
        "dili_rucam_agents.reproducibility.run_reproducibility_folder",
        fake_run_reproducibility_folder,
    )

    _main(
        [
            str(tmp_path / "input"),
            str(tmp_path / "output"),
            "--reproducibility",
            "--mask-scores",
            "--analyst-delta",
            "--analyst-restarts",
            "1",
        ]
    )

    assert captured["repeats"] == 5
    assert captured["enable_score_masking"] is True
    assert captured["use_analyst_delta"] is True
    assert captured["max_restarts"] == 1
    assert "summary.xlsx" in capsys.readouterr().out


def test_batch_cli_forwards_explicit_repeat_count(tmp_path: Path, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "dili_rucam_agents.reproducibility.run_reproducibility_folder",
        lambda **kwargs: captured.update(kwargs) or [],
    )
    _main(
        [
            str(tmp_path / "input"),
            str(tmp_path / "output"),
            "--reproducibility",
            "--repeats",
            "7",
        ]
    )
    assert captured["repeats"] == 7


def test_batch_cli_rejects_repeats_without_reproducibility(tmp_path: Path, capsys):
    with pytest.raises(SystemExit, match="2"):
        _main(
            [
                str(tmp_path / "input"),
                str(tmp_path / "output"),
                "--repeats",
                "3",
            ]
        )
    assert "--repeats requires --reproducibility" in capsys.readouterr().err


@pytest.mark.parametrize("value", ("0", "-1", "not-an-integer"))
def test_batch_cli_rejects_invalid_repeat_values(tmp_path: Path, value: str):
    with pytest.raises(SystemExit, match="2"):
        _main(
            [
                str(tmp_path / "input"),
                str(tmp_path / "output"),
                "--reproducibility",
                "--repeats",
                value,
            ]
        )
```

Add:

```python
def test_batch_cli_keeps_regular_dispatch_unchanged(tmp_path: Path, monkeypatch):
    captured = {}

    def fake_run_batch_folder(**kwargs):
        captured.update(kwargs)
        return Path(kwargs["output_dir"]) / "batch_summary.xlsx"

    monkeypatch.setattr(
        "dili_rucam_agents.batch.run_batch_folder", fake_run_batch_folder
    )

    _main([str(tmp_path / "input"), str(tmp_path / "output")])

    assert captured == {
        "input_dir": str(tmp_path / "input"),
        "output_dir": str(tmp_path / "output"),
        "prompt_path": None,
        "enable_score_masking": False,
        "strict_scoring": False,
        "use_analyst_delta": False,
        "use_analyst_epsilon": False,
        "use_analyst_zeta": False,
        "use_analyst_eta": False,
        "debug": False,
        "force_rerun": False,
        "max_restarts": 2,
    }
```

- [ ] **Step 2: Run CLI tests and verify `_main` cannot accept an argv list**

```bash
uv run pytest tests/test_reproducibility.py -k "batch_cli" -q
```

Expected: `_main` rejects the positional argv parameter or the new flags are unknown.

- [ ] **Step 3: Extend `_main` without creating an import cycle**

Import `Sequence` from `typing`, change the entry point to `def _main(argv: Sequence[str] | None = None) -> None:`, and call `parser.parse_args(argv)`.

Add:

```python
parser.add_argument(
    "--reproducibility",
    action="store_true",
    help="Run independent repeated analyses for every PDF.",
)
parser.add_argument(
    "--repeats",
    type=int,
    default=None,
    help="Positive repeat count for reproducibility mode (default: 5).",
)
```

Validate:

```python
if args.repeats is not None and not args.reproducibility:
    parser.error("--repeats requires --reproducibility")
if args.reproducibility and args.repeats is not None and args.repeats < 1:
    parser.error("--repeats must be a positive integer")
```

In the reproducibility branch, locally import the module to avoid the cycle:

```python
from dili_rucam_agents import reproducibility as reproducibility_module

summary_paths = reproducibility_module.run_reproducibility_folder(
    input_dir=args.input_dir,
    output_dir=args.output_dir,
    repeats=args.repeats if args.repeats is not None else 5,
    prompt_path=args.prompt_path,
    enable_score_masking=args.enable_score_masking,
    strict_scoring=args.strict_scoring,
    use_analyst_delta=args.use_analyst_delta,
    use_analyst_epsilon=args.use_analyst_epsilon,
    use_analyst_zeta=args.use_analyst_zeta,
    use_analyst_eta=args.use_analyst_eta,
    debug=args.debug,
    force_rerun=args.force_rerun,
    max_restarts=args.analyst_restarts,
)
for summary_path in summary_paths:
    print(summary_path)
return
```

Keep the current `run_batch_folder` call as the non-reproducibility branch.

- [ ] **Step 4: Run CLI and regular batch regressions**

```bash
uv run pytest tests/test_reproducibility.py -k "batch_cli" -q
uv run pytest tests/test_batch.py -q
```

Expected: new dispatch tests pass and regular batch tests remain green.

- [ ] **Step 5: Add quickstart and operational documentation**

In `README.md` Quickstart, add:

```bash
# reproducibility mode: five independent full analyses per PDF
uv run python scripts/run_batch.py \
    examples \
    results/results_reproducibility \
    --reproducibility \
    --repeats 5 \
    --mask-scores \
    --analyst-restarts 2
```

Under `## Batch Runs`, add `### Reproducibility mode` documenting the positional arguments, repeat validation, exact tree, sequential fail-fast behavior, repeat-local checkpoint reuse, force rerun, both workbook sheets and metric definitions, failed-row exclusion, absence of copied/nested/root artifacts, and repeat-count contraction semantics.

- [ ] **Step 6: Verify help text and documentation**

```bash
uv run python scripts/run_batch.py --help
rg -n "reproducibility|--repeats|summary.xlsx|exact-mode|sample standard" README.md
```

Expected: help lists both flags and README contains the command, layout, resume/failure rules, and both summary sheets.

- [ ] **Step 7: Commit CLI and documentation**

```bash
git add src/dili_rucam_agents/batch.py tests/test_reproducibility.py README.md
git commit -m "feat: expose reproducibility batch mode"
```

---

### Task 5: Run security, compatibility, and full-project verification

**Files:**
- Verify: `src/dili_rucam_agents/batch.py`
- Verify: `src/dili_rucam_agents/reproducibility.py`
- Verify: `src/dili_rucam_agents/crew/agents.py`
- Verify: `tests/test_batch.py`
- Verify: `tests/test_reproducibility.py`
- Verify: `tests/test_agents.py`
- Verify: `README.md`

**Interfaces:**
- Validates all interfaces and constraints from Tasks 1-4.
- Produces no new feature interface.

- [ ] **Step 1: Run the focused feature suite**

```bash
uv run pytest tests/test_reproducibility.py tests/test_batch.py tests/test_agents.py -q
```

Expected: all tests pass; no live provider is invoked.

- [ ] **Step 2: Run checkpoint and report-boundary regressions**

```bash
uv run pytest tests/test_checkpoints.py tests/test_analyst_report_validator.py tests/test_rucam_json_validator.py tests/test_crew_topology.py -q
```

Expected: all tests pass, proving repeat orchestration did not weaken checkpoint compatibility or strict report validation.

- [ ] **Step 3: Run the full offline suite**

```bash
uv run pytest -q
```

Expected: all tests pass. Existing ingestion deprecation warnings may remain, but no new warning is accepted without explanation.

- [ ] **Step 4: Run formatting, lint, and whitespace checks**

```bash
uv run ruff format --check src/dili_rucam_agents/batch.py src/dili_rucam_agents/reproducibility.py src/dili_rucam_agents/crew/agents.py tests/test_batch.py tests/test_reproducibility.py tests/test_agents.py
uv run ruff check src/dili_rucam_agents/batch.py src/dili_rucam_agents/reproducibility.py src/dili_rucam_agents/crew/agents.py tests/test_batch.py tests/test_reproducibility.py tests/test_agents.py
git diff --check
```

Expected: formatting and whitespace checks pass. Compare Ruff output with the baseline findings in `docs/exec-plans/active/agent-redesign.md`; no new feature finding is acceptable.

- [ ] **Step 5: Inspect final scope and commit verification-only corrections**

```bash
git status --short
git diff --stat 4a73acf..HEAD
git log --oneline 4a73acf..HEAD
```

Expected: only the planned source, test, and README files changed after the design commit; no generated workbook, PDF, log, checkpoint, cache, or credential file is tracked.

If verification required a correction, stage only the exact corrected files and commit them with:

```bash
git commit -m "fix: complete reproducibility verification"
```

If verification required no correction, do not create an empty commit.
