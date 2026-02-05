from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional

from dili_rucam_agents.crew.crew import run_crew


def run_end_to_end(
    pdf_path: str,
    prompt_path: Optional[str] = None,
    output_dir: Optional[str] = None,
    *,
    use_arbiter_beta: bool = False,
    use_arbiter_gamma: bool = False,
) -> str:
    """Public helper used by scripts/tests to run the full pipeline."""

    resolved_pdf = str(Path(pdf_path).expanduser().resolve())
    resolved_prompt = Path(prompt_path).expanduser().resolve() if prompt_path else None
    resolved_output_dir = Path(output_dir).expanduser().resolve() if output_dir else None

    if resolved_output_dir:
        resolved_output_dir.mkdir(parents=True, exist_ok=True)

    result = run_crew(
        resolved_pdf,
        resolved_prompt,
        capture_reports=resolved_output_dir is not None,
        use_arbiter_beta=use_arbiter_beta,
        use_arbiter_gamma=use_arbiter_gamma,
    )

    if isinstance(result, tuple):
        final_output, reports = result
    else:
        final_output, reports = result, {}

    if resolved_output_dir and isinstance(reports, dict):
        _persist_reports(reports, resolved_output_dir)

    return final_output


def _main() -> None:
    parser = argparse.ArgumentParser(description="Execute the full RUCAM crew on a PDF.")
    parser.add_argument("pdf_path", help="Path to the clinical case report PDF.")
    parser.add_argument(
        "--prompt-path",
        dest="prompt_path",
        help="Optional override for the production prompt file.",
    )
    parser.add_argument(
        "--output-dir",
        dest="output_dir",
        help="Directory to store markdown reports for GPT-5.2, Gemini 3.0, and enabled arbiters.",
    )
    parser.add_argument(
        "--arbiter-beta",
        dest="use_arbiter_beta",
        action="store_true",
        help="Enable the optional Arbiter Beta agent (ARBITER_BETA_MODEL).",
    )
    parser.add_argument(
        "--arbiter-gamma",
        dest="use_arbiter_gamma",
        action="store_true",
        help="Enable the optional Arbiter Gamma agent (ARBITER_GAMMA_MODEL).",
    )
    args = parser.parse_args()
    print(
        run_end_to_end(
            args.pdf_path,
            args.prompt_path,
            args.output_dir,
            use_arbiter_beta=args.use_arbiter_beta,
            use_arbiter_gamma=args.use_arbiter_gamma,
        )
    )


def _persist_reports(reports: dict[str, Optional[str]], output_dir: Path) -> None:
    filename_map = {
        "gpt_52": "gpt-5.2_report.md",
        "gemini_30": "gemini-3.0_report.md",
    }

    for key, content in reports.items():
        if not content:
            continue

        if key in filename_map:
            filename = filename_map[key]
        elif key.startswith("arbiter_"):
            filename = f"{key.replace('_', '-')}_report.md"
        else:
            continue

        (output_dir / filename).write_text(content, encoding="utf-8")


if __name__ == "__main__":  # pragma: no cover - CLI helper
    _main()
