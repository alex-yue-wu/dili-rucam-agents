"""The normative docs must describe the declared-roster analyst tier.

Each test pairs the presence of the new contract with the absence of the one it
replaced, so a doc that gains the new wording but keeps the old still fails.
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest

from dili_rucam_agents.checkpoints import CHECKPOINT_SCHEMA_VERSION

ROOT = Path(__file__).resolve().parents[1]
AGENTS = (ROOT / "AGENTS.md").read_text()
README = (ROOT / "README.md").read_text()
BOTH = {"AGENTS.md": AGENTS, "README.md": README}

REMOVED_ENV = re.compile(
    r"ANALYST_(?:ALPHA|BETA|GAMMA|DELTA|EPSILON|ZETA|ETA)_(?:MODEL|MAX_TOKENS)"
    r"|\bANALYST_MODEL\b|\bANALYST_MAX_TOKENS\b|\bLLM_MAX_TOKENS\b"
)
REMOVED_FLAG = re.compile(r"--analyst-(?:delta|epsilon|zeta|eta)\b|use_analyst_")


@pytest.mark.parametrize("name", BOTH)
def test_removed_env_vars_and_flags_are_gone(name):
    text = BOTH[name]
    assert not REMOVED_ENV.findall(text)
    assert not REMOVED_FLAG.findall(text)
    assert "CrewAI" not in text and "LiteLLM" not in text
    assert "3 default analysts" not in text
    assert "optional Analyst Delta" not in text


def test_agents_md_describes_the_roster():
    for needle in (
        "config/analysts.json",
        "config/analysts.study.json",
        "config/analysts.smoke.json",
        "--analysts",
        "one analyst",
    ):
        assert needle in AGENTS, needle
    for provider in ("openai", "anthropic", "google", "openrouter", "deepseek"):
        assert f"`{provider}`" in AGENTS, provider


def test_definition_of_done_is_in_terms_of_the_roster():
    done = AGENTS.split("## 9. Definition Of Done", 1)[1]
    assert "declared roster" in done
    assert "default analysts complete" not in done
    assert "optional analysts complete" not in done


def test_agents_md_states_the_output_contract():
    assert "renders Sections A, B and C" in AGENTS
    assert "total_score" in AGENTS and "category" in AGENTS
    assert "_assessment.json" in AGENTS and "_report.md" in AGENTS
    assert f"`CHECKPOINT_SCHEMA_VERSION` is {CHECKPOINT_SCHEMA_VERSION}" in AGENTS
    for ladder in ("json_schema", "json_object", "text"):
        assert ladder in AGENTS
    for counter in (
        "validation_retries",
        "transient_retries",
        "truncation_escalations",
        "mode_downgrades",
    ):
        assert counter in AGENTS and counter in README


def test_schema_version_is_not_documented_stale():
    for name, text in BOTH.items():
        assert "schema v3" not in text and "clean v3" not in text, name
    assert f"schema v{CHECKPOINT_SCHEMA_VERSION}" in README


def test_readme_shows_the_analysts_flag():
    assert "--analysts config/analysts.study.json" in README
    assert "config/analysts.json" in README
    assert ".env" in README


def test_readme_carries_the_two_stage_acceptance_gate():
    assert "--analysts config/analysts.smoke.json" in README
    assert "--analysts config/analysts.study.json" in README
    assert "--reproducibility" in README and "--repeats 5" in README
    assert "70 analyst runs" in README
    assert "fewer than 5" in README
    assert "truncation_escalations" in README
    assert "32000" in README


def test_readme_documents_the_ground_truth_baseline_shift():
    assert "claude-opus-4-7" in README
    assert "anthropic/claude-opus-4-7" in README
    assert "masked_rucam_score" in README


def test_readme_test_command_names_only_existing_files():
    section = README.split("## Tests", 1)[1]
    for path in re.findall(r"tests/test_\w+\.py", section):
        assert (ROOT / path).exists(), path


@pytest.mark.parametrize("name", BOTH)
def test_api_key_check_is_documented_as_upfront_and_exhaustive(name):
    text = " ".join(BOTH[name].split())
    assert "Before ingestion or any model call" in text
    assert "names every missing variable at once" in text
    assert (
        "(the roster's analysts, plus the ground-truth provider when masking is on)"
        in text
    )
    lowered = text.lower()
    for lazy in (
        "first call",
        "first use",
        "on first",
        "lazy",
        "lazily",
        "on demand",
        "when first needed",
    ):
        assert lazy not in lowered, lazy


def test_instruction_hash_is_documented_as_covering_the_persona():
    for field in ("agent_role", "agent_goal", "agent_backstory"):
        assert field in README
