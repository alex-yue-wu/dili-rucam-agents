from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional

from dili_rucam_agents.crew.crew import run_crew


def run_end_to_end(pdf_path: str, prompt_path: Optional[str] = None, output_dir: Optional[str] = None) -> str:
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
        help="Directory to store markdown reports for GPT-5.2, Gemini 3.0, and the arbiter.",
    )
    args = parser.parse_args()
    print(run_end_to_end(args.pdf_path, args.prompt_path, args.output_dir))


def _persist_reports(reports: dict[str, Optional[str]], output_dir: Path) -> None:
    filename_map = {
        "gpt_52": "gpt-5.2_report.md",
        "gemini_30": "gemini-3.0_report.md",
        "arbiter": "arbiter_report.md",
    }

    for key, filename in filename_map.items():
        content = reports.get(key)
        if not content:
            continue
        (output_dir / filename).write_text(content, encoding="utf-8")


if __name__ == "__main__":  # pragma: no cover - CLI helper
    _main()
