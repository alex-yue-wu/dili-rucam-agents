from __future__ import annotations

import subprocess
import sys

import pytest
from pydantic import ValidationError

from dili_rucam_agents.contracts import (
    AnalystInstructionContract,
    build_analyst_instruction_contract,
    load_rucam_prompt,
)

# Literal braces as the production prompt carries them (JSON examples, LaTeX).
PROMPT = 'R = \\frac{ALT}{ULN} and {"score": 0}'


def _build(label: str = "Analyst Alpha", **overrides):
    kwargs = dict(
        analyst_label=label,
        prompt_text=PROMPT,
        model_name="model-x",
        bundle_input_name="case_bundle_json",
    )
    kwargs.update(overrides)
    return build_analyst_instruction_contract(**kwargs)


def test_contract_keeps_the_per_analyst_persona():
    contract = _build()
    assert "Analyst Alpha" in contract.agent_backstory
    assert "board-certified hepatologist" in contract.agent_backstory
    assert contract.agent_role == "Analyst Alpha Expert DILI RUCAM Analyst"
    assert contract.instruction_template.startswith("You are the Analyst Alpha RUCAM Analyst.")


def test_two_analysts_have_different_sha256():
    assert _build("Analyst Alpha").sha256 != _build("Analyst Beta").sha256


def test_sha256_is_stable_across_builds_with_the_same_inputs():
    assert _build().sha256 == _build().sha256
    code = (
        "from dili_rucam_agents.contracts import build_analyst_instruction_contract as b;"
        "print(b(analyst_label='Analyst Alpha', prompt_text='R = \\\\frac{ALT}{ULN} and {\"score\": 0}',"
        "model_name='model-x', bundle_input_name='case_bundle_json').sha256)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == _build().sha256


def test_sha256_changes_when_any_field_changes():
    base = _build()
    assert base.model_copy(update={"agent_goal": "other"}).sha256 != base.sha256
    assert base.model_copy(update={"expected_output": "other"}).sha256 != base.sha256


def test_render_raises_on_an_unfilled_placeholder():
    contract = _build()
    with pytest.raises(KeyError, match="case_bundle_json"):
        contract.render(retry_requirement="")
    with pytest.raises(KeyError, match="retry_requirement"):
        contract.render(case_bundle_json="{}")


def test_render_inserts_the_case_bundle_under_its_declared_name():
    contract = _build(bundle_input_name="masked_bundle")
    text = contract.render(masked_bundle='{"a": {"b": 1}}', retry_requirement="")
    assert '{"a": {"b": 1}}' in text
    with pytest.raises(KeyError, match="masked_bundle"):
        contract.render(case_bundle_json="{}", retry_requirement="")


def test_render_leaves_prompt_braces_literal():
    text = _build().render(case_bundle_json="{}", retry_requirement="")
    assert PROMPT in text


def test_render_retry_inserts_the_diagnostic():
    contract = _build()
    block = contract.render_retry("bad {json}")
    assert "bad {json}" in block
    assert "RETRY REQUIREMENT" in block


def test_contract_is_frozen_and_forbids_extra_fields():
    contract = _build()
    with pytest.raises(ValidationError):
        contract.agent_goal = "x"
    with pytest.raises(ValidationError):
        AnalystInstructionContract(**contract.model_dump(), surprise="x")


def test_load_rucam_prompt_reads_strict_and_inferring_files():
    assert load_rucam_prompt(strict_scoring=True) != load_rucam_prompt()
