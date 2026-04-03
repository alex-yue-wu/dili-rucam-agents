import litellm

from dili_rucam_agents.crew.agents import build_arbiter_agent, build_rucam_agent


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


def test_openrouter_model_includes_custom_provider(monkeypatch):
    monkeypatch.delenv("ARBITER_BETA_MODEL", raising=False)
    monkeypatch.setenv("ARBITER_MODEL", "moonshotai/kimi-k2-thinking")
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    captured = []
    _install_fake_completion(monkeypatch, captured)

    agent = build_arbiter_agent(
        label="Arbiter Beta",
        model_env="ARBITER_BETA_MODEL",
        default_model="moonshotai/kimi-k2-thinking",
    )

    result = agent.llm.call("ping")

    assert result == "stub-response"
    params = captured[0]
    assert params["model"] == "moonshotai/kimi-k2-thinking"
    assert params["base_url"] == "https://openrouter.ai/api/v1"
    assert params["custom_llm_provider"] == "openrouter"


def test_deepseek_model_sets_provider(monkeypatch):
    monkeypatch.setenv("ARBITER_ALPHA_MODEL", "deepseek/deepseek-chat")

    captured = []
    _install_fake_completion(monkeypatch, captured)

    agent = build_arbiter_agent(
        label="Arbiter Alpha",
        model_env="ARBITER_ALPHA_MODEL",
        default_model="deepseek/deepseek-chat",
    )

    result = agent.llm.call("hello")

    assert result == "stub-response"
    params = captured[0]
    assert params["model"] == "deepseek/deepseek-chat"
    assert params["base_url"] == "https://api.deepseek.com"
    assert params["custom_llm_provider"] == "deepseek"


def test_deepseek_model_sets_provider_for_non_alpha_arbiter(monkeypatch):
    monkeypatch.setenv("ARBITER_BETA_MODEL", "deepseek/deepseek-chat")

    captured = []
    _install_fake_completion(monkeypatch, captured)

    agent = build_arbiter_agent(
        label="Arbiter Beta",
        model_env="ARBITER_BETA_MODEL",
        default_model="gpt-5.2",
    )

    result = agent.llm.call("hello")

    assert result == "stub-response"
    params = captured[0]
    assert params["model"] == "deepseek/deepseek-chat"
    assert params["base_url"] == "https://api.deepseek.com"
    assert params["custom_llm_provider"] == "deepseek"


def test_anthropic_models_skip_openrouter(monkeypatch):
    monkeypatch.setenv("ARBITER_GAMMA_MODEL", "claude-sonnet-4-5-20250929")
    monkeypatch.delenv("ARBITER_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    captured = []
    _install_fake_completion(monkeypatch, captured)

    agent = build_arbiter_agent(
        label="Arbiter Gamma",
        model_env="ARBITER_GAMMA_MODEL",
        default_model="anthropic/claude-sonnet-4.5",
    )

    params = agent.llm
    assert "claude" in params.model
    assert params.base_url is None
    assert "custom_llm_provider" not in params.additional_params


def test_analyst_model_prefers_specific_env(monkeypatch):
    monkeypatch.setenv("ANALYST_ALPHA_MODEL", "moonshotai/kimi-k2-thinking")
    monkeypatch.setenv("ANALYST_MODEL", "anthropic/claude-sonnet-4.5")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.2")

    agent = build_rucam_agent(
        label="Analyst Alpha",
        model_env="ANALYST_ALPHA_MODEL",
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
        fallback_envs=("GEMINI_MODEL",),
        default_model="gemini-3-pro-preview",
    )

    assert agent.llm.model == "gemini-3-pro-preview"


def test_analyst_openrouter_model_includes_custom_provider(monkeypatch):
    monkeypatch.setenv("ANALYST_ALPHA_MODEL", "moonshotai/kimi-k2-thinking")

    captured = []
    _install_fake_completion(monkeypatch, captured)

    agent = build_rucam_agent(
        label="Analyst Alpha",
        model_env="ANALYST_ALPHA_MODEL",
        fallback_envs=("OPENAI_MODEL",),
        default_model="gpt-5.2",
    )

    result = agent.llm.call("ping")

    assert result == "stub-response"
    params = captured[0]
    assert params["model"] == "moonshotai/kimi-k2-thinking"
    assert params["base_url"] == "https://openrouter.ai/api/v1"
    assert params["custom_llm_provider"] == "openrouter"


def test_analyst_deepseek_model_sets_provider(monkeypatch):
    monkeypatch.setenv("ANALYST_BETA_MODEL", "deepseek/deepseek-chat")

    captured = []
    _install_fake_completion(monkeypatch, captured)

    agent = build_rucam_agent(
        label="Analyst Beta",
        model_env="ANALYST_BETA_MODEL",
        fallback_envs=("GEMINI_MODEL",),
        default_model="gemini-3-pro-preview",
    )

    result = agent.llm.call("ping")

    assert result == "stub-response"
    params = captured[0]
    assert params["model"] == "deepseek/deepseek-chat"
    assert params["base_url"] == "https://api.deepseek.com"
    assert params["custom_llm_provider"] == "deepseek"


def test_analyst_anthropic_model_sets_provider(monkeypatch):
    monkeypatch.setenv("ANALYST_ALPHA_MODEL", "claude-sonnet-4-5-20250929")

    agent = build_rucam_agent(
        label="Analyst Alpha",
        model_env="ANALYST_ALPHA_MODEL",
        fallback_envs=("OPENAI_MODEL",),
        default_model="gpt-5.2",
    )

    assert "claude" in agent.llm.model
    assert agent.llm.base_url is None
    assert "custom_llm_provider" not in agent.llm.additional_params


def test_bare_claude_model_uses_default_anthropic_routing(monkeypatch):
    monkeypatch.setenv("ARBITER_ALPHA_MODEL", "claude-opus-4-6")

    agent = build_arbiter_agent(
        label="Arbiter Alpha",
        model_env="ARBITER_ALPHA_MODEL",
        default_model="gpt-5.2",
    )

    assert "claude" in agent.llm.model
    assert agent.llm.base_url is None
    assert "custom_llm_provider" not in agent.llm.additional_params


def test_analyst_gpt_model_uses_default_provider_routing(monkeypatch):
    monkeypatch.setenv("ANALYST_ALPHA_MODEL", "gpt-5.2")

    agent = build_rucam_agent(
        label="Analyst Alpha",
        model_env="ANALYST_ALPHA_MODEL",
        fallback_envs=("OPENAI_MODEL",),
        default_model="gpt-5.2",
    )

    assert agent.llm.model == "gpt-5.2"
    assert agent.llm.base_url is None
    assert "custom_llm_provider" not in agent.llm.additional_params


def test_analyst_gemini_model_uses_default_provider_routing(monkeypatch):
    monkeypatch.setenv("ANALYST_BETA_MODEL", "gemini-3-pro-preview")

    agent = build_rucam_agent(
        label="Analyst Beta",
        model_env="ANALYST_BETA_MODEL",
        fallback_envs=("GEMINI_MODEL",),
        default_model="gemini-3-pro-preview",
    )

    assert agent.llm.model == "gemini-3-pro-preview"
    assert agent.llm.base_url is None
    assert "custom_llm_provider" not in agent.llm.additional_params


def test_arbiter_max_tokens_env_overrides_default(monkeypatch):
    monkeypatch.setenv("ARBITER_ALPHA_MODEL", "claude-opus-4-6")
    monkeypatch.setenv("ARBITER_MAX_TOKENS", "12000")

    agent = build_arbiter_agent(
        label="Arbiter Alpha",
        model_env="ARBITER_ALPHA_MODEL",
        default_model="gpt-5.2",
    )

    assert agent.llm.max_tokens == 12000


def test_gemini_analyst_uses_max_output_tokens_env(monkeypatch):
    monkeypatch.setenv("ANALYST_BETA_MODEL", "gemini-3-pro-preview")
    monkeypatch.setenv("ANALYST_MAX_TOKENS", "9000")

    agent = build_rucam_agent(
        label="Analyst Beta",
        model_env="ANALYST_BETA_MODEL",
        fallback_envs=("GEMINI_MODEL",),
        default_model="gemini-3-pro-preview",
    )

    assert agent.llm.max_output_tokens == 9000
