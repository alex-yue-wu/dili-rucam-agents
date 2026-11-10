"""The instructions one analyst is sent, as a hashable, renderable object."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from textwrap import dedent
from typing import Optional

from pydantic import BaseModel, ConfigDict

DEFAULT_RUCAM_INFERRING_PROMPT_PATH = (
    Path(__file__).resolve().parent
    / "prompts"
    / "rucam_analysis_production_inferring.md"
)
DEFAULT_RUCAM_STRICT_PROMPT_PATH = (
    Path(__file__).resolve().parent
    / "prompts"
    / "rucam_analysis_production_strict.md"
)

ANALYST_INSTRUCTION_SCHEMA_VERSION = 1
RETRY_REQUIREMENT_PLACEHOLDER = "retry_requirement"
_RETRY_DIAGNOSTIC_PLACEHOLDER = "retry_diagnostic"


class _Strict(dict):
    """A format mapping that raises, rather than leaving a placeholder, if a name is absent."""

    def __missing__(self, key: str):
        raise KeyError(f"instruction placeholder {key!r} was not supplied")


def _literal(text: str) -> str:
    """Escape text so ``str.format`` renders it unchanged (the prompt carries JSON and LaTeX braces)."""
    return text.replace("{", "{{").replace("}", "}}")


class AnalystInstructionContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int
    analyst_label: str
    agent_role: str
    agent_goal: str
    agent_backstory: str
    instruction_template: str
    retry_instruction_template: str
    expected_output: str

    @property
    def sha256(self) -> str:
        # Every field, sorted keys, fixed separators: independent of dict order.
        encoded = json.dumps(
            self.model_dump(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()

    def render(self, **values: str) -> str:
        """Fill the instruction template; raises KeyError on any unfilled placeholder."""
        return self.instruction_template.format_map(_Strict(values))

    def render_retry(self, diagnostic: str) -> str:
        """The retry block for a rejected attempt; pass it as ``retry_requirement`` to ``render``."""
        return self.retry_instruction_template.format_map(
            _Strict({_RETRY_DIAGNOSTIC_PLACEHOLDER: diagnostic})
        )


def build_analyst_instruction_contract(
    *,
    analyst_label: str,
    prompt_text: str,
    model_name: str,
    bundle_input_name: str,
) -> AnalystInstructionContract:
    bundle_placeholder = "{" + bundle_input_name + "}"
    retry_placeholder = "{" + RETRY_REQUIREMENT_PLACEHOLDER + "}"
    label = _literal(analyst_label)
    instruction_template = dedent(
        f"""
        You are the {label} RUCAM Analyst. Consume the shared case_bundle_json exactly as provided below.
        Follow every instruction in the provided RUCAM production prompt without deviation.
        Never quote, restate, compare against, or discuss any author-reported, published, or previously assigned
        RUCAM score or category from the source document. Treat any such prior outcomes as withheld and exclude
        them from your narrative, table, and JSON output.
        Return exactly the single JSON object the production prompt specifies, and nothing else.

        --- BEGIN CASE BUNDLE JSON ---
        {bundle_placeholder}
        --- END CASE BUNDLE JSON ---

        --- BEGIN PRODUCTION PROMPT ---
        __PROMPT__
        --- END PRODUCTION PROMPT ---
        {retry_placeholder}

        Use the configured model "{_literal(model_name)}". Temperature must remain 0 when supported;
        otherwise use the provider-required default temperature.
        """
    ).strip()
    # Substituted after dedent so the prompt's own indentation is untouched.
    instruction_template = instruction_template.replace("__PROMPT__", _literal(prompt_text))
    return AnalystInstructionContract(
        schema_version=ANALYST_INSTRUCTION_SCHEMA_VERSION,
        analyst_label=analyst_label,
        agent_role=f"{analyst_label} Expert DILI RUCAM Analyst",
        agent_goal="Apply the production RUCAM prompt verbatim to case_bundle_json inputs.",
        agent_backstory=(
            f"{analyst_label} is a board-certified hepatologist and pharmacovigilance researcher. "
            "Always compute R-ratio, determine injury pattern, score all seven RUCAM items, "
            "and output the single JSON object exactly as specified."
        ),
        instruction_template=instruction_template,
        retry_instruction_template=dedent(
            """

            --- RETRY REQUIREMENT ---
            The previous attempt was rejected: {retry_diagnostic}
            Return a fresh, complete assessment as the single JSON object the production prompt specifies,
            and nothing else.
            --- END RETRY REQUIREMENT ---
            """
        ).rstrip(),
        expected_output=(
            f"A complete {analyst_label} RUCAM assessment as one JSON object."
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


__all__ = [
    "ANALYST_INSTRUCTION_SCHEMA_VERSION",
    "AnalystInstructionContract",
    "DEFAULT_RUCAM_INFERRING_PROMPT_PATH",
    "DEFAULT_RUCAM_STRICT_PROMPT_PATH",
    "RETRY_REQUIREMENT_PLACEHOLDER",
    "build_analyst_instruction_contract",
    "load_rucam_prompt",
]
