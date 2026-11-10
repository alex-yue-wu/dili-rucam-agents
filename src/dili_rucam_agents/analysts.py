"""Score one case with every analyst on the roster, without an agent executor.

This is where the layers meet: the roster says who scores, the contract says
what each is sent, the runner calls the provider and validates the reply, the
scorer computes total and category, and the renderer writes the report.

Analysts run one after another. Each gets a fresh prompt built from the shared
case bundle alone, so none can see another's output. The layer is ``gather``-
ready, but fan-out is off by design.

This module talks to the ``CompletionProvider`` protocol and imports no vendor
SDK.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Literal

from dili_rucam_agents.checkpoints import atomic_write_text
from dili_rucam_agents.contracts import (
    AnalystInstructionContract,
    RETRY_REQUIREMENT_PLACEHOLDER,
    build_analyst_instruction_contract,
)
from dili_rucam_agents.diagnostics import (
    FailureKind,
    SafeExecutionDiagnostic,
    SafeValidationDiagnostic,
    render_execution_diagnostic,
    render_validation_diagnostic,
)
from dili_rucam_agents.providers import (
    CompletionProvider,
    extract_json,
    model_spec,
    require_api_keys,
)
from dili_rucam_agents.rendering import render_report
from dili_rucam_agents.roster import AnalystConfig, AnalystRoster
from dili_rucam_agents.runner import (
    Attempt,
    RetryCounters,
    RunnerError,
    run_contract,
    validate_max_restarts,
)
from dili_rucam_agents.validators.analyst_report import (
    AnalystReportValidationError,
    validate_analyst_report,
)
from dili_rucam_agents.validators.rucam_assessment import (
    RucamAssessment,
    score_assessment,
)

AttemptStatus = Literal["running", "validation_failed", "execution_failed", "completed"]

_TRUNCATED = SafeValidationDiagnostic((("report", "report_truncated"),))
_INVALID = SafeValidationDiagnostic((("report", "invalid_report"),))


@dataclass(frozen=True)
class AnalystAttemptEvent:
    """One step of one analyst's run. Attempts count runner attempts, from 1."""

    analyst_key: str
    attempt: int
    max_attempts: int
    status: AttemptStatus
    report_text: str | None = None
    counters: RetryCounters | None = None
    execution_diagnostic: SafeExecutionDiagnostic | None = None
    validation_diagnostic: SafeValidationDiagnostic | None = None


@dataclass
class AnalystRunOutcome:
    """Reports and sidecars of the analysts that ran now.

    An analyst whose completed report was passed in is in ``reports`` only: it
    was not re-scored, so there is no assessment or counter set to report.
    """

    reports: dict[str, str] = field(default_factory=dict)
    assessments: dict[str, dict] = field(default_factory=dict)
    counters: dict[str, RetryCounters] = field(default_factory=dict)


class AnalystExecutionError(RuntimeError):
    def __init__(
        self,
        *,
        analyst_key: str,
        attempts: int,
        failure_kind: FailureKind,
        counters: RetryCounters | None = None,
        execution_diagnostic: SafeExecutionDiagnostic | None = None,
        validation_diagnostic: SafeValidationDiagnostic | None = None,
    ) -> None:
        self.analyst_key = analyst_key
        self.attempts = attempts
        self.failure_kind = failure_kind
        self.counters = counters
        self.execution_diagnostic = execution_diagnostic
        self.validation_diagnostic = validation_diagnostic
        #: Filled in by ``run_analysts``: every report finished before this
        #: failure, so a caller without ``on_report`` still keeps paid work.
        self.outcome: AnalystRunOutcome | None = None
        if execution_diagnostic is not None:
            self.last_error = render_execution_diagnostic(execution_diagnostic)
        else:
            self.last_error = render_validation_diagnostic(
                validation_diagnostic or _INVALID
            )
        super().__init__(
            f"{analyst_key} failed after {attempts} attempts "
            f"({failure_kind}): {self.last_error}"
        )


def run_analysts(
    *,
    case_bundle_json: str,
    roster: AnalystRoster,
    prompt_text: str,
    bundle_input_name: str,
    provider: CompletionProvider,
    max_retries: int = 2,
    completed_reports: Mapping[str, str] | None = None,
    on_report: Callable[[str, str, dict], None] | None = None,
    on_attempt: Callable[[AnalystAttemptEvent], None] | None = None,
    output_dir: Path | None = None,
    instruction_contracts: Mapping[str, AnalystInstructionContract] | None = None,
) -> AnalystRunOutcome:
    """Run every roster analyst that has no valid completed report yet.

    ``on_report(key, report_text, assessment)`` fires as each analyst finishes,
    so a caller can persist it before the next one starts. When ``output_dir``
    is given, each rejected reply is kept under ``attempts/`` as
    ``<key>.<n>.invalid.json`` (a JSON object parsed but broke the schema) or
    ``.invalid.txt`` (nothing parsed). The first failure raises
    ``AnalystExecutionError``; analysts already finished stay with the caller
    through ``on_report`` and ``AnalystExecutionError.outcome``.
    ``instruction_contracts`` supplies the contract sent for a key (so a caller
    that fingerprints the contract sends the very one it hashed); the others
    are built here.
    """

    # Both before any call, so a bad budget or a missing key costs nothing.
    max_retries = validate_max_restarts(max_retries)
    require_api_keys({config.provider for config in roster.analysts})

    outcome = AnalystRunOutcome()
    supplied = completed_reports or {}
    for config in roster.analysts:
        report = supplied.get(config.key)
        if not isinstance(report, str):
            continue
        try:
            validate_analyst_report(report)
        except AnalystReportValidationError:
            continue
        outcome.reports[config.key] = report

    pending = [c for c in roster.analysts if c.key not in outcome.reports]
    if pending:
        try:
            asyncio.run(
                _run_pending(
                    pending=pending,
                    outcome=outcome,
                    case_bundle_json=case_bundle_json,
                    prompt_text=prompt_text,
                    bundle_input_name=bundle_input_name,
                    provider=provider,
                    max_retries=max_retries,
                    on_report=on_report,
                    on_attempt=on_attempt,
                    output_dir=output_dir,
                    instruction_contracts=instruction_contracts or {},
                )
            )
        except AnalystExecutionError as exc:
            exc.outcome = outcome
            raise
    # Roster order, whether the report was supplied or scored now.
    outcome.reports = {c.key: outcome.reports[c.key] for c in roster.analysts}
    return outcome


async def _run_pending(*, pending: list[AnalystConfig], **kwargs) -> None:
    for config in pending:
        await _run_one(config=config, **kwargs)


async def _run_one(
    *,
    config: AnalystConfig,
    outcome: AnalystRunOutcome,
    case_bundle_json: str,
    prompt_text: str,
    bundle_input_name: str,
    provider: CompletionProvider,
    max_retries: int,
    on_report: Callable[[str, str, dict], None] | None,
    on_attempt: Callable[[AnalystAttemptEvent], None] | None,
    output_dir: Path | None,
    instruction_contracts: Mapping[str, AnalystInstructionContract],
) -> None:
    key = config.key
    max_attempts = max_retries + 1
    contract = instruction_contracts.get(key) or build_analyst_instruction_contract(
        analyst_label=config.label,
        prompt_text=prompt_text,
        model_name=config.model,
        bundle_input_name=bundle_input_name,
    )
    # The runner appends its own correction on a retry, so the contract's retry
    # block stays empty.
    instructions = contract.render(
        **{bundle_input_name: case_bundle_json, RETRY_REQUIREMENT_PLACEHOLDER: ""}
    )

    def emit(attempt: int, status: AttemptStatus, **fields) -> None:
        if on_attempt:
            on_attempt(
                AnalystAttemptEvent(
                    analyst_key=key,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    status=status,
                    **fields,
                )
            )

    current = 1  # the runner attempt in flight
    truncations = 0

    def on_runner_attempt(attempt: Attempt) -> None:
        nonlocal current, truncations
        if attempt.outcome == "truncated":
            # Not counted as a runner attempt, so it can share a number with a
            # later validation failure: the stem says which it was. The cut-off
            # text shows how far the model got before the budget ran out.
            truncations += 1
            if output_dir is not None and attempt.raw_text is not None:
                _persist_invalid_attempt(
                    output_dir,
                    key,
                    f"{attempt.number}.truncated{truncations}",
                    attempt.raw_text,
                    force_text=True,
                )
            return  # the event comes from the error
        if attempt.outcome != "validation_failed":
            return  # success: reported after the run
        if output_dir is not None and attempt.raw_text is not None:
            _persist_invalid_attempt(output_dir, key, attempt.number, attempt.raw_text)
        if attempt.number >= max_attempts:
            # The last attempt's failure ends the run, so its event is emitted
            # from the error handler below instead, where the retry counters
            # exist. A failed analyst is where they matter most: it spent its
            # whole validation budget and nothing else records how.
            return
        emit(attempt.number, "validation_failed", validation_diagnostic=_INVALID)
        current = attempt.number + 1
        emit(current, "running")

    emit(current, "running")
    try:
        result = await run_contract(
            instructions=instructions,
            model_class=RucamAssessment,
            spec=model_spec(config),
            provider=provider,
            max_retries=max_retries,
            on_attempt=on_runner_attempt,
        )
    except RunnerError as exc:
        # Every terminal event carries the counters, so a caller that persists
        # them records what the failed run actually spent.
        if exc.kind in ("execution", "terminal"):
            emit(
                current,
                "execution_failed",
                counters=exc.counters,
                execution_diagnostic=exc.execution_diagnostic,
            )
        elif exc.kind == "truncation":
            emit(
                current,
                "validation_failed",
                counters=exc.counters,
                validation_diagnostic=_TRUNCATED,
            )
        else:  # the validation budget ran out on the attempt in flight
            emit(
                current,
                "validation_failed",
                counters=exc.counters,
                validation_diagnostic=exc.validation_diagnostic or _INVALID,
            )
        raise AnalystExecutionError(
            analyst_key=key,
            attempts=exc.attempts,
            failure_kind=exc.kind,
            counters=exc.counters,
            execution_diagnostic=exc.execution_diagnostic,
            validation_diagnostic=(
                _TRUNCATED if exc.kind == "truncation" else exc.validation_diagnostic
            ),
        ) from exc

    assessment: RucamAssessment = result.value
    report_text = render_report(
        assessment=assessment,
        scored=score_assessment(assessment),
        analyst_label=config.label,
        model_name=config.model,
    )
    try:
        validate_analyst_report(report_text)
    except AnalystReportValidationError as exc:
        raise AnalystExecutionError(
            analyst_key=key,
            attempts=len(result.attempts),
            failure_kind="validation",
            counters=result.counters,
            validation_diagnostic=exc.diagnostic,
        ) from exc

    sidecar = assessment.model_dump(mode="json")
    outcome.reports[key] = report_text
    outcome.assessments[key] = sidecar
    outcome.counters[key] = result.counters
    emit(current, "completed", report_text=report_text, counters=result.counters)
    if on_report:
        on_report(key, report_text, sidecar)


def _persist_invalid_attempt(
    output_dir: Path, key: str, number: int | str, raw: str, *, force_text: bool = False
) -> None:
    """Keep a rejected reply: as JSON when an object parsed, as text otherwise."""
    parsed = None if force_text else _parse_object(raw)
    stem = output_dir / "attempts" / f"{key}.{number}.invalid"
    if parsed is not None:
        atomic_write_text(
            stem.with_name(stem.name + ".json"),
            json.dumps(parsed, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        )
    else:
        atomic_write_text(stem.with_name(stem.name + ".txt"), raw)


def _parse_object(raw: str) -> dict | None:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        try:
            value = extract_json(raw)
        except ValueError:  # JSONDecodeError is a ValueError too
            return None
    return value if isinstance(value, dict) else None


__all__ = [
    "AnalystAttemptEvent",
    "AnalystExecutionError",
    "AnalystRunOutcome",
    "run_analysts",
]
