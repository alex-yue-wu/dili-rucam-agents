from __future__ import annotations

import argparse
import contextlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from openpyxl import Workbook

from dili_rucam_agents.crew.agents import resolve_rucam_model
from dili_rucam_agents.crew.config import get_enabled_analyst_configs
from dili_rucam_agents.crew.crew import AnalystExecutionError, validate_max_restarts
from dili_rucam_agents.diagnostics import (
    SafeValidationDiagnostic,
    format_execution_error,
    render_validation_diagnostic,
    validate_validation_diagnostic,
)
from dili_rucam_agents.ground_truth import (
    extract_ground_truth_rucam_category,
    extract_ground_truth_rucam_score,
)
from dili_rucam_agents.pipeline import is_end_to_end_complete, run_end_to_end
from dili_rucam_agents.validators.analyst_report import (
    AnalystReportValidationError,
    parse_section_c_payload,
    validate_analyst_report,
)


_RUN_STATUS_FILENAME = "run_status.json"


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


class _AnalystReportFileValidationError(ValueError):
    def __init__(
        self, *, report_filename: str, diagnostic: SafeValidationDiagnostic
    ) -> None:
        self.report_filename = report_filename
        self.diagnostic = validate_validation_diagnostic(diagnostic)
        super().__init__(
            f"{report_filename}: {render_validation_diagnostic(self.diagnostic)}"
        )


def run_batch_folder(
    *,
    input_dir: str,
    output_dir: str,
    prompt_path: Optional[str] = None,
    enable_score_masking: bool = False,
    strict_scoring: bool = False,
    use_analyst_delta: bool = False,
    use_analyst_epsilon: bool = False,
    use_analyst_zeta: bool = False,
    use_analyst_eta: bool = False,
    debug: bool = False,
    force_rerun: bool = False,
    max_restarts: int = 2,
) -> Path:
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
    summary_rows: list[dict[str, Any]] = []

    for pdf_path in sorted(source_dir.glob("*.pdf")):
        print(
            f"\n===================== Analyzing PDF: {pdf_path} =====================\n"
        )
        pdf_output_dir = results_dir / pdf_path.stem
        try:
            result = _run_pdf_analysis(
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
                debug=debug,
                force_rerun=force_rerun,
                max_restarts=max_restarts,
            )
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
        if result.reused:
            print(f"Skipping completed PDF: {pdf_path.name}")
        summary_rows.append(result.row)

    summary_path = results_dir / "batch_summary.xlsx"
    _write_summary_workbook(summary_rows, enabled_analyst_configs, summary_path)
    print(f"\n===================== Batch analysis completed =====================\n")
    return summary_path


def _initialize_summary_row(
    pdf_path: Path,
    enabled_analyst_configs: list[dict[str, Any]],
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "pdf_filename": pdf_path.name,
        "masked_rucam_score": None,
        "masked_rucam_category": "None",
    }
    for config in enabled_analyst_configs:
        row[config["key"]] = None
    return row


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


def _status_path(pdf_output_dir: Path) -> Path:
    return pdf_output_dir / _RUN_STATUS_FILENAME


def _read_pdf_run_status(pdf_output_dir: Path) -> dict[str, Any] | None:
    status_path = _status_path(pdf_output_dir)
    if not status_path.exists():
        return None
    try:
        return json.loads(status_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _write_pdf_run_status(*, pdf_output_dir: Path, payload: dict[str, Any]) -> None:
    _status_path(pdf_output_dir).write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _is_pdf_run_complete(
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
    run_context: PdfRunContext = PdfRunContext(),
) -> bool:
    status = _read_pdf_run_status(pdf_output_dir)
    if not status or status.get("status") != "completed":
        return False
    if status.get("pdf_filename") != pdf_path.name:
        return False
    if status.get("masking_enabled") != enable_score_masking:
        return False
    if status.get("strict_scoring") != strict_scoring:
        return False
    for key, value in run_context.status_fields().items():
        if status.get(key) != value:
            return False
    return is_end_to_end_complete(
        str(pdf_path),
        str(pdf_output_dir),
        prompt_path=prompt_path,
        enable_score_masking=enable_score_masking,
        strict_scoring=strict_scoring,
        use_analyst_delta=use_analyst_delta,
        use_analyst_epsilon=use_analyst_epsilon,
        use_analyst_zeta=use_analyst_zeta,
        use_analyst_eta=use_analyst_eta,
    )


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
        "enabled_analysts": [config["key"] for config in enabled_analyst_configs],
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
            try:
                _populate_row_from_reports(
                    row,
                    pdf_output_dir,
                    enabled_analyst_configs,
                    enable_score_masking,
                    tolerate_invalid=True,
                )
            except (OSError, ValueError):
                pass
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


def _safe_batch_failure_diagnostic(exc: Exception) -> str:
    if isinstance(exc, _AnalystReportFileValidationError):
        return f"{exc.report_filename}: {render_validation_diagnostic(exc.diagnostic)}"
    if not isinstance(exc, AnalystExecutionError):
        return format_execution_error(exc)
    if exc.failure_kind == "validation":
        return (
            f"{exc.analyst_key} failed after {exc.attempts} attempts "
            f"(validation): "
            f"{render_validation_diagnostic(exc.validation_diagnostic)}"
        )
    cause = exc.__cause__
    execution_error = cause if isinstance(cause, Exception) else exc
    return (
        f"{exc.analyst_key} failed after {exc.attempts} attempts "
        f"(execution): {format_execution_error(execution_error)}"
    )


def _utc_now_isoformat() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _populate_row_from_reports(
    row: dict[str, Any],
    pdf_output_dir: Path,
    enabled_analyst_configs: list[dict[str, Any]],
    enable_score_masking: bool,
    *,
    tolerate_invalid: bool = False,
) -> None:
    if enable_score_masking:
        ground_truth_report_path = pdf_output_dir / "ground-truth-rucam-score_report.md"
        if ground_truth_report_path.exists():
            report_text = ground_truth_report_path.read_text(encoding="utf-8")
            row["masked_rucam_score"] = extract_ground_truth_rucam_score(report_text)
            row["masked_rucam_category"] = extract_ground_truth_rucam_category(
                report_text
            )

    for config in enabled_analyst_configs:
        report_path = pdf_output_dir / f"{config['key'].replace('_', '-')}_report.md"
        if not report_path.exists():
            continue
        report_text = report_path.read_text(encoding="utf-8")
        try:
            validated = validate_analyst_report(report_text, allow_legacy_json=True)
        except AnalystReportValidationError as exc:
            if tolerate_invalid:
                continue
            raise _AnalystReportFileValidationError(
                report_filename=report_path.name,
                diagnostic=exc.diagnostic,
            ) from exc
        row[config["key"]] = validated.payload.total_score


def extract_section_c_json(report_text: str) -> dict[str, Any]:
    try:
        return parse_section_c_payload(report_text, allow_legacy_json=True)
    except ValueError as exc:
        diagnostic = _describe_legacy_missing_section_c(report_text)
        if diagnostic is not None:
            raise ValueError(diagnostic) from exc
        raise


def _describe_legacy_missing_section_c(report_text: str) -> str | None:
    stripped = report_text.strip()
    if "see complete sections a, b, and c above." in stripped.lower():
        return (
            "Unable to locate SECTION C JSON in report; the model returned a summary "
            "placeholder instead of the full report."
        )
    if re.search(r"section\s+b", report_text, re.IGNORECASE) and not re.search(
        r"section\s+c", report_text, re.IGNORECASE
    ):
        return "Unable to locate SECTION C JSON in report; report appears truncated before SECTION C."
    return None


def _write_summary_workbook(
    rows: list[dict[str, Any]],
    enabled_analyst_configs: list[dict[str, Any]],
    output_path: Path,
) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Batch Summary"

    headers = ["pdf_filename"] + [
        config["resolved_model_name"] for config in enabled_analyst_configs
    ] + ["masked_rucam_score", "masked_rucam_category"]
    row_keys = ["pdf_filename"] + [
        config["key"] for config in enabled_analyst_configs
    ] + ["masked_rucam_score", "masked_rucam_category"]

    sheet.append(headers)
    for row in rows:
        sheet.append([row.get(key, "") for key in row_keys])

    for column_cells in sheet.columns:
        max_length = max(len(str(cell.value or "")) for cell in column_cells)
        sheet.column_dimensions[column_cells[0].column_letter].width = min(
            max(max_length + 2, 12), 60
        )

    workbook.save(output_path)


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Run dili_rucam_agents across a folder of PDFs."
    )
    parser.add_argument("input_dir", help="Directory containing input PDF files.")
    parser.add_argument(
        "output_dir",
        help="Directory where per-PDF results and the summary workbook are written.",
    )
    parser.add_argument(
        "--prompt-path",
        dest="prompt_path",
        help="Optional override for the production prompt file.",
    )
    parser.add_argument(
        "--mask-scores", dest="enable_score_masking", action="store_true"
    )
    parser.add_argument("--strict-scoring", dest="strict_scoring", action="store_true")
    parser.add_argument(
        "--analyst-delta", dest="use_analyst_delta", action="store_true"
    )
    parser.add_argument(
        "--analyst-epsilon", dest="use_analyst_epsilon", action="store_true"
    )
    parser.add_argument("--analyst-zeta", dest="use_analyst_zeta", action="store_true")
    parser.add_argument("--analyst-eta", dest="use_analyst_eta", action="store_true")
    parser.add_argument("--debug", dest="debug", action="store_true")
    parser.add_argument("--force-rerun", dest="force_rerun", action="store_true")
    parser.add_argument(
        "--analyst-restarts",
        type=int,
        choices=range(0, 3),
        default=2,
        help="Number of restarts per incomplete analyst (0-2; default: 2).",
    )
    args = parser.parse_args()

    summary_path = run_batch_folder(
        input_dir=args.input_dir,
        output_dir=args.output_dir,
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
    print(summary_path)


if __name__ == "__main__":  # pragma: no cover - CLI helper
    _main()


__all__ = [
    "extract_ground_truth_rucam_score",
    "extract_section_c_json",
    "run_batch_folder",
]
