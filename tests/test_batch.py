import json
import os
import re
import sys
from pathlib import Path
from unittest.mock import Mock

from openpyxl import load_workbook
import pytest

import dili_rucam_agents.pipeline as pipeline_module
import dili_rucam_agents.runner as runner_module
from dili_rucam_agents.analysts import AnalystExecutionError, AnalystRunOutcome
from dili_rucam_agents.batch import (
    PdfRunFailure,
    resolve_pdf_source_dir,
    _main as batch_main,
    PdfRunContext,
    _is_pdf_run_complete,
    _run_pdf_analysis,
    _safe_batch_failure_diagnostic,
    extract_ground_truth_rucam_category,
    extract_ground_truth_rucam_score,
    extract_section_c_json,
    run_batch_folder,
)
from dili_rucam_agents.checkpoints import CHECKPOINT_SCHEMA_VERSION
from dili_rucam_agents.contracts import load_rucam_prompt
from dili_rucam_agents.diagnostics import SafeValidationDiagnostic
from dili_rucam_agents.pipeline import is_end_to_end_complete, run_end_to_end
from dili_rucam_agents.providers import MissingApiKeyError, ModelResponse, api_key_env
from dili_rucam_agents.roster import (
    DEFAULT_ROSTER_PATH,
    Provider,
    RosterError,
    load_roster,
)
from dili_rucam_agents.validators.analyst_report import (
    AnalystReportValidationError,
    validate_analyst_report,
)
from fakes import FakeProvider, StatusError
from test_rucam_assessment import _assessment, _causes, _item

#: The three analysts the pre-roster topology ran by default. Most tests below
#: predate the roster and are kept on this shape so their assertions are
#: unchanged; the roster is now written per test rather than switched on.
THREE = ("analyst_alpha", "analyst_beta", "analyst_gamma")
STUDY_ROSTER_PATH = DEFAULT_ROSTER_PATH.with_name("analysts.study.json")

_MODELS = {
    "analyst_alpha": "gpt-5.5",
    "analyst_beta": "gemini-3.1-pro-preview",
    "analyst_gamma": "moonshotai/kimi-k2.5",
    "analyst_delta": "deepseek-v4-pro",
}


@pytest.fixture(autouse=True)
def api_keys(monkeypatch):
    """Every provider has a key, so nothing fails the pre-flight by accident."""
    for provider in Provider:
        monkeypatch.setenv(api_key_env(provider), "test-key")


def write_roster(
    tmp_path: Path,
    keys=THREE,
    *,
    name: str = "analysts.json",
    models: dict[str, str] | None = None,
    max_output_tokens: int = 32000,
) -> str:
    """Write a roster file and return its path, so a test's topology is explicit."""
    resolved = dict(_MODELS)
    resolved.update(models or {})
    payload = {
        "analysts": [
            {
                "key": key,
                "label": key.replace("_", " ").title(),
                "provider": "openai",
                "model": resolved[key],
                "max_output_tokens": max_output_tokens,
            }
            for key in keys
        ],
        "ground_truth": {"provider": "openai", "model": "gpt-5.5"},
    }
    path = tmp_path / name
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return str(path)


def rucam_scores(total_score: int = 6) -> dict[str, int]:
    """Seven legal item scores summing to ``total_score`` (6, 7 or 8)."""
    scores = {
        "time_to_onset": 2,
        "course": 1,
        "risk_factors": 0,
        "concomitant_drugs": 0,
        "other_causes_excluded": 2,
        "known_hepatotoxicity": 1,
        "rechallenge": 0,
    }
    if total_score == 7:
        scores["known_hepatotoxicity"] = 2
    elif total_score == 8:
        scores["course"] = 2
        scores["known_hepatotoxicity"] = 2
    elif total_score != 6:
        raise ValueError("only scores from 6 through 8 are supported")
    return scores


def complete_report(total_score: int = 6) -> str:
    payload = {
        "injury_pattern": "hepatocellular",
        "R_ratio": 6.4,
        "rucam_scores": rucam_scores(total_score),
        "total_score": total_score,
        "category": "Probable",
    }
    return (
        "## SECTION A\n\nClinical summary\n\n"
        f"## SECTION B\n\n| Item | Score |\n| --- | --- |\n| Total | {total_score} |\n\n"
        f"## SECTION C\n\n```json\n{json.dumps(payload)}\n```\n"
    )


def assessment_payload(total_score: int = 6, marker: str = "because") -> dict:
    """A valid RucamAssessment whose seven items sum to ``total_score``."""
    return _assessment(
        item_5_causes=_causes(3),
        **{
            name: _item(score, rationale=marker)
            for name, score in rucam_scores(total_score).items()
        },
    )


def assessment_json(total_score: int = 6) -> str:
    return json.dumps(assessment_payload(total_score), indent=2, sort_keys=True) + "\n"


def write_complete_reports(result_dir: Path, keys=THREE, total_score: int = 6) -> None:
    """Only the report, as the markdown-era pipeline left behind."""
    result_dir.mkdir(parents=True, exist_ok=True)
    for key in keys:
        (result_dir / f"{key.replace('_', '-')}_report.md").write_text(
            complete_report(total_score), encoding="utf-8"
        )


def write_complete_artifacts(result_dir: Path, keys=THREE, total_score: int = 6) -> None:
    """Both artifacts a completed analyst now needs: report and sidecar."""
    write_complete_reports(result_dir, keys, total_score)
    for key in keys:
        (result_dir / f"{key.replace('_', '-')}_assessment.json").write_text(
            assessment_json(total_score), encoding="utf-8"
        )


def fake_ingestion(monkeypatch, *, counters: dict[str, int] | None = None) -> None:
    """Replace PDF parsing and the item 6 cascade with a fixed bundle.

    Patched on ``pipeline_module`` rather than on ``_prepare_case_bundle_json``,
    so a test can still guard that the pipeline is the thing calling them.
    """
    counts = counters if counters is not None else {}
    counts.setdefault("build", 0)
    counts.setdefault("mask", 0)

    class _Bundle:
        def to_dict(self):
            return {"pdf_path": "case.pdf", "normalized_text": "RUCAM score 9"}

    def build(path):
        counts["build"] += 1
        return _Bundle()

    def mask(payload):
        counts["mask"] += 1
        return {**payload, "normalized_text": "[RUCAM_SCORE_MASKED]"}

    monkeypatch.setattr(pipeline_module, "build_case_bundle", build)
    monkeypatch.setattr(
        pipeline_module,
        "apply_hepatotoxicity_evidence",
        lambda payload, settings: (payload, None),
    )
    monkeypatch.setattr(pipeline_module, "mask_case_bundle_payload", mask)


def use_provider(monkeypatch, provider) -> None:
    """Both of the pipeline's provider call sites get this one fake."""
    monkeypatch.setattr(pipeline_module, "_build_provider", lambda: provider)


def reply(total_score: int = 6, marker: str = "because") -> str:
    return json.dumps(assessment_payload(total_score, marker))


def read_manifest(output_dir: Path) -> dict:
    return json.loads(
        (output_dir / "analyst_checkpoints.json").read_text(encoding="utf-8")
    )


GROUND_TRUTH_REPORT = (
    "# Ground Truth RUCAM Score Report\n\n"
    "## Stable Fields\n"
    "```text\nGROUND_TRUTH_RUCAM_SCORE: 8\nGROUND_TRUTH_RUCAM_CATEGORY: Probable\n```\n"
)


def test_shared_pdf_runner_returns_stable_analyst_keys_and_repeat_status(
    tmp_path: Path, monkeypatch
):
    pdf_path = tmp_path / "case.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    result_dir = tmp_path / "case" / "case_2"
    roster_path = write_roster(tmp_path)
    analysts = load_roster(roster_path).analysts

    monkeypatch.setattr(
        "dili_rucam_agents.batch.is_end_to_end_complete",
        lambda *args, **kwargs: False,
    )

    def fake_run_end_to_end(pdf_path, output_dir=None, **kwargs):
        write_complete_reports(Path(output_dir))
        return "ok"

    monkeypatch.setattr("dili_rucam_agents.batch.run_end_to_end", fake_run_end_to_end)

    result = _run_pdf_analysis(
        pdf_path=pdf_path,
        pdf_output_dir=result_dir,
        prompt_path=None,
        roster_path=roster_path,
        analysts=analysts,
        enable_score_masking=False,
        strict_scoring=False,
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
    monkeypatch.setattr("dili_rucam_agents.batch.is_end_to_end_complete", validator)

    assert not _is_pdf_run_complete(
        pdf_path=pdf_path,
        pdf_output_dir=result_dir,
        prompt_path=None,
        roster_path=write_roster(tmp_path),
        enable_score_masking=False,
        strict_scoring=False,
        run_context=PdfRunContext(mode="reproducibility", repeat_index=2),
    )
    validator.assert_not_called()


def test_shared_pdf_runner_preserves_run_failure_when_partial_report_is_invalid(
    tmp_path: Path, monkeypatch
):
    pdf_path = tmp_path / "case.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    result_dir = tmp_path / "case"
    roster_path = write_roster(tmp_path)
    analysts = load_roster(roster_path).analysts

    monkeypatch.setattr(
        "dili_rucam_agents.batch.is_end_to_end_complete",
        lambda *args, **kwargs: False,
    )

    def fake_run_end_to_end(pdf_path, output_dir=None, **kwargs):
        result_path = Path(output_dir)
        result_path.mkdir(parents=True, exist_ok=True)
        (result_path / "analyst-alpha_report.md").write_text(
            complete_report(), encoding="utf-8"
        )
        (result_path / "analyst-beta_report.md").write_text(
            "incomplete", encoding="utf-8"
        )
        raise RuntimeError("original analysis failure")

    monkeypatch.setattr("dili_rucam_agents.batch.run_end_to_end", fake_run_end_to_end)

    with pytest.raises(PdfRunFailure, match="RuntimeError") as exc_info:
        _run_pdf_analysis(
            pdf_path=pdf_path,
            pdf_output_dir=result_dir,
            prompt_path=None,
            roster_path=roster_path,
            analysts=analysts,
            enable_score_masking=False,
            strict_scoring=False,
            debug=False,
            force_rerun=False,
            max_restarts=2,
        )

    assert "RuntimeError" in exc_info.value.diagnostic
    assert "analyst-beta_report.md" not in exc_info.value.diagnostic
    assert exc_info.value.row["analyst_alpha"] == 6
    assert exc_info.value.row["analyst_beta"] is None


def test_shared_pdf_runner_preserves_run_failure_with_malformed_ground_truth(
    tmp_path: Path, monkeypatch
):
    pdf_path = tmp_path / "case.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    result_dir = tmp_path / "case"
    roster_path = write_roster(tmp_path)
    analysts = load_roster(roster_path).analysts

    monkeypatch.setattr(
        "dili_rucam_agents.batch.is_end_to_end_complete",
        lambda *args, **kwargs: False,
    )

    def fake_run_end_to_end(pdf_path, output_dir=None, **kwargs):
        result_path = Path(output_dir)
        result_path.mkdir(parents=True, exist_ok=True)
        (result_path / "ground-truth-rucam-score_report.md").write_text(
            "GROUND_TRUTH_RUCAM_SCORE: not-a-score\n", encoding="utf-8"
        )
        raise RuntimeError("original analysis failure")

    monkeypatch.setattr("dili_rucam_agents.batch.run_end_to_end", fake_run_end_to_end)

    with pytest.raises(PdfRunFailure, match="RuntimeError") as exc_info:
        _run_pdf_analysis(
            pdf_path=pdf_path,
            pdf_output_dir=result_dir,
            prompt_path=None,
            roster_path=roster_path,
            analysts=analysts,
            enable_score_masking=True,
            strict_scoring=False,
            debug=False,
            force_rerun=False,
            max_restarts=2,
        )

    assert "RuntimeError" in exc_info.value.diagnostic
    status = json.loads((result_dir / "run_status.json").read_text())
    assert status["status"] == "failed"


def test_extract_section_c_json_parses_last_json_block():
    report_text = """
Intro

```json
{"ignored": true}
```

## SECTION C

```json
{"total_score": 8, "category": "Probable"}
```
"""

    payload = extract_section_c_json(report_text)

    assert payload["total_score"] == 8
    assert payload["category"] == "Probable"


def test_extract_section_c_json_recovers_common_malformed_llm_json():
    report_text = """
## SECTION C

```json
{
{
  "injury_pattern": "hepatocellular",",
  "R_ratio": 6.44,,
  "rucam_scores": { {
    "time_to_onset": 2,,
    "course": 0,,
    "risk_factors": 0,,
    "concomitant_drugs": 0,,
    "other_causes_excluded": 0,,
    "known_hepatotoxicity": 2,,
    "rechallenge": 0
  }, },
  "total_score": 4,,
  "category": "Possible",",
  "rules_version": "RUCAM_ULN_LiverTox_v1",",
  "notes": [ [
    "ULN inferred using LiverTox defaults",",
    "Course inferred from qualitative description (no numeric trajectory reported)",)
  ] ]
} ]
}
```
"""

    payload = extract_section_c_json(report_text)

    assert payload["injury_pattern"] == "hepatocellular"
    assert payload["R_ratio"] == 6.44
    assert payload["total_score"] == 4
    assert payload["category"] == "Possible"
    assert payload["rucam_scores"]["time_to_onset"] == 2
    assert payload["notes"] == [
        "ULN inferred using LiverTox defaults",
        "Course inferred from qualitative description (no numeric trajectory reported",
    ]

    strict_report = "## SECTION A\n\nSummary\n\n## SECTION B\n\nTable\n\n" + report_text
    with pytest.raises(AnalystReportValidationError, match="Invalid SECTION C JSON"):
        validate_analyst_report(strict_report)


def test_extract_section_c_json_parses_unfenced_json_below_section_c():
    report_text = """
## SECTION C — MACHINE-READABLE JSON

{
  "injury_pattern": "Not reported",
  "R_ratio": null,
  "rucam_scores": {
    "time_to_onset": 0,
    "course": 0,
    "risk_factors": 0,
    "concomitant_drugs": 0,
    "other_causes_excluded": -2,
    "known_hepatotoxicity": 0,
    "rechallenge": 0
  },
  "total_score": -2,
  "category": "Excluded",
  "notes": [
    "No extractable case content was provided in the case bundle."
  ],
  "rules_version": "RUCAM_ULN_LiverTox_v1"
}
"""

    payload = extract_section_c_json(report_text)

    assert payload["total_score"] == -2
    assert payload["category"] == "Excluded"
    assert payload["rucam_scores"]["other_causes_excluded"] == -2


def test_extract_section_c_json_raises_helpful_error_for_summary_placeholder():
    report_text = "See complete Sections A, B, and C above."

    try:
        extract_section_c_json(report_text)
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "summary placeholder" in str(exc)


def test_extract_section_c_json_raises_helpful_error_for_truncated_report():
    report_text = """
## SECTION A

Complete narrative.

## SECTION B

| RUCAM Item | Score |
| --- | --- |
| Total | 6 |
"""

    try:
        extract_section_c_json(report_text)
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "truncated before SECTION C" in str(exc)


def test_extract_ground_truth_rucam_score_reads_stable_field():
    report = """
# Ground Truth RUCAM Score Report

## Stable Fields
```text
GROUND_TRUTH_RUCAM_SCORE: 8
```
"""

    assert extract_ground_truth_rucam_score(report) == 8


def test_extract_ground_truth_rucam_category_reads_stable_field():
    report = """
# Ground Truth RUCAM Score Report

## Stable Fields
```text
GROUND_TRUTH_RUCAM_SCORE: 8
GROUND_TRUTH_RUCAM_CATEGORY: Probable
```
"""

    assert extract_ground_truth_rucam_category(report) == "Probable"


def test_run_batch_folder_writes_summary_workbook(tmp_path: Path, monkeypatch):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()
    (input_dir / "case-a.pdf").write_bytes(b"%PDF-1.4")
    (input_dir / "case-b.pdf").write_bytes(b"%PDF-1.4")

    def fake_run_end_to_end(
        pdf_path: str,
        prompt_path: str | None = None,
        output_dir: str | None = None,
        **kwargs,
    ) -> str:
        result_dir = Path(output_dir)
        result_dir.mkdir(parents=True, exist_ok=True)
        for report_name in (
            "analyst-alpha_report.md",
            "analyst-beta_report.md",
            "analyst-gamma_report.md",
        ):
            result_dir.joinpath(report_name).write_text(
                complete_report(7),
                encoding="utf-8",
            )
        if kwargs.get("enable_score_masking"):
            result_dir.joinpath("ground-truth-rucam-score_report.md").write_text(
                "# Ground Truth RUCAM Score Report\n\n"
                "## Stable Fields\n"
                "```text\nGROUND_TRUTH_RUCAM_SCORE: 8\nGROUND_TRUTH_RUCAM_CATEGORY: Probable\n```\n",
                encoding="utf-8",
            )
        return "ok"

    monkeypatch.setattr("dili_rucam_agents.batch.run_end_to_end", fake_run_end_to_end)

    summary_path = run_batch_folder(
        input_dir=str(input_dir),
        output_dir=str(output_dir),
        roster_path=write_roster(tmp_path),
        enable_score_masking=True,
    )

    workbook = load_workbook(summary_path)
    sheet = workbook["Batch Summary"]
    headers = [cell.value for cell in sheet[1]]
    first_row = [cell.value for cell in sheet[2]]
    row_map = dict(zip(headers, first_row))
    log_path = output_dir / "case-a" / "case-a.log"

    assert summary_path.name == "batch_summary.xlsx"
    assert headers == [
        "pdf_filename",
        "gpt-5.5",
        "gemini-3.1-pro-preview",
        "moonshotai/kimi-k2.5",
        "masked_rucam_score",
        "masked_rucam_category",
    ]
    assert row_map["pdf_filename"] == "case-a.pdf"
    assert row_map["masked_rucam_score"] == 8
    assert row_map["masked_rucam_category"] == "Probable"
    assert row_map["gpt-5.5"] == 7
    assert row_map["gemini-3.1-pro-preview"] == 7
    assert row_map["moonshotai/kimi-k2.5"] == 7
    assert not log_path.exists()


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

    monkeypatch.setattr("dili_rucam_agents.batch.run_end_to_end", fake_run_end_to_end)
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

    monkeypatch.setattr("dili_rucam_agents.batch.run_end_to_end", fake_run_end_to_end)
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

    monkeypatch.setattr("dili_rucam_agents.batch.run_end_to_end", fake_run_end_to_end)
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


def test_run_batch_folder_records_analyst_failure_details(tmp_path, monkeypatch):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    (input_dir / "case.pdf").write_bytes(b"%PDF-1.4")

    def fake_run_end_to_end(*args, **kwargs):
        raise AnalystExecutionError(
            analyst_key="analyst_beta",
            attempts=3,
            failure_kind="validation",
            validation_diagnostic=SafeValidationDiagnostic(
                (("SECTION_C", "invalid_json"),)
            ),
        )

    monkeypatch.setattr("dili_rucam_agents.batch.run_end_to_end", fake_run_end_to_end)

    with pytest.raises(RuntimeError, match="case.pdf"):
        run_batch_folder(input_dir=str(input_dir), output_dir=str(output_dir))

    status = json.loads(
        (output_dir / "case" / "run_status.json").read_text(encoding="utf-8")
    )
    assert status["failed_analyst"] == "analyst_beta"
    assert status["attempts"] == 3
    assert status["failure_kind"] == "validation"


def test_run_batch_folder_writes_log_only_in_debug_mode(tmp_path: Path, monkeypatch):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()
    (input_dir / "case-a.pdf").write_bytes(b"%PDF-1.4")

    def fake_run_end_to_end(
        pdf_path: str,
        prompt_path: str | None = None,
        output_dir: str | None = None,
        **kwargs,
    ) -> str:
        result_dir = Path(output_dir)
        result_dir.mkdir(parents=True, exist_ok=True)
        for report_name in (
            "analyst-alpha_report.md",
            "analyst-beta_report.md",
            "analyst-gamma_report.md",
        ):
            result_dir.joinpath(report_name).write_text(
                complete_report(7),
                encoding="utf-8",
            )
        return "ok"

    monkeypatch.setattr("dili_rucam_agents.batch.run_end_to_end", fake_run_end_to_end)

    run_batch_folder(
        input_dir=str(input_dir),
        output_dir=str(output_dir),
        debug=True,
    )

    log_path = output_dir / "case-a" / "case-a.log"
    assert log_path.exists()
    assert "Running PDF:" in log_path.read_text(encoding="utf-8")


def test_run_end_to_end_sends_the_strict_prompt_when_asked(tmp_path, monkeypatch):
    """strict_scoring picks the strict prompt file, not the inferring one."""

    pdf_path = tmp_path / "example.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    fake_ingestion(monkeypatch)
    captured = {}

    def fake_run_analysts(**kwargs):
        captured.update(kwargs)
        return AnalystRunOutcome(reports={"analyst_alpha": complete_report()})

    monkeypatch.setattr(pipeline_module, "run_analysts", fake_run_analysts)

    result = run_end_to_end(
        str(pdf_path), roster_path=write_roster(tmp_path, ("analyst_alpha",)),
        strict_scoring=True,
    )

    assert result == complete_report()
    assert captured["prompt_text"] == load_rucam_prompt(strict_scoring=True)
    assert captured["prompt_text"] != load_rucam_prompt(strict_scoring=False)


def test_run_end_to_end_sends_the_inferring_prompt_by_default(tmp_path, monkeypatch):
    pdf_path = tmp_path / "example.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    fake_ingestion(monkeypatch)
    captured = {}

    def fake_run_analysts(**kwargs):
        captured.update(kwargs)
        return AnalystRunOutcome()

    monkeypatch.setattr(pipeline_module, "run_analysts", fake_run_analysts)
    run_end_to_end(str(pdf_path), roster_path=write_roster(tmp_path, ("analyst_alpha",)))

    assert captured["prompt_text"] == load_rucam_prompt(strict_scoring=False)


def test_run_end_to_end_masks_the_bundle_it_sends_to_the_analysts(
    tmp_path, monkeypatch
):
    pdf_path = tmp_path / "example.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    fake_ingestion(monkeypatch)
    captured = {}

    def fake_run_analysts(**kwargs):
        captured.update(kwargs)
        return AnalystRunOutcome()

    monkeypatch.setattr(pipeline_module, "run_analysts", fake_run_analysts)
    roster_path = write_roster(tmp_path, ("analyst_alpha",))

    run_end_to_end(str(pdf_path), roster_path=roster_path, enable_score_masking=True)
    assert captured["bundle_input_name"] == "masked_case_bundle_json"
    assert "[RUCAM_SCORE_MASKED]" in captured["case_bundle_json"]
    assert "RUCAM score 9" not in captured["case_bundle_json"]

    run_end_to_end(str(pdf_path), roster_path=roster_path)
    assert captured["bundle_input_name"] == "raw_case_bundle_json"
    assert "RUCAM score 9" in captured["case_bundle_json"]


def test_missing_api_key_fails_before_the_pdf_is_ingested(tmp_path, monkeypatch):
    """The ingestion stack and the item 6 cascade are the expensive part."""

    pdf_path = tmp_path / "example.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    counters: dict[str, int] = {}
    fake_ingestion(monkeypatch, counters=counters)
    monkeypatch.setattr(
        pipeline_module,
        "run_analysts",
        Mock(side_effect=AssertionError("ran an analyst without a key")),
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    roster_path = tmp_path / "analysts.json"
    roster_path.write_text(
        json.dumps(
            {
                "analysts": [
                    {
                        "key": "analyst_zeta",
                        "label": "Analyst Zeta",
                        "provider": "anthropic",
                        "model": "claude-opus-4-7",
                    }
                ],
                "ground_truth": {"provider": "openai", "model": "gpt-5.5"},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(MissingApiKeyError, match="ANTHROPIC_API_KEY"):
        run_end_to_end(str(pdf_path), roster_path=str(roster_path))

    assert counters["build"] == 0


def test_missing_ground_truth_key_fails_the_preflight_only_when_masking(
    tmp_path, monkeypatch
):
    """The ground-truth model is called only when masking is on."""

    pdf_path = tmp_path / "example.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    fake_ingestion(monkeypatch)
    monkeypatch.setattr(
        pipeline_module, "run_analysts", lambda **kwargs: AnalystRunOutcome()
    )
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    roster_path = tmp_path / "analysts.json"
    roster_path.write_text(
        json.dumps(
            {
                "analysts": [
                    {
                        "key": "analyst_alpha",
                        "label": "Analyst Alpha",
                        "provider": "openai",
                        "model": "gpt-5.5",
                    }
                ],
                "ground_truth": {
                    "provider": "google",
                    "model": "gemini-3.1-pro-preview",
                },
            }
        ),
        encoding="utf-8",
    )

    run_end_to_end(str(pdf_path), roster_path=str(roster_path))
    with pytest.raises(MissingApiKeyError, match="GEMINI_API_KEY"):
        run_end_to_end(
            str(pdf_path), roster_path=str(roster_path), enable_score_masking=True
        )


def test_run_end_to_end_persists_invalid_attempt_then_valid_report(
    tmp_path, monkeypatch
):
    pdf_path = tmp_path / "example.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    output_dir = tmp_path / "case"
    fake_ingestion(monkeypatch)
    rejected = json.dumps({"injury_pattern": "not-a-pattern"})
    use_provider(monkeypatch, FakeProvider(rejected, reply()))

    run_end_to_end(
        str(pdf_path),
        output_dir=str(output_dir),
        roster_path=write_roster(tmp_path, ("analyst_alpha",)),
    )

    (attempt_path,) = (output_dir / "attempts").glob("analyst_alpha.1.*.invalid.json")
    assert json.loads(attempt_path.read_text()) == json.loads(rejected)
    assert (
        validate_analyst_report(
            (output_dir / "analyst-alpha_report.md").read_text()
        ).payload.total_score
        == 6
    )
    sidecar = json.loads((output_dir / "analyst-alpha_assessment.json").read_text())
    assert sidecar["injury_pattern"] == "hepatocellular"
    entry = read_manifest(output_dir)["analysts"]["analyst_alpha"]
    assert entry["status"] == "completed"
    assert [item["failure_kind"] for item in entry["attempt_history"]] == ["validation"]
    assert entry["validation_retries"] == 1


def test_attempt_history_accumulates_across_invocations(tmp_path, monkeypatch):
    """The history grows, and every rejected reply keeps its own file.

    ``run_analysts`` writes ``attempts/<key>.<n>.<uid>.invalid.*``. The attempt
    number restarts at 1 in each invocation, so the unique id is what stops the
    second invocation's first rejected reply replacing the first invocation's.
    """

    pdf_path = tmp_path / "example.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    output_dir = tmp_path / "case"
    fake_ingestion(monkeypatch)
    roster_path = write_roster(tmp_path, ("analyst_alpha",))

    for marker in ("first", "second"):
        use_provider(
            monkeypatch, FakeProvider(json.dumps({"rejected": marker}))
        )
        with pytest.raises(AnalystExecutionError):
            run_end_to_end(
                str(pdf_path),
                output_dir=str(output_dir),
                roster_path=roster_path,
                max_restarts=0,
            )

    attempt_paths = sorted((output_dir / "attempts").glob("*.invalid.*"))
    assert len(attempt_paths) == 2
    assert sorted(
        json.loads(path.read_text())["rejected"] for path in attempt_paths
    ) == ["first", "second"]
    entry = read_manifest(output_dir)["analysts"]["analyst_alpha"]
    assert entry["total_attempts"] == 2
    assert [item["total_attempt"] for item in entry["attempt_history"]] == [1, 2]
    assert [item["failure_kind"] for item in entry["attempt_history"]] == [
        "validation",
        "validation",
    ]
    assert entry["status"] == "failed"


def test_a_resumed_run_keeps_the_failed_invocations_retry_count(
    tmp_path, monkeypatch
):
    """The gate's own measurement must survive the resume it recommends.

    An analyst that exhausts its validation budget fails the repeat, and
    ``RESUME_HINT`` tells the user to re-run the same command. The resumed
    invocation succeeds on its first attempt, so a last-write-wins counter
    would report 0 validation retries for a run that spent 2 -- the direction
    that passes the acceptance bar.
    """

    pdf_path = tmp_path / "example.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    output_dir = tmp_path / "case"
    fake_ingestion(monkeypatch)
    roster_path = write_roster(tmp_path, ("analyst_alpha",))

    rejected = json.dumps({"rejected": "spent the budget"})
    use_provider(monkeypatch, FakeProvider(rejected, rejected, rejected))
    with pytest.raises(AnalystExecutionError):
        run_end_to_end(
            str(pdf_path),
            output_dir=str(output_dir),
            roster_path=roster_path,
            max_restarts=2,
        )
    failed = read_manifest(output_dir)["analysts"]["analyst_alpha"]
    assert failed["validation_retries"] == 2

    use_provider(monkeypatch, FakeProvider(reply()))
    run_end_to_end(
        str(pdf_path),
        output_dir=str(output_dir),
        roster_path=roster_path,
        max_restarts=2,
    )

    entry = read_manifest(output_dir)["analysts"]["analyst_alpha"]
    assert entry["status"] == "completed"
    assert entry["validation_retries"] == 2
    # The breakdown: only the attempt that ended the failed invocation carries
    # the counters, and it says that invocation spent its whole budget.
    history = entry["attempt_history"]
    assert [item.get("validation_retries") for item in history] == [None, None, 2]
    # Every rejected reply of the failed invocation is still on disk.
    assert len(list((output_dir / "attempts").glob("*.invalid.json"))) == 3


def test_a_truncated_reply_is_recorded_as_a_truncation(tmp_path, monkeypatch):
    pdf_path = tmp_path / "example.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    output_dir = tmp_path / "case"
    fake_ingestion(monkeypatch)
    # The runner widens the budget once before giving up, so a truncation needs
    # two cut-off replies to become a truncation failure.
    use_provider(
        monkeypatch,
        FakeProvider(
            ModelResponse(text='{"injury_pattern":', truncated=True),
            ModelResponse(text='{"injury_pattern": "hepato', truncated=True),
        ),
    )

    with pytest.raises(AnalystExecutionError) as exc_info:
        run_end_to_end(
            str(pdf_path),
            output_dir=str(output_dir),
            roster_path=write_roster(tmp_path, ("analyst_alpha",)),
            max_restarts=0,
        )

    assert exc_info.value.failure_kind == "truncation"
    entry = read_manifest(output_dir)["analysts"]["analyst_alpha"]
    assert entry["failure_kind"] == "truncation"
    assert "stops partway" in entry["last_error"]
    assert entry["status"] == "failed"
    assert entry["truncation_escalations"] == 1
    # How far the model got, kept for judging the output budget.
    assert sorted(
        re.sub(r"\.[0-9a-f]{8}\.invalid\.", ".<uid>.invalid.", path.name)
        for path in (output_dir / "attempts").iterdir()
    ) == [
        "analyst_alpha.1.truncated1.<uid>.invalid.txt",
        "analyst_alpha.1.truncated2.<uid>.invalid.txt",
    ]


def test_run_end_to_end_resume_false_supplies_no_completed_reports(
    tmp_path, monkeypatch
):
    pdf_path = tmp_path / "example.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    output_dir = tmp_path / "case"
    fake_ingestion(monkeypatch)
    roster_path = write_roster(tmp_path, ("analyst_alpha",))
    use_provider(monkeypatch, FakeProvider(reply()))
    run_end_to_end(
        str(pdf_path), output_dir=str(output_dir), roster_path=roster_path
    )
    captured = {}

    def fake_run_analysts(**kwargs):
        captured.update(kwargs)
        return AnalystRunOutcome()

    monkeypatch.setattr(pipeline_module, "run_analysts", fake_run_analysts)

    run_end_to_end(
        str(pdf_path),
        output_dir=str(output_dir),
        roster_path=roster_path,
        resume=True,
    )
    assert set(captured["completed_reports"]) == {"analyst_alpha"}

    run_end_to_end(
        str(pdf_path),
        output_dir=str(output_dir),
        roster_path=roster_path,
        resume=False,
    )
    assert captured["completed_reports"] == {}


def test_run_end_to_end_forwards_max_restarts(tmp_path, monkeypatch):
    pdf_path = tmp_path / "example.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    fake_ingestion(monkeypatch)
    captured = {}

    def fake_run_analysts(**kwargs):
        captured.update(kwargs)
        return AnalystRunOutcome()

    monkeypatch.setattr(pipeline_module, "run_analysts", fake_run_analysts)
    run_end_to_end(
        str(pdf_path),
        roster_path=write_roster(tmp_path, ("analyst_alpha",)),
        max_restarts=1,
    )

    assert captured["max_retries"] == 1


def test_run_end_to_end_rejects_more_than_two_restarts(tmp_path, monkeypatch):
    pdf_path = tmp_path / "example.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    counters: dict[str, int] = {}
    fake_ingestion(monkeypatch, counters=counters)
    monkeypatch.setattr(
        pipeline_module,
        "run_analysts",
        Mock(side_effect=AssertionError("ran with an illegal restart budget")),
    )

    with pytest.raises(ValueError, match="0 through 2"):
        run_end_to_end(str(pdf_path), max_restarts=3)

    assert counters["build"] == 0


def test_run_end_to_end_passes_fingerprinted_instruction_contracts_unchanged(
    tmp_path, monkeypatch
):
    pdf_path = tmp_path / "example.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    output_dir = tmp_path / "case"
    fake_ingestion(monkeypatch)
    checkpoint_contexts = []
    captured = {}
    original_builder = pipeline_module._build_checkpoint_context

    def capture_checkpoint_context(**kwargs):
        context = original_builder(**kwargs)
        checkpoint_contexts.append(context)
        return context

    def fake_run_analysts(**kwargs):
        captured.update(kwargs)
        return AnalystRunOutcome()

    monkeypatch.setattr(
        pipeline_module, "_build_checkpoint_context", capture_checkpoint_context
    )
    monkeypatch.setattr(pipeline_module, "run_analysts", fake_run_analysts)

    run_end_to_end(
        str(pdf_path),
        output_dir=str(output_dir),
        roster_path=write_roster(tmp_path),
    )

    assert len(checkpoint_contexts) == 1
    # The very object whose sha256 went into the resume fingerprint is the one
    # sent, so the two cannot disagree.
    assert (
        captured["instruction_contracts"]
        is checkpoint_contexts[0].instruction_contracts
    )
    assert set(captured["instruction_contracts"]) == set(THREE)


def test_run_end_to_end_omits_both_callbacks_without_output_dir(tmp_path, monkeypatch):
    pdf_path = tmp_path / "example.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    fake_ingestion(monkeypatch)
    captured = {}

    def fake_run_analysts(**kwargs):
        captured.update(kwargs)
        return AnalystRunOutcome()

    monkeypatch.setattr(pipeline_module, "run_analysts", fake_run_analysts)
    run_end_to_end(str(pdf_path), roster_path=write_roster(tmp_path))

    assert captured["on_attempt"] is None
    assert captured["on_report"] is None
    assert captured["output_dir"] is None


def test_later_failure_preserves_completed_alpha_checkpoint(tmp_path, monkeypatch):
    pdf_path = tmp_path / "example.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    output_dir = tmp_path / "case"
    fake_ingestion(monkeypatch)
    # Alpha answers, beta never does.
    use_provider(monkeypatch, FakeProvider(reply(), json.dumps({"bad": True})))

    with pytest.raises(AnalystExecutionError) as exc_info:
        run_end_to_end(
            str(pdf_path),
            output_dir=str(output_dir),
            roster_path=write_roster(tmp_path, ("analyst_alpha", "analyst_beta")),
            max_restarts=0,
        )

    assert exc_info.value.analyst_key == "analyst_beta"
    assert (
        validate_analyst_report(
            (output_dir / "analyst-alpha_report.md").read_text()
        ).payload.total_score
        == 6
    )
    assert (output_dir / "analyst-alpha_assessment.json").exists()
    analysts = read_manifest(output_dir)["analysts"]
    assert analysts["analyst_alpha"]["status"] == "completed"
    assert analysts["analyst_beta"]["status"] == "failed"


def test_second_invocation_supplies_all_checkpointed_reports(tmp_path, monkeypatch):
    pdf_path = tmp_path / "example.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    output_dir = tmp_path / "case"
    fake_ingestion(monkeypatch)
    roster_path = write_roster(tmp_path)
    use_provider(monkeypatch, FakeProvider(*[reply() for _ in THREE]))
    run_end_to_end(str(pdf_path), output_dir=str(output_dir), roster_path=roster_path)
    captured = {}

    def fake_run_analysts(**kwargs):
        captured.update(kwargs)
        return AnalystRunOutcome(reports=dict(kwargs["completed_reports"]))

    monkeypatch.setattr(pipeline_module, "run_analysts", fake_run_analysts)
    run_end_to_end(str(pdf_path), output_dir=str(output_dir), roster_path=roster_path)

    assert set(captured["completed_reports"]) == set(THREE)


def test_a_report_without_its_sidecar_does_not_count_as_completed(
    tmp_path, monkeypatch
):
    pdf_path = tmp_path / "example.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    output_dir = tmp_path / "case"
    fake_ingestion(monkeypatch)
    roster_path = write_roster(tmp_path, ("analyst_alpha",))
    use_provider(monkeypatch, FakeProvider(reply()))
    run_end_to_end(str(pdf_path), output_dir=str(output_dir), roster_path=roster_path)
    assert is_end_to_end_complete(
        str(pdf_path), str(output_dir), roster_path=roster_path
    )

    (output_dir / "analyst-alpha_assessment.json").unlink()

    assert not is_end_to_end_complete(
        str(pdf_path), str(output_dir), roster_path=roster_path
    )


def test_is_end_to_end_complete_uses_validated_checkpoint_manifest(
    tmp_path, monkeypatch
):
    pdf_path = tmp_path / "example.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    output_dir = tmp_path / "case"
    fake_ingestion(monkeypatch)
    roster_path = write_roster(tmp_path)
    use_provider(monkeypatch, FakeProvider(*[reply() for _ in THREE]))
    run_end_to_end(str(pdf_path), output_dir=str(output_dir), roster_path=roster_path)
    manifest_path = output_dir / "analyst_checkpoints.json"
    manifest_before = manifest_path.read_text(encoding="utf-8")

    assert is_end_to_end_complete(
        str(pdf_path), str(output_dir), roster_path=roster_path
    )
    assert manifest_path.read_text(encoding="utf-8") == manifest_before


def test_schema_v3_manifest_cannot_skip_execution_and_is_replaced_cleanly(
    tmp_path, monkeypatch
):
    secret = "MODEL-CONTROLLED-V3-MANIFEST-SECRET"
    pdf_path = tmp_path / "example.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    output_dir = tmp_path / "case"
    fake_ingestion(monkeypatch)
    roster_path = write_roster(tmp_path)
    use_provider(monkeypatch, FakeProvider(*[reply() for _ in THREE]))
    run_end_to_end(str(pdf_path), output_dir=str(output_dir), roster_path=roster_path)
    manifest_path = output_dir / "analyst_checkpoints.json"
    legacy_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    legacy_manifest["schema_version"] = CHECKPOINT_SCHEMA_VERSION - 1
    for entry in legacy_manifest["analysts"].values():
        entry["last_error"] = secret
        entry["attempt_history"] = [{"failure_kind": "validation", "error": secret}]
    manifest_path.write_text(json.dumps(legacy_manifest, indent=2), encoding="utf-8")
    manifest_before = manifest_path.read_bytes()

    assert not is_end_to_end_complete(
        str(pdf_path), str(output_dir), roster_path=roster_path
    )
    assert manifest_path.read_bytes() == manifest_before

    captured_completed_reports = []
    real_run_analysts = pipeline_module.run_analysts

    def capture_then_run(**kwargs):
        captured_completed_reports.append(dict(kwargs["completed_reports"]))
        return real_run_analysts(**kwargs)

    monkeypatch.setattr(pipeline_module, "run_analysts", capture_then_run)
    use_provider(monkeypatch, FakeProvider(*[reply() for _ in THREE]))
    run_end_to_end(str(pdf_path), output_dir=str(output_dir), roster_path=roster_path)

    current_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # The reports on disk are still valid, but an unreadable manifest must not
    # let them stand in for execution.
    assert captured_completed_reports == [{}]
    assert current_manifest["schema_version"] == CHECKPOINT_SCHEMA_VERSION
    assert current_manifest["schema_version"] == 4
    assert secret not in json.dumps(current_manifest)
    assert all(
        entry["status"] == "completed"
        for entry in current_manifest["analysts"].values()
    )


def test_is_end_to_end_complete_does_not_write_a_contracted_roster(
    tmp_path, monkeypatch
):
    """Removing an analyst leaves the others' checkpoints valid and untouched."""

    pdf_path = tmp_path / "example.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    output_dir = tmp_path / "case"
    fake_ingestion(monkeypatch)
    wide = write_roster(tmp_path, (*THREE, "analyst_delta"), name="wide.json")
    narrow = write_roster(tmp_path, THREE, name="narrow.json")
    use_provider(monkeypatch, FakeProvider(*[reply() for _ in range(4)]))
    run_end_to_end(str(pdf_path), output_dir=str(output_dir), roster_path=wide)
    manifest_path = output_dir / "analyst_checkpoints.json"
    manifest_before = manifest_path.read_bytes()
    assert json.loads(manifest_before)["enabled_analysts"] == [
        *THREE,
        "analyst_delta",
    ]
    writer = Mock(side_effect=AssertionError("completion check wrote manifest"))
    monkeypatch.setattr("dili_rucam_agents.checkpoints.atomic_write_json", writer)

    assert is_end_to_end_complete(str(pdf_path), str(output_dir), roster_path=narrow)
    writer.assert_not_called()
    assert manifest_path.read_bytes() == manifest_before


def test_run_batch_folder_skips_completed_pdf(tmp_path: Path, monkeypatch):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()
    (input_dir / "case-a.pdf").write_bytes(b"%PDF-1.4")

    result_dir = output_dir / "case-a"
    result_dir.mkdir()
    result_dir.joinpath("run_status.json").write_text(
        json.dumps(
            {
                "pdf_filename": "case-a.pdf",
                "status": "completed",
                "masking_enabled": True,
                "strict_scoring": False,
                "enabled_analysts": ["analyst_alpha", "analyst_beta", "analyst_gamma"],
            }
        ),
        encoding="utf-8",
    )
    for report_name in (
        "analyst-alpha_report.md",
        "analyst-beta_report.md",
        "analyst-gamma_report.md",
    ):
        result_dir.joinpath(report_name).write_text(
            complete_report(7),
            encoding="utf-8",
        )
    result_dir.joinpath("ground-truth-rucam-score_report.md").write_text(
        "# Ground Truth RUCAM Score Report\n\n"
        "## Stable Fields\n"
        "```text\nGROUND_TRUTH_RUCAM_SCORE: 8\nGROUND_TRUTH_RUCAM_CATEGORY: Probable\n```\n",
        encoding="utf-8",
    )
    result_dir.joinpath("masked-case-bundle_report.md").write_text(
        "# Masked Case Bundle Report\n\n## Stable Fields\n```text\nMASKED_RUCAM_SCORES: 8\n```\n",
        encoding="utf-8",
    )

    calls: list[str] = []

    def fake_run_end_to_end(*args, **kwargs):
        calls.append("called")
        return "ok"

    monkeypatch.setattr("dili_rucam_agents.batch.run_end_to_end", fake_run_end_to_end)
    monkeypatch.setattr(
        "dili_rucam_agents.batch.is_end_to_end_complete", lambda *args, **kwargs: True
    )

    run_batch_folder(
        input_dir=str(input_dir),
        output_dir=str(output_dir),
        enable_score_masking=True,
    )

    assert calls == []


def test_manifestless_completed_batch_enters_pipeline_once_then_skips(
    tmp_path: Path, monkeypatch
):
    """A finished run with no manifest is adopted once, then skipped."""

    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    pdf_path = input_dir / "case-a.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 legacy")
    result_dir = output_dir / "case-a"
    write_complete_artifacts(result_dir)
    result_dir.joinpath("masked-case-bundle_report.md").write_text(
        "# Masked Case Bundle Report\n", encoding="utf-8"
    )
    result_dir.joinpath("ground-truth-rucam-score_report.md").write_text(
        GROUND_TRUTH_REPORT, encoding="utf-8"
    )
    result_dir.joinpath("run_status.json").write_text(
        json.dumps(
            {
                "pdf_filename": pdf_path.name,
                "status": "completed",
                "masking_enabled": True,
                "strict_scoring": False,
                "enabled_analysts": list(THREE),
            }
        ),
        encoding="utf-8",
    )
    roster_path = write_roster(tmp_path)
    fake_ingestion(monkeypatch)
    pipeline_calls = []
    real_run_analysts = pipeline_module.run_analysts

    def capture_then_run(**kwargs):
        pipeline_calls.append(dict(kwargs["completed_reports"]))
        return real_run_analysts(**kwargs)

    monkeypatch.setattr(pipeline_module, "run_analysts", capture_then_run)
    # One reply, for the ground-truth finder; no analyst needs scoring.
    use_provider(monkeypatch, FakeProvider(GROUND_TRUTH_REPORT))

    run_batch_folder(
        input_dir=str(input_dir),
        output_dir=str(output_dir),
        roster_path=roster_path,
        enable_score_masking=True,
    )

    assert [set(reports) for reports in pipeline_calls] == [set(THREE)]
    assert result_dir.joinpath("analyst_checkpoints.json").exists()

    skip_guard = Mock(side_effect=AssertionError("compatible batch entered pipeline"))
    ingestion_guard = Mock(side_effect=AssertionError("compatible batch ingested PDF"))
    masking_guard = Mock(side_effect=AssertionError("compatible batch masked case"))
    monkeypatch.setattr(pipeline_module, "run_analysts", skip_guard)
    monkeypatch.setattr(pipeline_module, "build_case_bundle", ingestion_guard)
    monkeypatch.setattr(pipeline_module, "mask_case_bundle_payload", masking_guard)

    run_batch_folder(
        input_dir=str(input_dir),
        output_dir=str(output_dir),
        roster_path=roster_path,
        enable_score_masking=True,
    )

    skip_guard.assert_not_called()
    ingestion_guard.assert_not_called()
    masking_guard.assert_not_called()


@pytest.mark.parametrize("compatibility_change", ("model", "tokens", "prompt", "pdf"))
def test_adopted_legacy_batch_invalidates_on_compatibility_change(
    tmp_path: Path, monkeypatch, compatibility_change: str
):
    pdf_path = tmp_path / "case-a.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 legacy")
    result_dir = tmp_path / "case-a"
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("initial prompt", encoding="utf-8")
    write_complete_artifacts(result_dir)
    result_dir.joinpath("run_status.json").write_text(
        json.dumps(
            {
                "pdf_filename": pdf_path.name,
                "status": "completed",
                "masking_enabled": False,
                "strict_scoring": False,
                "enabled_analysts": list(THREE),
            }
        ),
        encoding="utf-8",
    )
    roster_path = write_roster(tmp_path)
    fake_ingestion(monkeypatch)
    monkeypatch.setattr(
        pipeline_module,
        "run_analysts",
        lambda **kwargs: AnalystRunOutcome(reports=dict(kwargs["completed_reports"])),
    )

    run_end_to_end(
        str(pdf_path),
        prompt_path=str(prompt_path),
        output_dir=str(result_dir),
        roster_path=roster_path,
    )
    assert is_end_to_end_complete(
        str(pdf_path),
        str(result_dir),
        prompt_path=str(prompt_path),
        roster_path=roster_path,
    )

    if compatibility_change == "model":
        roster_path = write_roster(
            tmp_path, models={"analyst_alpha": "changed-model"}
        )
    elif compatibility_change == "tokens":
        roster_path = write_roster(tmp_path, max_output_tokens=16001)
    elif compatibility_change == "prompt":
        prompt_path.write_text("changed prompt", encoding="utf-8")
    else:
        pdf_path.write_bytes(b"%PDF-1.4 changed")

    assert not is_end_to_end_complete(
        str(pdf_path),
        str(result_dir),
        prompt_path=str(prompt_path),
        roster_path=roster_path,
    )


def test_batch_contracted_roster_skips_without_mutating_checkpoint(
    tmp_path: Path, monkeypatch
):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    pdf_path = input_dir / "case-a.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    result_dir = output_dir / "case-a"
    wide = write_roster(tmp_path, (*THREE, "analyst_delta"), name="wide.json")
    narrow = write_roster(tmp_path, THREE, name="narrow.json")
    fake_ingestion(monkeypatch)
    use_provider(monkeypatch, FakeProvider(*[reply() for _ in range(4)]))

    run_end_to_end(str(pdf_path), output_dir=str(result_dir), roster_path=wide)
    result_dir.joinpath("run_status.json").write_text(
        json.dumps(
            {
                "pdf_filename": "case-a.pdf",
                "status": "completed",
                "masking_enabled": False,
                "strict_scoring": False,
                "enabled_analysts": [*THREE, "analyst_delta"],
            }
        ),
        encoding="utf-8",
    )
    manifest_path = result_dir / "analyst_checkpoints.json"
    manifest_before = manifest_path.read_bytes()

    monkeypatch.setattr(
        "dili_rucam_agents.batch.run_end_to_end",
        Mock(side_effect=AssertionError("contracted batch reran pipeline")),
    )

    run_batch_folder(
        input_dir=str(input_dir), output_dir=str(output_dir), roster_path=narrow
    )

    assert manifest_path.read_bytes() == manifest_before


def test_batch_roster_expansion_reenters_pipeline(tmp_path: Path, monkeypatch):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    pdf_path = input_dir / "case-a.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    result_dir = output_dir / "case-a"
    narrow = write_roster(tmp_path, THREE, name="narrow.json")
    wide = write_roster(tmp_path, (*THREE, "analyst_delta"), name="wide.json")
    fake_ingestion(monkeypatch)
    use_provider(monkeypatch, FakeProvider(*[reply() for _ in THREE]))

    run_end_to_end(str(pdf_path), output_dir=str(result_dir), roster_path=narrow)
    result_dir.joinpath("run_status.json").write_text(
        json.dumps(
            {
                "pdf_filename": "case-a.pdf",
                "status": "completed",
                "masking_enabled": False,
                "strict_scoring": False,
                "enabled_analysts": list(THREE),
            }
        ),
        encoding="utf-8",
    )
    calls = []

    def expanded_run(pdf_path, *, output_dir, **kwargs):
        calls.append(Path(pdf_path).name)
        write_complete_artifacts(Path(output_dir), (*THREE, "analyst_delta"))
        return "ok"

    monkeypatch.setattr("dili_rucam_agents.batch.run_end_to_end", expanded_run)

    summary_path = run_batch_folder(
        input_dir=str(input_dir), output_dir=str(output_dir), roster_path=wide
    )

    assert calls == ["case-a.pdf"]
    headers = [cell.value for cell in load_workbook(summary_path)["Batch Summary"][1]]
    assert headers[1:5] == [_MODELS[key] for key in (*THREE, "analyst_delta")]


def test_run_batch_folder_force_rerun_ignores_completed_status(
    tmp_path: Path, monkeypatch
):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()
    (input_dir / "case-a.pdf").write_bytes(b"%PDF-1.4")

    result_dir = output_dir / "case-a"
    result_dir.mkdir()
    result_dir.joinpath("run_status.json").write_text(
        json.dumps(
            {
                "pdf_filename": "case-a.pdf",
                "status": "completed",
                "masking_enabled": False,
                "strict_scoring": False,
                "enabled_analysts": ["analyst_alpha", "analyst_beta", "analyst_gamma"],
            }
        ),
        encoding="utf-8",
    )
    for report_name in (
        "analyst-alpha_report.md",
        "analyst-beta_report.md",
        "analyst-gamma_report.md",
    ):
        result_dir.joinpath(report_name).write_text(
            complete_report(7),
            encoding="utf-8",
        )

    calls: list[str] = []

    def fake_run_end_to_end(
        pdf_path: str,
        prompt_path: str | None = None,
        output_dir: str | None = None,
        **kwargs,
    ) -> str:
        calls.append(Path(pdf_path).name)
        result_dir = Path(output_dir)
        for report_name in (
            "analyst-alpha_report.md",
            "analyst-beta_report.md",
            "analyst-gamma_report.md",
        ):
            result_dir.joinpath(report_name).write_text(
                complete_report(8),
                encoding="utf-8",
            )
        return "ok"

    monkeypatch.setattr("dili_rucam_agents.batch.run_end_to_end", fake_run_end_to_end)

    run_batch_folder(
        input_dir=str(input_dir),
        output_dir=str(output_dir),
        force_rerun=True,
    )

    assert calls == ["case-a.pdf"]


def test_is_pdf_run_complete_requires_status_and_parseable_reports(tmp_path: Path):
    result_dir = tmp_path / "case-a"
    result_dir.mkdir()
    pdf_path = tmp_path / "case-a.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")

    assert not _is_pdf_run_complete(
        pdf_path=pdf_path,
        pdf_output_dir=result_dir,
        prompt_path=None,
        roster_path=write_roster(tmp_path),
        enable_score_masking=True,
        strict_scoring=False,
    )


def test_completed_status_delegates_all_compatibility_arguments(tmp_path, monkeypatch):
    pdf_path = tmp_path / "case-a.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    result_dir = tmp_path / "case-a"
    result_dir.mkdir()
    roster_path = write_roster(tmp_path, (*THREE, "analyst_delta"))
    result_dir.joinpath("run_status.json").write_text(
        json.dumps(
            {
                "pdf_filename": "case-a.pdf",
                "status": "completed",
                "masking_enabled": True,
                "strict_scoring": True,
                "enabled_analysts": [
                    "analyst_alpha",
                    "analyst_beta",
                    "analyst_gamma",
                    "analyst_delta",
                ],
            }
        ),
        encoding="utf-8",
    )
    captured_calls = []
    validator_outcomes = iter((True, False))

    def fake_is_end_to_end_complete(pdf_arg, output_arg, prompt_path=None, **kwargs):
        captured_calls.append((pdf_arg, output_arg, prompt_path, kwargs))
        return next(validator_outcomes)

    monkeypatch.setattr(
        "dili_rucam_agents.batch.is_end_to_end_complete",
        fake_is_end_to_end_complete,
    )

    settings = {
        "pdf_path": pdf_path,
        "pdf_output_dir": result_dir,
        "prompt_path": str(tmp_path / "prompt.md"),
        "roster_path": roster_path,
        "enable_score_masking": True,
        "strict_scoring": True,
    }

    assert _is_pdf_run_complete(**settings) is True
    assert _is_pdf_run_complete(**settings) is False
    forwarded = (
        str(pdf_path),
        str(result_dir),
        str(tmp_path / "prompt.md"),
        {
            "roster_path": roster_path,
            "enable_score_masking": True,
            "strict_scoring": True,
        },
    )
    assert captured_calls == [forwarded, forwarded]


def test_run_batch_folder_stops_on_error_and_marks_failed(tmp_path: Path, monkeypatch):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()
    (input_dir / "case-a.pdf").write_bytes(b"%PDF-1.4")
    (input_dir / "case-b.pdf").write_bytes(b"%PDF-1.4")

    calls: list[str] = []

    def fake_run_end_to_end(pdf_path: str, **kwargs) -> str:
        calls.append(Path(pdf_path).name)
        if Path(pdf_path).name == "case-a.pdf":
            raise RuntimeError("simulated failure")
        return "ok"

    monkeypatch.setattr("dili_rucam_agents.batch.run_end_to_end", fake_run_end_to_end)

    try:
        run_batch_folder(
            input_dir=str(input_dir),
            output_dir=str(output_dir),
        )
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "case-a.pdf" in str(exc)

    assert calls == ["case-a.pdf"]
    status = json.loads(
        (output_dir / "case-a" / "run_status.json").read_text(encoding="utf-8")
    )
    assert status["status"] == "failed"
    assert "RuntimeError" in status["error"]
    assert "simulated failure" not in status["error"]
    assert not (output_dir / "case-b" / "run_status.json").exists()


def test_batch_execution_exception_diagnostics_never_persist_raw_text(
    tmp_path: Path, monkeypatch
):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    (input_dir / "case-a.pdf").write_bytes(b"%PDF-1.4")
    secret = "sk-live-TOKEN-DO-NOT-STORE"
    clinical_text = "Patient Jane Doe ALT 980 after Drug Q"

    class ProviderRequestError(RuntimeError):
        status_code = 502

    def fail_run(*args, **kwargs):
        raise ProviderRequestError(
            f"Authorization: Bearer {secret}; request body: {clinical_text}"
        )

    monkeypatch.setattr("dili_rucam_agents.batch.run_end_to_end", fail_run)

    with pytest.raises(RuntimeError) as exc_info:
        run_batch_folder(
            input_dir=str(input_dir),
            output_dir=str(output_dir),
            debug=True,
        )

    result_dir = output_dir / "case-a"
    status_text = result_dir.joinpath("run_status.json").read_text(encoding="utf-8")
    log_text = result_dir.joinpath("case-a.log").read_text(encoding="utf-8")
    workbook = load_workbook(output_dir / "batch_summary.xlsx", data_only=True)
    workbook_text = " ".join(
        str(cell.value)
        for row in workbook.active.iter_rows()
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


def test_batch_validation_diagnostics_never_persist_caller_text(
    tmp_path: Path, monkeypatch
):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    (input_dir / "case-a.pdf").write_bytes(b"%PDF-1.4")
    secret = "MODEL-CONTROLLED-BATCH-SECRET"
    clinical_text = "Patient Jane Doe ALT 980 after Drug Q"

    def fail_run(*args, **kwargs):
        raise AnalystExecutionError(
            analyst_key="analyst_alpha",
            attempts=3,
            failure_kind="validation",
            validation_diagnostic=SafeValidationDiagnostic(
                (("rucam_scores.time_to_onset", "invalid_integer"),)
            ),
        )

    monkeypatch.setattr("dili_rucam_agents.batch.run_end_to_end", fail_run)

    with pytest.raises(RuntimeError) as exc_info:
        run_batch_folder(
            input_dir=str(input_dir),
            output_dir=str(output_dir),
            debug=True,
        )

    result_dir = output_dir / "case-a"
    status_text = result_dir.joinpath("run_status.json").read_text(encoding="utf-8")
    log_text = result_dir.joinpath("case-a.log").read_text(encoding="utf-8")
    workbook = load_workbook(output_dir / "batch_summary.xlsx", data_only=True)
    workbook_text = " ".join(
        str(cell.value)
        for row in workbook.active.iter_rows()
        for cell in row
        if cell.value is not None
    )
    diagnostics = (str(exc_info.value), status_text, log_text, workbook_text)
    for diagnostic in diagnostics:
        assert secret not in diagnostic
        assert clinical_text not in diagnostic
        assert "rucam_scores.time_to_onset" in diagnostic
        assert "valid integer" in diagnostic


def test_batch_validation_sink_rejects_forged_structured_diagnostic():
    secret = "MODEL-CONTROLLED-FORGED-BATCH-SECRET"
    error = AnalystExecutionError(
        analyst_key="analyst_alpha",
        attempts=1,
        failure_kind="validation",
        validation_diagnostic=SafeValidationDiagnostic(
            (("total_score", "score_sum_mismatch"),)
        ),
    )
    forged = object.__new__(SafeValidationDiagnostic)
    object.__setattr__(forged, "issues", (("total_score", secret),))
    error.validation_diagnostic = forged

    with pytest.raises(ValueError) as exc_info:
        _safe_batch_failure_diagnostic(error)

    assert secret not in str(exc_info.value)


def test_batch_validation_sink_never_executes_hostile_issues_hooks():
    secret = "HOSTILE-BATCH-ISSUES-SECRET"

    class HostileIssues:
        def __init__(self):
            self.invoked = []

        def _raise(self, hook):
            self.invoked.append(hook)
            raise RuntimeError(f"{hook}: {secret}")

        def __iter__(self):
            return self._raise("__iter__")

        def __len__(self):
            return self._raise("__len__")

        def __repr__(self):
            return self._raise("__repr__")

    error = AnalystExecutionError(
        analyst_key="analyst_alpha",
        attempts=1,
        failure_kind="validation",
        validation_diagnostic=SafeValidationDiagnostic(
            (("total_score", "score_sum_mismatch"),)
        ),
    )
    issues = HostileIssues()
    forged = object.__new__(SafeValidationDiagnostic)
    object.__setattr__(forged, "issues", issues)
    error.validation_diagnostic = forged

    with pytest.raises(ValueError) as exc_info:
        _safe_batch_failure_diagnostic(error)

    assert issues.invoked == []
    assert secret not in str(exc_info.value)


def test_run_batch_folder_identifies_analyst_report_when_section_c_is_missing(
    tmp_path: Path, monkeypatch
):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()
    (input_dir / "case-a.pdf").write_bytes(b"%PDF-1.4")

    def fake_run_end_to_end(
        pdf_path: str,
        prompt_path: str | None = None,
        output_dir: str | None = None,
        **kwargs,
    ) -> str:
        result_dir = Path(output_dir)
        result_dir.mkdir(parents=True, exist_ok=True)
        result_dir.joinpath("analyst-alpha_report.md").write_text(
            complete_report(7),
            encoding="utf-8",
        )
        result_dir.joinpath("analyst-beta_report.md").write_text(
            "See complete Sections A, B, and C above.\n",
            encoding="utf-8",
        )
        result_dir.joinpath("analyst-gamma_report.md").write_text(
            complete_report(7),
            encoding="utf-8",
        )
        return "ok"

    monkeypatch.setattr("dili_rucam_agents.batch.run_end_to_end", fake_run_end_to_end)

    try:
        run_batch_folder(
            input_dir=str(input_dir),
            output_dir=str(output_dir),
            roster_path=write_roster(tmp_path),
        )
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        message = str(exc)
        assert "analyst-beta_report.md" in message
        assert "summary placeholder" in message


def test_run_batch_folder_reruns_failed_pdf_on_resume(tmp_path: Path, monkeypatch):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()
    (input_dir / "case-a.pdf").write_bytes(b"%PDF-1.4")

    result_dir = output_dir / "case-a"
    result_dir.mkdir()
    result_dir.joinpath("run_status.json").write_text(
        json.dumps(
            {
                "pdf_filename": "case-a.pdf",
                "status": "failed",
                "error": "old failure",
            }
        ),
        encoding="utf-8",
    )

    calls: list[str] = []

    def fake_run_end_to_end(
        pdf_path: str,
        prompt_path: str | None = None,
        output_dir: str | None = None,
        **kwargs,
    ) -> str:
        calls.append(Path(pdf_path).name)
        result_dir = Path(output_dir)
        for report_name in (
            "analyst-alpha_report.md",
            "analyst-beta_report.md",
            "analyst-gamma_report.md",
        ):
            result_dir.joinpath(report_name).write_text(
                complete_report(7),
                encoding="utf-8",
            )
        return "ok"

    monkeypatch.setattr("dili_rucam_agents.batch.run_end_to_end", fake_run_end_to_end)

    run_batch_folder(
        input_dir=str(input_dir),
        output_dir=str(output_dir),
    )

    assert calls == ["case-a.pdf"]
    status = json.loads((result_dir / "run_status.json").read_text(encoding="utf-8"))
    assert status["status"] == "completed"


# -- the roster is what selects the analysts --------------------------------


def _fake_pipeline_writing(monkeypatch, keys, total_score: int = 7):
    """Stand in for the pipeline, writing one valid report pair per key."""

    def fake_run_end_to_end(pdf_path, prompt_path=None, output_dir=None, **kwargs):
        write_complete_artifacts(Path(output_dir), keys, total_score)
        return "ok"

    monkeypatch.setattr("dili_rucam_agents.batch.run_end_to_end", fake_run_end_to_end)


def test_default_run_scores_one_analyst(tmp_path: Path, monkeypatch):
    """No --analysts means config/analysts.json: one column, one analyst key."""

    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "case-a.pdf").write_bytes(b"%PDF-1.4")
    default_keys = [config.key for config in load_roster().analysts]
    assert default_keys == ["analyst_alpha"]  # the shipped default
    _fake_pipeline_writing(monkeypatch, default_keys)

    summary_path = run_batch_folder(
        input_dir=str(input_dir), output_dir=str(tmp_path / "output")
    )

    sheet = load_workbook(summary_path)["Batch Summary"]
    headers = [cell.value for cell in sheet[1]]
    assert headers == [
        "pdf_filename",
        "gpt-5.5",
        "masked_rucam_score",
        "masked_rucam_category",
    ]
    assert [cell.value for cell in sheet[2]][:2] == ["case-a.pdf", 7]


def test_analysts_flag_selects_the_study_roster(tmp_path: Path, monkeypatch):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "case-a.pdf").write_bytes(b"%PDF-1.4")
    study = load_roster(STUDY_ROSTER_PATH)
    assert len(study.analysts) == 7
    _fake_pipeline_writing(monkeypatch, [config.key for config in study.analysts])

    summary_path = run_batch_folder(
        input_dir=str(input_dir),
        output_dir=str(tmp_path / "output"),
        roster_path=str(STUDY_ROSTER_PATH),
    )

    sheet = load_workbook(summary_path)["Batch Summary"]
    headers = [cell.value for cell in sheet[1]]
    assert headers[1:-2] == [config.model for config in study.analysts]
    assert [cell.value for cell in sheet[2]][1:-2] == [7] * 7


def test_workbook_columns_follow_the_roster_order(tmp_path: Path, monkeypatch):
    """Column order is the roster's order, not an order fixed in code."""

    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "case-a.pdf").write_bytes(b"%PDF-1.4")
    reversed_keys = tuple(reversed(THREE))
    _fake_pipeline_writing(monkeypatch, reversed_keys)

    summary_path = run_batch_folder(
        input_dir=str(input_dir),
        output_dir=str(tmp_path / "output"),
        roster_path=write_roster(tmp_path, reversed_keys),
    )

    headers = [cell.value for cell in load_workbook(summary_path)["Batch Summary"][1]]
    assert headers[1:4] == [_MODELS[key] for key in reversed_keys]
    assert headers[1:4] != [_MODELS[key] for key in THREE]


@pytest.mark.parametrize(
    "flag", ("--analyst-delta", "--analyst-epsilon", "--analyst-zeta", "--analyst-eta")
)
def test_removed_analyst_flags_are_rejected_by_the_parser(
    tmp_path: Path, capsys, flag: str
):
    with pytest.raises(SystemExit, match="2"):
        batch_main([str(tmp_path / "input"), str(tmp_path / "output"), flag])

    assert "unrecognized arguments" in capsys.readouterr().err


def test_both_clis_accept_the_analysts_flag(tmp_path: Path, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "dili_rucam_agents.batch.run_batch_folder",
        lambda **kwargs: captured.update(kwargs) or Path("batch_summary.xlsx"),
    )
    batch_main(
        [
            str(tmp_path / "input"),
            str(tmp_path / "output"),
            "--analysts",
            str(STUDY_ROSTER_PATH),
        ]
    )
    assert captured["roster_path"] == str(STUDY_ROSTER_PATH)

    pipeline_captured = {}
    monkeypatch.setattr(
        pipeline_module,
        "run_end_to_end",
        lambda *args, **kwargs: pipeline_captured.update(kwargs) or "ok",
    )
    monkeypatch.setattr(
        sys, "argv", ["pipeline", "case.pdf", "--analysts", str(STUDY_ROSTER_PATH)]
    )
    pipeline_module._main()
    assert pipeline_captured["roster_path"] == str(STUDY_ROSTER_PATH)


def test_a_malformed_roster_names_the_file_and_runs_nothing(tmp_path: Path, monkeypatch):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "case-a.pdf").write_bytes(b"%PDF-1.4")
    roster_path = tmp_path / "broken.json"
    roster_path.write_text('{"analysts": []}', encoding="utf-8")
    monkeypatch.setattr(
        "dili_rucam_agents.batch.run_end_to_end",
        Mock(side_effect=AssertionError("ran with a malformed roster")),
    )

    with pytest.raises(RosterError, match="broken.json"):
        run_batch_folder(
            input_dir=str(input_dir),
            output_dir=str(tmp_path / "output"),
            roster_path=str(roster_path),
        )


# -- the CLI reads .env, and a failed analyst records what it spent ---------


def test_both_clis_load_dotenv_before_doing_any_work(tmp_path: Path, monkeypatch):
    """Keys in a .env file used to be loaded by importing crewai.

    python-dotenv is a declared dependency and .gitignore anticipates a .env,
    so a command-line run still reads one. It must happen before the API-key
    pre-flight, which is the first thing a run does.
    """

    order: list[str] = []
    monkeypatch.setattr(
        "dili_rucam_agents.batch.load_dotenv", lambda _path: order.append("dotenv")
    )
    monkeypatch.setattr(
        "dili_rucam_agents.batch.run_batch_folder",
        lambda **kwargs: order.append("run") or Path("batch_summary.xlsx"),
    )
    batch_main([str(tmp_path / "input"), str(tmp_path / "output")])
    assert order == ["dotenv", "run"]

    order.clear()
    monkeypatch.setattr(
        pipeline_module, "load_dotenv", lambda _path: order.append("dotenv")
    )
    monkeypatch.setattr(
        pipeline_module,
        "run_end_to_end",
        lambda *args, **kwargs: order.append("run") or "ok",
    )
    monkeypatch.setattr(sys, "argv", ["pipeline", "case.pdf"])
    pipeline_module._main()
    assert order == ["dotenv", "run"]


def test_the_clis_look_for_dotenv_from_the_invocation_directory(
    tmp_path: Path, monkeypatch
):
    """Not from this package's own directory, which is where the default looks."""

    (tmp_path / ".env").write_text("DILI_PROBE_VAR=from-dotenv\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DILI_PROBE_VAR", raising=False)
    monkeypatch.setattr(
        "dili_rucam_agents.batch.run_batch_folder",
        lambda **kwargs: Path("batch_summary.xlsx"),
    )

    batch_main([str(tmp_path / "input"), str(tmp_path / "output")])

    assert os.environ["DILI_PROBE_VAR"] == "from-dotenv"


def test_a_failed_analysts_retry_counters_reach_the_manifest(tmp_path, monkeypatch):
    """The gate counts validation retries, and a failure is where they land.

    An analyst that fails has by definition spent its whole validation budget,
    so its counters are the ones the acceptance gate most needs.
    """

    pdf_path = tmp_path / "example.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    output_dir = tmp_path / "case"
    fake_ingestion(monkeypatch)
    use_provider(
        monkeypatch,
        FakeProvider(*[json.dumps({"nonsense": n}) for n in range(1, 4)]),
    )

    with pytest.raises(AnalystExecutionError) as exc_info:
        run_end_to_end(
            str(pdf_path),
            output_dir=str(output_dir),
            roster_path=write_roster(tmp_path, ("analyst_alpha",)),
            max_restarts=2,
        )

    entry = read_manifest(output_dir)["analysts"]["analyst_alpha"]
    assert entry["status"] == "failed"
    assert entry["failure_kind"] == "validation"
    # Three attempts, so two retries -- the same units the error reports.
    assert entry["validation_retries"] == 2
    assert exc_info.value.counters.validation_retries == 2
    assert entry["transient_retries"] == 0
    assert entry["truncation_escalations"] == 0
    assert entry["mode_downgrades"] == 0
    assert entry["final_mode"] == "json_schema"
    # One history entry per attempt, not two for the last one.
    assert [item["total_attempt"] for item in entry["attempt_history"]] == [1, 2, 3]


def test_a_bad_key_is_persisted_as_terminal_not_execution(
    tmp_path, monkeypatch
):
    """The manifest must separate "the endpoint is flaky" from "the key is wrong"."""
    pdf_path = tmp_path / "example.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    output_dir = tmp_path / "case"
    fake_ingestion(monkeypatch)
    use_provider(monkeypatch, FakeProvider(StatusError(401, "bad key")))

    with pytest.raises(AnalystExecutionError):
        run_end_to_end(
            str(pdf_path),
            output_dir=str(output_dir),
            roster_path=write_roster(tmp_path, ("analyst_alpha",)),
            max_restarts=0,
        )

    entry = read_manifest(output_dir)["analysts"]["analyst_alpha"]
    assert entry["failure_kind"] == "terminal"
    assert entry["last_error"] == (
        "Execution error [type=StatusError; status_code=401]"
    )
    assert entry["validation_retries"] == 0
    assert entry["transient_retries"] == 0
    assert entry["final_mode"] == "json_schema"
    assert [item["failure_kind"] for item in entry["attempt_history"]] == ["terminal"]


def test_an_exhausted_transient_budget_is_still_persisted_as_execution(
    tmp_path, monkeypatch
):
    """A retryable status that never clears is the other reading of the status."""
    pdf_path = tmp_path / "example.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    output_dir = tmp_path / "case"
    fake_ingestion(monkeypatch)
    # The schedule itself is pinned in test_runner; don't spend 7.5s here.
    monkeypatch.setattr(runner_module, "_backoff_seconds", lambda errors: 0.0)
    use_provider(
        monkeypatch, FakeProvider(*[StatusError(429, "slow down")] * 5)
    )

    with pytest.raises(AnalystExecutionError):
        run_end_to_end(
            str(pdf_path),
            output_dir=str(output_dir),
            roster_path=write_roster(tmp_path, ("analyst_alpha",)),
            max_restarts=0,
        )

    entry = read_manifest(output_dir)["analysts"]["analyst_alpha"]
    assert entry["failure_kind"] == "execution"
    assert entry["transient_retries"] == 4


# A path that is not a directory used to make the whole run a no-op that reported
# success: "Batch analysis completed", an empty workbook, exit 0. A file and a
# mistyped path both reach it. An existing empty directory is a separate, and
# deliberate, case.


def test_a_file_where_a_directory_belongs_is_refused_by_name(tmp_path):
    pdf = tmp_path / "case.pdf"
    pdf.write_bytes(b"%PDF-1.4")

    with pytest.raises(NotADirectoryError, match=re.escape(str(pdf))):
        resolve_pdf_source_dir(str(pdf))


def test_a_mistyped_path_is_refused_by_name(tmp_path):
    missing = tmp_path / "nope"

    with pytest.raises(NotADirectoryError, match=re.escape(str(missing))):
        resolve_pdf_source_dir(str(missing))


def test_an_existing_but_empty_directory_is_still_accepted(tmp_path):
    # Deliberate: "process every PDF here, there are none" is a reasonable ask,
    # and tests/test_reproducibility.py pins the resulting empty result.
    assert resolve_pdf_source_dir(str(tmp_path)) == tmp_path.resolve()


def test_a_directory_holding_a_pdf_resolves(tmp_path):
    (tmp_path / "case.pdf").write_bytes(b"%PDF-1.4")

    assert resolve_pdf_source_dir(str(tmp_path)) == tmp_path.resolve()


def test_run_batch_folder_refuses_a_file_without_writing_a_workbook(tmp_path):
    pdf = tmp_path / "case.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    out = tmp_path / "out"

    with pytest.raises(NotADirectoryError, match=re.escape(str(pdf))):
        run_batch_folder(input_dir=str(pdf), output_dir=str(out))

    # The old behaviour's tell was a summary workbook that said nothing happened.
    assert not (out / "batch_summary.xlsx").exists()


def test_run_reproducibility_folder_refuses_a_file_too(tmp_path):
    from dili_rucam_agents.reproducibility import run_reproducibility_folder

    pdf = tmp_path / "case.pdf"
    pdf.write_bytes(b"%PDF-1.4")

    with pytest.raises(NotADirectoryError, match=re.escape(str(pdf))):
        run_reproducibility_folder(input_dir=str(pdf), output_dir=str(tmp_path / "out"))
