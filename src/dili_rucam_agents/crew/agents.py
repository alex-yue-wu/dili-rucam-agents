from __future__ import annotations

import os
from typing import Optional

from crewai import Agent, LLM

from dili_rucam_agents.ingestion.build_bundle import CaseBundleExtractionTool
from dili_rucam_agents.litellm_runtime import configure_litellm_runtime
from dili_rucam_agents.masking import ScoreMaskingTool

from .tasks import AnalystInstructionContract


configure_litellm_runtime()


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


def resolve_analyst_max_output_tokens(max_tokens_env: str) -> int | None:
    return (
        _read_int_env(max_tokens_env, "ANALYST_MAX_TOKENS", "LLM_MAX_TOKENS") or 12000
    )


_resolve_analyst_max_output_tokens = resolve_analyst_max_output_tokens


def _build_routed_llm_kwargs(
    *,
    model: str,
    max_output_tokens: int | None = None,
) -> dict:
    normalized_model = model.lower()
    normalized_model_name = normalized_model.split("/")[-1].split(":")[0]
    is_anthropic_model = (
        "anthropic" in normalized_model or "claude" in normalized_model_name
    )
    is_deepseek_model = "deepseek" in normalized_model
    is_openai_reasoning_model = normalized_model.startswith(("gpt-5", "o1", "o3", "o4"))
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
    elif is_gemini_model:
        if "/" not in model:
            routed_model = f"gemini/{model}"
    elif is_deepseek_model:
        if "/" not in model:
            routed_model = f"deepseek/{model}"
        else:
            base_url = "https://api.deepseek.com"
            custom_llm_provider = "deepseek"
    else:
        openrouter_models = {
            "glm-5",
            "kimi-k2-thinking",
            "kimi-k2.5",
            "glm-4.7",
            "glm-5",
            "qwen-max",
            "qwen3.5-plus-02-15",
            "qwen3.6-plus",
            "qwen3.6-plus:free",
        }
        if normalized_model_name in openrouter_models and not is_anthropic_model:
            base_url = "https://openrouter.ai/api/v1"
            custom_llm_provider = "openrouter"
        # OpenAI GPT and Google Gemini routes are resolved by LiteLLM/CrewAI
        # from the model string and API key environment variables.

    llm_kwargs = {"model": routed_model}
    if not (is_openai_reasoning_model or is_anthropic_model):
        llm_kwargs["temperature"] = 0
    if base_url:
        llm_kwargs["base_url"] = base_url
    if custom_llm_provider:
        llm_kwargs["custom_llm_provider"] = custom_llm_provider
    if max_output_tokens:
        if is_gemini_model:
            llm_kwargs["max_output_tokens"] = max_output_tokens
        elif is_openai_reasoning_model or custom_llm_provider == "openrouter":
            llm_kwargs["max_completion_tokens"] = max_output_tokens
        else:
            llm_kwargs["max_tokens"] = max_output_tokens

    return llm_kwargs


def _build_crewai_llm(**llm_kwargs: Any) -> LLM:
    llm_params = dict(llm_kwargs)
    openrouter_max_completion_tokens = None
    if llm_params.get("custom_llm_provider") == "openrouter":
        openrouter_max_completion_tokens = llm_params.pop("max_completion_tokens", None)
    llm = LLM(**llm_params)
    if openrouter_max_completion_tokens:
        llm.additional_params["max_completion_tokens"] = (
            openrouter_max_completion_tokens
        )
    return llm


def build_ingestion_agent(model: Optional[str] = None) -> Agent:
    """Agent responsible for deterministic PDF ingestion."""

    tool = CaseBundleExtractionTool()
    ingestion_model = (
        model
        or os.getenv("INGESTION_MODEL")
        or os.getenv("OPENAI_MODEL", "gpt-5.4-mini")
    )

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
        llm=_build_crewai_llm(model=ingestion_model, temperature=0),
    )


def build_rucam_agent(
    *,
    label: str,
    model_env: str,
    max_tokens_env: str,
    fallback_envs: tuple[str, ...] = (),
    default_model: str,
    instruction_contract: AnalystInstructionContract | None = None,
) -> Agent:
    """Factory for configurable RUCAM analysts."""

    analyst_model = resolve_rucam_model(
        model_env=model_env,
        fallback_envs=fallback_envs,
        default_model=default_model,
    )
    llm_kwargs = _build_routed_llm_kwargs(
        model=analyst_model,
        max_output_tokens=resolve_analyst_max_output_tokens(max_tokens_env),
    )

    return Agent(
        role=(
            instruction_contract.agent_role
            if instruction_contract
            else f"{label} Expert DILI RUCAM Analyst"
        ),
        goal=(
            instruction_contract.agent_goal
            if instruction_contract
            else "Apply the production RUCAM prompt verbatim to case_bundle_json inputs."
        ),
        backstory=(
            instruction_contract.agent_backstory
            if instruction_contract
            else (
                f"{label} is a board-certified hepatologist and pharmacovigilance researcher. "
                "Always compute R-ratio, determine injury pattern, score all seven RUCAM items, "
                "and output Sections A/B/C exactly as specified."
            )
        ),
        allow_delegation=False,
        verbose=True,
        llm=_build_crewai_llm(**llm_kwargs),
    )


def resolve_rucam_model(
    *,
    model_env: str,
    fallback_envs: tuple[str, ...] = (),
    default_model: str,
) -> str:
    analyst_model = os.getenv(model_env)
    if not analyst_model:
        analyst_model = os.getenv("ANALYST_MODEL")
    if not analyst_model:
        for env_name in fallback_envs:
            analyst_model = os.getenv(env_name)
            if analyst_model:
                break
    return analyst_model or default_model


def build_score_masking_agent(model: Optional[str] = None) -> Agent:
    """Agent responsible for masking prior RUCAM scores before analysis."""

    tool = ScoreMaskingTool()
    masking_model = (
        model or os.getenv("MASKING_MODEL") or os.getenv("OPENAI_MODEL", "gpt-5.4-mini")
    )
    llm_kwargs = _build_routed_llm_kwargs(
        model=masking_model,
        max_output_tokens=_read_int_env("MASKING_MAX_TOKENS", "LLM_MAX_TOKENS"),
    )

    return Agent(
        role="RUCAM Score Masking Specialist",
        goal=(
            "Remove prior RUCAM scores and score-derived conclusions from extracted case bundles before analysts run."
        ),
        backstory=(
            "You use deterministic redaction rules to hide prior RUCAM judgments while preserving the raw clinical evidence."
        ),
        allow_delegation=False,
        tools=[tool],
        verbose=True,
        llm=_build_crewai_llm(**llm_kwargs),
    )


def resolve_ground_truth_score_finder_model(model: Optional[str] = None) -> str:
    """Resolve the model used by the ground-truth RUCAM score finder."""

    return (
        model
        or os.getenv("GROUND_TRUTH_SCORE_FINDER_MODEL")
        or os.getenv("OPENAI_MODEL", "gpt-5.4")
    )


def build_ground_truth_rucam_score_finder_agent(model: Optional[str] = None) -> Agent:
    """Agent responsible for identifying the author-reported RUCAM outcome in the raw PDF extraction."""

    finder_model = resolve_ground_truth_score_finder_model(model)
    llm_kwargs = _build_routed_llm_kwargs(
        model=finder_model,
        max_output_tokens=_read_int_env(
            "GROUND_TRUTH_SCORE_FINDER_MAX_TOKENS", "LLM_MAX_TOKENS"
        ),
    )

    return Agent(
        role="Ground Truth RUCAM Score Finder",
        goal="Identify the single best author-reported RUCAM score and category from the raw case bundle.",
        backstory=(
            "You focus only on author-reported RUCAM outcomes in the source PDF extraction and return a fixed-format markdown report."
        ),
        allow_delegation=False,
        verbose=True,
        llm=_build_crewai_llm(**llm_kwargs),
    )


__all__ = [
    "build_ground_truth_rucam_score_finder_agent",
    "build_ingestion_agent",
    "build_rucam_agent",
    "build_score_masking_agent",
    "resolve_analyst_max_output_tokens",
    "resolve_ground_truth_score_finder_model",
    "resolve_rucam_model",
]
