from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from typing import Optional

from crewai import Agent, Task

from dili_rucam_agents.ground_truth import load_ground_truth_prompt

DEFAULT_RUCAM_INFERRING_PROMPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "prompts"
    / "rucam_analysis_production_inferring.md"
)
DEFAULT_RUCAM_STRICT_PROMPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "prompts"
    / "rucam_analysis_production_strict.md"
)


def load_rucam_prompt(
    prompt_path: Optional[Path] = None, *, strict_scoring: bool = False
) -> str:
    path = prompt_path or (
        DEFAULT_RUCAM_STRICT_PROMPT_PATH
        if strict_scoring
        else DEFAULT_RUCAM_INFERRING_PROMPT_PATH
    )
    return path.read_text(encoding="utf-8")


def create_case_bundle_task(*, pdf_path: str, agent: Agent) -> Task:
    return Task(
        name="case_bundle_generation",
        description=dedent(
            f"""
            Deterministically ingest the clinical PDF located at "{pdf_path}".
            Use the case_bundle_extractor tool to produce the canonical case_bundle_json contract defined in agent.md.
            Preserve every page, avoid hallucinations, and document extraction gaps.
            """
        ).strip(),
        expected_output=(
            "A valid case_bundle_json object containing pdf_path, extraction_notes, blocks, normalized_text, "
            "tables, unknowns, and quality as described in agent.md."
        ),
        agent=agent,
        inputs={"pdf_path": pdf_path},
    )


def create_analysis_task(
    *,
    agent: Agent,
    analyst_label: str,
    prompt_text: str,
    model_name: str,
    bundle_input_name: str,
    case_bundle_task: Task | None = None,
) -> Task:
    bundle_placeholder = "{" + bundle_input_name + "}"
    description = dedent(
        f"""
        You are the {analyst_label} RUCAM Analyst. Consume the shared case_bundle_json exactly as provided below.
        Follow every instruction in the provided RUCAM production prompt without deviation.
        Never quote, restate, compare against, or discuss any author-reported, published, or previously assigned
        RUCAM score or category from the source document. Treat any such prior outcomes as withheld and exclude
        them from your narrative, table, and JSON output.
        Return a complete SECTION A, SECTION B, and fenced SECTION C JSON.
        Do not stop after SECTION A or partway through SECTION B; if space is constrained, prioritize completing
        SECTION B and the fenced SECTION C JSON over adding detail to SECTION A.

        --- BEGIN CASE BUNDLE JSON ---
        {bundle_placeholder}
        --- END CASE BUNDLE JSON ---

        --- BEGIN PRODUCTION PROMPT ---
        {prompt_text}
        --- END PRODUCTION PROMPT ---

        Use the configured model "{model_name}". Temperature must remain 0 when supported;
        otherwise use the provider-required default temperature.
        """
    ).strip()

    task_kwargs = {}
    if case_bundle_task is not None:
        task_kwargs["context"] = [case_bundle_task]

    return Task(
        name=f"{analyst_label.lower().replace(' ', '_')}_analysis",
        description=description,
        expected_output=(
            f"A complete {analyst_label} report containing SECTION A narrative, SECTION B RUCAM table, "
            "and fenced SECTION C JSON."
        ),
        agent=agent,
        **task_kwargs,
    )


def create_masking_task(*, agent: Agent, case_bundle_task: Task) -> Task:
    description = dedent(
        f"""
        Consume the shared case_bundle_json and use the score_masker tool to redact prior RUCAM scores,
        RUCAM score tables, and explicit causality-category conclusions from the extracted text.
        Preserve the original schema and all non-RUCAM clinical evidence. Output only the masked case_bundle_json.
        """
    ).strip()

    return Task(
        name="score_masking",
        description=description,
        expected_output="A valid masked case_bundle_json object with prior RUCAM scoring references redacted.",
        context=[case_bundle_task],
        agent=agent,
    )


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


__all__ = [
    "DEFAULT_RUCAM_INFERRING_PROMPT_PATH",
    "DEFAULT_RUCAM_STRICT_PROMPT_PATH",
    "load_rucam_prompt",
    "build_ground_truth_score_finder_prompt",
    "create_case_bundle_task",
    "create_analysis_task",
    "create_masking_task",
]
