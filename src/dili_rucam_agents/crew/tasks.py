from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
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

ANALYST_INSTRUCTION_SCHEMA_VERSION = 1
_RETRY_REQUIREMENT_SLOT = "[[ANALYST_RETRY_REQUIREMENT]]"
_RETRY_DIAGNOSTIC_SLOT = "[[SANITIZED_RETRY_DIAGNOSTIC]]"


@dataclass(frozen=True)
class AnalystInstructionContract:
    schema_version: int
    agent_role: str
    agent_goal: str
    agent_backstory: str
    task_name: str
    task_description_template: str
    retry_instruction_template: str
    task_expected_output: str

    @property
    def sha256(self) -> str:
        encoded = json.dumps(
            asdict(self), sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    def render_task_description(self, *, retry_instruction: str | None) -> str:
        retry_block = ""
        if retry_instruction:
            retry_block = self.retry_instruction_template.replace(
                _RETRY_DIAGNOSTIC_SLOT, retry_instruction
            )
        return self.task_description_template.replace(
            _RETRY_REQUIREMENT_SLOT, retry_block
        )


def build_analyst_instruction_contract(
    *,
    analyst_label: str,
    prompt_text: str,
    model_name: str,
    bundle_input_name: str,
) -> AnalystInstructionContract:
    bundle_placeholder = "{" + bundle_input_name + "}"
    task_description_template = dedent(
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
        {_RETRY_REQUIREMENT_SLOT}

        Use the configured model "{model_name}". Temperature must remain 0 when supported;
        otherwise use the provider-required default temperature.
        """
    ).strip()
    return AnalystInstructionContract(
        schema_version=ANALYST_INSTRUCTION_SCHEMA_VERSION,
        agent_role=f"{analyst_label} Expert DILI RUCAM Analyst",
        agent_goal="Apply the production RUCAM prompt verbatim to case_bundle_json inputs.",
        agent_backstory=(
            f"{analyst_label} is a board-certified hepatologist and pharmacovigilance researcher. "
            "Always compute R-ratio, determine injury pattern, score all seven RUCAM items, "
            "and output Sections A/B/C exactly as specified."
        ),
        task_name=f"{analyst_label.lower().replace(' ', '_')}_analysis",
        task_description_template=task_description_template,
        retry_instruction_template=dedent(
            f"""

            --- RETRY REQUIREMENT ---
            The previous attempt was rejected: {_RETRY_DIAGNOSTIC_SLOT}
            Return a fresh, complete report with non-empty SECTION A and SECTION B,
            followed by strict fenced SECTION C JSON.
            --- END RETRY REQUIREMENT ---
            """
        ).rstrip(),
        task_expected_output=(
            f"A complete {analyst_label} report containing SECTION A narrative, SECTION B RUCAM table, "
            "and fenced SECTION C JSON."
        ),
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
    retry_instruction: str | None = None,
    instruction_contract: AnalystInstructionContract | None = None,
) -> Task:
    contract = instruction_contract or build_analyst_instruction_contract(
        analyst_label=analyst_label,
        prompt_text=prompt_text,
        model_name=model_name,
        bundle_input_name=bundle_input_name,
    )
    description = contract.render_task_description(retry_instruction=retry_instruction)

    task_kwargs = {}
    if case_bundle_task is not None:
        task_kwargs["context"] = [case_bundle_task]

    return Task(
        name=contract.task_name,
        description=description,
        expected_output=contract.task_expected_output,
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
    "ANALYST_INSTRUCTION_SCHEMA_VERSION",
    "AnalystInstructionContract",
    "DEFAULT_RUCAM_INFERRING_PROMPT_PATH",
    "DEFAULT_RUCAM_STRICT_PROMPT_PATH",
    "load_rucam_prompt",
    "build_analyst_instruction_contract",
    "build_ground_truth_score_finder_prompt",
    "create_case_bundle_task",
    "create_analysis_task",
    "create_masking_task",
]
