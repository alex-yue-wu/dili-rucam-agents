from __future__ import annotations

import argparse
import contextlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from openpyxl import Workbook

from dili_rucam_agents.crew.agents import resolve_rucam_model
from dili_rucam_agents.crew.config import get_enabled_analyst_configs
from dili_rucam_agents.crew.crew import AnalystExecutionError, validate_max_restarts
from dili_rucam_agents.ground_truth import (
    extract_ground_truth_rucam_category,
    extract_ground_truth_rucam_score,
)
from dili_rucam_agents.pipeline import is_end_to_end_complete, run_end_to_end
from dili_rucam_agents.validators.analyst_report import (
    parse_section_c_payload,
    validate_analyst_report,
)


_RUN_STATUS_FILENAME = "run_status.json"


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

    enabled_analyst_configs = get_enabled_analyst_configs(
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
        pdf_output_dir.mkdir(parents=True, exist_ok=True)
        row = _initialize_summary_row(pdf_path, pdf_output_dir, enabled_analyst_configs)
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
        ):
            print(f"Skipping completed PDF: {pdf_path.name}")
            _populate_row_from_reports(
                row, pdf_output_dir, enabled_analyst_configs, enable_score_masking
            )
            summary_rows.append(row)
            continue

        stdout_cm: contextlib.AbstractContextManager[object]
        stderr_cm: contextlib.AbstractContextManager[object]
        log_file = None
        if debug:
            log_path = pdf_output_dir / f"{pdf_path.stem}.log"
            log_file = log_path.open("w", encoding="utf-8")
            stdout_cm = contextlib.redirect_stdout(log_file)
            stderr_cm = contextlib.redirect_stderr(log_file)
        else:
            stdout_cm = contextlib.nullcontext()
            stderr_cm = contextlib.nullcontext()

        with contextlib.ExitStack() as stack:
            if log_file is not None:
                stack.callback(log_file.close)
            stack.enter_context(stdout_cm)
            stack.enter_context(stderr_cm)
            try:
                _write_pdf_run_status(
                    pdf_output_dir=pdf_output_dir,
                    payload={
                        "pdf_filename": pdf_path.name,
                        "status": "running",
                        "masking_enabled": enable_score_masking,
                        "strict_scoring": strict_scoring,
                        "enabled_analysts": [
                            config["key"] for config in enabled_analyst_configs
                        ],
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
                    row, pdf_output_dir, enabled_analyst_configs, enable_score_masking
                )
                _write_pdf_run_status(
                    pdf_output_dir=pdf_output_dir,
                    payload={
                        "pdf_filename": pdf_path.name,
                        "status": "completed",
                        "masking_enabled": enable_score_masking,
                        "strict_scoring": strict_scoring,
                        "enabled_analysts": [
                            config["key"] for config in enabled_analyst_configs
                        ],
                        "completed_at": _utc_now_isoformat(),
                    },
                )
                if debug:
                    print("Completed successfully.")
            except AnalystExecutionError as exc:  # pragma: no cover - real batch runs
                _stop_batch_after_failure(
                    exc=exc,
                    pdf_path=pdf_path,
                    pdf_output_dir=pdf_output_dir,
                    enable_score_masking=enable_score_masking,
                    strict_scoring=strict_scoring,
                    enabled_analyst_configs=enabled_analyst_configs,
                    row=row,
                    summary_rows=summary_rows,
                    results_dir=results_dir,
                    debug=debug,
                    analyst_failure={
                        "failed_analyst": exc.analyst_key,
                        "attempts": exc.attempts,
                        "failure_kind": exc.failure_kind,
                    },
                )
            except Exception as exc:  # pragma: no cover - exercised in real batch runs
                _stop_batch_after_failure(
                    exc=exc,
                    pdf_path=pdf_path,
                    pdf_output_dir=pdf_output_dir,
                    enable_score_masking=enable_score_masking,
                    strict_scoring=strict_scoring,
                    enabled_analyst_configs=enabled_analyst_configs,
                    row=row,
                    summary_rows=summary_rows,
                    results_dir=results_dir,
                    debug=debug,
                )

        summary_rows.append(row)

    summary_path = results_dir / "batch_summary.xlsx"
    _write_summary_workbook(summary_rows, enabled_analyst_configs, summary_path)
    print(f"\n===================== Batch analysis completed =====================\n")
    return summary_path


def _initialize_summary_row(
    pdf_path: Path,
    pdf_output_dir: Path,
    enabled_analyst_configs: list[dict[str, Any]],
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "pdf_filename": pdf_path.name,
        "masked_rucam_score": None,
        "masked_rucam_category": "None",
    }
    for config in enabled_analyst_configs:
        model_name = resolve_rucam_model(
            model_env=config["model_env"],
            fallback_envs=config["fallback_envs"],
            default_model=config["default_model"],
        )
        config["resolved_model_name"] = model_name
        row[model_name] = None
    return row


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
    if status.get("enabled_analysts") != [
        config["key"] for config in enabled_analyst_configs
    ]:
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


def _stop_batch_after_failure(
    *,
    exc: Exception,
    pdf_path: Path,
    pdf_output_dir: Path,
    enable_score_masking: bool,
    strict_scoring: bool,
    enabled_analyst_configs: list[dict[str, Any]],
    row: dict[str, Any],
    summary_rows: list[dict[str, Any]],
    results_dir: Path,
    debug: bool,
    analyst_failure: dict[str, Any] | None = None,
) -> None:
    if debug:
        print(f"Run failed: {exc}")
    row["masked_rucam_score"] = f"ERROR: {exc}"
    row["masked_rucam_category"] = f"ERROR: {exc}"
    _write_pdf_run_status(
        pdf_output_dir=pdf_output_dir,
        payload={
            "pdf_filename": pdf_path.name,
            "status": "failed",
            "masking_enabled": enable_score_masking,
            "strict_scoring": strict_scoring,
            "enabled_analysts": [
                config["key"] for config in enabled_analyst_configs
            ],
            "failed_at": _utc_now_isoformat(),
            "error": str(exc),
            **(analyst_failure or {}),
        },
    )
    summary_rows.append(row)
    _write_summary_workbook(
        summary_rows, enabled_analyst_configs, results_dir / "batch_summary.xlsx"
    )
    raise RuntimeError(f"Batch stopped at {pdf_path.name}: {exc}") from exc


def _utc_now_isoformat() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _populate_row_from_reports(
    row: dict[str, Any],
    pdf_output_dir: Path,
    enabled_analyst_configs: list[dict[str, Any]],
    enable_score_masking: bool,
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
        except ValueError as exc:
            raise ValueError(f"{report_path.name}: {exc}") from exc
        row[config["resolved_model_name"]] = validated.payload.total_score


def extract_section_c_json(report_text: str) -> dict[str, Any]:
    return parse_section_c_payload(report_text, allow_legacy_json=True)


def _write_summary_workbook(
    rows: list[dict[str, Any]],
    enabled_analyst_configs: list[dict[str, Any]],
    output_path: Path,
) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Batch Summary"

    headers = ["pdf_filename"]
    for config in enabled_analyst_configs:
        headers.append(config["resolved_model_name"])
    headers.append("masked_rucam_score")
    headers.append("masked_rucam_category")

    sheet.append(headers)
    for row in rows:
        sheet.append([row.get(header, "") for header in headers])

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
