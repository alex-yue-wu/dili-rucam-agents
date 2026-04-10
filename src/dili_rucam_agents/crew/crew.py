from __future__ import annotations

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

    if "analyst_alpha" in task_map:
        task_map["gpt_52"] = task_map["analyst_alpha"]
    if "analyst_beta" in task_map:
        task_map["gemini_30"] = task_map["analyst_beta"]

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
    **kwargs,
) -> str | Tuple[str, Dict[str, Optional[str]]]:
    raw_case_bundle_json, masked_case_bundle_json = _prepare_case_bundle_json(
        pdf_path=pdf_path,
        enable_score_masking=enable_score_masking,
    )
    prepared_case_bundle_json = masked_case_bundle_json or raw_case_bundle_json

    crew, task_map = build_crew(
        pdf_path=pdf_path,
        prompt_path=prompt_path,
        strict_scoring=strict_scoring,
        use_analyst_delta=use_analyst_delta,
        use_analyst_epsilon=use_analyst_epsilon,
        use_analyst_zeta=use_analyst_zeta,
        use_analyst_eta=use_analyst_eta,
    )
    final_output = crew.kickoff(
        inputs={
            "pdf_path": pdf_path,
            "prepared_case_bundle_json": prepared_case_bundle_json,
            **kwargs,
        }
    )

    if not capture_reports:
        return final_output

    reports: Dict[str, Optional[str]] = {}
    if masked_case_bundle_json:
        reports["raw_case_bundle"] = raw_case_bundle_json
        reports["masked_case_bundle"] = masked_case_bundle_json
        reports["ground_truth_rucam_score"] = _run_ground_truth_score_finder(raw_case_bundle_json)

    for key, task in task_map.items():
        if not key.startswith("analyst_"):
            continue
        reports[key] = _task_output_text(task)

    return final_output, reports


def _task_output_text(task: Task) -> Optional[str]:
    output = getattr(task, "output", None)
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
    return str(output)


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
