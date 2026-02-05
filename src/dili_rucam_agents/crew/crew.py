from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional, Tuple

from crewai import Crew, Process, Task

from .agents import build_arbiter_agent, build_ingestion_agent, build_rucam_agent
from .tasks import (
    create_analysis_task,
    create_arbiter_task,
    create_case_bundle_task,
    load_rucam_prompt,
)

TaskMap = Dict[str, Task]


def build_crew(
    pdf_path: str,
    prompt_path: Optional[Path] = None,
    *,
    use_arbiter_beta: bool = False,
    use_arbiter_gamma: bool = False,
) -> Tuple[Crew, TaskMap]:
    prompt_text = load_rucam_prompt(prompt_path)

    ingestion_agent = build_ingestion_agent()
    gpt_agent = build_rucam_agent(
        label="GPT-5.2",
        model_env="OPENAI_MODEL",
        default_model="gpt-5.2",
        vendor_note="OpenAI GPT-5.2 deterministic reasoning model.",
    )
    gemini_agent = build_rucam_agent(
        label="Gemini 3.0",
        model_env="GEMINI_MODEL",
        default_model="gemini-3-pro-preview",
        vendor_note="Google Gemini 3.0 dual-encoder causal reasoning model.",
    )
    arbiter_configs = [
        {
            "label": "Arbiter Alpha",
            "model_env": "ARBITER_ALPHA_MODEL",
            "enabled": True,
            "default_model": "deepseek-reasoner",
        },
        {
            "label": "Arbiter Beta",
            "model_env": "ARBITER_BETA_MODEL",
            "enabled": use_arbiter_beta,
            "default_model": "gpt-5.2",
        },
        {
            "label": "Arbiter Gamma",
            "model_env": "ARBITER_GAMMA_MODEL",
            "enabled": use_arbiter_gamma,
            "default_model": "gpt-5.2",
        },
    ]

    for config in arbiter_configs:
        if not config["enabled"]:
            continue
        config["agent"] = build_arbiter_agent(
            label=config["label"],
            model_env=config["model_env"],
            default_model=config.get("default_model", "gpt-5.2"),
        )

    case_bundle_task = create_case_bundle_task(pdf_path=pdf_path, agent=ingestion_agent)
    gpt_task = create_analysis_task(
        agent=gpt_agent,
        case_bundle_task=case_bundle_task,
        analyst_label="GPT-5.2",
        prompt_text=prompt_text,
        model_reference="OPENAI_MODEL",
    )
    gemini_task = create_analysis_task(
        agent=gemini_agent,
        case_bundle_task=case_bundle_task,
        analyst_label="Gemini 3.0",
        prompt_text=prompt_text,
        model_reference="GEMINI_MODEL",
    )
    arbiter_tasks = []
    for config in arbiter_configs:
        agent = config.get("agent")
        if not agent:
            continue
        task = create_arbiter_task(
            agent=agent,
            gpt_task=gpt_task,
            gemini_task=gemini_task,
            arbiter_label=config["label"],
        )
        config["task"] = task
        arbiter_tasks.append(task)

    arbiter_agents = [config["agent"] for config in arbiter_configs if config.get("agent")]

    crew = Crew(
        agents=[ingestion_agent, gpt_agent, gemini_agent, *arbiter_agents],
        tasks=[case_bundle_task, gpt_task, gemini_task, *arbiter_tasks],
        process=Process.sequential,
        verbose=True,
    )

    task_map: TaskMap = {
        "case_bundle": case_bundle_task,
        "gpt_52": gpt_task,
        "gemini_30": gemini_task,
    }

    for config in arbiter_configs:
        task = config.get("task")
        if not task:
            continue
        label_key = f"arbiter_{config['label'].lower().replace(' ', '_')}"
        task_map[label_key] = task

    return crew, task_map


def run_crew(
    pdf_path: str,
    prompt_path: Optional[Path] = None,
    *,
    capture_reports: bool = False,
    use_arbiter_beta: bool = False,
    use_arbiter_gamma: bool = False,
    **kwargs,
) -> str | Tuple[str, Dict[str, Optional[str]]]:
    crew, task_map = build_crew(
        pdf_path=pdf_path,
        prompt_path=prompt_path,
        use_arbiter_beta=use_arbiter_beta,
        use_arbiter_gamma=use_arbiter_gamma,
    )
    final_output = crew.kickoff(inputs={"pdf_path": pdf_path, **kwargs})

    if not capture_reports:
        return final_output

    reports = {
        "gpt_52": _task_output_text(task_map["gpt_52"]),
        "gemini_30": _task_output_text(task_map["gemini_30"]),
    }

    for key, task in task_map.items():
        if not key.startswith("arbiter_"):
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
    return str(output)


__all__ = ["build_crew", "run_crew"]
