"""The chat/agent entrypoints must both support a configured keyless provider."""
import pytest

from core.llm.client import LLMClient, ProviderConfig


@pytest.mark.asyncio
@pytest.mark.parametrize('entry', ['chat', 'run_agent'])
async def test_keyless_provider_reaches_model_construction(config, monkeypatch, entry):
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
    kwargs = {'tools': []} if entry == 'run_agent' else {}
    with pytest.raises(ModelBoundary):
        await getattr(client, entry)(provider, [{'role': 'user', 'content': 'hello'}], **kwargs)
    assert seen == ['not-needed']
