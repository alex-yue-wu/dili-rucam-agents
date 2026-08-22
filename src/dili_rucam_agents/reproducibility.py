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

from dili_rucam_agents.batch import (
    PdfRunContext,
    PdfRunFailure,
    _get_resolved_analyst_configs,
    _run_pdf_analysis,
)
from dili_rucam_agents.crew.agents import resolve_ground_truth_score_finder_model
from dili_rucam_agents.crew.crew import validate_max_restarts


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
    modes = sorted(
        score for score, count in counts.items() if count == highest_frequency
    )
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
        *[f"{config['key']}_score" for config in enabled_analyst_configs],
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
            values.extend([record.masked_rucam_score, record.masked_rucam_category])
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
    completed_records = [record for record in records if record.status == "completed"]

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


def validate_repeats(repeats: int) -> int:
    if type(repeats) is not int or repeats < 1:
        raise ValueError("repeats must be a positive integer")
    return repeats


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
        print(
            f"\n===================== Reproducibility PDF: {pdf_path} "
            f"({repeats} repeats) =====================\n"
        )
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
                        masked_rucam_category=exc.row.get("masked_rucam_category"),
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

            if result.reused:
                print(f"Skipping completed repeat: {repeat_dir.name}")
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
                    masked_rucam_category=result.row.get("masked_rucam_category"),
                )
            )
            _write_reproducibility_workbook(
                records=records,
                enabled_analyst_configs=enabled_analyst_configs,
                enable_score_masking=enable_score_masking,
                output_path=summary_path,
            )
        summary_paths.append(summary_path)
    print(
        "\n===================== Reproducibility analysis completed "
        "=====================\n"
    )
    return summary_paths


__all__ = [
    "ReproducibilityRunRecord",
    "ScoreStatistics",
    "run_reproducibility_folder",
    "validate_repeats",
]
