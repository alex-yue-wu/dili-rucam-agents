"""The loop above the provider: render, call, parse, validate, retry.

Four retry budgets, deliberately separate, so one kind of failure cannot spend
another kind's chances:

* validation: the reply parsed but broke the schema (``max_retries``);
* transient: a provider error worth retrying, with backoff;
* truncation: the output budget ran out, so one escalation to a wider budget;
* mode downgrade: the provider refused the structured-output request, so step
  down the ladder.

Only validation failures are appended to ``attempts``. The other three
``continue`` without doing so, which is why ``RetryCounters`` exists: a counter
derived from ``attempts`` cannot see them.

This module talks to the ``CompletionProvider`` protocol and imports no vendor
SDK.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import json
import random
from typing import Any

from pydantic import BaseModel, ValidationError

from dili_rucam_agents.diagnostics import (
    FailureKind,
    SafeExecutionDiagnostic,
    SafeValidationDiagnostic,
    build_execution_diagnostic,
)
from dili_rucam_agents.providers import (
    SUPPORTED_MODES,
    CompletionProvider,
    ModelSpec,
    StructuredMode,
    extract_json,
)

#: Status codes where retrying cannot help: bad key, bad model, bad request.
TERMINAL_STATUS_CODES: frozenset[int] = frozenset({400, 401, 403, 404, 422})

#: Consecutive transient errors retried before giving up; the next one raises.
_MAX_TRANSIENT_RETRIES = 3
_TRUNCATION_MULTIPLIER = 2

_BACKOFF_BASE_SECONDS = 0.5
_BACKOFF_CAP_SECONDS = 4.0
_BACKOFF_JITTER = 0.1

#: Phrases an endpoint uses to say it cannot honour a structured-output request.
_FORMAT_MARKERS = (
    "response_format",
    "requested parameters",
    "json_schema",
    "response_schema",
    "input_schema",
    "response_mime_type",
    "structured output",
)


@dataclass(frozen=True)
class Attempt:
    number: int
    mode: StructuredMode
    outcome: str
    raw_text: str | None = None


@dataclass(frozen=True)
class RetryCounters:
    validation_retries: int
    transient_retries: int
    truncation_escalations: int
    mode_downgrades: int
    final_mode: StructuredMode


@dataclass(frozen=True)
class RunResult:
    value: Any
    attempts: list[Attempt]
    counters: RetryCounters
    #: Summed over every call, retries included; None when nothing was reported.
    input_tokens: int | None
    output_tokens: int | None


class RunnerError(Exception):
    """A contract that could not be satisfied. Carries only safe diagnostics."""

    def __init__(
        self,
        *,
        kind: FailureKind,
        attempts: int,
        counters: RetryCounters,
        execution_diagnostic: SafeExecutionDiagnostic | None = None,
        validation_diagnostic: SafeValidationDiagnostic | None = None,
    ) -> None:
        self.kind = kind
        self.attempts = attempts
        self.counters = counters
        self.execution_diagnostic = execution_diagnostic
        self.validation_diagnostic = validation_diagnostic
        detail = ""
        if validation_diagnostic is not None:
            detail = ", ".join(f"{path}:{code}" for path, code in validation_diagnostic.issues)
        elif execution_diagnostic is not None:
            detail = execution_diagnostic.exception_type
        super().__init__(f"{kind} failure after {attempts} attempt(s): {detail}".rstrip(": "))


def _status_code(exc: BaseException) -> int | None:
    code = getattr(exc, "status_code", None)
    return code if isinstance(code, int) else None


def _error_text(exc: BaseException) -> str:
    """The error's message plus the provider's own, when the SDK keeps one."""
    text = str(exc)
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        message = error.get("message") if isinstance(error, dict) else body.get("message")
        if isinstance(message, str):
            text = f"{text} {message}"
    return text.lower()


def _is_format_rejection(exc: BaseException) -> bool:
    """Did the provider reject the structured-output request specifically?

    Every status needs a marker in the message: a 400 for another cause (a
    context length, say) would only recur on every lower mode. OpenRouter
    answers 404 (sometimes 422) when no endpoint can honour ``response_format``,
    and a 404 for a wrong model id ("No endpoints found for <model>") must stay
    terminal.
    """
    status = _status_code(exc)
    if status in (400, 404, 422, None):
        text = _error_text(exc)
        return any(marker in text for marker in _FORMAT_MARKERS)
    return False


def _backoff_seconds(errors_so_far: int) -> float:
    delay = min(_BACKOFF_BASE_SECONDS * 2**errors_so_far, _BACKOFF_CAP_SECONDS)
    return delay + random.uniform(0, delay * _BACKOFF_JITTER)


def _with_schema(instructions: str, schema: dict[str, Any]) -> list[dict[str, str]]:
    """The prompt, ending in the response schema.

    Native ``json_schema`` carries the schema only on its own tier, and the
    lower tiers send none, so the prompt carries it in every mode.
    """
    rendered = json.dumps(schema, indent=2, sort_keys=True)
    block = (
        "\n\n--- BEGIN RESPONSE SCHEMA (JSON Schema) ---\n"
        f"{rendered}\n"
        "--- END RESPONSE SCHEMA ---\n"
        "Return exactly one JSON object conforming to this schema. "
        "Use only the property names it defines. "
        "Omit an optional property rather than inventing a value for it."
    )
    return [{"role": "user", "content": instructions + block}]


def _parse(text: str) -> Any:
    """The reply's JSON object, in every mode: JSON mode is not always a guarantee."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return extract_json(text)


def _correction(exc: ValidationError | ValueError) -> str:
    """What to tell the model, built from locations and messages, never inputs."""
    if isinstance(exc, ValidationError):
        issues = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'reply'}: {e['msg']}"
            for e in exc.errors(include_url=False, include_context=False, include_input=False)[:16]
        )
        problem = f"It did not match the schema: {issues}."
    else:
        problem = "It was not a single valid JSON object."
    return (
        f"Your previous reply was rejected. {problem} "
        "Return the complete corrected JSON object, keeping every value that was "
        "not at fault exactly as you wrote it."
    )


def _validation_diagnostic(exc: ValidationError | ValueError) -> SafeValidationDiagnostic:
    code = "invalid_value" if isinstance(exc, ValidationError) else "invalid_report"
    return SafeValidationDiagnostic((("report", code),))


async def run_contract(
    *,
    instructions: str,
    model_class: type[BaseModel],
    spec: ModelSpec,
    provider: CompletionProvider,
    max_retries: int = 2,
    max_output_ceiling: int = 64000,
    on_attempt: Callable[[Attempt], None] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> RunResult:
    """Call, parse, validate and retry until the reply fits ``model_class``.

    ``max_retries`` bounds validation retries only. Transient errors, truncation
    and mode downgrades each have a budget of their own. ``sleep`` backs the
    transient backoff; tests inject a recorder.
    """
    schema = model_class.model_json_schema()
    base_messages = _with_schema(instructions, schema)
    ladder = SUPPORTED_MODES[spec.provider]

    attempts: list[Attempt] = []
    current_spec = spec
    mode_index = 0
    transient_errors = transient_retries = 0
    truncation_escalations = mode_downgrades = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    correction: str | None = None
    previous_reply = ""
    last_validation: SafeValidationDiagnostic | None = None

    def counters(*, finished: bool = False) -> RetryCounters:
        # `attempts` holds only validation failures, plus the success when there
        # is one. A call in flight after k failures is retry number k; once the
        # run is over, the last entry was a final answer, not a retry.
        return RetryCounters(
            validation_retries=len(attempts) - 1 if finished else len(attempts),
            transient_retries=transient_retries,
            truncation_escalations=truncation_escalations,
            mode_downgrades=mode_downgrades,
            final_mode=ladder[mode_index],
        )

    def record(attempt: Attempt, *, keep: bool) -> None:
        if keep:
            attempts.append(attempt)
        if on_attempt:
            on_attempt(attempt)

    while len(attempts) <= max_retries:
        number = len(attempts) + 1
        mode = ladder[mode_index]
        messages = list(base_messages)
        if correction:
            # The reply goes back with the correction: "keep every other value"
            # means nothing to a model that cannot see those values.
            messages += [
                {"role": "assistant", "content": previous_reply.strip() or "(empty reply)"},
                {"role": "user", "content": correction},
            ]

        try:
            response = await provider.complete(
                current_spec, messages, mode, schema if mode is StructuredMode.json_schema else None
            )
        except Exception as exc:  # noqa: BLE001 - classified immediately below
            if _is_format_rejection(exc) and mode_index < len(ladder) - 1:
                mode_index += 1
                mode_downgrades += 1
                continue
            if _status_code(exc) in TERMINAL_STATUS_CODES:
                raise RunnerError(
                    kind="terminal",
                    attempts=number,
                    counters=counters(),
                    execution_diagnostic=build_execution_diagnostic(exc),
                ) from None
            transient_errors += 1
            if transient_errors > _MAX_TRANSIENT_RETRIES:
                raise RunnerError(
                    kind="execution",
                    attempts=number,
                    counters=counters(),
                    execution_diagnostic=build_execution_diagnostic(exc),
                ) from None
            await sleep(_backoff_seconds(transient_errors - 1))
            transient_retries += 1
            continue

        transient_errors = 0  # the stall, if there was one, is over
        if response.input_tokens is not None:
            input_tokens = (input_tokens or 0) + response.input_tokens
        if response.output_tokens is not None:
            output_tokens = (output_tokens or 0) + response.output_tokens

        if response.truncated:
            record(Attempt(number, mode, "truncated", response.text), keep=False)
            if truncation_escalations >= 1 or current_spec.max_output_tokens >= max_output_ceiling:
                # Already widened, or nowhere left to widen: another call would
                # spend as much and end the same way.
                raise RunnerError(kind="truncation", attempts=number, counters=counters())
            truncation_escalations += 1
            current_spec = current_spec.model_copy(
                update={
                    "max_output_tokens": min(
                        current_spec.max_output_tokens * _TRUNCATION_MULTIPLIER,
                        max_output_ceiling,
                    )
                }
            )
            continue

        try:
            value = model_class.model_validate(_parse(response.text))
        except (ValidationError, ValueError) as exc:
            previous_reply = response.text
            correction = _correction(exc)
            last_validation = _validation_diagnostic(exc)
            record(Attempt(number, mode, "validation_failed", response.text), keep=True)
            continue

        record(Attempt(number, mode, "completed"), keep=True)
        return RunResult(
            value=value,
            attempts=attempts,
            counters=counters(finished=True),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    raise RunnerError(
        kind="validation",
        attempts=len(attempts),
        counters=counters(finished=True),
        validation_diagnostic=last_validation,
    )


__all__ = [
    "TERMINAL_STATUS_CODES",
    "Attempt",
    "RetryCounters",
    "RunResult",
    "RunnerError",
    "run_contract",
]
