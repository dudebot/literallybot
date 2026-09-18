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

from cogs.optional.gpt import AiSettingsView, Gpt
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
async def test_successful_calls_roll_anchor_lifetime_forward(config, monkeypatch):
    config.set(1, 'ai_history_min_messages', 3)
    config.set(1, 'ai_history_max_messages', 6)
    author = NS(id=10, bot=False, display_name='A', name='A')
    messages = [NS(id=i, author=author, content=f'message {i} ' * 30,
                   created_at=datetime.fromtimestamp(i, timezone.utc),
                   reference=None, mentions=[], embeds=[], attachments=[])
                for i in range(1, 6)]
    clock = 100
    fetch_delay = 0
    monkeypatch.setattr('cogs.optional.gpt.time', NS(monotonic=lambda: clock))

    async def history(limit, before):
        nonlocal clock
        clock += fetch_delay
        for m in [m for m in messages if m.id < before.id][-limit:][::-1]:
            yield m

    @asynccontextmanager
    async def typing():
        yield

    async def send(*args, **kwargs):
        pass

    async def chat(*args, **kwargs):
        return NS(text='hello', usage=None)

    bot = NS(config=config, logger=logging.getLogger('test'), user=NS(id=99, display_name='Bot'))
    gpt = Gpt(bot)
    gpt.llm.chat = chat
    ctx = NS(guild=NS(id=1), channel=NS(id=2, history=history),
             author=author, typing=typing, send=send)
    for clock, trigger in [(100, 2), (350, 3), (600, 4)]:
        ctx.message = messages[trigger]
        await gpt.process_askgpt(ctx, 'hello')
        state, = gpt._history_windows.states.values()
        assert state.anchor == 1
        assert state.sent_at == clock

    async def fail(*args, **kwargs):
        raise RuntimeError('network failed')

    clock = 700
    gpt.llm.chat = fail
    await gpt.process_askgpt(ctx, 'hello')
    state, = gpt._history_windows.states.values()
    assert state.anchor == 1 and state.sent_at == 600
    # Still warm when scraping starts, expired by the time Discord returns.
    clock, fetch_delay = 899, 2
    _, _, anchor, sent_at = await gpt._prepare_cached_history(ctx, gpt.get_provider_config(1), [])
    assert anchor == 3 and sent_at == 901


@pytest.mark.asyncio
async def test_candidate_references_share_fetches_but_refresh_next_invocation(config):
    config.set(1, 'ai_history_min_messages', 3)
    config.set(1, 'ai_history_max_messages', 6)
    author = NS(id=10, bot=False, display_name='A', name='A')
    messages = [NS(id=i, author=author, content=f'message {i} ' * 30,
                   created_at=datetime.fromtimestamp(i, timezone.utc),
                   reference=NS(message_id=100) if i == 3 else None,
                   mentions=[], embeds=[], attachments=[])
                for i in range(1, 5)]
    reference = NS(id=100, author=author, content='original reference',
                   created_at=datetime.fromtimestamp(0, timezone.utc),
                   reference=None, mentions=[])
    fetched = []

    async def fetch_message(message_id):
        fetched.append(message_id)
        return reference

    async def history(limit, before):
        for m in [m for m in messages if m.id < before.id][-limit:][::-1]:
            yield m

    bot = NS(config=config, logger=logging.getLogger('test'), user=NS(id=99, display_name='Bot'))
    gpt = Gpt(bot)
    ctx = NS(guild=NS(id=1), channel=NS(id=2, history=history, fetch_message=fetch_message),
             author=author, message=messages[2])
    pc = gpt.get_provider_config(1)
    first, key, anchor, when = await gpt._prepare_cached_history(ctx, pc, [], now=100)
    gpt._history_windows.commit(key, anchor, first, when)
    ctx.message = messages[3]
    reference.content = 'edited reference'
    second, _, anchor, _ = await gpt._prepare_cached_history(ctx, pc, [], now=101)
    assert anchor == 1
    assert fetched == [100, 100]  # Once per invocation, not once per candidate.
    assert second[:4] == first[:4]
    assert 'edited reference' in second[-2]['content']
    assert '[TRIGGER MESSAGE]' in second[-1]['content']


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
async def test_history_controls_and_model_cache_keep_scopes_and_recheck_access(config):
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
    view = AiSettingsView(gpt, interaction.user, NS(id=1, name='Test'))
    view.rerender = render
    minimum = view._history_select('min')
    minimum._values = ['50']
    await minimum.callback(interaction)
    assert config.get(1, 'ai_history_min_messages') == 50
    assert config.get(1, 'ai_history_max_messages') == 50
    maximum = view._history_select('max')
    maximum._values = ['100']
    config.set(1, 'admins', [])
    await maximum.callback(interaction)
    assert config.get(1, 'ai_history_max_messages') == 50
    assert config.get_global('ai_history_min_messages') is None
    view.stop()
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
