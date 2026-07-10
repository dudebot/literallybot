"""Keyless-provider invariant tests for core/llm/client.py.

Locks the fix history of the chat() vs chat_stream() asymmetry (chat_stream
originally hard-raised "No API key found" for keyless providers because it
predated the requires_api_key flag): every public entrypoint must resolve
keys through `_resolve_api_key`, which substitutes the "not-needed" dummy
for providers with `requires_api_key: false`.

Zero network: `_build_model` is monkeypatched to halt each call before any
SDK/model construction, after key resolution has happened.

Salvaged from the dev branch fix/post-sprint-seams minus its UsageTracker
tests (UsageTracker stays deleted on main — see docs/decision-records.md,
2026-07-10 amendment).
"""

import pytest

from core.llm.client import LLMClient, ProviderConfig


class _NoKeyConfig:
    """Config stub that never has a stored API key."""

    def get(self, *a, **k):
        return None

    def set(self, *a, **k):
        pass


KEYLESS = {
    "name": "Ollama (local)",
    "base_url": "http://localhost:11434/v1",
    "requires_api_key": False,
    "default_model": "m",
    "models": {"m": {}},
}
KEYED = {
    "name": "xAI Grok",
    "base_url": "https://api.x.ai/v1",
    "default_model": "m",
    "models": {"m": {}},
}


class _Sentinel(Exception):
    """Raised by the fake _build_model to halt before SDK construction."""


@pytest.fixture(autouse=True)
def _no_env_keys(monkeypatch):
    # _get_api_key falls back to os.environ; exported keys on a dev box
    # would mask the missing-key path.
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("XAI_API_KEY", raising=False)


def _provider_config(info):
    return ProviderConfig(provider="ollama" if info is KEYLESS else "xai",
                          model="m", provider_info=info, all_providers={})


def test_resolver_keyless_returns_dummy():
    assert LLMClient(_NoKeyConfig())._resolve_api_key("ollama", KEYLESS) == "not-needed"


def test_resolver_keyed_raises():
    with pytest.raises(ValueError, match="No API key"):
        LLMClient(_NoKeyConfig())._resolve_api_key("xai", KEYED)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["chat", "run_agent", "chat_stream"])
async def test_every_entrypoint_resolves_keyless(monkeypatch, entry):
    """Locks the invariant: no public entrypoint hard-raises on a keyless
    provider. Parametrized over the method NAME so a resurrected/renamed
    streaming path is covered the day it exists (skip if absent)."""
    client = LLMClient(_NoKeyConfig())
    if not hasattr(client, entry):
        pytest.skip(f"{entry} not present on this build")

    seen = {}

    def fake_build(provider, model, provider_info, api_key):
        seen["api_key"] = api_key
        raise _Sentinel  # halt before any network / SDK construction

    monkeypatch.setattr(client, "_build_model", fake_build)
    pc = _provider_config(KEYLESS)
    messages = [{"role": "user", "content": "hi"}]

    with pytest.raises(_Sentinel):
        method = getattr(client, entry)
        if entry == "chat_stream":
            # MUST iterate: async generators defer the raise to first
            # __anext__ -- a call-time-only check would have passed
            # against the historical bug.
            async for _ in method(pc, messages):
                pass
        elif entry == "run_agent":
            await method(pc, messages, tools=[])
        else:
            await method(pc, messages)

    assert seen["api_key"] == "not-needed"
