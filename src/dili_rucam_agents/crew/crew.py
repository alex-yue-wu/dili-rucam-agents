from __future__ import annotations

from pathlib import Path
from typing import Optional

from crewai import Crew, Process

from .agents import build_arbiter_agent, build_ingestion_agent, build_rucam_agent
from .tasks import (
    create_analysis_task,
    create_arbiter_task,
    create_case_bundle_task,
    load_rucam_prompt,
)


def build_crew(pdf_path: str, prompt_path: Optional[Path] = None) -> Crew:
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
    arbiter_agent = build_arbiter_agent()

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
    arbiter_task = create_arbiter_task(agent=arbiter_agent, gpt_task=gpt_task, gemini_task=gemini_task)

    return Crew(
        agents=[ingestion_agent, gpt_agent, gemini_agent, arbiter_agent],
        tasks=[case_bundle_task, gpt_task, gemini_task, arbiter_task],
        process=Process.sequential,
        verbose=True,
    )


def run_crew(pdf_path: str, prompt_path: Optional[Path] = None, **kwargs) -> str:
    crew = build_crew(pdf_path=pdf_path, prompt_path=prompt_path)
    return crew.kickoff(inputs={"pdf_path": pdf_path, **kwargs})


__all__ = ["build_crew", "run_crew"]
