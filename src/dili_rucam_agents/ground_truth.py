from __future__ import annotations

import re
from pathlib import Path
from typing import Optional


DEFAULT_GROUND_TRUTH_PROMPT_PATH = (
    Path(__file__).resolve().parent / "prompts" / "ground_truth_rucam_finder.md"
)

_GROUND_TRUTH_SCORE_RE = re.compile(r"GROUND_TRUTH_RUCAM_SCORE:\s*(.+)")
_GROUND_TRUTH_CATEGORY_RE = re.compile(r"GROUND_TRUTH_RUCAM_CATEGORY:\s*(.+)")


def load_ground_truth_prompt(prompt_path: Optional[Path] = None) -> str:
    path = prompt_path or DEFAULT_GROUND_TRUTH_PROMPT_PATH
    return path.read_text(encoding="utf-8")


def extract_ground_truth_rucam_score(report_text: str) -> int | None:
    match = _GROUND_TRUTH_SCORE_RE.search(report_text)
    if not match:
        return None
    value = match.group(1).strip()
    if not value or value == "None":
        return None
    return int(value)


def extract_ground_truth_rucam_category(report_text: str) -> str:
    match = _GROUND_TRUTH_CATEGORY_RE.search(report_text)
    if not match:
        return "None"
    value = match.group(1).strip()
    return value or "None"


__all__ = [
    "DEFAULT_GROUND_TRUTH_PROMPT_PATH",
    "extract_ground_truth_rucam_category",
    "extract_ground_truth_rucam_score",
    "load_ground_truth_prompt",
]
