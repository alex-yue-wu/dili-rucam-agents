from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError
import pytest

from dili_rucam_agents.roster import (
    DEFAULT_ROSTER_PATH,
    AnalystConfig,
    Provider,
    RosterError,
    load_roster,
)

CONFIG_DIR = DEFAULT_ROSTER_PATH.parent
STUDY = CONFIG_DIR / "analysts.study.json"
SMOKE = CONFIG_DIR / "analysts.smoke.json"

GROUND_TRUTH = {"provider": "openai", "model": "gpt-5.5"}


def entry(key: str = "analyst_alpha", **overrides) -> dict:
    base = {
        "key": key,
        "label": "Analyst Alpha",
        "provider": "openai",
        "model": "gpt-5.5",
    }
    base.update(overrides)
    return base


def write(tmp_path: Path, analysts: list[dict], name: str = "roster.json") -> Path:
    path = tmp_path / name
    path.write_text(
        json.dumps({"analysts": analysts, "ground_truth": GROUND_TRUTH}),
        encoding="utf-8",
    )
    return path


def test_single_analyst_roster_loads(tmp_path):
    roster = load_roster(write(tmp_path, [entry()]))
    assert len(roster.analysts) == 1
    assert roster.analysts[0].key == "analyst_alpha"
    assert roster.analysts[0].max_output_tokens == 32000
    assert roster.ground_truth.max_output_tokens == 8000


def test_report_filename_replaces_underscores_with_hyphens():
    config = AnalystConfig(
        key="analyst_alpha",
        label="Analyst Alpha",
        provider="openai",
        model="gpt-5.5",
    )
    assert config.report_filename == "analyst-alpha_report.md"


def test_empty_analyst_list_is_rejected(tmp_path):
    with pytest.raises(RosterError):
        load_roster(write(tmp_path, []))


def test_duplicate_key_is_rejected(tmp_path):
    path = write(tmp_path, [entry(), entry(label="Other", model="gpt-5.4")])
    with pytest.raises(RosterError, match="analyst_alpha"):
        load_roster(path)


def test_unknown_field_is_rejected(tmp_path):
    with pytest.raises(RosterError):
        load_roster(write(tmp_path, [entry(temperature=0.5)]))
    with pytest.raises(ValidationError):
        AnalystConfig(**entry(), bogus=1)


def test_key_must_match_pattern(tmp_path):
    with pytest.raises(RosterError, match="Analyst-Alpha"):
        load_roster(write(tmp_path, [entry(key="Analyst-Alpha")]))


def test_missing_file_names_the_path():
    with pytest.raises(RosterError, match="nope.json"):
        load_roster("nope.json")


def test_a_file_that_is_not_utf_8_names_the_path(tmp_path):
    """UnicodeDecodeError is a ValueError, not an OSError, so it needs wrapping."""
    path = tmp_path / "latin1.json"
    path.write_bytes(b'{"analysts": [{"label": "Analyst \xe9"}]}')
    with pytest.raises(RosterError, match="latin1.json"):
        load_roster(path)


def test_malformed_json_names_the_path(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(RosterError, match="broken.json"):
        load_roster(path)


def test_shipped_default_roster_has_exactly_one_analyst():
    roster = load_roster()
    assert len(roster.analysts) == 1
    assert roster.analysts[0].key == "analyst_alpha"
    assert roster.ground_truth.provider is Provider.openai


def test_shipped_study_roster_reproduces_todays_seven_models():
    """provider is the one roster field with no predecessor to copy from.

    key, label, model and max_output_tokens all came across from the retired
    crew config; provider replaced routing by model-name sniffing, so a typo in
    it reaches a paid run with nothing to catch it on the way.
    """
    roster = load_roster(STUDY)
    assert [(a.key, a.provider, a.model) for a in roster.analysts] == [
        ("analyst_alpha", Provider.openai, "gpt-5.5"),
        ("analyst_beta", Provider.google, "gemini-3.1-pro-preview"),
        ("analyst_gamma", Provider.openrouter, "moonshotai/kimi-k2.5"),
        ("analyst_delta", Provider.deepseek, "deepseek-v4-pro"),
        ("analyst_epsilon", Provider.openrouter, "qwen/qwen3.5-plus-02-15"),
        ("analyst_zeta", Provider.anthropic, "claude-opus-4-7"),
        ("analyst_eta", Provider.openrouter, "z-ai/glm-5"),
    ]
    assert (roster.ground_truth.provider, roster.ground_truth.model) == (
        Provider.openai,
        "gpt-5.5",
    )


def test_shipped_smoke_roster_covers_every_provider():
    roster = load_roster(SMOKE)
    assert {a.provider for a in roster.analysts} == set(Provider)
