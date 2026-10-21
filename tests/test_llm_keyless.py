"""Active agent conversations support a configured keyless provider."""
import pytest

from core.llm.client import LLMClient, ProviderConfig


@pytest.mark.asyncio
async def test_keyless_provider_reaches_model_construction(config, monkeypatch):
    monkeypatch.delenv('OLLAMA_API_KEY', raising=False)
    info = {'name': 'Local', 'base_url': 'http://localhost:11434/v1',
            'requires_api_key': False, 'default_model': 'm', 'models': {'m': {}}}
    provider = ProviderConfig(provider='ollama', model='m', provider_info=info, all_providers={})
    client = LLMClient(config)
    seen = []

    class ModelBoundary(Exception):
        pass

    def build(provider, model, provider_info, api_key):
        seen.append(api_key)
        raise ModelBoundary

    monkeypatch.setattr(client, '_build_model', build)
    with pytest.raises(ModelBoundary):
        await client.run_agent(provider, [{'role': 'user', 'content': 'hello'}], tools=[])
    assert len(seen) == 1 and isinstance(seen[0], str) and seen[0]
