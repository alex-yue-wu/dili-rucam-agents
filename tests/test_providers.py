"""The provider layer's request builders are pure, so no transport is needed."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from pydantic import BaseModel, ValidationError
import pytest

from dili_rucam_agents.providers import (
    BASE_URLS,
    TIMEOUT_SECONDS,
    GeminiAPIError,
    SdkProvider,
    _UNSUPPORTED_BOUNDS,
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
from dili_rucam_agents.validators.rucam_assessment import RucamAssessment

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


def _transformed(schema):
    """The schema as the Anthropic request carries it."""
    request = anthropic_request(
        spec(Provider.anthropic), MSGS, StructuredMode.json_schema, schema
    )
    return request["output_config"]["format"]["schema"]


def _nodes(node):
    """Every dict in a JSON Schema tree, the root included."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _nodes(value)
    elif isinstance(node, list):
        for item in node:
            yield from _nodes(item)


def test_anthropic_schema_is_closed_and_bounds_move_to_descriptions():
    schema = _transformed(SCHEMA)
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


# -- google_request against the installed google-genai ----------------------
#
# Task 2 checked these kwargs by hand against google-genai 1.49.0 and none was
# exercised by a test, because none runs without a live call. Building the real
# SDK types from the kwargs makes the check part of the suite, so the next
# major bump cannot rename one silently.


def test_google_request_config_is_accepted_by_the_installed_sdk():
    from google.genai import types

    config = google_request(
        spec(Provider.google), MSGS, StructuredMode.json_schema, SCHEMA
    )["config"]
    built = types.GenerateContentConfig(
        **config,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )

    assert built.max_output_tokens == 32000
    assert built.temperature == 0.0
    assert built.system_instruction == "You are an analyst."
    assert built.response_mime_type == "application/json"
    assert built.response_json_schema == SCHEMA
    assert built.automatic_function_calling.disable is True
    with pytest.raises(ValidationError):  # a renamed kwarg would not pass silently
        types.GenerateContentConfig(**config, max_output_token=1)


def test_google_contents_are_accepted_by_the_installed_sdk():
    from google.genai import types

    contents = google_request(
        spec(Provider.google),
        [*MSGS, {"role": "assistant", "content": "earlier reply"}],
        StructuredMode.text,
        None,
    )["contents"]
    built = [types.Content.model_validate(item) for item in contents]

    assert [content.role for content in built] == ["user", "model"]
    assert built[1].parts[0].text == "earlier reply"


def test_google_http_options_timeout_is_still_milliseconds():
    from google.genai import types

    options = types.HttpOptions(
        timeout=TIMEOUT_SECONDS * 1000,
        retry_options=types.HttpRetryOptions(attempts=1),
    )

    assert options.timeout == 600_000
    # The unit is the SDK's, not ours: a switch to seconds would make the
    # timeout 600_000 seconds rather than ten minutes.
    assert "millisecond" in types.HttpOptions.model_fields["timeout"].description
    assert options.retry_options.attempts == 1


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
    assert req["max_tokens"] == 32000  # not OpenAI's max_completion_tokens
    assert "max_completion_tokens" not in req


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


# -- anthropic.transform_schema, the SDK's own transform --------------------
#
# Task 2 shipped a local stand-in because `anthropic.transform_schema` did not
# exist at anthropic 0.71.1. It does from 1.11.0, and the request now calls it.
# The two never agreed textually -- the SDK writes a bound as
# "{minimum: -4, maximum: 14}" where the stand-in wrote
# "Bounds: minimum: -4, maximum: 14." -- so these tests pin the SDK's behaviour
# on the properties that matter instead of comparing against deleted code.


def _property(schema, name):
    return _transformed({"type": "object", "properties": {name: schema}})[
        "properties"
    ][name]


def test_anthropic_unsupported_keyword_list_is_pinned():
    # From the "JSON Schema limitations" section of Anthropic's structured
    # outputs documentation: numerical and string-length constraints, and array
    # size bounds. The transform must strip every one of them.
    assert set(_UNSUPPORTED_BOUNDS) == {
        "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
        "minLength", "maxLength", "minItems", "maxItems",
    }
    for keyword in _UNSUPPORTED_BOUNDS:
        out = _property({"type": "integer", keyword: 3}, "s")
        assert keyword not in out
        assert "3" in out["description"]


def test_the_sdk_also_strips_pattern_although_the_docs_list_it_as_supported():
    """Recorded because the stand-in deliberately left `pattern` alone.

    Anthropic's documentation lists `pattern` and `format` as supported, so the
    deleted local transform preserved both. The SDK's transform keeps `format`
    but moves `pattern` into the description, so a pattern constraint reaches
    the model as prose rather than as a guarantee. No field of RucamAssessment
    carries one today.
    """
    out = _property({"type": "string", "pattern": "^[A-Z]", "format": "date"}, "s")
    assert out["format"] == "date"
    assert "pattern" not in out
    assert "^[A-Z]" in out["description"]
    assert not any(
        "pattern" in node for node in _nodes(_transformed(RucamAssessment.model_json_schema()))
    )


def test_every_object_node_of_the_real_schema_is_closed_and_has_no_bounds():
    """The whole production schema, not a representative fragment."""
    source = RucamAssessment.model_json_schema()
    transformed = _transformed(source)

    assert any("minimum" in node for node in _nodes(source))  # the probe is real
    for node in _nodes(transformed):
        for keyword in _UNSUPPORTED_BOUNDS:
            assert keyword not in node, f"{keyword} survived in {node}"
        if node.get("type") == "object" or "properties" in node:
            assert node.get("additionalProperties") is False
    # onset_day is `int | None` with ge=1, so its bound is restated as prose.
    onset = transformed["properties"]["onset_day"]
    assert "minimum" not in onset
    assert "{minimum: 1}" in json.dumps(onset)
    assert source["properties"]["onset_day"]["anyOf"][0]["minimum"] == 1


def test_sub_schema_keywords_outside_the_bounds_table_are_not_left_open():
    """The first gap in the deleted stand-in, which left these unrecursed.

    The SDK does not recurse into them either -- it removes the keyword and
    restates it in the node's description -- but the consequence the gap had,
    an open sub-schema reaching the API with bounds intact, is gone.
    """
    for keyword in ("contains", "propertyNames", "unevaluatedItems"):
        source = {
            "type": "object",
            "properties": {
                "field": {
                    "type": "object",
                    keyword: {
                        "type": "object",
                        "properties": {"x": {"type": "integer", "minimum": 0}},
                    },
                }
            },
        }
        field = _transformed(source)["properties"]["field"]
        assert keyword not in field
        assert field["additionalProperties"] is False
        assert "minimum" not in json.dumps(
            {k: v for k, v in field.items() if k != "description"}
        )


def test_a_dict_valued_field_loses_its_value_schema_and_is_closed():
    """The second gap, which the SDK shares rather than fixes.

    Anthropic's structured outputs have no open map, so an
    `additionalProperties` sub-schema -- what pydantic emits for a
    `dict[str, X]` field -- becomes `False` and the value schema is discarded.
    No field of RucamAssessment is dict-valued, so nothing is lost today; a
    future one would be sent as a closed, empty object.
    """

    class WithMap(BaseModel):
        labs: dict[str, int]

    source = WithMap.model_json_schema()
    assert isinstance(source["properties"]["labs"]["additionalProperties"], dict)

    labs = _transformed(source)["properties"]["labs"]
    assert labs["additionalProperties"] is False
    assert labs.get("properties") == {}
    assert not any("integer" in json.dumps(node) for node in _nodes(labs))


def test_a_property_named_like_a_keyword_survives_the_transform():
    # A lab reference range ({minimum, maximum}) must keep both fields while
    # `required` still lists them.
    schema = {
        "type": "object",
        "properties": {
            "item": {"type": "string"},
            "maximum": {"type": "integer", "maximum": 9},
            "minItems": {"type": "integer"},
        },
        "required": ["item", "maximum", "minItems"],
    }
    out = _transformed(schema)
    assert set(out["properties"]) == {"item", "maximum", "minItems"}
    assert out["properties"]["maximum"]["type"] == "integer"
    assert "maximum" not in out["properties"]["maximum"]
    assert "9" in out["properties"]["maximum"]["description"]
    assert out["required"] == ["item", "maximum", "minItems"]
    assert out["additionalProperties"] is False


def test_the_transform_reaches_nested_items_and_unions():
    schema = {"type": "array", "items": {"anyOf": [
        {"type": "object", "properties": {"x": {"type": "integer", "minimum": 0}}},
        {"type": "null"},
    ]}, "minItems": 2}
    out = _transformed(schema)
    obj = out["items"]["anyOf"][0]
    assert obj["additionalProperties"] is False
    assert "minimum" not in obj["properties"]["x"]
    assert "minItems" not in out


# -- SdkProvider, with fakes in place of the SDKs ----------------------------


class _Stream:
    def __init__(self, message):
        self.message = message

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_final_message(self):
        return self.message


class _FakeAnthropic:
    def __init__(self, message=None, **kwargs):
        self.init_kwargs = kwargs
        self.message = message
        self.streamed = None
        self.messages = self
        self.create_called = False

    def stream(self, **request):
        self.streamed = request
        return _Stream(self.message)

    async def create(self, **request):  # pragma: no cover - must not be used
        self.create_called = True


def _anthropic_message(blocks, stop_reason="end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type=t, text=x) for t, x in blocks],
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=11, output_tokens=22),
    )


def _complete(provider, spec_, mode=StructuredMode.text, schema=None):
    return asyncio.run(provider.complete(spec_, MSGS, mode, schema))


def test_anthropic_call_streams_and_keeps_only_text_blocks():
    fake = _FakeAnthropic(_anthropic_message(
        [("thinking", "hidden"), ("text", '{"a":'), ("text", " 1}")]))
    sdk = SdkProvider()
    sdk._clients[Provider.anthropic] = fake
    resp = _complete(sdk, spec(Provider.anthropic, "claude-opus-4-7", 64000))
    assert fake.streamed["max_tokens"] == 64000  # went through messages.stream
    assert not fake.create_called
    assert resp.text == '{"a": 1}'
    assert (resp.input_tokens, resp.output_tokens, resp.truncated) == (11, 22, False)


def test_anthropic_max_tokens_stop_reason_is_a_truncation():
    fake = _FakeAnthropic(_anthropic_message([("text", "cut")], stop_reason="max_tokens"))
    sdk = SdkProvider()
    sdk._clients[Provider.anthropic] = fake
    assert _complete(sdk, spec(Provider.anthropic)).truncated is True


class _FakeOpenAI:
    instances: list = []

    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.requests = []
        self.finish_reason = "stop"
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        _FakeOpenAI.instances.append(self)

    async def _create(self, **request):
        self.requests.append(request)
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="hi"), finish_reason=self.finish_reason)],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=4),
        )


def test_openai_clients_are_built_once_per_provider_with_no_sdk_retries(monkeypatch):
    monkeypatch.setattr("openai.AsyncOpenAI", _FakeOpenAI)
    _FakeOpenAI.instances = []
    monkeypatch.setenv("OPENAI_API_KEY", "k1")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k2")
    sdk = SdkProvider()
    _complete(sdk, spec(Provider.openai, "gpt-5.5"))
    _complete(sdk, spec(Provider.openai, "gpt-5.5"))
    _complete(sdk, spec(Provider.deepseek, "deepseek-v4-pro"))
    assert len(_FakeOpenAI.instances) == 2  # reused for openai, separate for deepseek
    openai_client, deepseek_client = _FakeOpenAI.instances
    assert len(openai_client.requests) == 2
    assert openai_client.init_kwargs["max_retries"] == 0
    assert openai_client.init_kwargs["api_key"] == "k1"
    assert deepseek_client.init_kwargs["base_url"] == BASE_URLS[Provider.deepseek]
    assert deepseek_client.requests[0]["max_tokens"] == 32000  # the OpenAI path


def test_openai_length_finish_reason_is_a_truncation():
    fake = _FakeOpenAI()
    fake.finish_reason = "length"
    sdk = SdkProvider()
    sdk._clients[Provider.openai] = fake
    assert _complete(sdk, spec(Provider.openai)).truncated is True


def test_anthropic_client_is_built_once_with_no_sdk_retries(monkeypatch):
    built = []

    def factory(**kwargs):
        built.append(_FakeAnthropic(_anthropic_message([("text", "x")]), **kwargs))
        return built[-1]

    monkeypatch.setattr("anthropic.AsyncAnthropic", factory)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    sdk = SdkProvider()
    _complete(sdk, spec(Provider.anthropic))
    _complete(sdk, spec(Provider.anthropic))
    assert len(built) == 1
    assert built[0].init_kwargs["max_retries"] == 0


def test_missing_key_is_named_when_a_client_is_first_needed(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(MissingApiKeyError, match="DEEPSEEK_API_KEY"):
        _complete(SdkProvider(), spec(Provider.deepseek))


class _FakeGoogle:
    def __init__(self, finish_reason):
        self.finish_reason = finish_reason
        self.calls = 0
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content=self._generate))

    async def _generate(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(
            text="ok",
            candidates=[SimpleNamespace(finish_reason=self.finish_reason)],
            usage_metadata=SimpleNamespace(
                prompt_token_count=5, candidates_token_count=6, thoughts_token_count=7),
        )


def test_google_max_tokens_finish_reason_is_a_truncation_and_client_is_reused():
    from google.genai import types

    sdk = SdkProvider()
    sdk._clients[Provider.google] = _FakeGoogle(types.FinishReason.MAX_TOKENS)
    first = _complete(sdk, spec(Provider.google, "gemini-3.1-pro-preview"))
    assert first.truncated is True
    assert first.output_tokens == 13
    sdk._clients[Provider.google].finish_reason = types.FinishReason.STOP
    assert _complete(sdk, spec(Provider.google, "gemini-3.1-pro-preview")).truncated is False
    assert sdk._clients[Provider.google].calls == 2


def test_google_client_is_built_with_no_sdk_retries(monkeypatch):
    from google import genai

    captured = {}

    def factory(**kwargs):
        captured.update(kwargs)
        return _FakeGoogle(None)

    monkeypatch.setattr(genai, "Client", factory)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    _complete(SdkProvider(), spec(Provider.google, "gemini-3.1-pro-preview"))
    assert captured["http_options"].retry_options.attempts == 1


def test_every_provider_declares_modes_highest_first_and_ending_in_text():
    from dili_rucam_agents.providers import SUPPORTED_MODES, starting_mode

    assert set(SUPPORTED_MODES) == set(Provider)
    for provider, modes in SUPPORTED_MODES.items():
        assert modes[-1] is StructuredMode.text
        assert list(modes) == sorted(modes, key=list(StructuredMode).index)
        assert starting_mode(provider) is modes[0]


def test_only_deepseek_lacks_native_json_schema():
    from dili_rucam_agents.providers import SUPPORTED_MODES

    lacking = {p for p, m in SUPPORTED_MODES.items() if StructuredMode.json_schema not in m}
    assert lacking == {Provider.deepseek}
