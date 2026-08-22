from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Dict, Literal, Optional, Tuple

from crewai import Crew, Process, Task

from dili_rucam_agents.diagnostics import (
    FailureKind,
    SafeExecutionDiagnostic,
    SafeValidationDiagnostic,
    build_execution_diagnostic,
    format_execution_error,
    render_validation_diagnostic,
    validate_execution_diagnostic,
    validate_failure_kind,
    validate_validation_diagnostic,
)
from dili_rucam_agents.ingestion.build_bundle import build_case_bundle
from dili_rucam_agents.masking import mask_case_bundle_payload
from dili_rucam_agents.validators.analyst_report import (
    AnalystReportValidationError,
    validate_analyst_report,
)

from .agents import (
    build_ground_truth_rucam_score_finder_agent,
    build_ingestion_agent,
    build_rucam_agent,
    resolve_rucam_model,
)
from .config import get_enabled_analyst_configs
from .tasks import (
    AnalystInstructionContract,
    build_analyst_instruction_contract,
    build_ground_truth_score_finder_prompt,
    create_analysis_task,
    create_case_bundle_task,
    load_rucam_prompt,
)

TaskMap = Dict[str, Task]

AttemptStatus = Literal["running", "validation_failed", "execution_failed", "completed"]
_ATTEMPT_STATUSES = frozenset(
    ("running", "validation_failed", "execution_failed", "completed")
)


@dataclass(frozen=True)
class AnalystAttemptEvent:
    analyst_key: str
    attempt: int
    max_attempts: int
    status: AttemptStatus
    report_text: str | None = None
    execution_diagnostic: SafeExecutionDiagnostic | None = None
    validation_diagnostic: SafeValidationDiagnostic | None = None

    @property
    def error(self) -> str | None:
        if self.validation_diagnostic is None:
            return None
        return render_validation_diagnostic(self.validation_diagnostic)

    def __post_init__(self) -> None:
        if type(self.analyst_key) is not str:
            raise TypeError("analyst_key must be a string")
        if not self.analyst_key:
            raise ValueError("analyst_key must not be empty")
        if type(self.attempt) is not int:
            raise TypeError("attempt must be an integer")
        if type(self.max_attempts) is not int:
            raise TypeError("max_attempts must be an integer")
        if (
            self.attempt < 1
            or self.max_attempts < 1
            or self.attempt > self.max_attempts
        ):
            raise ValueError("attempt must be between 1 and max_attempts")
        if type(self.status) is not str or self.status not in _ATTEMPT_STATUSES:
            raise ValueError("status must be a supported analyst attempt status")
        if self.report_text is not None and type(self.report_text) is not str:
            raise TypeError("report_text must be a string or None")

        if self.status == "running":
            if (
                self.report_text is not None
                or self.execution_diagnostic is not None
                or self.validation_diagnostic is not None
            ):
                raise ValueError("running events cannot carry result data")
            return
        if self.status == "validation_failed":
            if self.report_text is not None:
                raise ValueError("validation failure events cannot carry report_text")
            if self.execution_diagnostic is not None:
                raise ValueError(
                    "validation failure events cannot carry execution diagnostics"
                )
            if self.validation_diagnostic is None:
                raise ValueError(
                    "validation failure events require a validation diagnostic"
                )
            object.__setattr__(
                self,
                "validation_diagnostic",
                validate_validation_diagnostic(self.validation_diagnostic),
            )
            return
        if self.status == "execution_failed":
            if self.report_text is not None or self.validation_diagnostic is not None:
                raise ValueError(
                    "execution failure events cannot carry validation result data"
                )
            if self.execution_diagnostic is None:
                raise ValueError(
                    "execution failure events require an execution diagnostic"
                )
            object.__setattr__(
                self,
                "execution_diagnostic",
                validate_execution_diagnostic(self.execution_diagnostic),
            )
            return
        if self.report_text is None:
            raise ValueError("completed events require report_text")
        if (
            self.execution_diagnostic is not None
            or self.validation_diagnostic is not None
        ):
            raise ValueError("completed events cannot carry failure diagnostics")


class AnalystExecutionError(RuntimeError):
    def __init__(
        self,
        *,
        analyst_key: str,
        attempts: int,
        failure_kind: FailureKind,
        last_error: str = "",
        last_exception: BaseException | None = None,
        validation_diagnostic: SafeValidationDiagnostic | None = None,
    ) -> None:
        self.analyst_key = analyst_key
        self.attempts = attempts
        self.failure_kind = validate_failure_kind(failure_kind)
        execution_exception = (
            last_exception if last_exception is not None else Exception()
        )
        if self.failure_kind == "execution":
            if validation_diagnostic is not None:
                raise ValueError(
                    "execution failures cannot carry validation diagnostics"
                )
            self.validation_diagnostic = None
            self.last_error = format_execution_error(execution_exception)
        else:
            source = validation_diagnostic or SafeValidationDiagnostic(
                (("report", "invalid_report"),)
            )
            self.validation_diagnostic = validate_validation_diagnostic(source)
            self.last_error = render_validation_diagnostic(self.validation_diagnostic)
        super().__init__(
            f"{analyst_key} failed after {attempts} attempts "
            f"({self.failure_kind}): {self.last_error}"
        )


def validate_max_restarts(max_restarts: int) -> int:
    if (
        isinstance(max_restarts, bool)
        or not isinstance(max_restarts, int)
        or not 0 <= max_restarts <= 2
    ):
        raise ValueError("max_restarts must be an integer from 0 through 2")
    return max_restarts


def build_crew(
    pdf_path: str,
    prompt_path: Optional[Path] = None,
    *,
    strict_scoring: bool = False,
    use_analyst_delta: bool = False,
    use_analyst_epsilon: bool = False,
    use_analyst_zeta: bool = False,
    use_analyst_eta: bool = False,
) -> Tuple[Crew, TaskMap]:
    prompt_text = load_rucam_prompt(prompt_path, strict_scoring=strict_scoring)

    ingestion_agent = build_ingestion_agent()
    analyst_configs = get_enabled_analyst_configs(
        use_analyst_delta=use_analyst_delta,
        use_analyst_epsilon=use_analyst_epsilon,
        use_analyst_zeta=use_analyst_zeta,
        use_analyst_eta=use_analyst_eta,
    )
    for config in analyst_configs:
        model_name = resolve_rucam_model(
            model_env=config["model_env"],
            fallback_envs=config["fallback_envs"],
            default_model=config["default_model"],
        )
        instruction_contract = build_analyst_instruction_contract(
            analyst_label=config["label"],
            prompt_text=prompt_text,
            model_name=model_name,
            bundle_input_name="prepared_case_bundle_json",
        )
        config["agent"] = build_rucam_agent(
            label=config["label"],
            model_env=config["model_env"],
            max_tokens_env=config["max_tokens_env"],
            fallback_envs=config["fallback_envs"],
            default_model=config["default_model"],
            instruction_contract=instruction_contract,
        )
        config["instruction_contract"] = instruction_contract

    case_bundle_task = create_case_bundle_task(pdf_path=pdf_path, agent=ingestion_agent)
    analyst_tasks = []
    for config in analyst_configs:
        task = create_analysis_task(
            agent=config["agent"],
            analyst_label=config["label"],
            prompt_text=prompt_text,
            model_name=config["agent"].llm.model,
            bundle_input_name="prepared_case_bundle_json",
            instruction_contract=config["instruction_contract"],
        )
        config["task"] = task
        analyst_tasks.append(task)

    analyst_agents = [config["agent"] for config in analyst_configs]
    crew_agents = [ingestion_agent]
    crew_tasks = [case_bundle_task]

    crew = Crew(
        agents=[*crew_agents, *analyst_agents],
        tasks=[*crew_tasks, *analyst_tasks],
        process=Process.sequential,
        verbose=True,
    )

    task_map: TaskMap = {
        "case_bundle_raw": case_bundle_task,
        "case_bundle": case_bundle_task,
    }

    for config in analyst_configs:
        task = config.get("task")
        if not task:
            continue
        task_map[config["key"]] = task

    return crew, task_map


def run_crew(
    pdf_path: str,
    prompt_path: Optional[Path] = None,
    *,
    capture_reports: bool = False,
    enable_score_masking: bool = False,
    strict_scoring: bool = False,
    use_analyst_delta: bool = False,
    use_analyst_epsilon: bool = False,
    use_analyst_zeta: bool = False,
    use_analyst_eta: bool = False,
    completed_reports: Mapping[str, str] | None = None,
    on_report: Callable[[str, str], None] | None = None,
    max_restarts: int = 2,
    on_attempt: Callable[[AnalystAttemptEvent], None] | None = None,
    _on_invalid_report_audit: (
        Callable[[str, int, str, SafeValidationDiagnostic], None] | None
    ) = None,
    instruction_contracts: Mapping[str, AnalystInstructionContract] | None = None,
    **kwargs,
) -> str | Tuple[str, Dict[str, Optional[str]]]:
    max_restarts = validate_max_restarts(max_restarts)
    raw_case_bundle_json, masked_case_bundle_json = _prepare_case_bundle_json(
        pdf_path=pdf_path,
        enable_score_masking=enable_score_masking,
    )
    bundle_input_name = (
        "masked_case_bundle_json" if masked_case_bundle_json else "raw_case_bundle_json"
    )
    selected_case_bundle_json = masked_case_bundle_json or raw_case_bundle_json

    analyst_configs = get_enabled_analyst_configs(
        use_analyst_delta=use_analyst_delta,
        use_analyst_epsilon=use_analyst_epsilon,
        use_analyst_zeta=use_analyst_zeta,
        use_analyst_eta=use_analyst_eta,
    )

    final_output = ""
    reports: Dict[str, Optional[str]] = {}
    supplied_reports = completed_reports or {}
    for config in analyst_configs:
        key = config["key"]
        report_text = supplied_reports.get(key)
        if not isinstance(report_text, str):
            continue
        try:
            validate_analyst_report(report_text)
        except AnalystReportValidationError:
            continue
        reports[key] = report_text
    if reports:
        final_output = next(reversed(reports.values()), "") or ""
    max_attempts = max_restarts + 1
    for config in analyst_configs:
        key = config["key"]
        if key in reports:
            continue
        retry_instruction: str | None = None
        last_error = ""
        last_exception: Exception | None = None
        last_validation_diagnostic: SafeValidationDiagnostic | None = None
        failure_kind: FailureKind = "validation"
        for attempt in range(1, max_attempts + 1):
            crew, task = _build_isolated_analyst_run(
                config=config,
                bundle_input_name=bundle_input_name,
                prompt_path=prompt_path,
                strict_scoring=strict_scoring,
                retry_instruction=retry_instruction,
                instruction_contract=(instruction_contracts or {}).get(key),
            )
            print(f"{key}: attempt {attempt}/{max_attempts}")
            running_event = AnalystAttemptEvent(
                analyst_key=key,
                attempt=attempt,
                max_attempts=max_attempts,
                status="running",
            )
            if on_attempt:
                on_attempt(running_event)

            try:
                final_output = crew.kickoff(
                    inputs={
                        "pdf_path": pdf_path,
                        bundle_input_name: selected_case_bundle_json,
                        **kwargs,
                    }
                )
                fallback_output = _output_text(final_output)
                report_text = _task_output_text(task) or fallback_output or ""
            except Exception as exc:
                last_error = format_execution_error(exc)
                last_exception = exc
                failure_kind = "execution"
                retry_instruction = None
                last_validation_diagnostic = None
                execution_event = AnalystAttemptEvent(
                    analyst_key=key,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    status="execution_failed",
                    execution_diagnostic=build_execution_diagnostic(exc),
                )
                if on_attempt:
                    on_attempt(execution_event)
            else:
                try:
                    validate_analyst_report(report_text)
                except AnalystReportValidationError as exc:
                    last_validation_diagnostic = validate_validation_diagnostic(
                        exc.diagnostic
                    )
                    last_error = render_validation_diagnostic(
                        last_validation_diagnostic
                    )
                    last_exception = exc
                    failure_kind = "validation"
                    retry_instruction = last_error
                    if _on_invalid_report_audit:
                        _on_invalid_report_audit(
                            key,
                            attempt,
                            report_text,
                            last_validation_diagnostic,
                        )
                    validation_event = AnalystAttemptEvent(
                        analyst_key=key,
                        attempt=attempt,
                        max_attempts=max_attempts,
                        status="validation_failed",
                        validation_diagnostic=last_validation_diagnostic,
                    )
                    if on_attempt:
                        on_attempt(validation_event)
                else:
                    completed_event = AnalystAttemptEvent(
                        analyst_key=key,
                        attempt=attempt,
                        max_attempts=max_attempts,
                        status="completed",
                        report_text=report_text,
                    )
                    if on_attempt:
                        on_attempt(completed_event)
                    if capture_reports:
                        reports[key] = report_text
                        if on_report:
                            on_report(key, report_text)
                    break
        else:
            execution_error = AnalystExecutionError(
                analyst_key=key,
                attempts=max_attempts,
                failure_kind=failure_kind,
                last_error=last_error,
                last_exception=last_exception,
                validation_diagnostic=last_validation_diagnostic,
            )
            if last_exception is not None:
                raise execution_error from last_exception
            raise execution_error

    if not capture_reports:
        return final_output

    if masked_case_bundle_json:
        reports["raw_case_bundle"] = raw_case_bundle_json
        reports["masked_case_bundle"] = masked_case_bundle_json
        reports["ground_truth_rucam_score"] = _run_ground_truth_score_finder(
            raw_case_bundle_json
        )

    return final_output, reports


def _build_isolated_analyst_run(
    *,
    config: dict[str, Any],
    bundle_input_name: str,
    prompt_path: Path | None,
    strict_scoring: bool,
    retry_instruction: str | None,
    instruction_contract: AnalystInstructionContract | None = None,
) -> tuple[Crew, Task]:
    prompt_text = load_rucam_prompt(prompt_path, strict_scoring=strict_scoring)
    model_name = resolve_rucam_model(
        model_env=config["model_env"],
        fallback_envs=config["fallback_envs"],
        default_model=config["default_model"],
    )
    contract = instruction_contract or build_analyst_instruction_contract(
        analyst_label=config["label"],
        prompt_text=prompt_text,
        model_name=model_name,
        bundle_input_name=bundle_input_name,
    )
    agent = build_rucam_agent(
        label=config["label"],
        model_env=config["model_env"],
        max_tokens_env=config["max_tokens_env"],
        fallback_envs=config["fallback_envs"],
        default_model=config["default_model"],
        instruction_contract=contract,
    )
    task = create_analysis_task(
        agent=agent,
        analyst_label=config["label"],
        prompt_text=prompt_text,
        model_name=agent.llm.model,
        bundle_input_name=bundle_input_name,
        retry_instruction=retry_instruction,
        instruction_contract=contract,
    )
    crew = Crew(
        agents=[agent],
        tasks=[task],
        process=Process.sequential,
        verbose=True,
    )
    return crew, task


def _task_output_text(task: Task) -> Optional[str]:
    output = getattr(task, "output", None)
    return _output_text(output)


def _output_text(output: object) -> Optional[str]:
    if output is None:
        return None
    raw = getattr(output, "raw", None)
    if isinstance(raw, str) and raw.strip():
        return raw
    json_dict = getattr(output, "json_dict", None)
    if json_dict:
        return str(json_dict)
    pydantic_obj = getattr(output, "pydantic", None)
    if pydantic_obj:
        return pydantic_obj.model_dump_json()
    if isinstance(output, dict):
        return json.dumps(output)
    text = str(output)
    return text if text.strip() else None


def _prepare_case_bundle_json(
    *,
    pdf_path: str,
    enable_score_masking: bool,
) -> tuple[str, Optional[str]]:
    raw_payload = build_case_bundle(Path(pdf_path)).to_dict()
    raw_case_bundle_json = json.dumps(raw_payload, indent=2)

    if not enable_score_masking:
        return raw_case_bundle_json, None

    masked_payload = mask_case_bundle_payload(raw_payload)
    return raw_case_bundle_json, json.dumps(masked_payload, indent=2)


def _run_ground_truth_score_finder(raw_case_bundle_json: str) -> str:
    agent = build_ground_truth_rucam_score_finder_agent()
    prompt = build_ground_truth_score_finder_prompt(
        raw_case_bundle_json=raw_case_bundle_json,
        model_name=agent.llm.model,
    )
    result = agent.llm.call(prompt)
    return result if isinstance(result, str) else str(result)


__all__ = [
    "AnalystAttemptEvent",
    "AnalystExecutionError",
    "AttemptStatus",
    "build_crew",
    "run_crew",
    "validate_max_restarts",
]
