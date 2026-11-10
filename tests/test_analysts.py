from __future__ import annotations

import json
from pathlib import Path

import pytest

from dili_rucam_agents.analysts import (
    AnalystAttemptEvent,
    AnalystExecutionError,
    run_analysts,
)
from dili_rucam_agents.contracts import (
    RETRY_REQUIREMENT_PLACEHOLDER,
    build_analyst_instruction_contract,
)
from dili_rucam_agents.providers import (
    MissingApiKeyError,
    ModelResponse,
    api_key_env,
)
from dili_rucam_agents.rendering import RULES_VERSION
from dili_rucam_agents.roster import AnalystConfig, AnalystRoster, Provider
from dili_rucam_agents.validators.analyst_report import validate_analyst_report
from fakes import FakeProvider, StatusError
from test_rendering import _SCORES
from test_rucam_assessment import _assessment, _causes, _item

BUNDLE = '{"case": "unique-bundle-marker"}'
PROMPT = "PROMPT-BODY"
BUNDLE_NAME = "raw_case_bundle_json"


@pytest.fixture(autouse=True)
def api_keys(monkeypatch):
    for provider in Provider:
        monkeypatch.setenv(api_key_env(provider), "test-key")


def _roster(n: int = 1) -> AnalystRoster:
    return AnalystRoster.model_validate(
        {
            "analysts": [
                {
                    "key": f"analyst_{i}",
                    "label": f"Analyst {i}",
                    "provider": "openai" if i % 2 else "anthropic",
                    "model": f"model-{i}",
                }
                for i in range(n)
            ],
            "ground_truth": {"provider": "openai", "model": "gt"},
        }
    )


def _reply(marker: str = "because", **overrides) -> str:
    items = {name: _item(score, rationale=marker) for name, score in _SCORES.items()}
    data = _assessment(item_5_causes=_causes(3), **items)
    data.update(overrides)
    return json.dumps(data)


def _run(provider, roster=None, **kwargs):
    return run_analysts(
        case_bundle_json=BUNDLE,
        roster=roster or _roster(1),
        prompt_text=PROMPT,
        bundle_input_name=BUNDLE_NAME,
        provider=provider,
        **kwargs,
    )


def test_one_analyst_roster_produces_a_report_and_a_sidecar():
    provider = FakeProvider(_reply())
    outcome = _run(provider)

    assert list(outcome.reports) == ["analyst_0"]
    validated = validate_analyst_report(outcome.reports["analyst_0"])
    # Total and category come from score_assessment, not from the model.
    assert validated.payload.total_score == 9
    assert validated.payload.category == "Highly probable"
    assert f'"rules_version": "{RULES_VERSION}"' in outcome.reports["analyst_0"]
    assert "Analyst 0 (model-0)" in outcome.reports["analyst_0"]
    sidecar = outcome.assessments["analyst_0"]
    assert sidecar["injury_pattern"] == "hepatocellular"
    assert "total_score" not in sidecar and "category" not in sidecar
    assert outcome.counters["analyst_0"].validation_retries == 0
    assert len(provider.calls) == 1


def test_seven_analyst_roster_produces_seven_of_each():
    provider = FakeProvider(*[_reply(f"m{i}") for i in range(7)])
    outcome = _run(provider, _roster(7))

    keys = [f"analyst_{i}" for i in range(7)]
    assert list(outcome.reports) == keys
    assert list(outcome.assessments) == keys
    assert list(outcome.counters) == keys
    assert len(provider.calls) == 7
    # Each analyst is called on its own model, in roster order.
    assert [c[0].model for c in provider.calls] == [f"model-{i}" for i in range(7)]


def test_a_completed_report_passed_in_is_not_re_scored():
    first = _run(FakeProvider(_reply("first")), _roster(1)).reports["analyst_0"]
    provider = FakeProvider(_reply("second"))
    events: list[AnalystAttemptEvent] = []
    reported: list[str] = []

    outcome = _run(
        provider,
        _roster(2),
        completed_reports={"analyst_0": first},
        on_attempt=events.append,
        on_report=lambda key, text, assessment: reported.append(key),
    )

    assert outcome.reports["analyst_0"] == first
    assert "analyst_0" not in outcome.assessments
    assert "analyst_0" not in outcome.counters
    # Only analyst_1 reached the provider, and only analyst_1 produced events.
    assert [c[0].model for c in provider.calls] == ["model-1"]
    assert {e.analyst_key for e in events} == {"analyst_1"}
    assert reported == ["analyst_1"]


def test_an_invalid_completed_report_is_scored_again():
    provider = FakeProvider(_reply())
    outcome = _run(provider, completed_reports={"analyst_0": "not a report"})
    assert len(provider.calls) == 1
    assert outcome.reports["analyst_0"] != "not a report"


def test_a_failing_analyst_raises_with_its_failure_kind_and_counters():
    provider = FakeProvider(_reply("fine"), '{"nonsense": 1}', '{"nonsense": 2}', '{"nonsense": 3}')
    with pytest.raises(AnalystExecutionError) as caught:
        _run(provider, _roster(2), max_retries=2)

    error = caught.value
    assert error.analyst_key == "analyst_1"
    assert error.failure_kind == "validation"
    assert error.attempts == 3
    assert error.counters.validation_retries == 2  # three attempts, two retries
    assert error.validation_diagnostic is not None
    assert error.execution_diagnostic is None
    assert "analyst_1" in str(error)


def test_an_execution_failure_carries_kind_diagnostic_and_event():
    provider = FakeProvider(StatusError(401, "bad key"))
    events: list[AnalystAttemptEvent] = []
    with pytest.raises(AnalystExecutionError) as caught:
        _run(provider, on_attempt=events.append)

    assert caught.value.failure_kind == "terminal"
    assert caught.value.execution_diagnostic is not None
    assert caught.value.validation_diagnostic is None
    assert [e.status for e in events] == ["running", "execution_failed"]
    assert events[1].execution_diagnostic == caught.value.execution_diagnostic


def test_a_truncated_run_reports_truncation():
    cut = ModelResponse(text="{", truncated=True)
    with pytest.raises(AnalystExecutionError) as caught:
        _run(FakeProvider(cut, cut))
    assert caught.value.failure_kind == "truncation"
    assert caught.value.counters.truncation_escalations == 1


def test_attempt_events_follow_the_runner_attempts():
    provider = FakeProvider('{"nonsense": 1}', _reply())
    events: list[AnalystAttemptEvent] = []
    outcome = _run(provider, max_retries=2, on_attempt=events.append)

    assert [(e.attempt, e.max_attempts, e.status) for e in events] == [
        (1, 3, "running"),
        (1, 3, "validation_failed"),
        (2, 3, "running"),
        (2, 3, "completed"),
    ]
    assert events[1].validation_diagnostic is not None
    assert events[-1].report_text == outcome.reports["analyst_0"]
    assert outcome.counters["analyst_0"].validation_retries == 1


def test_on_report_receives_key_report_and_assessment():
    seen = []
    outcome = _run(FakeProvider(_reply()), on_report=lambda *args: seen.append(args))
    assert seen == [("analyst_0", outcome.reports["analyst_0"], outcome.assessments["analyst_0"])]


def test_each_analyst_gets_its_own_contract_sha256():
    roster = _roster(2)
    provider = FakeProvider(_reply("a"), _reply("b"))
    _run(provider, roster)

    expected = []
    for config in roster.analysts:
        contract = build_analyst_instruction_contract(
            analyst_label=config.label,
            prompt_text=PROMPT,
            model_name=config.model,
            bundle_input_name=BUNDLE_NAME,
        )
        expected.append(contract)
    assert expected[0].sha256 != expected[1].sha256
    for contract, call in zip(expected, provider.calls):
        rendered = contract.render(
            **{BUNDLE_NAME: BUNDLE, RETRY_REQUIREMENT_PLACEHOLDER: ""}
        )
        sent = call[1][0]["content"]
        assert sent.startswith(rendered)
        assert BUNDLE in sent and PROMPT in sent
    assert provider.calls[0][1][0]["content"] != provider.calls[1][1][0]["content"]


def test_no_analyst_sees_another_analysts_output():
    provider = FakeProvider(_reply("SECRET-ALPHA-RATIONALE"), _reply("SECRET-BETA-RATIONALE"))
    outcome = _run(provider, _roster(2))

    assert "SECRET-ALPHA-RATIONALE" in outcome.reports["analyst_0"]
    second_call_text = json.dumps(provider.calls[1][1])
    assert "SECRET-ALPHA-RATIONALE" not in second_call_text
    assert "Analyst 0" not in second_call_text
    # And the report, not just the raw reply, stays out of later prompts.
    assert "SECTION A" not in second_call_text


def test_api_keys_are_checked_before_any_call(monkeypatch):
    monkeypatch.delenv(api_key_env(Provider.anthropic))
    provider = FakeProvider(_reply(), _reply())
    with pytest.raises(MissingApiKeyError, match="ANTHROPIC_API_KEY"):
        _run(provider, _roster(2))
    # analyst_0 (openai) is first and would have succeeded: nothing was called.
    assert provider.calls == []


def test_rejected_attempts_persist_as_invalid_artifacts(tmp_path: Path):
    parsed_but_wrong = '{"nonsense": 1}'
    unparseable = "I could not produce JSON, sorry"
    provider = FakeProvider(parsed_but_wrong, unparseable, _reply())
    outcome = _run(provider, output_dir=tmp_path)

    attempts = tmp_path / "attempts"
    as_json = attempts / "analyst_0.1.invalid.json"
    as_text = attempts / "analyst_0.2.invalid.txt"
    assert json.loads(as_json.read_text()) == {"nonsense": 1}
    assert as_text.read_text() == unparseable
    assert not (attempts / "analyst_0.1.invalid.txt").exists()
    assert not (attempts / "analyst_0.2.invalid.json").exists()
    assert sorted(p.name for p in attempts.iterdir()) == [
        "analyst_0.1.invalid.json",
        "analyst_0.2.invalid.txt",
    ]
    assert outcome.counters["analyst_0"].validation_retries == 2


def test_no_audit_files_without_an_output_dir_or_a_rejection(tmp_path: Path):
    _run(FakeProvider(_reply()), output_dir=tmp_path)
    assert not (tmp_path / "attempts").exists()
