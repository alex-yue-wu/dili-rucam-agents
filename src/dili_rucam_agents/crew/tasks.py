from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from typing import Optional

from crewai import Agent, Task


DEFAULT_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "rucam_analysis_production.md"


def load_rucam_prompt(prompt_path: Optional[Path] = None) -> str:
    path = prompt_path or DEFAULT_PROMPT_PATH
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
    case_bundle_task: Task,
    analyst_label: str,
    prompt_text: str,
    model_reference: str,
) -> Task:
    description = dedent(
        f"""
        You are the {analyst_label} RUCAM Analyst. Consume the shared case_bundle_json exactly as produced.
        Follow every instruction in rucam_analysis_production.md without deviation.

        --- BEGIN PRODUCTION PROMPT ---
        {prompt_text}
        --- END PRODUCTION PROMPT ---

        Reference your model via the {model_reference} environment variable. Temperature must remain 0.
        """
    ).strip()

    return Task(
        name=f"{analyst_label.lower().replace(' ', '_')}_analysis",
        description=description,
        expected_output=(
            f"A complete {analyst_label} report containing SECTION A narrative, SECTION B RUCAM table, "
            "and SECTION C JSON."
        ),
        context=[case_bundle_task],
        agent=agent,
    )


def create_arbiter_task(
    *,
    agent: Agent,
    gpt_task: Task,
    gemini_task: Task,
    arbiter_label: str,
) -> Task:
    description = dedent(
        """
        Review the GPT-5.2 and Gemini 3.0 reports. Identify disagreements in extracted facts, R-ratio,
        injury pattern, and each RUCAM item. Resolve conflicts strictly according to the case_bundle evidence.
        Produce a final report in the same SECTION A/B/C format plus SECTION D — Arbiter Justification explaining
        why you selected each final score whenever discrepancies existed.
        """
    ).strip()

    safe_label = arbiter_label.lower().replace(" ", "_")

    return Task(
        name=f"{safe_label}_senior_hepatology_arbitration",
        description=description,
        expected_output=(
            "Final SECTION A/B/C report with consolidated scores plus SECTION D — Arbiter Justification."
        ),
        context=[gpt_task, gemini_task],
        agent=agent,
    )


__all__ = [
    "DEFAULT_PROMPT_PATH",
    "load_rucam_prompt",
    "create_case_bundle_task",
    "create_analysis_task",
    "create_arbiter_task",
]
