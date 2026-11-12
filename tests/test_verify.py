"""The roster pre-flight: one call per entry, before a run that costs seventy.

Every test here is offline. ``FakeProvider`` implements only
``CompletionProvider``, so nothing in this file can reach a vendor.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dili_rucam_agents.providers import ModelResponse, StructuredMode, api_key_env
from dili_rucam_agents.roster import Provider, load_roster
from dili_rucam_agents.verify import (
    PROBE_PROMPT,
    VerifyResult,
    _main,
    verify_roster,
)
from fakes import FakeProvider, StatusError


@pytest.fixture(autouse=True)
def api_keys(monkeypatch):
    """Every provider has a key, so nothing fails the pre-flight by accident."""
    for provider in Provider:
        monkeypatch.setenv(api_key_env(provider), "test-key")



def write_roster(tmp_path: Path, analysts, ground_truth=None) -> Path:
    path = tmp_path / "analysts.json"
    path.write_text(
        json.dumps(
            {
                "analysts": analysts,
                "ground_truth": ground_truth
                or {"provider": "openai", "model": "gt-model"},
            }
        ),
        encoding="utf-8",
    )
    return path


def entry(key: str, provider: str, model: str, **extra) -> dict:
    return {
        "key": key,
        "label": key.replace("_", " ").title(),
        "provider": provider,
        "model": model,
        **extra,
    }


def test_every_entry_is_called_once_in_its_own_starting_mode(tmp_path):
    """The mode matters: deepseek has no json_schema rung to start on.

    A pre-flight that called every provider in json_schema would report a
    failure the real run would never hit, and vice versa.
    """
    roster = load_roster(
        write_roster(
            tmp_path,
            [
                entry("analyst_alpha", "openai", "gpt-5.5"),
                entry("analyst_delta", "deepseek", "deepseek-v4-pro"),
            ],
        )
    )
    provider = FakeProvider("{}", "{}", "{}")

    results = verify_roster(roster, provider)

    assert [r.ok for r in results] == [True, True, True]
    assert [r.key for r in results] == [
        "analyst_alpha",
        "analyst_delta",
        "ground_truth",
    ]
    assert provider.modes == [
        StructuredMode.json_schema,
        StructuredMode.json_object,
        StructuredMode.text,
    ]
    assert len(provider.calls) == 3


def test_the_probe_prompt_says_json(tmp_path):
    """DeepSeek's json_object mode 400s on a prompt that never says "json".

    Verified live on 2026-11-11, and 400 is terminal: a pre-flight whose own
    prompt omitted the word would report every deepseek entry as broken.
    """
    assert "json" in PROBE_PROMPT.lower()

    roster = load_roster(
        write_roster(tmp_path, [entry("analyst_delta", "deepseek", "v4")])
    )
    provider = FakeProvider("{}", "{}")

    verify_roster(roster, provider)

    assert "json" in provider.calls[0][1][0]["content"].lower()


def test_the_declared_api_version_reaches_the_spec(tmp_path):
    """Verifying an entry on a surface the run will not use proves nothing."""
    roster = load_roster(
        write_roster(
            tmp_path,
            [entry("analyst_beta", "google", "gemini-x", api_version="v1alpha")],
        )
    )
    provider = FakeProvider("{}", "{}")

    verify_roster(roster, provider)

    assert provider.calls[0][0].api_version == "v1alpha"


def test_one_bad_model_does_not_hide_the_entries_after_it(tmp_path):
    """The point is to learn every bad name in one pass, not the first.

    Stopping at the first failure would cost a round trip per typo, which is
    the loop this tool exists to end.
    """
    roster = load_roster(
        write_roster(
            tmp_path,
            [
                entry("analyst_alpha", "openai", "typo-model"),
                entry("analyst_gamma", "openrouter", "good-model"),
            ],
        )
    )
    provider = FakeProvider(StatusError(404, "model not found"), "{}", "{}")

    results = verify_roster(roster, provider)

    assert [(r.key, r.ok) for r in results] == [
        ("analyst_alpha", False),
        ("analyst_gamma", True),
        ("ground_truth", True),
    ]
    assert "404" in results[0].detail
    assert "model not found" in results[0].detail


def test_truncation_is_reported_without_failing_the_entry(tmp_path):
    """The probe caps output, so a reasoning model is expected to be cut off.

    Reachability is the question here; a truncated reply still proves the model
    answered. Treating it as a failure would condemn every reasoning model in
    the roster.
    """
    roster = load_roster(
        write_roster(tmp_path, [entry("analyst_zeta", "anthropic", "claude-x")])
    )
    provider = FakeProvider(
        ModelResponse(text="{", truncated=True, output_tokens=256),
        "{}",
    )

    results = verify_roster(roster, provider)

    assert results[0].ok is True
    assert "truncated" in results[0].detail


def test_ground_truth_can_be_left_out(tmp_path):
    """It is only called with --mask-scores, so its key may not even be set."""
    roster = load_roster(
        write_roster(tmp_path, [entry("analyst_alpha", "openai", "gpt-5.5")])
    )
    provider = FakeProvider("{}")

    results = verify_roster(roster, provider, include_ground_truth=False)

    assert [r.key for r in results] == ["analyst_alpha"]


def test_a_missing_key_is_named_before_any_call_is_made(tmp_path, monkeypatch):
    """Spending one call to be told a key is unset is a wasted round trip."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    roster_path = write_roster(
        tmp_path, [entry("analyst_gamma", "openrouter", "glm")]
    )
    provider = FakeProvider("{}", "{}")

    with pytest.raises(SystemExit) as exc_info:
        _main(["--analysts", str(roster_path)], provider=provider)

    assert "OPENROUTER_API_KEY" in str(exc_info.value)
    assert provider.calls == []


def test_main_exits_non_zero_when_an_entry_fails(tmp_path, capsys):
    roster_path = write_roster(
        tmp_path,
        [
            entry("analyst_alpha", "openai", "typo"),
            entry("analyst_gamma", "openrouter", "fine"),
        ],
    )
    provider = FakeProvider(StatusError(404, "nope"), "{}", "{}")

    with pytest.raises(SystemExit) as exc_info:
        _main(["--analysts", str(roster_path)], provider=provider)

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "analyst_alpha" in out
    assert "analyst_gamma" in out
    assert "1 of 3" in out


def test_main_exits_zero_when_every_entry_answers(tmp_path, capsys):
    roster_path = write_roster(
        tmp_path, [entry("analyst_alpha", "openai", "gpt-5.5")]
    )
    provider = FakeProvider("{}", "{}")

    _main(["--analysts", str(roster_path)], provider=provider)

    assert "all 2 entries answered" in capsys.readouterr().out


def test_the_provider_is_closed_even_when_an_entry_fails(tmp_path):
    roster = load_roster(
        write_roster(tmp_path, [entry("analyst_alpha", "openai", "typo")])
    )
    provider = FakeProvider(StatusError(500, "boom"), StatusError(500, "boom"))

    verify_roster(roster, provider)

    assert provider.closes == 1


def test_results_carry_what_a_fix_needs(tmp_path):
    """key, provider and model: enough to find the roster line to edit."""
    roster = load_roster(
        write_roster(tmp_path, [entry("analyst_beta", "google", "gemini-x")])
    )
    provider = FakeProvider(StatusError(404, "gone"), "{}")

    first = verify_roster(roster, provider)[0]

    assert isinstance(first, VerifyResult)
    assert (first.key, first.provider, first.model) == (
        "analyst_beta",
        Provider.google,
        "gemini-x",
    )
