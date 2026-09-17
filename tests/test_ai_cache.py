"""Regressions that would silently increase bills or reuse the wrong context."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import logging
from types import SimpleNamespace as NS

import httpx
import openai
import pytest
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from cogs.optional.gpt import Gpt
from core.llm.client import LLMClient, ProviderConfig


@pytest.mark.asyncio
async def test_channel_window_honors_cost_edits_expiry_and_failed_calls(config):
    info = {'models': {'grok-4.6': {'cache_input_ratio': .25, 'cache_ttl_seconds': 300}}}
    config.set_global('ai_providers', {'xai': info})
    config.set(1, 'current_ai_model', 'grok-4.6')
    config.set(1, 'ai_history_min_messages', 3)
    config.set(1, 'ai_history_max_messages', 6)
    author = NS(id=10, bot=False, display_name='A', name='A')
    messages = [NS(id=i, author=author, content=f'message {i} ' * 30,
                   created_at=datetime.fromtimestamp(i, timezone.utc),
                   reference=None, mentions=[], embeds=[], attachments=[])
                for i in range(1, 11)]
    async def history(limit, before):
        for m in [m for m in messages if m.id < before.id][-limit:][::-1]:
            yield m
    channel = NS(id=2, history=history)
    bot = NS(config=config, logger=logging.getLogger('test'), user=NS(id=99, display_name='Bot'))
    gpt = Gpt(bot)
    pc = gpt.get_provider_config(1)
    ctx = NS(guild=NS(id=1), channel=channel, message=messages[2], author=author)
    first, key, anchor, when = await gpt._prepare_cached_history(ctx, pc, [], now=100)
    gpt._history_windows.commit(key, anchor, first, when)
    ctx.message = messages[3]
    second, _, anchor, _ = await gpt._prepare_cached_history(ctx, pc, [], now=101)
    assert anchor == 1
    assert second[:4] == first[:4]  # static system + all three old messages
    # Editing the oldest message breaks the shared history: extending now
    # costs more than the fresh three-message baseline.
    messages[0].content = 'edited ' * 30
    _, _, anchor, _ = await gpt._prepare_cached_history(ctx, pc, [], now=102)
    assert anchor == 2
    # A persona change must also reset the economically useful prefix.
    config.set(1, 'gpt_personality_data', {'prompt': 'New persona'})
    _, _, anchor, _ = await gpt._prepare_cached_history(ctx, pc, [], now=103)
    assert anchor == 2
    config.rem(1, 'gpt_personality_data')
    ctx.message = messages[6]
    _, _, anchor, _ = await gpt._prepare_cached_history(ctx, pc, [], now=104)
    assert anchor == 5  # anchor 1 fell outside max=6
    ctx.message = messages[3]
    _, _, anchor, _ = await gpt._prepare_cached_history(ctx, pc, [], now=400)
    assert anchor == 2  # expired cache
    # Failed API calls must not publish an anchor as if it were cached.
    @asynccontextmanager
    async def typing():
        yield
    sent = []
    async def send(text, **kwargs):
        sent.append(text)
    async def fail(*args, **kwargs):
        raise RuntimeError('network failed')
    ctx.typing, ctx.send = typing, send
    gpt.llm.chat = fail
    await gpt.process_askgpt(ctx, 'hello')
    assert not gpt._history_windows.states
    assert 'network failed' in sent[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize('entry', ['chat', 'run_agent'])
async def test_xai_wire_routes_conversation_and_accounts_cached_input(config, monkeypatch, entry):
    headers = []
    def handle(request):
        headers.append(request.headers)
        return httpx.Response(200, json={
            'id': 'response', 'object': 'chat.completion', 'created': 1, 'model': 'grok-4.6',
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'hello'}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 10000, 'completion_tokens': 100, 'total_tokens': 10100,
                      'prompt_tokens_details': {'cached_tokens': 8000},
                      'completion_tokens_details': {'reasoning_tokens': 60}}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        sdk = openai.AsyncOpenAI(api_key='test-only', base_url='https://api.x.ai/v1', http_client=http)
        client = LLMClient(config)
        config.set_global('XAI_API_KEY', 'test-only')
        monkeypatch.setattr(client, '_build_model', lambda *args: OpenAIChatModel(
            'grok-4.6', provider=OpenAIProvider(openai_client=sdk)))
        info = {'base_url': 'https://api.x.ai/v1', 'models': {'grok-4.6': {}}}
        pc = ProviderConfig('xai', 'grok-4.6', info, {})
        kwargs = {'tools': []} if entry == 'run_agent' else {}
        result = await getattr(client, entry)(pc, [{'role': 'user', 'content': 'hi'}],
                                              metadata={'guild': '1', 'channel': '2', 'sender': '3'}, **kwargs)
        assert headers[0]['x-grok-conv-id'] == 'literallybot:1:2:grok-4.6'
        assert result.usage.cached_prompt_tokens == 8000
        assert result.usage.reasoning_tokens == 60
        assert result.usage.estimated_cost_usd == pytest.approx(.0086)


@pytest.mark.asyncio
async def test_history_and_model_cache_modals_keep_scopes_and_recheck_access(config):
    from cogs.optional.gpt import _HistorySettingsModal, _CacheSettingsModal
    config.set(1, 'admins', [8])
    config.set_global('superadmins', [7])
    config.set_global('ai_providers', {'xai': {'models': {'grok-4.6': {'max_tokens': 2000}}}})
    bot = NS(config=config, logger=logging.getLogger('test'))
    gpt = Gpt(bot)
    sent = []
    async def send(text, **kwargs):
        sent.append(text)
    async def render(interaction):
        pass
    panel = NS(bot=bot, gpt=gpt, invoker_id=8, _cfg_ctx=lambda: 1, rerender=render,
               mgmt_provider='xai', mgmt_model='grok-4.6')
    interaction = NS(user=NS(id=8), guild=NS(id=1), client=bot,
                     response=NS(send_message=send))
    history = _HistorySettingsModal(panel)
    history.minimum._value, history.maximum._value = '15', '40'
    await history.on_submit(interaction)
    assert config.get(1, 'ai_history_max_messages') == 40
    assert config.get_global('ai_history_max_messages') is None
    cache = _CacheSettingsModal(panel)
    cache.ratio._value, cache.ttl._value = '25', '300'
    await cache.on_submit(interaction)
    assert 'cache_input_ratio' not in gpt.llm.get_all_providers()['xai']['models']['grok-4.6']
    interaction.user.id = panel.invoker_id = 7
    await cache.on_submit(interaction)
    model = gpt.llm.get_all_providers()['xai']['models']['grok-4.6']
    assert model == {'max_tokens': 2000, 'cache_input_ratio': .25, 'cache_ttl_seconds': 300}
    config.set_global('superadmins', [])
    cache.ratio._value = '5'
    await cache.on_submit(interaction)
    assert gpt.llm.get_all_providers()['xai']['models']['grok-4.6']['cache_input_ratio'] == .25
