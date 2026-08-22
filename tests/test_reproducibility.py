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
