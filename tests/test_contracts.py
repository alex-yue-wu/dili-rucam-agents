from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

import dili_rucam_agents.contracts as contracts_module
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
    alpha, beta = _build("Analyst Alpha"), _build("Analyst Beta")
    assert alpha.sha256 != beta.sha256
    # The persona must diverge on its own, not only through the label in the template.
    assert alpha.agent_backstory != beta.agent_backstory
    assert alpha.agent_role != beta.agent_role


def test_persona_alone_moves_the_hash():
    base = _build()
    for field in ("agent_backstory", "agent_role"):
        assert base.model_copy(update={field: "other"}).sha256 != base.sha256


def test_sha256_is_stable_across_builds_with_the_same_inputs():
    assert _build().sha256 == _build().sha256
    code = (
        "from dili_rucam_agents.contracts import build_analyst_instruction_contract as b;"
        "print(b(analyst_label='Analyst Alpha', prompt_text='R = \\\\frac{ALT}{ULN} and {\"score\": 0}',"
        "model_name='model-x', bundle_input_name='case_bundle_json').sha256)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        # Import the package under test, not whichever copy the venv resolves.
        env={**os.environ, "PYTHONPATH": str(Path(contracts_module.__file__).parents[1])},
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


@pytest.mark.parametrize("strict", [True, False])
def test_render_keeps_the_real_prompts_braces_literal(strict):
    prompt = load_rucam_prompt(strict_scoring=strict)
    contract = _build(prompt_text=prompt)
    text = contract.render(case_bundle_json='{"bundle": 1}', retry_requirement="")
    assert '{"bundle": 1}' in text
    for probe in ('"injury_pattern"', '"r_ratio_basis"'):
        assert probe in prompt  # the probe is real
        assert probe in text
    assert prompt in text


@pytest.mark.parametrize("strict", [True, False])
def test_rendered_instructions_say_json_for_deepseek(strict):
    """DeepSeek's json_object mode refuses a prompt that never says "json".

    Verified live on 2026-11-11: the same call that succeeds with "Return a
    single JSON object" returns `400 - Prompt must contain the word 'json' in
    some form to use 'response_format' of type 'json_object'` without it. 400 is
    in TERMINAL_STATUS_CODES, so there is no retry and no mode downgrade behind
    it -- an edit that took the word out of the prompt would kill every DeepSeek
    analyst outright, and `json_object` is the highest mode DeepSeek supports,
    so it is the mode every DeepSeek run starts in.
    """
    contract = _build(prompt_text=load_rucam_prompt(strict_scoring=strict))
    text = contract.render(case_bundle_json='{"bundle": 1}', retry_requirement="")
    assert "json" in text.lower()
