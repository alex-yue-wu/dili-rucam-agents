from __future__ import annotations

import os
from typing import Optional

from crewai import Agent, LLM

from dili_rucam_agents.ingestion.build_bundle import CaseBundleExtractionTool


def _read_int_env(*names: str) -> int | None:
    for name in names:
        raw = os.getenv(name)
        if not raw:
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        if value > 0:
            return value
    return None


def _build_routed_llm_kwargs(
    *,
    model: str,
    max_output_tokens: int | None = None,
) -> dict:
    normalized_model = model.lower()
    normalized_model_name = normalized_model.split("/")[-1].split(":")[-1]
    is_anthropic_model = "anthropic" in normalized_model or "claude" in normalized_model_name
    is_deepseek_model = "deepseek" in normalized_model
    is_gemini_model = (
        "gemini" in normalized_model_name
        or normalized_model.startswith("gemini")
        or normalized_model.startswith("google/gemini")
    )
    routed_model = model

    base_url = None
    custom_llm_provider = None
    if is_anthropic_model:
        if "/" not in model:
            routed_model = f"anthropic/{model}"
    elif is_deepseek_model:
        base_url = "https://api.deepseek.com"
        custom_llm_provider = "deepseek"
    else:
        openrouter_models = {
            "kimi-k2-thinking",
            "glm-4.7",
            "qwen-max",
        }
        if normalized_model_name in openrouter_models and not is_anthropic_model:
            base_url = "https://openrouter.ai/api/v1"
            custom_llm_provider = "openrouter"
        # OpenAI GPT and Google Gemini routes are resolved by LiteLLM/CrewAI
        # from the model string and API key environment variables.

    llm_kwargs = {"model": routed_model, "temperature": 0}
    if base_url:
        llm_kwargs["base_url"] = base_url
    if custom_llm_provider:
        llm_kwargs["custom_llm_provider"] = custom_llm_provider
    if max_output_tokens:
        if is_gemini_model:
            llm_kwargs["max_output_tokens"] = max_output_tokens
        else:
            llm_kwargs["max_tokens"] = max_output_tokens

    return llm_kwargs


def build_ingestion_agent(model: Optional[str] = None) -> Agent:
    """Agent responsible for deterministic PDF ingestion."""

    tool = CaseBundleExtractionTool()
    ingestion_model = model or os.getenv("INGESTION_MODEL") or os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    return Agent(
        role="Deterministic PDF Ingestion Specialist",
        goal="Generate canonical case_bundle_json payloads from research PDFs without hallucination.",
        backstory=(
            "You orchestrate unstructured, pdfplumber, and PyMuPDF extractors. "
            "You never invent data and always describe missing segments."
        ),
        allow_delegation=False,
        tools=[tool],
        verbose=True,
        llm=LLM(model=ingestion_model, temperature=0),
    )


def build_rucam_agent(
    *,
    label: str,
    model_env: str,
    fallback_envs: tuple[str, ...] = (),
    default_model: str,
) -> Agent:
    """Factory for configurable RUCAM analysts."""

    analyst_model = os.getenv(model_env)
    if not analyst_model:
        analyst_model = os.getenv("ANALYST_MODEL")
    if not analyst_model:
        for env_name in fallback_envs:
            analyst_model = os.getenv(env_name)
            if analyst_model:
                break
    if not analyst_model:
        analyst_model = default_model
    llm_kwargs = _build_routed_llm_kwargs(
        model=analyst_model,
        max_output_tokens=_read_int_env("ANALYST_MAX_TOKENS", "LLM_MAX_TOKENS"),
    )

    return Agent(
        role=f"{label} Expert DILI RUCAM Analyst",
        goal="Apply the production RUCAM prompt verbatim to case_bundle_json inputs.",
        backstory=(
            f"{label} is a board-certified hepatologist and pharmacovigilance researcher. "
            "Always compute R-ratio, determine injury pattern, score all seven RUCAM items, "
            "and output Sections A/B/C exactly as specified."
        ),
        allow_delegation=False,
        verbose=True,
        llm=LLM(**llm_kwargs),
    )


def build_arbiter_agent(
    *,
    label: str,
    model_env: str,
    default_model: str,
) -> Agent:
    """Factory for the ensemble of hepatology arbiters."""

    arbiter_model = (
        os.getenv(model_env)
        or os.getenv("ARBITER_MODEL")
        or os.getenv("OPENAI_MODEL")
        or default_model
    )

    llm_kwargs = _build_routed_llm_kwargs(
        model=arbiter_model,
        max_output_tokens=_read_int_env("ARBITER_MAX_TOKENS", "LLM_MAX_TOKENS"),
    )

    return Agent(
        role=f"{label} Senior Hepatology Arbiter",
        goal=(
            "Compare Analyst Alpha and Analyst Beta reports, resolve every discrepancy, and emit a single final "
            "RUCAM decision plus justification."
        ),
        backstory=(
            "You chaired international RUCAM harmonization panels and only side with evidence backed by the "
            "case bundle and scoring rules."
        ),
        allow_delegation=False,
        verbose=True,
        llm=LLM(**llm_kwargs),
    )


__all__ = [
    "build_ingestion_agent",
    "build_rucam_agent",
    "build_arbiter_agent",
]
