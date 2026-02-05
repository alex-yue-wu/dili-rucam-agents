import litellm

from dili_rucam_agents.crew.agents import build_arbiter_agent


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
