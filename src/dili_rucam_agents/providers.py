"""Transport: a model spec and a message list in, one completion out.

One coroutine per vendor SDK. The provider is declared on each roster entry, so
nothing here guesses a vendor from a model name. The request builders are pure
functions returning kwargs dicts and import no SDK; each ``_complete_*``
coroutine imports its SDK lazily, so importing this module never needs one.

Keys come from the environment. A client is built once per provider and reused
for the run. SDK-level retries are off; the runner owns retrying.
"""

from __future__ import annotations

import copy
from enum import StrEnum
import json
import os
import re
from typing import Any, Iterable, Protocol

from pydantic import BaseModel, ConfigDict, Field

from dili_rucam_agents.roster import AnalystConfig, GroundTruthConfig, Provider

#: Per request. Long enough for a slow reasoning model on a full paper.
TIMEOUT_SECONDS = 600

#: Endpoints for the providers that speak OpenAI Chat Completions.
BASE_URLS: dict[Provider, str] = {
    Provider.openai: "https://api.openai.com/v1",
    Provider.openrouter: "https://openrouter.ai/api/v1",
    Provider.deepseek: "https://api.deepseek.com",
}

_API_KEY_ENV: dict[Provider, str] = {
    Provider.openai: "OPENAI_API_KEY",
    Provider.anthropic: "ANTHROPIC_API_KEY",
    Provider.google: "GEMINI_API_KEY",
    Provider.openrouter: "OPENROUTER_API_KEY",
    Provider.deepseek: "DEEPSEEK_API_KEY",
}

#: Models that reject an explicit temperature. The boundary matters: without it
#: "gpt-50-turbo" matches "gpt-5" and silently loses its temperature. Claude
#: removed sampling parameters from Opus 4.7 on (not Haiku 4.5 or the 4.6
#: models); OpenRouter spells those versions with a dot, hence ``[-.]``.
_NO_TEMPERATURE = re.compile(
    r"^(gpt-[56]|o1|o3|o4|claude-(fable|mythos|opus-5|opus-4[-.][78]|sonnet-5))\b"
)


class MissingApiKeyError(RuntimeError):
    """A provider the roster uses has no API key in the environment."""


class StructuredMode(StrEnum):
    """How hard we ask the provider to guarantee JSON. Tried in this order."""

    json_schema = "json_schema"
    json_object = "json_object"
    text = "text"


class ModelSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: Provider
    model: str = Field(min_length=1)
    max_output_tokens: int = Field(default=32000, gt=0)
    #: Ignored for a model that fixes its own (see `takes_temperature`).
    temperature: float = Field(default=0.0, ge=0, le=1)


class ModelResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    truncated: bool = False
    input_tokens: int | None = None
    output_tokens: int | None = None


class CompletionProvider(Protocol):
    """What the runner needs. Tests supply a fake implementing only this."""

    async def complete(
        self,
        spec: ModelSpec,
        messages: list[dict[str, str]],
        mode: StructuredMode,
        schema: dict[str, Any] | None,
    ) -> ModelResponse: ...


def model_spec(config: AnalystConfig | GroundTruthConfig) -> ModelSpec:
    """The one place a roster entry becomes a call spec."""
    return ModelSpec(
        provider=config.provider,
        model=config.model,
        max_output_tokens=config.max_output_tokens,
    )


def takes_temperature(model: str) -> bool:
    """Whether ``model`` accepts a temperature, or fixes its own.

    Judged on the bare name, so an OpenRouter id ("anthropic/claude-sonnet-5")
    answers as the model behind it.
    """
    return not _NO_TEMPERATURE.match(model.split("/")[-1])


def api_key_env(provider: Provider) -> str:
    return _API_KEY_ENV[provider]


def require_api_keys(providers: Iterable[Provider]) -> None:
    """Fail before any call, naming every missing variable."""
    missing = sorted(
        {api_key_env(p) for p in providers if not os.environ.get(api_key_env(p))}
    )
    if missing:
        raise MissingApiKeyError(
            "missing API key environment variable(s): " + ", ".join(missing)
        )


def _split_system(
    messages: list[dict[str, str]],
) -> tuple[str | None, list[dict[str, str]]]:
    """Anthropic and Google take the system prompt apart from the turns."""
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system") or None
    return system, [m for m in messages if m["role"] != "system"]


# strict=False deliberately. Strict mode requires every property to be in
# `required` and every object to set additionalProperties=false; these schemas
# violate the first by design, because optional fields are how they record what
# a source did not report. A strict request is rejected outright.


def openai_request(
    spec: ModelSpec,
    messages: list[dict[str, str]],
    mode: StructuredMode,
    schema: dict[str, Any] | None,
) -> dict[str, Any]:
    """Chat Completions arguments for OpenAI, OpenRouter and DeepSeek."""
    request: dict[str, Any] = {"model": spec.model, "messages": messages}
    # OpenAI's reasoning models reject `max_tokens`; the others document only it.
    cap = "max_completion_tokens" if spec.provider is Provider.openai else "max_tokens"
    request[cap] = spec.max_output_tokens
    if takes_temperature(spec.model):
        request["temperature"] = spec.temperature
    if mode is StructuredMode.json_schema and schema is not None:
        request["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "assessment", "schema": schema, "strict": False},
        }
    elif mode is StructuredMode.json_object:
        request["response_format"] = {"type": "json_object"}
    if spec.provider is Provider.openrouter and "response_format" in request:
        # Otherwise OpenRouter treats the format as a preference and may route
        # to an endpoint that ignores it.
        request["extra_body"] = {"provider": {"require_parameters": True}}
    return request


#: JSON Schema bounds Anthropic's grammar rejects; they move into the description.
_UNSUPPORTED_BOUNDS = (
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "minItems", "maxItems",
)


def _anthropic_schema(node: Any) -> Any:
    """Close every object and move unsupported bounds into descriptions.

    Mirrors what the SDK's ``transform_schema`` does, locally, so building a
    request needs no SDK. Pydantic validates the reply against the real model.
    """
    if isinstance(node, list):
        return [_anthropic_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    out = {k: _anthropic_schema(v) for k, v in node.items() if k not in _UNSUPPORTED_BOUNDS}
    if out.get("type") == "object" or "properties" in out:
        out["additionalProperties"] = False
    moved = [f"{k}: {node[k]}" for k in _UNSUPPORTED_BOUNDS if k in node]
    if moved:
        note = "Bounds: " + ", ".join(moved) + "."
        out["description"] = f"{out['description']} {note}" if out.get("description") else note
    return out


def anthropic_request(
    spec: ModelSpec,
    messages: list[dict[str, str]],
    mode: StructuredMode,
    schema: dict[str, Any] | None,
) -> dict[str, Any]:
    """Messages API arguments; the coroutine streams them.

    The schema tier uses native structured output, never a forced tool call.
    Anthropic has no schema-less JSON mode, so ``json_object`` sends nothing
    extra and relies on the schema the runner puts in the prompt.
    """
    system, turns = _split_system(messages)
    request: dict[str, Any] = {
        "model": spec.model,
        "messages": turns,
        "max_tokens": spec.max_output_tokens,
    }
    if system:
        request["system"] = system
    if takes_temperature(spec.model):
        # SDK 1.x dropped the sampling keywords, but the API still honours them
        # on Haiku 4.5 and the 4.6 models.
        request["extra_body"] = {"temperature": spec.temperature}
    if mode is StructuredMode.json_schema and schema is not None:
        request["output_config"] = {
            "format": {"type": "json_schema", "schema": _anthropic_schema(copy.deepcopy(schema))}
        }
    return request


def google_request(
    spec: ModelSpec,
    messages: list[dict[str, str]],
    mode: StructuredMode,
    schema: dict[str, Any] | None,
) -> dict[str, Any]:
    """``generate_content`` arguments; ``config`` becomes a ``GenerateContentConfig``."""
    system, turns = _split_system(messages)
    config: dict[str, Any] = {"max_output_tokens": spec.max_output_tokens}
    if system:
        config["system_instruction"] = system
    if takes_temperature(spec.model):
        config["temperature"] = spec.temperature
    if mode is StructuredMode.json_schema and schema is not None:
        config["response_mime_type"] = "application/json"
        config["response_json_schema"] = schema
    elif mode is StructuredMode.json_object:
        config["response_mime_type"] = "application/json"
    contents = [
        {"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
        for m in turns
    ]
    return {"model": spec.model, "contents": contents, "config": config}


def google_output_tokens(usage: Any) -> int | None:
    """Gemini bills thinking as output, as OpenAI and Anthropic count it."""
    if usage is None or usage.candidates_token_count is None:
        return None
    return usage.candidates_token_count + (usage.thoughts_token_count or 0)


class GeminiAPIError(Exception):
    """A Gemini API error in the shape the runner classifies by.

    The Anthropic and OpenAI SDKs raise errors with an integer ``status_code``;
    Google's carry it as ``code``. ``body`` takes the Anthropic SDK's shape.
    """

    def __init__(self, status_code: int, message: str, provider_message: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = {"error": {"message": provider_message}} if provider_message else None

    @classmethod
    def from_sdk(cls, exc: Any) -> GeminiAPIError:
        return cls(exc.code, str(exc), getattr(exc, "message", None))


_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def extract_json(text: str) -> dict[str, Any]:
    """Pull a JSON object out of model output that may be wrapped in prose.

    Used only in ``text`` mode, where the provider gave no structural guarantee.
    """
    match = _FENCE.search(text)
    if match:
        return json.loads(match.group(1))
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError("no JSON object found in model output")


class SdkProvider:
    """Real provider: each vendor's own SDK. Never exercised by the test suite.

    One client per provider, built on first use from the environment and reused
    for the run.
    """

    def __init__(self) -> None:
        self._clients: dict[Provider, Any] = {}

    def _key(self, provider: Provider) -> str:
        require_api_keys([provider])
        return os.environ[api_key_env(provider)]

    async def complete(
        self,
        spec: ModelSpec,
        messages: list[dict[str, str]],
        mode: StructuredMode,
        schema: dict[str, Any] | None,
    ) -> ModelResponse:
        if spec.provider is Provider.anthropic:
            return await self._complete_anthropic(spec, messages, mode, schema)
        if spec.provider is Provider.google:
            return await self._complete_google(spec, messages, mode, schema)
        return await self._complete_openai(spec, messages, mode, schema)

    async def _complete_openai(
        self,
        spec: ModelSpec,
        messages: list[dict[str, str]],
        mode: StructuredMode,
        schema: dict[str, Any] | None,
    ) -> ModelResponse:
        from openai import AsyncOpenAI

        client = self._clients.get(spec.provider)
        if client is None:
            client = self._clients[spec.provider] = AsyncOpenAI(
                api_key=self._key(spec.provider),
                base_url=BASE_URLS[spec.provider],
                max_retries=0,
                timeout=TIMEOUT_SECONDS,
            )
        response = await client.chat.completions.create(
            **openai_request(spec, messages, mode, schema)
        )
        choice = response.choices[0]
        usage = response.usage
        return ModelResponse(
            text=choice.message.content or "",
            truncated=choice.finish_reason == "length",
            input_tokens=usage.prompt_tokens if usage else None,
            output_tokens=usage.completion_tokens if usage else None,
        )

    async def _complete_anthropic(
        self,
        spec: ModelSpec,
        messages: list[dict[str, str]],
        mode: StructuredMode,
        schema: dict[str, Any] | None,
    ) -> ModelResponse:
        from anthropic import AsyncAnthropic

        client = self._clients.get(spec.provider)
        if client is None:
            client = self._clients[spec.provider] = AsyncAnthropic(
                api_key=self._key(spec.provider), max_retries=0, timeout=TIMEOUT_SECONDS
            )
        # Streamed: the SDK refuses a large non-streaming `max_tokens`.
        async with client.messages.stream(
            **anthropic_request(spec, messages, mode, schema)
        ) as stream:
            message = await stream.get_final_message()
        # Thinking blocks come first on models that think; only text is the answer.
        text = "".join(block.text for block in message.content if block.type == "text")
        return ModelResponse(
            text=text,
            truncated=message.stop_reason == "max_tokens",
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
        )

    async def _complete_google(
        self,
        spec: ModelSpec,
        messages: list[dict[str, str]],
        mode: StructuredMode,
        schema: dict[str, Any] | None,
    ) -> ModelResponse:
        from google import genai
        from google.genai import errors, types

        client = self._clients.get(spec.provider)
        if client is None:
            client = self._clients[spec.provider] = genai.Client(
                api_key=self._key(spec.provider),
                http_options=types.HttpOptions(
                    timeout=TIMEOUT_SECONDS * 1000,
                    retry_options=types.HttpRetryOptions(attempts=1),
                ),
            )
        request = google_request(spec, messages, mode, schema)
        try:
            response = await client.aio.models.generate_content(
                model=request["model"],
                contents=request["contents"],
                config=types.GenerateContentConfig(
                    **request["config"],
                    # No tools are passed; off, so the SDK neither loops nor warns.
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(
                        disable=True
                    ),
                ),
            )
        except errors.APIError as exc:
            raise GeminiAPIError.from_sdk(exc) from None
        candidate = response.candidates[0] if response.candidates else None
        usage = response.usage_metadata
        return ModelResponse(
            text=response.text or "",
            truncated=candidate is not None
            and candidate.finish_reason == types.FinishReason.MAX_TOKENS,
            input_tokens=usage.prompt_token_count if usage else None,
            output_tokens=google_output_tokens(usage),
        )

    async def aclose(self) -> None:
        """Close the clients that hold connections; call once at the end of a run."""
        for provider, client in self._clients.items():
            if provider is Provider.google:
                await client.aio.aclose()
            else:
                await client.close()
        self._clients.clear()
