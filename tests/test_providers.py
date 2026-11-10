"""The provider layer's request builders are pure, so no transport is needed."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dili_rucam_agents.providers import (
    BASE_URLS,
    GeminiAPIError,
    MissingApiKeyError,
    ModelSpec,
    StructuredMode,
    anthropic_request,
    api_key_env,
    extract_json,
    google_output_tokens,
    google_request,
    model_spec,
    openai_request,
    require_api_keys,
    takes_temperature,
)
from dili_rucam_agents.roster import AnalystConfig, GroundTruthConfig, Provider

MSGS = [
    {"role": "system", "content": "You are an analyst."},
    {"role": "user", "content": "Score the case."},
]
SCHEMA = {
    "type": "object",
    "properties": {"total": {"type": "integer", "minimum": -4, "maximum": 14}},
    "required": ["total"],
}


def spec(provider: Provider, model: str = "some-model", cap: int = 32000) -> ModelSpec:
    return ModelSpec(provider=provider, model=model, max_output_tokens=cap)


def test_temperature_boundary_after_gpt_prefix():
    assert takes_temperature("gpt-50-turbo") is True
    assert takes_temperature("gpt-5.5") is False


def test_claude_models_that_accept_temperature_keep_it():
    assert takes_temperature("claude-haiku-4-5") is True
    assert takes_temperature("claude-opus-4-7") is False


def test_openrouter_id_is_judged_on_the_bare_model_name():
    assert takes_temperature("anthropic/claude-sonnet-5") is False
    assert takes_temperature("moonshotai/kimi-k2.5") is True


def test_openrouter_request_requires_parameters():
    req = openai_request(spec(Provider.openrouter), MSGS, StructuredMode.json_schema, SCHEMA)
    assert req["extra_body"]["provider"]["require_parameters"] is True


def test_json_schema_mode_is_not_strict():
    req = openai_request(spec(Provider.openai), MSGS, StructuredMode.json_schema, SCHEMA)
    assert req["response_format"]["json_schema"]["strict"] is False


def test_text_mode_carries_no_response_format():
    for provider in (Provider.openai, Provider.openrouter, Provider.deepseek):
        req = openai_request(spec(provider), MSGS, StructuredMode.text, SCHEMA)
        assert "response_format" not in req
        assert "extra_body" not in req
    assert "output_config" not in anthropic_request(
        spec(Provider.anthropic), MSGS, StructuredMode.text, SCHEMA
    )
    config = google_request(spec(Provider.google), MSGS, StructuredMode.text, SCHEMA)["config"]
    assert "response_mime_type" not in config


def test_anthropic_request_streams():
    # A large max_tokens is refused by the SDK unless the call streams, so the
    # request must not carry a flag that turns streaming off.
    req = anthropic_request(spec(Provider.anthropic, "claude-opus-4-7", 64000), MSGS,
                            StructuredMode.json_schema, SCHEMA)
    assert req["max_tokens"] == 64000
    assert "stream" not in req  # the coroutine opens client.messages.stream(**req)


def test_anthropic_system_prompt_is_split_out_of_messages():
    req = anthropic_request(spec(Provider.anthropic), MSGS, StructuredMode.text, None)
    assert req["system"] == "You are an analyst."
    assert [m["role"] for m in req["messages"]] == ["user"]


def test_anthropic_schema_is_closed_and_bounds_move_to_descriptions():
    req = anthropic_request(spec(Provider.anthropic), MSGS, StructuredMode.json_schema, SCHEMA)
    schema = req["output_config"]["format"]["schema"]
    assert schema["additionalProperties"] is False
    total = schema["properties"]["total"]
    assert "minimum" not in total and "maximum" not in total
    assert "-4" in total["description"] and "14" in total["description"]
    assert "minimum" in SCHEMA["properties"]["total"]  # the caller's schema is untouched


def test_anthropic_temperature_is_sent_only_where_accepted():
    kept = anthropic_request(spec(Provider.anthropic, "claude-haiku-4-5"), MSGS,
                             StructuredMode.text, None)
    dropped = anthropic_request(spec(Provider.anthropic, "claude-opus-4-7"), MSGS,
                                StructuredMode.text, None)
    assert kept["extra_body"] == {"temperature": 0.0}
    assert "extra_body" not in dropped


def test_google_request_sums_thinking_tokens():
    usage = SimpleNamespace(candidates_token_count=900, thoughts_token_count=3100)
    assert google_output_tokens(usage) == 4000
    assert google_output_tokens(SimpleNamespace(candidates_token_count=900,
                                                thoughts_token_count=None)) == 900
    assert google_output_tokens(None) is None


def test_google_errors_are_normalised_to_status_code():
    class SdkError(Exception):
        code = 429
        message = "quota"

    sdk_error = SdkError("429 RESOURCE_EXHAUSTED")
    normalised = GeminiAPIError.from_sdk(sdk_error)
    assert normalised.status_code == 429
    assert str(normalised) == "429 RESOURCE_EXHAUSTED"
    assert normalised.body == {"error": {"message": "quota"}}


def test_deepseek_uses_the_openai_path_with_its_own_base_url():
    assert BASE_URLS[Provider.deepseek] == "https://api.deepseek.com"
    assert BASE_URLS[Provider.deepseek] != BASE_URLS[Provider.openai]
    req = openai_request(spec(Provider.deepseek, "deepseek-v4-pro"), MSGS,
                         StructuredMode.json_object, None)
    assert req["response_format"] == {"type": "json_object"}
    assert req["messages"] == MSGS


def test_openai_reasoning_model_uses_max_completion_tokens_not_max_tokens():
    req = openai_request(spec(Provider.openai, "gpt-5.5", 32000), MSGS, StructuredMode.text, None)
    assert req["max_completion_tokens"] == 32000
    assert "max_tokens" not in req
    assert "temperature" not in req


def test_openrouter_uses_max_tokens():
    req = openai_request(spec(Provider.openrouter, "z-ai/glm-5", 32000), MSGS,
                         StructuredMode.text, None)
    assert req["max_tokens"] == 32000
    assert "max_completion_tokens" not in req


def test_model_spec_carries_the_rosters_provider_model_and_cap():
    analyst = AnalystConfig(key="alpha", label="Alpha", provider=Provider.deepseek,
                            model="deepseek-v4-pro", max_output_tokens=24000)
    built = model_spec(analyst)
    assert (built.provider, built.model, built.max_output_tokens) == (
        Provider.deepseek, "deepseek-v4-pro", 24000)
    truth = model_spec(GroundTruthConfig(provider=Provider.openai, model="gpt-5.5"))
    assert truth.max_output_tokens == 8000


def test_model_spec_omits_temperature_for_a_model_that_rejects_one():
    config = AnalystConfig(key="alpha", label="Alpha", provider=Provider.openai, model="gpt-5.5")
    req = openai_request(model_spec(config), MSGS, StructuredMode.text, None)
    assert "temperature" not in req
    config = AnalystConfig(key="b", label="B", provider=Provider.openai, model="gpt-4.1")
    assert openai_request(model_spec(config), MSGS, StructuredMode.text, None)["temperature"] == 0.0


def test_extract_json_reads_a_fenced_object():
    assert extract_json('Here:\n```json\n{"a": 1}\n```\nDone') == {"a": 1}
    assert extract_json('prose {"a": {"b": 2}} tail') == {"a": {"b": 2}}
    with pytest.raises(ValueError):
        extract_json("no object here")


def test_require_api_keys_names_the_missing_variable(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(MissingApiKeyError, match="ANTHROPIC_API_KEY"):
        require_api_keys([Provider.anthropic])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    require_api_keys([Provider.anthropic])


def test_every_provider_has_a_key_variable():
    assert {api_key_env(p) for p in Provider} == {
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
        "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY",
    }
