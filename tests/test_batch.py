import json
from pathlib import Path

from openpyxl import load_workbook
import pytest

from dili_rucam_agents.batch import (
    _is_pdf_run_complete,
    extract_ground_truth_rucam_category,
    extract_ground_truth_rucam_score,
    extract_section_c_json,
    run_batch_folder,
)
from dili_rucam_agents.crew.crew import AnalystAttemptEvent, AnalystExecutionError
from dili_rucam_agents.pipeline import is_end_to_end_complete, run_end_to_end
from dili_rucam_agents.validators.analyst_report import validate_analyst_report


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
                '## SECTION C\n```json\n{"total_score": 7, "category": "Probable"}\n```\n',
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
                '## SECTION C\n```json\n{"total_score": 7, "category": "Probable"}\n```\n',
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


def test_run_end_to_end_forwards_strict_scoring(monkeypatch):
    captured_kwargs = {}

    def fake_run_crew(pdf_path, prompt_path=None, **kwargs):
        captured_kwargs.update(kwargs)
        return "ok"

    monkeypatch.setattr("dili_rucam_agents.pipeline.run_crew", fake_run_crew)

    result = run_end_to_end("example.pdf", strict_scoring=True)

    assert result == "ok"
    assert captured_kwargs["strict_scoring"] is True


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
    run_end_to_end(str(pdf_path), output_dir=str(output_dir), resume=False)

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
                AnalystAttemptEvent(key, 1, 3, "completed", report_text=complete_report())
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


def test_is_end_to_end_complete_uses_validated_checkpoint_manifest(tmp_path, monkeypatch):
    pdf_path = tmp_path / "example.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    output_dir = tmp_path / "case"
    analyst_keys = ("analyst_alpha", "analyst_beta", "analyst_gamma")

    def seed_run(pdf_path, prompt_path=None, **kwargs):
        for key in analyst_keys:
            kwargs["on_attempt"](AnalystAttemptEvent(key, 1, 3, "running"))
            kwargs["on_attempt"](
                AnalystAttemptEvent(key, 1, 3, "completed", report_text=complete_report())
            )
        return "ok", {key: complete_report() for key in analyst_keys}

    monkeypatch.setattr("dili_rucam_agents.pipeline.run_crew", seed_run)
    run_end_to_end(str(pdf_path), output_dir=str(output_dir))
    manifest_path = output_dir / "analyst_checkpoints.json"
    manifest_before = manifest_path.read_text(encoding="utf-8")

    assert is_end_to_end_complete(str(pdf_path), str(output_dir))
    assert manifest_path.read_text(encoding="utf-8") == manifest_before


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
            '## SECTION C\n```json\n{"total_score": 7, "category": "Probable"}\n```\n',
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

    run_batch_folder(
        input_dir=str(input_dir),
        output_dir=str(output_dir),
        enable_score_masking=True,
    )

    assert calls == []


def test_run_batch_folder_force_rerun_ignores_completed_status(tmp_path: Path, monkeypatch):
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
            '## SECTION C\n```json\n{"total_score": 7, "category": "Probable"}\n```\n',
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
                '## SECTION C\n```json\n{"total_score": 8, "category": "Probable"}\n```\n',
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

    assert not _is_pdf_run_complete(
        pdf_output_dir=result_dir,
        enabled_analyst_configs=[
            {"key": "analyst_alpha"},
            {"key": "analyst_beta"},
            {"key": "analyst_gamma"},
        ],
        enable_score_masking=True,
    )


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
    status = json.loads((output_dir / "case-a" / "run_status.json").read_text(encoding="utf-8"))
    assert status["status"] == "failed"
    assert "simulated failure" in status["error"]
    assert not (output_dir / "case-b" / "run_status.json").exists()


def test_run_batch_folder_identifies_analyst_report_when_section_c_is_missing(tmp_path: Path, monkeypatch):
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
            '## SECTION C\n```json\n{"total_score": 7, "category": "Probable"}\n```\n',
            encoding="utf-8",
        )
        result_dir.joinpath("analyst-beta_report.md").write_text(
            "See complete Sections A, B, and C above.\n",
            encoding="utf-8",
        )
        result_dir.joinpath("analyst-gamma_report.md").write_text(
            '## SECTION C\n```json\n{"total_score": 7, "category": "Probable"}\n```\n',
            encoding="utf-8",
        )
        return "ok"

    monkeypatch.setattr("dili_rucam_agents.batch.run_end_to_end", fake_run_end_to_end)

    try:
        run_batch_folder(
            input_dir=str(input_dir),
            output_dir=str(output_dir),
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
                '## SECTION C\n```json\n{"total_score": 7, "category": "Probable"}\n```\n',
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
