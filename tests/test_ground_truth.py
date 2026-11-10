import pytest

from dili_rucam_agents import ground_truth

from dili_rucam_agents.ground_truth import (
    extract_ground_truth_rucam_category,
    extract_ground_truth_rucam_score,
    load_ground_truth_prompt,
    run_ground_truth_score_finder,
)
from dili_rucam_agents.providers import MissingApiKeyError, StructuredMode
from dili_rucam_agents.roster import GroundTruthConfig, Provider, load_roster
from fakes import FakeProvider

_REPORT = """# Ground Truth RUCAM Score Report

## Stable Fields
```text
GROUND_TRUTH_RUCAM_SCORE: 7
GROUND_TRUTH_RUCAM_CATEGORY: Probable
```
"""
_BUNDLE = '{"case": "raw bundle marker 7f3a"}'


def _config(provider: Provider = Provider.anthropic) -> GroundTruthConfig:
    return GroundTruthConfig(provider=provider, model="gt-model-x", max_output_tokens=1234)


def test_load_ground_truth_prompt_contains_stable_fields():
    prompt = load_ground_truth_prompt()

    assert "GROUND_TRUTH_RUCAM_SCORE" in prompt
    assert "GROUND_TRUTH_RUCAM_CATEGORY" in prompt
    assert "## Evidence" in prompt


def test_extract_ground_truth_rucam_score_reads_stable_field():
    report = """
# Ground Truth RUCAM Score Report

## Stable Fields
```text
GROUND_TRUTH_RUCAM_SCORE: 8
GROUND_TRUTH_RUCAM_CATEGORY: Probable
```
"""

    assert extract_ground_truth_rucam_score(report) == 8


def test_extract_ground_truth_rucam_category_reads_stable_field():
    report = """
# Ground Truth RUCAM Score Report

## Stable Fields
```text
GROUND_TRUTH_RUCAM_SCORE: 8
GROUND_TRUTH_RUCAM_CATEGORY: Highly probable
```
"""

    assert extract_ground_truth_rucam_category(report) == "Highly probable"


def test_finder_calls_the_configured_ground_truth_model(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    provider = FakeProvider(_REPORT)

    text = run_ground_truth_score_finder(
        raw_case_bundle_json=_BUNDLE, config=_config(), provider=provider
    )

    assert text == _REPORT
    assert len(provider.calls) == 1
    spec, messages, mode, schema = provider.calls[0]
    assert (spec.provider, spec.model, spec.max_output_tokens) == (
        Provider.anthropic,
        "gt-model-x",
        1234,
    )
    assert mode is StructuredMode.text
    assert schema is None
    sent = "\n".join(m["content"] for m in messages)
    assert "raw bundle marker 7f3a" in sent
    assert "GROUND_TRUTH_RUCAM_SCORE" in sent
    # The model name is interpolated into the prompt; pin it end to end.
    assert 'Use the configured model "gt-model-x".' in sent


def test_finder_output_still_yields_a_score_and_a_category(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    text = run_ground_truth_score_finder(
        raw_case_bundle_json=_BUNDLE, config=_config(), provider=FakeProvider(_REPORT)
    )

    assert extract_ground_truth_rucam_score(text) == 7
    assert extract_ground_truth_rucam_category(text) == "Probable"


def test_finder_never_appears_as_an_analyst_column():
    roster = load_roster()

    # The finder is configured beside the analysts, not among them ...
    assert isinstance(roster.ground_truth, GroundTruthConfig)
    # ... so no analyst key can collide with its aggregate row ...
    assert not any(a.key.startswith("ground_truth") for a in roster.analysts)
    # ... and the finder's report key is not among the roster's analyst keys.
    assert "ground_truth_rucam_score" not in {a.key for a in roster.analysts}


def test_missing_ground_truth_key_is_reported_before_the_call(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    provider = FakeProvider(_REPORT)

    with pytest.raises(MissingApiKeyError, match="ANTHROPIC_API_KEY"):
        run_ground_truth_score_finder(
            raw_case_bundle_json=_BUNDLE, config=_config(), provider=provider
        )

    assert provider.calls == []


def test_builder_output_is_pinned_to_a_golden_string(monkeypatch):
    # The prompt produces the baseline column every analyst is compared with,
    # so any rewording of the template must fail here.
    monkeypatch.setattr(ground_truth, "load_ground_truth_prompt", lambda: "PROMPT BODY")

    prompt = ground_truth.build_ground_truth_score_finder_prompt(
        raw_case_bundle_json='{"k": 1}', model_name="gt-model-x"
    )

    assert prompt == (
        "You are the Ground Truth RUCAM Score Finder. Consume the shared raw_case_bundle_json exactly as provided below.\n"
        "Find the author-reported RUCAM outcome in the source report, not a recomputed score.\n"
        "\n"
        "--- BEGIN RAW CASE BUNDLE JSON ---\n"
        '{"k": 1}\n'
        "--- END RAW CASE BUNDLE JSON ---\n"
        "\n"
        "--- BEGIN GROUND TRUTH PROMPT ---\n"
        "PROMPT BODY\n"
        "--- END GROUND TRUTH PROMPT ---\n"
        "\n"
        'Use the configured model "gt-model-x". Temperature must remain 0 when supported;\n'
        "otherwise use the provider-required default temperature.\n"
        "Return only the markdown report requested by the prompt."
    )
