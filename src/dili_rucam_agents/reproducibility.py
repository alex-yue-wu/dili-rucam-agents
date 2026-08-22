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


__all__ = [
    "ReproducibilityRunRecord",
    "ScoreStatistics",
]
