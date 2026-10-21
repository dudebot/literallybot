"""Vision follows its Discord turn through cache windows, never the closing turn."""
import asyncio
import inspect
import hashlib
import json
import logging
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
import httpx
import openai
import pytest
from pydantic_ai.messages import BinaryContent, ModelRequest, ModelResponse
from pydantic_ai.providers.openai import OpenAIProvider

from cogs.optional.gpt import Gpt
from core.llm.client import _to_pai_messages
from utils.ai_images import ImageCache, message_images, normalize_image
from test_vision import JPEG


def attachment(i, query='old'):
    return NS(id=i, filename=f'{i}.jpg', content_type='image/jpeg',
              url=f'https://cdn.discordapp.com/attachments/1/{i}/{i}.jpg?ex={query}')


def message(i, *, images=(), embeds=(), bot=False, reference=None):
    return NS(id=i, content=f'message {i} ' * 30,
              author=NS(id=99 if bot else 10, bot=bot, display_name='A', name='A'),
              mentions=[], attachments=list(images), embeds=list(embeds), reference=reference)


def setup(config, messages):
    config.set(1, 'ai_history_min_messages', 4)
    config.set(1, 'ai_history_max_messages', 10)
    async def history(limit, before):
        for msg in [m for m in messages if m.id < before.id][-limit:]:
            yield msg
    channel = NS(id=2, history=history, fetch_message=AsyncMock())
    bot = NS(config=config, logger=logging.getLogger('test'), user=NS(id=99, display_name='Bot'))
    cog = Gpt(bot)
    ctx = NS(guild=NS(id=1), channel=channel, author=messages[-1].author, message=messages[-1])
    return cog, ctx


@pytest.mark.asyncio
async def test_assistant_ids_are_metadata_and_user_prefixes_are_preserved(config):
    messages = [message(1), message(2, bot=True), message(3)]
    cog, ctx = setup(config, messages)
    history, _ = await cog._build_history(ctx, messages=messages)
    assert history == [
        {'role': 'user', 'content': f'[msg_id: 1] 10: {messages[0].content}'},
        {'role': 'user', 'content': '[history metadata, not a request] '
         'The next assistant message is Discord message_id=2.'},
        {'role': 'assistant', 'content': messages[1].content},
        {'role': 'user', 'content': f'[msg_id: 3] 10: {messages[2].content}'},
    ]


@pytest.mark.parametrize('tools', [[], ['add_reaction'], ['send_message']])
def test_message_id_framing_explains_reply_lifecycle(config, tools):
    cog, ctx = setup(config, [message(1)])
    prompt = cog._build_system_prompt(ctx, tools)
    assert 'Every history line' not in prompt
    assert 'harness-supplied metadata identifying existing Discord messages' in prompt
    assert "not part of anyone's message text or a format to continue" in prompt
    assert 'Use them only in structured tool arguments, taking visible IDs directly' in prompt
    assert 'Your final reply is just the message body' in prompt
    assert 'posted in the channel answering the trigger' in prompt
    assert 'Discord assigns its ID after posting' in prompt
    assert ('send_message with reference_message_id, then stay_quiet' in prompt) == (
        'send_message' in tools)


@pytest.mark.asyncio
async def test_historical_pixels_survive_followups_with_identical_wire_prefix(config, monkeypatch):
    messages = [message(i, images=[attachment(100)] if i == 1 else []) for i in range(1, 7)]
    cog, ctx = setup(config, messages)
    fetch = AsyncMock(return_value=(200, normalize_image(JPEG)))
    monkeypatch.setattr('utils.ai_images.fetch_image', fetch)
    monkeypatch.setattr(cog.llm, 'chat', AsyncMock(side_effect=AssertionError('chat called')))
    config.set_global('XAI_API_KEY', 'test-only')
    requests = []
    def handle(request):
        requests.append(json.loads(request.content)['messages'])
        return httpx.Response(200, json={
            'id': 'test', 'object': 'chat.completion', 'created': 1, 'model': 'grok-4.6',
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'hello'},
                         'finish_reason': 'stop'}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        sdk = openai.AsyncOpenAI(api_key='test-only', base_url='https://api.x.ai/v1', http_client=http)
        monkeypatch.setattr('core.llm.client.OpenAIProvider', lambda **kwargs: OpenAIProvider(openai_client=sdk))
        pc = cog.get_provider_config(ctx)
        for turn in (4, 5, 6):
            ctx.message = messages[turn - 1]
            prepared, key, anchor, sent = await cog._prepare_cached_history(ctx, pc, [], now=turn)
            assert anchor == 1
            assert prepared[1]['images'][0]['data'] == normalize_image(JPEG)['data']
            assert not prepared[-1].get('images')
            await cog._run_agentic(ctx, pc, prepared, {}, 'look again', [])
            cog._history_windows.commit(key, anchor, prepared, sent)
    # Compare the actual provider wire bytes, including the base64 image input.
    prefix = lambda request: json.dumps(request[:5], separators=(',', ':')).encode()
    assert prefix(requests[0]) == prefix(requests[1]) == prefix(requests[2])
    assert requests[1][1]['content'][1]['type'] == 'image_url'
    fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_embed_bot_and_reference_images_keep_their_source_turn(config, monkeypatch):
    embed = discord.Embed(title='Preview')
    embed.set_image(url='https://media.discordapp.net/image.png')
    embed.set_thumbnail(url='https://media.discordapp.net/thumb.png')
    target = message(1, images=[attachment(1)], bot=True)
    posted = message(2, embeds=[embed])
    trigger = message(3, reference=NS(message_id=1))
    cog, ctx = setup(config, [posted, trigger])
    ctx.channel.fetch_message.return_value = target
    fetch = AsyncMock(return_value=(200, normalize_image(JPEG)))
    monkeypatch.setattr('utils.ai_images.fetch_image', fetch)
    history, _ = await cog._build_history(ctx, messages=[posted, trigger])
    assert len(history[0]['images']) == 2
    assert history[-1]['role'] == 'assistant' and len(history[-1]['images']) == 1
    assert history[-1]['content'] == '[REFERENCED MESSAGE] ' + target.content
    assert history[-1]['_selected_images'] == [(1, ('attachment', 1))]
    assert history[-2] == {
        'role': 'user', 'content': '[history metadata, not a request] '
        'The next assistant message is Discord message_id=1.'}
    converted = _to_pai_messages(history)
    bot_index = next(i for i, m in enumerate(converted) if isinstance(m, ModelResponse))
    assert isinstance(converted[bot_index + 1], ModelRequest)
    part = converted[bot_index + 1].parts[0].content
    assert part[0] == '[Images from the preceding bot message]'
    assert isinstance(part[1], BinaryContent)
    assert fetch.await_count == 3


@pytest.mark.asyncio
async def test_cap_evicts_oldest_pins_and_caches_failures(config, monkeypatch):
    messages = [message(i, images=[attachment(i)]) for i in range(1, 11)]
    cog, ctx = setup(config, messages)
    async def fetch(url):
        return 200, None if '/10.jpg?' in url else normalize_image(JPEG)
    mock = AsyncMock(side_effect=fetch)
    monkeypatch.setattr('utils.ai_images.fetch_image', mock)
    history, _ = await cog._build_history(ctx, messages=messages)
    assert sum(len(m.get('images', [])) for m in history) == 7
    assert '[image: 1.jpg]' in history[0]['content']
    assert '[image: 2.jpg]' in history[1]['content']
    assert '[image: 10.jpg]' in history[-1]['content']
    retained = [key for m in history for key in m.get('_selected_images', [])]
    newer = message(11, images=[attachment(11)])
    ctx.message = newer
    second, _ = await cog._build_history(ctx, messages=[*messages[1:], newer], retained=retained)
    assert '[image: 3.jpg]' in second[1]['content']
    assert second[-1]['images']
    assert sum(len(m.get('images', [])) for m in second) == 7
    assert mock.await_count == 9  # Negative cache avoids a second failed fetch.
    # Retained pixels remain reusable after older messages leave the window.
    third, _ = await cog._build_history(ctx, messages=[*messages[3:], newer], retained=retained)
    assert third[-1]['images']
    assert mock.await_count == 9


@pytest.mark.asyncio
async def test_ninth_trigger_image_evicts_oldest_but_reply_target_is_required(config, monkeypatch):
    messages = [message(i, images=[attachment(i)]) for i in range(1, 9)]
    cog, ctx = setup(config, messages)
    monkeypatch.setattr('utils.ai_images.fetch_image', AsyncMock(return_value=(200, normalize_image(JPEG))))
    first, _ = await cog._build_history(ctx, messages=messages)
    retained = [key for turn in first for key in turn.get('_selected_images', [])]
    trigger = message(9, images=[attachment(9)])
    ctx.message = trigger
    second, _ = await cog._build_history(ctx, messages=[*messages, trigger], retained=retained)
    assert second[-1]['images']
    assert '[image: 1.jpg]' in second[0]['content']
    assert sum(len(turn.get('images', [])) for turn in second) == 8
    trigger.reference = NS(message_id=1)
    third, _ = await cog._build_history(ctx, messages=[*messages, trigger], retained=retained)
    assert third[0]['images'] and third[-1]['images']
    assert '[image: 2.jpg]' in third[1]['content']
    assert sum(len(turn.get('images', [])) for turn in third) == 8


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [403, 404])
async def test_expired_urls_refresh_message_once(config, monkeypatch, status):
    old = message(1, images=[attachment(1), attachment(2)])
    fresh = message(1, images=[attachment(1, 'new'), attachment(2, 'new')])
    cog, ctx = setup(config, [old])
    ctx.channel.fetch_message.return_value = fresh
    fetch = AsyncMock(side_effect=lambda url: (200, normalize_image(JPEG)) if 'ex=new' in url else (status, None))
    monkeypatch.setattr('utils.ai_images.fetch_image', fetch)
    history, _ = await cog._build_history(ctx, messages=[old])
    assert len(history[0]['images']) == 2
    ctx.channel.fetch_message.assert_awaited_once_with(1)
    assert fetch.await_count == 4


@pytest.mark.asyncio
async def test_failed_refresh_is_negative_cached(config, monkeypatch):
    old = message(1, images=[attachment(1)])
    cog, ctx = setup(config, [old])
    ctx.channel.fetch_message.side_effect = RuntimeError('deleted')
    fetch = AsyncMock(return_value=(403, None))
    monkeypatch.setattr('utils.ai_images.fetch_image', fetch)
    for _ in range(2):
        history, _ = await cog._build_history(ctx, messages=[old])
        assert '[image: 1.jpg]' in history[0]['content']
    fetch.assert_awaited_once()
    ctx.channel.fetch_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_lru_bounds_identity_negative_expiry_and_coalescing(monkeypatch):
    cache = ImageCache(count=2, max_bytes=5)
    image = lambda data: {'data': data, 'media_type': 'image/jpeg'}
    cache.put('a', image(b'12'))
    cache.put('b', image(b'34'))
    cache.put('c', image(b'56'))
    assert list(cache.entries) == ['b', 'c'] and cache.bytes == 4  # Count eviction.
    cache.put('d', image(b'789'))
    assert list(cache.entries) == ['c', 'd'] and cache.bytes == 5  # Byte eviction.
    assert cache.put('oversize', image(b'123456')) is None and cache.bytes <= 5
    old, fresh = message(1, images=[attachment(1)]), message(1, images=[attachment(1, 'fresh')])
    assert message_images(old)[0][0] == message_images(fresh)[0][0]
    clock = 0
    monkeypatch.setattr('utils.ai_images.time', NS(monotonic=lambda: clock))
    fetch = AsyncMock(return_value=(0, None))
    monkeypatch.setattr('utils.ai_images.fetch_image', fetch)
    item = message_images(old)[0]
    await asyncio.gather(*(cache.get(item, old, None, {}) for _ in range(5)))
    await cache.get(item, fresh, None, {})
    fetch.assert_awaited_once()
    clock = 31
    await cache.get(item, fresh, None, {})
    assert fetch.await_count == 2


def test_no_secondary_askgpt_path():
    source = inspect.getsource(inspect.getmodule(Gpt))
    assert 'llm.chat(' not in source
    assert 'call_ai_api' not in source


@pytest.mark.asyncio
@pytest.mark.parametrize('trigger_count, reply_count', [(10, 2), (3, 10)])
async def test_explicit_inputs_obey_hard_cap_and_priority(config, monkeypatch, trigger_count, reply_count):
    target = message(1, images=[attachment(i) for i in range(1, reply_count + 1)])
    trigger = message(2, images=[attachment(i) for i in range(101, 101 + trigger_count)],
                      reference=NS(message_id=1))
    cog, ctx = setup(config, [target, trigger])
    fetch = AsyncMock(return_value=(200, normalize_image(JPEG)))
    monkeypatch.setattr('utils.ai_images.fetch_image', fetch)
    history, _ = await cog._build_history(ctx, messages=[target, trigger])
    chosen = {key for turn in history for key in turn.get('_selected_images', [])}
    expected = ([(2, ('attachment', i)) for i in range(101, 101 + trigger_count)]
                + [(1, ('attachment', i)) for i in range(1, reply_count + 1)])[:8]
    assert chosen == set(expected)
    assert sum(len(turn.get('images', [])) for turn in history) == 8
    assert fetch.await_count == 8
    for turn, source in zip(history, [target, trigger]):
        for att in source.attachments:
            assert (f'[image: {att.filename}]' in turn['content']) == ((source.id, ('attachment', att.id)) not in chosen)


@pytest.mark.asyncio
async def test_reply_pins_persist_through_text_followup_and_fill_vacancies(config, monkeypatch):
    messages = [message(i, images=[attachment(i)]) for i in range(1, 10)]
    reply = message(10, reference=NS(message_id=1))
    messages.append(reply)
    cog, ctx = setup(config, messages)
    monkeypatch.setattr('utils.ai_images.fetch_image', AsyncMock(return_value=(200, normalize_image(JPEG))))
    first, _ = await cog._build_history(ctx, messages=messages)
    retained = [key for turn in first for key in turn.get('_selected_images', [])]
    assert {key[0] for key in retained} == {1, 3, 4, 5, 6, 7, 8, 9}
    followup = message(11)
    ctx.message = followup
    second, _ = await cog._build_history(ctx, messages=[*messages, followup], retained=retained)
    assert second[:len(first)] == first
    # A deleted pin leaves one vacancy; fill it without replacing other pins.
    third, _ = await cog._build_history(ctx, messages=[m for m in messages if m.id != 3] + [followup], retained=retained)
    assert {key[0] for turn in third for key in turn.get('_selected_images', [])} == {1, 2, 4, 5, 6, 7, 8, 9}


def test_history_snapshots_release_pixels_preserve_comparisons_and_prune_expiry():
    from utils.ai_history import HistoryWindows, estimate_message_tokens, prefix_tokens
    windows = HistoryWindows()
    image = {'data': b'pixel payload', 'media_type': 'image/jpeg'}
    messages = [{'role': 'user', 'content': 'look', 'images': [image]}]
    cache = ImageCache(count=1)
    cache.put('old', image)
    windows.commit('old', 1, messages, 100, ttl=10)
    cache.put('new', {'data': b'new', 'media_type': 'image/jpeg'})
    assert 'old' not in cache.entries
    snapshot = windows.states['old'].messages
    assert snapshot[0]['images'] == [(hashlib.sha256(image['data']).hexdigest(), 'image/jpeg', 13)]
    assert prefix_tokens(snapshot, messages) == estimate_message_tokens(messages[0])
    for changed in ({**image, 'data': b'different'}, {**image, 'media_type': 'image/png'}):
        assert prefix_tokens(snapshot, [{**messages[0], 'images': [changed]}]) == 0
    windows.commit('warm', 1, messages, 101, ttl=100)
    windows.commit('new', 1, messages, 110)
    assert set(windows.states) == {'warm', 'new'}


@pytest.mark.asyncio
async def test_image_400_retry_commits_only_the_request_sent(config, monkeypatch):
    from contextlib import asynccontextmanager
    from pydantic_ai.exceptions import ModelHTTPError
    from utils.ai_history import prefix_tokens, estimate_message_tokens
    trigger = message(1, images=[attachment(1)])
    cog, ctx = setup(config, [trigger])
    @asynccontextmanager
    async def typing():
        yield
    ctx.typing, ctx.send = typing, AsyncMock()
    monkeypatch.setattr('utils.ai_images.fetch_image', AsyncMock(return_value=(200, normalize_image(JPEG))))
    run = AsyncMock(side_effect=[ModelHTTPError(400, 'test'), 'hello'])
    monkeypatch.setattr(cog, '_run_agentic', run)
    await cog.process_askgpt(ctx, 'look')
    assert run.await_count == 2
    original, retried = [call.args[2] for call in run.await_args_list]
    assert any(turn.get('images') for turn in original)
    assert not any(turn.get('images') for turn in retried)
    state, = cog._history_windows.states.values()
    assert state.messages == retried
    assert prefix_tokens(state.messages, retried) == sum(map(estimate_message_tokens, retried))


@pytest.mark.asyncio
async def test_failed_image_logs_warning_without_signed_query(config, monkeypatch, caplog):
    trigger = message(1, images=[attachment(1, 'secret-signature')])
    cog, ctx = setup(config, [trigger])
    monkeypatch.setattr('utils.ai_images.fetch_image', AsyncMock(return_value=(200, None)))
    with caplog.at_level(logging.WARNING, logger='test'):
        await cog._build_history(ctx, messages=[trigger])
    assert 'https://cdn.discordapp.com/attachments/1/1/1.jpg' in caplog.text
    assert 'secret-signature' not in caplog.text and '?ex=' not in caplog.text
    assert any(record.levelno == logging.WARNING for record in caplog.records)
