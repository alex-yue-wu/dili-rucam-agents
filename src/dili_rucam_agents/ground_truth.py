from __future__ import annotations

import asyncio
import re
from pathlib import Path
from textwrap import dedent
from typing import Optional

from dili_rucam_agents.providers import (
    CompletionProvider,
    StructuredMode,
    model_spec,
    require_api_keys,
)
from dili_rucam_agents.roster import GroundTruthConfig


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


def build_ground_truth_score_finder_prompt(
    *, raw_case_bundle_json: str, model_name: str
) -> str:
    prompt_text = load_ground_truth_prompt()
    return dedent(
        f"""
        You are the Ground Truth RUCAM Score Finder. Consume the shared raw_case_bundle_json exactly as provided below.
        Find the author-reported RUCAM outcome in the source report, not a recomputed score.

        --- BEGIN RAW CASE BUNDLE JSON ---
        {raw_case_bundle_json}
        --- END RAW CASE BUNDLE JSON ---

        --- BEGIN GROUND TRUTH PROMPT ---
        {prompt_text}
        --- END GROUND TRUTH PROMPT ---

        Use the configured model "{model_name}". Temperature must remain 0 when supported;
        otherwise use the provider-required default temperature.
        Return only the markdown report requested by the prompt.
        """
    ).strip()


def run_ground_truth_score_finder(
    *,
    raw_case_bundle_json: str,
    config: GroundTruthConfig,
    provider: CompletionProvider,
) -> str:
    """Read the RUCAM outcome the source paper published, in one free-text call.

    Deliberately a single call with no retry budget, as the CrewAI path it
    replaces was: the reply is regex-read free text, so there is no schema to
    retry toward, and this feeds a reporting column, not a scored analyst. A
    transient failure surfaces to the caller.
    """
    require_api_keys([config.provider])
    prompt = build_ground_truth_score_finder_prompt(
        raw_case_bundle_json=raw_case_bundle_json, model_name=config.model
    )

    async def call_then_close():
        # The provider's client is built inside this loop and must be closed
        # inside it, so the one call and the close share the one asyncio.run.
        try:
            return await provider.complete(
                model_spec(config),
                [{"role": "user", "content": prompt}],
                StructuredMode.text,
                None,
            )
        finally:
            await provider.aclose()

    return asyncio.run(call_then_close()).text


__all__ = [
    "build_ground_truth_score_finder_prompt",
    "run_ground_truth_score_finder",
    "DEFAULT_GROUND_TRUTH_PROMPT_PATH",
    "extract_ground_truth_rucam_category",
    "extract_ground_truth_rucam_score",
    "load_ground_truth_prompt",
]
