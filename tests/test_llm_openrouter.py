"""OpenRouter is reached through its own pydantic-ai provider, free tier only."""
from types import SimpleNamespace

import pytest

from core.llm.client import DEFAULT_PROVIDERS, LLMClient
from core.llm.usage import UsageRecord, estimate_cost, known_output_price


def test_openrouter_model_uses_openrouter_endpoint_and_max_tokens(config):
    info = DEFAULT_PROVIDERS["openrouter"]
    model = LLMClient(config)._build_model(
        "openrouter", info["default_model"], info, "sk-or-test")
    assert str(model.client.base_url).rstrip("/") == "https://openrouter.ai/api/v1"
    assert model.profile["openai_chat_supports_max_completion_tokens"] is False


def test_openrouter_free_models_cost_nothing_and_paid_stay_unknown():
    assert known_output_price("openrouter", "google/gemma-4-31b-it:free") == 0.0
    assert known_output_price("openrouter", "google/gemma-4-31b-it") is None
    record = UsageRecord(provider="openrouter", model="google/gemma-4-31b-it:free",
                         prompt_tokens=10, completion_tokens=10, total_tokens=20)
    assert estimate_cost(record) == 0.0


@pytest.mark.asyncio
async def test_openrouter_discovery_keeps_only_free_models(config, monkeypatch):
    listing = SimpleNamespace(data=[SimpleNamespace(id="a/free-one:free"),
                                    SimpleNamespace(id="a/paid-one")])

    class FakeOpenAI:
        def __init__(self, **_):
            self.models = SimpleNamespace(list=lambda: listing)

    monkeypatch.setattr("core.llm.client.openai.OpenAI", FakeOpenAI)
    info = {"name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "models": {}}
    found = await LLMClient(config).discover_models("openrouter", "sk-or-test", info)
    assert found == ["a/free-one:free"]
    assert list(info["models"]) == ["a/free-one:free"]
