import litellm
import pytest
from litellm.litellm_core_utils import litellm_logging

from dili_rucam_agents.crew.agents import (
    ANALYST_MAX_OUTPUT_TOKENS_CEILING,
    _build_routed_llm_kwargs,
    escalate_analyst_max_output_tokens,
    build_ground_truth_rucam_score_finder_agent,
    build_rucam_agent,
    build_score_masking_agent,
    resolve_analyst_max_output_tokens,
    resolve_ground_truth_score_finder_model,
)
from dili_rucam_agents.crew.config import get_enabled_analyst_configs


def _install_fake_completion(monkeypatch, captured_params):
    class DummyMessage:
        def __init__(self, content: str):
            self.content = content
            self.tool_calls = []

    class DummyChoice:
        def __init__(self, content: str):
            self.message = DummyMessage(content)

    class DummyResponse:
        def __init__(self, content: str):
            self.choices = [DummyChoice(content)]
            self.usage = None

    def fake_completion(**kwargs):
        captured_params.append(kwargs)
        return DummyResponse("stub-response")

    monkeypatch.setattr(litellm, "completion", fake_completion)


def test_masking_agent_uses_tool_and_default_routing(monkeypatch):
    monkeypatch.setenv("MASKING_MODEL", "gpt-5.2")

    agent = build_score_masking_agent()

    assert agent.llm.model == "gpt-5.2"
    assert agent.tools[0].name == "score_masker"


def test_ground_truth_score_finder_agent_uses_tool_and_default_routing(monkeypatch):
    monkeypatch.setenv("GROUND_TRUTH_SCORE_FINDER_MODEL", "gpt-5.4")

    agent = build_ground_truth_rucam_score_finder_agent()

    assert agent.llm.model == "gpt-5.4"
    assert agent.tools == []


def test_ground_truth_model_resolution_matches_agent_fallbacks(monkeypatch):
    monkeypatch.delenv("GROUND_TRUTH_SCORE_FINDER_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    assert resolve_ground_truth_score_finder_model() == "gpt-5.4"

    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.5")
    assert resolve_ground_truth_score_finder_model() == "gpt-5.5"

    monkeypatch.setenv("GROUND_TRUTH_SCORE_FINDER_MODEL", "gpt-5.6")
    assert resolve_ground_truth_score_finder_model() == "gpt-5.6"
    assert resolve_ground_truth_score_finder_model("explicit-model") == "explicit-model"


def test_litellm_runtime_disables_standard_logging_payload():
    assert (
        litellm_logging.get_standard_logging_object_payload(
            None, None, None, None, None, "success"
        )
        is None
    )
    assert litellm.service_callback == []
    assert litellm.success_callback == []
    assert litellm.failure_callback == []


def test_analyst_model_prefers_specific_env(monkeypatch):
    monkeypatch.setenv("ANALYST_ALPHA_MODEL", "moonshotai/kimi-k2-thinking")
    monkeypatch.setenv("ANALYST_MODEL", "anthropic/claude-sonnet-4.5")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.2")

    agent = build_rucam_agent(
        label="Analyst Alpha",
        model_env="ANALYST_ALPHA_MODEL",
        max_tokens_env="ANALYST_ALPHA_MAX_TOKENS",
        fallback_envs=("OPENAI_MODEL",),
        default_model="gpt-5.2",
    )

    assert agent.llm.model == "moonshotai/kimi-k2-thinking"


def test_analyst_model_prefers_shared_env_over_legacy_fallback(monkeypatch):
    monkeypatch.delenv("ANALYST_BETA_MODEL", raising=False)
    monkeypatch.setenv("ANALYST_MODEL", "anthropic/claude-sonnet-4.5")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3-pro-preview")

    agent = build_rucam_agent(
        label="Analyst Beta",
        model_env="ANALYST_BETA_MODEL",
        max_tokens_env="ANALYST_BETA_MAX_TOKENS",
        fallback_envs=("GEMINI_MODEL",),
        default_model="gemini-3-pro-preview",
    )

    assert agent.llm.model == "claude-sonnet-4.5"


def test_analyst_model_uses_legacy_fallback_when_no_new_env(monkeypatch):
    monkeypatch.delenv("ANALYST_BETA_MODEL", raising=False)
    monkeypatch.delenv("ANALYST_MODEL", raising=False)
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3-pro-preview")

    agent = build_rucam_agent(
        label="Analyst Beta",
        model_env="ANALYST_BETA_MODEL",
        max_tokens_env="ANALYST_BETA_MAX_TOKENS",
        fallback_envs=("GEMINI_MODEL",),
        default_model="gemini-3-pro-preview",
    )

    assert agent.llm.model == "gemini-3-pro-preview"


def test_analyst_openrouter_model_includes_custom_provider(monkeypatch):
    monkeypatch.setenv("ANALYST_ALPHA_MODEL", "moonshotai/kimi-k2.5")

    captured = []
    _install_fake_completion(monkeypatch, captured)

    agent = build_rucam_agent(
        label="Analyst Alpha",
        model_env="ANALYST_ALPHA_MODEL",
        max_tokens_env="ANALYST_ALPHA_MAX_TOKENS",
        fallback_envs=("OPENAI_MODEL",),
        default_model="gpt-5.5",
    )

    result = agent.llm.call("ping")

    assert result == "stub-response"
    params = captured[0]
    assert params["model"] == "moonshotai/kimi-k2.5"
    assert params["base_url"] == "https://openrouter.ai/api/v1"
    assert params["custom_llm_provider"] == "openrouter"


def test_analyst_deepseek_model_sets_provider(monkeypatch):
    monkeypatch.setenv("ANALYST_DELTA_MODEL", "deepseek-v4-pro")

    agent = build_rucam_agent(
        label="Analyst Delta",
        model_env="ANALYST_DELTA_MODEL",
        max_tokens_env="ANALYST_DELTA_MAX_TOKENS",
        fallback_envs=("OPENAI_MODEL",),
        default_model="deepseek-v4-pro",
    )

    assert agent.llm.model == "deepseek/deepseek-v4-pro"
    assert agent.llm.base_url is None
    assert "custom_llm_provider" not in agent.llm.additional_params


def test_analyst_anthropic_model_sets_provider(monkeypatch):
    monkeypatch.setenv("ANALYST_GAMMA_MODEL", "claude-sonnet-4-5-20250929")

    agent = build_rucam_agent(
        label="Analyst Gamma",
        model_env="ANALYST_GAMMA_MODEL",
        max_tokens_env="ANALYST_GAMMA_MAX_TOKENS",
        fallback_envs=("OPENAI_MODEL",),
        default_model="anthropic/claude-sonnet-4.5",
    )

    assert "claude" in agent.llm.model
    assert agent.llm.base_url is None
    assert "custom_llm_provider" not in agent.llm.additional_params


def test_bare_claude_model_uses_default_anthropic_routing(monkeypatch):
    monkeypatch.setenv("ANALYST_GAMMA_MODEL", "claude-opus-4-6")

    agent = build_rucam_agent(
        label="Analyst Gamma",
        model_env="ANALYST_GAMMA_MODEL",
        max_tokens_env="ANALYST_GAMMA_MAX_TOKENS",
        fallback_envs=("OPENAI_MODEL",),
        default_model="anthropic/claude-sonnet-4.5",
    )

    assert "claude" in agent.llm.model
    assert agent.llm.base_url is None
    assert "custom_llm_provider" not in agent.llm.additional_params


def test_anthropic_model_omits_deprecated_temperature():
    params = _build_routed_llm_kwargs(model="claude-opus-4-7", max_output_tokens=12000)

    assert params["model"] == "anthropic/claude-opus-4-7"
    assert params["max_tokens"] == 12000
    assert "temperature" not in params


def test_analyst_zeta_default_model_is_latest_opus():
    configs = get_enabled_analyst_configs(use_analyst_zeta=True)
    zeta_config = next(config for config in configs if config["key"] == "analyst_zeta")

    assert zeta_config["default_model"] == "claude-opus-4-7"


def test_analyst_gpt_model_uses_default_provider_routing(monkeypatch):
    monkeypatch.setenv("ANALYST_ALPHA_MODEL", "gpt-5.2")

    agent = build_rucam_agent(
        label="Analyst Alpha",
        model_env="ANALYST_ALPHA_MODEL",
        max_tokens_env="ANALYST_ALPHA_MAX_TOKENS",
        fallback_envs=("OPENAI_MODEL",),
        default_model="gpt-5.2",
    )

    assert agent.llm.model == "gpt-5.2"
    assert agent.llm.base_url is None
    assert "custom_llm_provider" not in agent.llm.additional_params


def test_openai_gpt5_models_use_max_completion_tokens():
    params = _build_routed_llm_kwargs(model="gpt-5.4", max_output_tokens=12000)

    assert params["model"] == "gpt-5.4"
    assert params["max_completion_tokens"] == 12000
    assert "max_tokens" not in params


def test_openai_gpt55_model_omits_unsupported_temperature():
    params = _build_routed_llm_kwargs(model="gpt-5.5", max_output_tokens=12000)

    assert params["model"] == "gpt-5.5"
    assert params["max_completion_tokens"] == 12000
    assert "temperature" not in params


def test_analyst_gemini_model_uses_default_provider_routing(monkeypatch):
    monkeypatch.setenv("ANALYST_BETA_MODEL", "gemini-3-pro-preview")

    params = _build_routed_llm_kwargs(model="gemini-3-pro-preview")

    assert params["model"] == "gemini/gemini-3-pro-preview"
    assert "base_url" not in params
    assert "custom_llm_provider" not in params


def test_resolve_analyst_max_output_tokens_prefers_specific_env(monkeypatch):
    monkeypatch.setenv("ANALYST_GAMMA_MAX_TOKENS", "16000")
    monkeypatch.setenv("ANALYST_MAX_TOKENS", "8000")

    assert resolve_analyst_max_output_tokens("ANALYST_GAMMA_MAX_TOKENS") == 16000


def test_resolve_analyst_max_output_tokens_defaults_to_32000(monkeypatch):
    monkeypatch.delenv("ANALYST_GAMMA_MAX_TOKENS", raising=False)
    monkeypatch.delenv("ANALYST_MAX_TOKENS", raising=False)
    monkeypatch.delenv("LLM_MAX_TOKENS", raising=False)

    assert resolve_analyst_max_output_tokens("ANALYST_GAMMA_MAX_TOKENS") == 32000


def test_openrouter_model_with_free_suffix_is_normalized_correctly(monkeypatch):
    monkeypatch.setenv("ANALYST_EPSILON_MODEL", "qwen/qwen3.6-plus:free")

    captured = []
    _install_fake_completion(monkeypatch, captured)

    agent = build_rucam_agent(
        label="Analyst Epsilon",
        model_env="ANALYST_EPSILON_MODEL",
        max_tokens_env="ANALYST_EPSILON_MAX_TOKENS",
        fallback_envs=("OPENAI_MODEL",),
        default_model="qwen/qwen3.5-plus-02-15",
    )

    result = agent.llm.call("ping")

    assert result == "stub-response"
    params = captured[0]
    assert params["model"] == "qwen/qwen3.6-plus:free"
    assert params["base_url"] == "https://openrouter.ai/api/v1"
    assert params["custom_llm_provider"] == "openrouter"


def test_qwen_35_openrouter_model_is_routed_correctly(monkeypatch):
    monkeypatch.setenv("ANALYST_EPSILON_MODEL", "qwen/qwen3.5-plus-02-15")

    captured = []
    _install_fake_completion(monkeypatch, captured)

    agent = build_rucam_agent(
        label="Analyst Epsilon",
        model_env="ANALYST_EPSILON_MODEL",
        max_tokens_env="ANALYST_EPSILON_MAX_TOKENS",
        fallback_envs=("OPENAI_MODEL",),
        default_model="qwen/qwen3.5-plus-02-15",
    )

    result = agent.llm.call("ping")

    assert result == "stub-response"
    params = captured[0]
    assert params["model"] == "qwen/qwen3.5-plus-02-15"
    assert params["base_url"] == "https://openrouter.ai/api/v1"
    assert params["custom_llm_provider"] == "openrouter"


def test_glm_openrouter_model_is_routed_correctly(monkeypatch):
    monkeypatch.setenv("ANALYST_ETA_MODEL", "z-ai/glm-5")

    captured = []
    _install_fake_completion(monkeypatch, captured)

    agent = build_rucam_agent(
        label="Analyst Eta",
        model_env="ANALYST_ETA_MODEL",
        max_tokens_env="ANALYST_ETA_MAX_TOKENS",
        fallback_envs=("OPENAI_MODEL",),
        default_model="z-ai/glm-5",
    )

    result = agent.llm.call("ping")

    assert result == "stub-response"
    params = captured[0]
    assert params["model"] == "z-ai/glm-5"
    assert params["base_url"] == "https://openrouter.ai/api/v1"
    assert params["custom_llm_provider"] == "openrouter"
    assert params["max_completion_tokens"] == 32000
    assert "max_tokens" not in params


@pytest.mark.parametrize(
    ("truncation_restarts", "expected"),
    [(0, 32000), (1, 64000), (2, 64000), (7, 64000)],
)
def test_truncation_restarts_widen_the_output_budget_up_to_the_ceiling(
    truncation_restarts, expected
):
    """Repeating a truncated attempt under the same budget only moves the cut."""

    assert (
        escalate_analyst_max_output_tokens(
            32000, truncation_restarts=truncation_restarts
        )
        == expected
    )
    assert expected <= ANALYST_MAX_OUTPUT_TOKENS_CEILING


def test_escalation_starts_from_a_pinned_budget():
    assert escalate_analyst_max_output_tokens(8000, truncation_restarts=1) == 16000
    assert escalate_analyst_max_output_tokens(8000, truncation_restarts=2) == 32000


def test_escalation_leaves_an_unset_budget_unset():
    assert escalate_analyst_max_output_tokens(None, truncation_restarts=2) is None


@pytest.mark.parametrize("restarts", [-1, 1.5, "1", True])
def test_escalation_rejects_a_non_count_restart_value(restarts):
    with pytest.raises(ValueError, match="non-negative integer"):
        escalate_analyst_max_output_tokens(32000, truncation_restarts=restarts)


def test_a_truncation_restart_reaches_the_analyst_llm(monkeypatch):
    captured = []
    _install_fake_completion(monkeypatch, captured)
    for name in (
        "ANALYST_ETA_MAX_TOKENS",
        "ANALYST_MAX_TOKENS",
        "LLM_MAX_TOKENS",
        "ANALYST_ETA_MODEL",
        "ANALYST_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)

    agent = build_rucam_agent(
        label="Analyst Eta",
        model_env="ANALYST_ETA_MODEL",
        max_tokens_env="ANALYST_ETA_MAX_TOKENS",
        default_model="z-ai/glm-5",
        truncation_restarts=1,
    )
    agent.llm.call("ping")

    assert captured[0]["max_completion_tokens"] == 64000
