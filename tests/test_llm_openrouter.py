"""OpenRouter is reached through its own pydantic-ai model, free tier only."""
import functools
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_ai.tools import Tool

from core.llm.client import DEFAULT_PROVIDERS, LLMClient, ProviderConfig
from core.llm.usage import UsageRecord, estimate_cost, known_output_price


def test_openrouter_free_models_cost_nothing_and_paid_stay_unknown():
    assert known_output_price("openrouter", "google/gemma-4-31b-it:free") == 0.0
    assert known_output_price("openrouter", "google/gemma-4-31b-it") is None
    record = UsageRecord(provider="openrouter", model="google/gemma-4-31b-it:free",
                         prompt_tokens=10, completion_tokens=10, total_tokens=20)
    assert estimate_cost(record) == 0.0


@pytest.mark.asyncio
async def test_openrouter_agent_round_trip_keeps_reasoning_and_usage(config, monkeypatch):
    """Wire shape of a tool-calling run: OpenRouter's endpoint, the plain
    `max_tokens` field, reasoning_details echoed back, and usage counters."""
    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "t1", "type": "function",
                "function": {"name": "double", "arguments": '{"x": 2}'}}],
                "reasoning_details": [{"type": "reasoning.encrypted", "data": "sig",
                                       "id": "r1", "format": "unknown", "index": 0}]}
            finish = "tool_calls"
        else:
            message, finish = {"role": "assistant", "content": "4"}, "stop"
        return httpx.Response(200, json={
            "id": "x", "object": "chat.completion", "created": 1, "model": "m",
            "provider": "p", "choices": [{"index": 0, "message": message,
                                          "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr("core.llm.client.OpenRouterProvider",
                        functools.partial(OpenRouterProvider, http_client=http))
    info = DEFAULT_PROVIDERS["openrouter"]
    pc = ProviderConfig("openrouter", info["default_model"], info, {})

    def double(x: int) -> int:
        return x * 2

    result = await LLMClient(config).run_agent(
        pc, [], tools=[Tool(double)], user_prompt="double 2")
    await http.aclose()

    assert result.text == "4"
    assert bodies[0]["max_tokens"] == 8000 and "max_completion_tokens" not in bodies[0]
    echoed = [m for m in bodies[1]["messages"] if m["role"] == "assistant"]
    assert echoed[0]["reasoning_details"][0]["data"] == "sig"
    assert result.usage.prompt_tokens == 20


@pytest.mark.asyncio
async def test_openrouter_discovery_keeps_only_free_models(config, monkeypatch):
    listing = SimpleNamespace(data=[SimpleNamespace(id="a/free-one:free"),
                                    SimpleNamespace(id="a/unpriced:free"),
                                    SimpleNamespace(id="a/paid-one")])

    class FakeOpenAI:
        def __init__(self, **_):
            self.models = SimpleNamespace(list=lambda: listing)

    monkeypatch.setattr("core.llm.client.openai.OpenAI", FakeOpenAI)
    # A panel-added provider's default model starts unpriced.
    info = {"name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1",
            "models": {"a/unpriced:free": {}}}
    llm = LLMClient(config)
    found = await llm.discover_models("openrouter", "sk-or-test", info)
    assert found == ["a/free-one:free", "a/unpriced:free"]
    stored = llm.get_all_providers()["openrouter"]["models"]
    assert stored == {"a/free-one:free": {"cost_per_mtok_output": 0.0},
                      "a/unpriced:free": {"cost_per_mtok_output": 0.0}}
