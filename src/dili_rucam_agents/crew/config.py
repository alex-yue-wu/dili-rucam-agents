from __future__ import annotations

from typing import Any


ANALYST_SPECS: tuple[dict[str, Any], ...] = (
    {
        "key": "analyst_alpha",
        "label": "Analyst Alpha",
        "model_env": "ANALYST_ALPHA_MODEL",
        "max_tokens_env": "ANALYST_ALPHA_MAX_TOKENS",
        "fallback_envs": ("OPENAI_MODEL",),
        "default_model": "gpt-5.4",
        "flag_name": None,
    },
    {
        "key": "analyst_beta",
        "label": "Analyst Beta",
        "model_env": "ANALYST_BETA_MODEL",
        "max_tokens_env": "ANALYST_BETA_MAX_TOKENS",
        "fallback_envs": ("GEMINI_MODEL", "OPENAI_MODEL"),
        "default_model": "gemini-3.1-pro-preview",
        "flag_name": None,
    },
    {
        "key": "analyst_gamma",
        "label": "Analyst Gamma",
        "model_env": "ANALYST_GAMMA_MODEL",
        "max_tokens_env": "ANALYST_GAMMA_MAX_TOKENS",
        "fallback_envs": ("ANTHROPIC_MODEL", "OPENAI_MODEL"),
        "default_model": "moonshotai/kimi-k2.5",
        "flag_name": None,
    },
    {
        "key": "analyst_delta",
        "label": "Analyst Delta",
        "model_env": "ANALYST_DELTA_MODEL",
        "max_tokens_env": "ANALYST_DELTA_MAX_TOKENS",
        "fallback_envs": ("ANALYST_MODEL", "OPENAI_MODEL"),
        "default_model": "deepseek-reasoner",
        "flag_name": "use_analyst_delta",
    },
    {
        "key": "analyst_epsilon",
        "label": "Analyst Epsilon",
        "model_env": "ANALYST_EPSILON_MODEL",
        "max_tokens_env": "ANALYST_EPSILON_MAX_TOKENS",
        "fallback_envs": ("ANALYST_MODEL", "GEMINI_MODEL", "OPENAI_MODEL"),
        "default_model": "qwen/qwen3.5-plus-02-15",
        "flag_name": "use_analyst_epsilon",
    },
    {
        "key": "analyst_zeta",
        "label": "Analyst Zeta",
        "model_env": "ANALYST_ZETA_MODEL",
        "max_tokens_env": "ANALYST_ZETA_MAX_TOKENS",
        "fallback_envs": ("ANALYST_MODEL", "OPENAI_MODEL"),
        "default_model": "claude-opus-4-7",
        "flag_name": "use_analyst_zeta",
    },
    {
        "key": "analyst_eta",
        "label": "Analyst Eta",
        "model_env": "ANALYST_ETA_MODEL",
        "max_tokens_env": "ANALYST_ETA_MAX_TOKENS",
        "fallback_envs": ("ANALYST_MODEL", "OPENAI_MODEL"),
        "default_model": "z-ai/glm-5",
        "flag_name": "use_analyst_eta",
    },
)


def get_enabled_analyst_configs(**flags: bool) -> list[dict[str, Any]]:
    enabled_configs: list[dict[str, Any]] = []
    for spec in ANALYST_SPECS:
        flag_name = spec["flag_name"]
        enabled = flag_name is None or bool(flags.get(flag_name, False))
        if not enabled:
            continue
        enabled_configs.append(dict(spec))
    return enabled_configs


__all__ = ["ANALYST_SPECS", "get_enabled_analyst_configs"]
