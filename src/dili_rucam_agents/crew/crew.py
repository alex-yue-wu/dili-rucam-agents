from __future__ import annotations

from collections.abc import Callable, Mapping
import json
from pathlib import Path
from typing import Dict, Optional, Tuple

from crewai import Crew, Process, Task

from dili_rucam_agents.ingestion.build_bundle import build_case_bundle
from dili_rucam_agents.masking import mask_case_bundle_payload

from .agents import (
    build_ground_truth_rucam_score_finder_agent,
    build_ingestion_agent,
    build_rucam_agent,
)
from .config import get_enabled_analyst_configs
from .tasks import (
    build_ground_truth_score_finder_prompt,
    create_analysis_task,
    create_case_bundle_task,
    load_rucam_prompt,
)

TaskMap = Dict[str, Task]


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
        config["agent"] = build_rucam_agent(
            label=config["label"],
            model_env=config["model_env"],
            max_tokens_env=config["max_tokens_env"],
            fallback_envs=config["fallback_envs"],
            default_model=config["default_model"],
        )

    case_bundle_task = create_case_bundle_task(pdf_path=pdf_path, agent=ingestion_agent)
    analyst_tasks = []
    for config in analyst_configs:
        task = create_analysis_task(
            agent=config["agent"],
            analyst_label=config["label"],
            prompt_text=prompt_text,
            model_name=config["agent"].llm.model,
            bundle_input_name="prepared_case_bundle_json",
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
    **kwargs,
) -> str | Tuple[str, Dict[str, Optional[str]]]:
    raw_case_bundle_json, masked_case_bundle_json = _prepare_case_bundle_json(
        pdf_path=pdf_path,
        enable_score_masking=enable_score_masking,
    )
    bundle_input_name = (
        "masked_case_bundle_json" if masked_case_bundle_json else "raw_case_bundle_json"
    )
    selected_case_bundle_json = masked_case_bundle_json or raw_case_bundle_json

    analyst_runs = _build_isolated_analyst_runs(
        prompt_path=prompt_path,
        strict_scoring=strict_scoring,
        use_analyst_delta=use_analyst_delta,
        use_analyst_epsilon=use_analyst_epsilon,
        use_analyst_zeta=use_analyst_zeta,
        use_analyst_eta=use_analyst_eta,
        bundle_input_name=bundle_input_name,
    )

    final_output = ""
    reports: Dict[str, Optional[str]] = dict(completed_reports or {})
    if completed_reports:
        final_output = next(reversed(reports.values()), "") or ""
    for key, crew, task in analyst_runs:
        if key in reports:
            continue
        final_output = crew.kickoff(
            inputs={
                "pdf_path": pdf_path,
                bundle_input_name: selected_case_bundle_json,
                **kwargs,
            }
        )
        fallback_output = _output_text(final_output)
        if capture_reports:
            report_text = _require_non_empty_report(
                key, _task_output_text(task) or fallback_output
            )
            reports[key] = report_text
            if on_report:
                on_report(key, report_text)
        else:
            _require_non_empty_report(key, fallback_output)

    if not capture_reports:
        return final_output

    if masked_case_bundle_json:
        reports["raw_case_bundle"] = raw_case_bundle_json
        reports["masked_case_bundle"] = masked_case_bundle_json
        reports["ground_truth_rucam_score"] = _run_ground_truth_score_finder(
            raw_case_bundle_json
        )

    return final_output, reports


def _build_isolated_analyst_runs(
    *,
    prompt_path: Optional[Path] = None,
    strict_scoring: bool = False,
    use_analyst_delta: bool = False,
    use_analyst_epsilon: bool = False,
    use_analyst_zeta: bool = False,
    use_analyst_eta: bool = False,
    bundle_input_name: str,
) -> list[tuple[str, Crew, Task]]:
    prompt_text = load_rucam_prompt(prompt_path, strict_scoring=strict_scoring)
    analyst_configs = get_enabled_analyst_configs(
        use_analyst_delta=use_analyst_delta,
        use_analyst_epsilon=use_analyst_epsilon,
        use_analyst_zeta=use_analyst_zeta,
        use_analyst_eta=use_analyst_eta,
    )
    analyst_runs: list[tuple[str, Crew, Task]] = []

    for config in analyst_configs:
        agent = build_rucam_agent(
            label=config["label"],
            model_env=config["model_env"],
            max_tokens_env=config["max_tokens_env"],
            fallback_envs=config["fallback_envs"],
            default_model=config["default_model"],
        )
        task = create_analysis_task(
            agent=agent,
            analyst_label=config["label"],
            prompt_text=prompt_text,
            model_name=agent.llm.model,
            bundle_input_name=bundle_input_name,
        )
        crew = Crew(
            agents=[agent],
            tasks=[task],
            process=Process.sequential,
            verbose=True,
        )
        analyst_runs.append((config["key"], crew, task))

    return analyst_runs


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


def _require_non_empty_report(key: str, report_text: Optional[str]) -> str:
    if report_text and report_text.strip():
        return report_text
    raise RuntimeError(f"{key} produced an empty report.")


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


__all__ = ["build_crew", "run_crew"]
