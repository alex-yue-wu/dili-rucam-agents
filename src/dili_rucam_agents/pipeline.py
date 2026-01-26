from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional

from dili_rucam_agents.crew.crew import run_crew


def run_end_to_end(pdf_path: str, prompt_path: Optional[str] = None) -> str:
    """Public helper used by scripts/tests to run the full pipeline."""

    resolved_pdf = str(Path(pdf_path).expanduser().resolve())
    resolved_prompt = Path(prompt_path).expanduser().resolve() if prompt_path else None
    return run_crew(resolved_pdf, resolved_prompt)


def _main() -> None:
    parser = argparse.ArgumentParser(description="Execute the full RUCAM crew on a PDF.")
    parser.add_argument("pdf_path", help="Path to the clinical case report PDF.")
    parser.add_argument(
        "--prompt-path",
        dest="prompt_path",
        help="Optional override for the production prompt file.",
    )
    args = parser.parse_args()
    print(run_end_to_end(args.pdf_path, args.prompt_path))


if __name__ == "__main__":  # pragma: no cover - CLI helper
    _main()
