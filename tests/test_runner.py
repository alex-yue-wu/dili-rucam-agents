from __future__ import annotations

import asyncio
import json
from pathlib import Path

from pydantic import BaseModel
import pytest

from dili_rucam_agents import runner as runner_module
from dili_rucam_agents.providers import (
    ModelResponse,
    ModelSpec,
    StructuredMode,
    starting_mode,
)
from dili_rucam_agents.roster import Provider
from dili_rucam_agents.runner import RetryCounters, RunnerError, run_contract
from fakes import FakeProvider, StatusError


class Verdict(BaseModel):
    score: int
    label: str


GOOD = json.dumps({"score": 3, "label": "probable"})
BAD = json.dumps({"score": "three", "label": "probable"})
SPEC = ModelSpec(provider=Provider.openai, model="gpt-5.5")
DEEPSEEK = ModelSpec(provider=Provider.deepseek, model="deepseek-v4-pro")
OPENROUTER = ModelSpec(provider=Provider.openrouter, model="moonshotai/kimi-k2.5")
OPENROUTER_REFUSAL = (
    "No endpoints found that can handle the requested parameters: response_format"
)


@pytest.fixture
def sleeps():
    recorded: list[float] = []

    async def sleep(seconds: float) -> None:
        recorded.append(seconds)

    sleep.recorded = recorded  # type: ignore[attr-defined]
    return sleep


def run(provider, sleeps, spec=SPEC, **kwargs):
    return asyncio.run(
        run_contract(
            instructions="Score the case.",
            model_class=Verdict,
            spec=spec,
            provider=provider,
            sleep=sleeps,
            **kwargs,
        )
    )


def run_error(provider, sleeps, **kwargs) -> RunnerError:
    with pytest.raises(RunnerError) as caught:
        run(provider, sleeps, **kwargs)
    return caught.value


def test_valid_first_reply_spends_no_retries(sleeps):
    result = run(FakeProvider(GOOD), sleeps)
    assert result.value == Verdict(score=3, label="probable")
    assert result.counters == RetryCounters(0, 0, 0, 0, StructuredMode.json_schema)
    assert [a.outcome for a in result.attempts] == ["completed"]


def test_schema_violation_spends_a_validation_retry(sleeps):
    result = run(FakeProvider(BAD, GOOD), sleeps)
    assert result.counters.validation_retries == 1
    assert [a.outcome for a in result.attempts] == ["validation_failed", "completed"]
    assert result.attempts[0].raw_text == BAD


def test_validation_retry_sends_back_the_previous_reply_then_the_correction(sleeps):
    fake = FakeProvider(BAD, GOOD)
    run(fake, sleeps)
    assert len(fake.calls) == 2
    assert fake.last_messages[-2] == {"role": "assistant", "content": BAD}
    assert fake.last_messages[-1]["role"] == "user"
    assert "score" in fake.last_messages[-1]["content"]
    assert fake.last_messages[0] == fake.calls[0][1][0]


def test_unparseable_reply_is_a_validation_failure(sleeps):
    result = run(FakeProvider("I cannot help with that.", GOOD), sleeps)
    assert result.counters.validation_retries == 1


def test_fenced_reply_is_accepted(sleeps):
    result = run(FakeProvider(f"Here you go:\n```json\n{GOOD}\n```"), sleeps)
    assert result.value.score == 3


def test_exhausted_validation_budget_raises_validation_kind(sleeps):
    fake = FakeProvider(BAD, BAD, BAD)
    error = run_error(fake, sleeps, max_retries=2)
    assert error.kind == "validation"
    assert error.attempts == 3
    assert error.counters.validation_retries == 2
    assert error.validation_diagnostic is not None
    assert len(fake.calls) == 3


def test_transient_error_does_not_consume_a_validation_attempt(sleeps):
    result = run(FakeProvider(StatusError(429, "slow down"), GOOD), sleeps)
    assert result.counters.validation_retries == 0
    assert result.counters.transient_retries == 1
    assert len(result.attempts) == 1


def test_transient_errors_do_not_shrink_the_validation_budget(sleeps):
    fake = FakeProvider(StatusError(429), BAD, StatusError(503), BAD, GOOD)
    result = run(fake, sleeps, max_retries=2)
    assert result.counters.validation_retries == 2
    assert result.counters.transient_retries == 2


def test_three_transient_errors_raise_execution_kind(sleeps):
    fake = FakeProvider(StatusError(429), StatusError(500), StatusError(503))
    error = run_error(fake, sleeps)
    assert error.kind == "execution"
    assert error.counters.transient_retries == 2
    assert error.execution_diagnostic.status_code == 503
    assert len(fake.calls) == 3


def test_transient_backoff_grows_and_is_jittered(sleeps, monkeypatch):
    jitter_args = []

    def fixed_jitter(low, high):
        jitter_args.append((low, high))
        return high

    monkeypatch.setattr(runner_module.random, "uniform", fixed_jitter)
    run(FakeProvider(StatusError(429), StatusError(429), GOOD), sleeps)
    first, second = sleeps.recorded
    assert first == pytest.approx(0.5 * 1.1)
    assert second == pytest.approx(1.0 * 1.1)
    assert jitter_args == [(0, pytest.approx(0.05)), (0, pytest.approx(0.1))]


def test_backoff_is_capped(monkeypatch):
    monkeypatch.setattr(runner_module.random, "uniform", lambda low, high: 0.0)
    assert runner_module._backoff_seconds(10) == 4.0


def test_truncated_reply_doubles_max_output_tokens_once(sleeps):
    fake = FakeProvider(ModelResponse(text='{"sc', truncated=True), GOOD)
    result = run(fake, sleeps)
    assert [call[0].max_output_tokens for call in fake.calls] == [32000, 64000]
    assert result.counters.truncation_escalations == 1
    assert result.counters.validation_retries == 0
    # The cut-off reply is not an answer, so no correction turn is sent.
    assert [m["role"] for m in fake.last_messages] == ["user"]


def test_second_truncation_raises_truncation_kind(sleeps):
    truncated = ModelResponse(text="{", truncated=True)
    fake = FakeProvider(truncated, truncated)
    error = run_error(fake, sleeps, max_output_ceiling=200000)
    assert error.kind == "truncation"
    assert error.counters.truncation_escalations == 1
    assert len(fake.calls) == 2


def test_truncation_never_exceeds_the_ceiling(sleeps):
    truncated = ModelResponse(text="{", truncated=True)
    fake = FakeProvider(truncated, truncated, GOOD)
    spec = SPEC.model_copy(update={"max_output_tokens": 40000})
    error = run_error(fake, sleeps, spec=spec, max_output_ceiling=64000)
    assert [call[0].max_output_tokens for call in fake.calls] == [40000, 64000]
    assert error.kind == "truncation"


def test_truncation_at_the_ceiling_fails_without_another_call(sleeps):
    fake = FakeProvider(ModelResponse(text="{", truncated=True), GOOD)
    spec = SPEC.model_copy(update={"max_output_tokens": 64000})
    error = run_error(fake, sleeps, spec=spec, max_output_ceiling=64000)
    assert error.kind == "truncation"
    assert len(fake.calls) == 1


def test_format_rejection_steps_down_the_mode_ladder(sleeps):
    fake = FakeProvider(StatusError(400, "bad response_format"), GOOD)
    result = run(fake, sleeps)
    assert fake.modes == [StructuredMode.json_schema, StructuredMode.json_object]
    assert result.counters.mode_downgrades == 1
    assert result.counters.final_mode is StructuredMode.json_object
    assert result.counters.transient_retries == 0
    assert sleeps.recorded == []


def test_recurring_format_rejection_in_text_mode_is_terminal(sleeps):
    fake = FakeProvider(*[StatusError(400, "bad response_format")] * 3)
    error = run_error(fake, sleeps)
    assert error.kind == "terminal"
    assert fake.modes == list(StructuredMode)
    assert error.counters.mode_downgrades == 2


def test_401_fails_immediately_with_terminal_kind(sleeps):
    fake = FakeProvider(StatusError(401, "bad key"), GOOD)
    error = run_error(fake, sleeps)
    assert error.kind == "terminal"
    assert error.execution_diagnostic.status_code == 401
    assert len(fake.calls) == 1
    assert sleeps.recorded == []


def test_mode_downgrade_does_not_append_an_attempt(sleeps):
    seen = []
    fake = FakeProvider(StatusError(400, "response_format"), GOOD)
    result = run(fake, sleeps, on_attempt=seen.append)
    assert len(result.attempts) == 1
    assert result.attempts[0].number == 1
    assert result.attempts[0].mode is StructuredMode.json_object
    assert [a.outcome for a in seen] == ["completed"]


@pytest.mark.parametrize("mode_calls", [0, 1, 2])
def test_schema_is_in_the_prompt_in_every_mode(sleeps, mode_calls):
    fake = FakeProvider(*[StatusError(400, "response_format")] * mode_calls, GOOD)
    run(fake, sleeps)
    assert fake.modes[-1] is list(StructuredMode)[mode_calls]
    schema_text = json.dumps(Verdict.model_json_schema(), indent=2, sort_keys=True)
    assert schema_text in fake.calls[-1][1][0]["content"]
    sent_schema = fake.calls[-1][3]
    assert (sent_schema is not None) == (fake.modes[-1] is StructuredMode.json_schema)


def test_the_ladder_starts_at_the_highest_mode_the_provider_supports(sleeps):
    assert starting_mode(Provider.deepseek) is StructuredMode.json_object
    assert starting_mode(Provider.openai) is StructuredMode.json_schema
    fake = FakeProvider(GOOD)
    run(fake, sleeps, spec=DEEPSEEK)
    assert fake.modes == [StructuredMode.json_object]


def test_a_mode_never_supported_is_not_counted_as_a_downgrade(sleeps):
    result = run(FakeProvider(GOOD), sleeps, spec=DEEPSEEK)
    assert result.counters == RetryCounters(0, 0, 0, 0, StructuredMode.json_object)


def test_a_real_refusal_on_deepseek_still_counts_as_a_downgrade(sleeps):
    fake = FakeProvider(StatusError(400, "response_format unsupported"), GOOD)
    result = run(fake, sleeps, spec=DEEPSEEK)
    assert fake.modes == [StructuredMode.json_object, StructuredMode.text]
    assert result.counters.mode_downgrades == 1


@pytest.mark.parametrize("status", [404, 422])
def test_openrouter_format_refusal_is_a_format_rejection_not_a_terminal_404(sleeps, status):
    fake = FakeProvider(StatusError(status, OPENROUTER_REFUSAL), GOOD)
    result = run(fake, sleeps, spec=OPENROUTER)
    assert fake.modes == [StructuredMode.json_schema, StructuredMode.json_object]
    assert result.counters.mode_downgrades == 1


def test_openrouter_refusal_message_in_the_body_is_recognised(sleeps):
    refusal = StatusError(404, "Error code: 404")
    refusal.body = {"error": {"message": OPENROUTER_REFUSAL}}
    result = run(FakeProvider(refusal, GOOD), sleeps, spec=OPENROUTER)
    assert result.counters.mode_downgrades == 1


def test_a_404_for_a_wrong_model_id_is_still_terminal(sleeps):
    fake = FakeProvider(StatusError(404, "No endpoints found for moonshotai/kimi-k9."))
    error = run_error(fake, sleeps, spec=OPENROUTER)
    assert error.kind == "terminal"
    assert len(fake.calls) == 1


def test_tokens_are_summed_across_every_call(sleeps):
    fake = FakeProvider(
        ModelResponse(text=BAD, input_tokens=10, output_tokens=5),
        ModelResponse(text=GOOD, input_tokens=20, output_tokens=7),
    )
    result = run(fake, sleeps)
    assert (result.input_tokens, result.output_tokens) == (30, 12)
    assert run(FakeProvider(GOOD), sleeps).input_tokens is None


def test_runner_module_imports_no_vendor_sdk():
    source = Path(runner_module.__file__).read_text()
    for sdk in ("openai", "anthropic", "google.genai", "crewai", "litellm"):
        assert f"import {sdk}" not in source and f"from {sdk}" not in source
