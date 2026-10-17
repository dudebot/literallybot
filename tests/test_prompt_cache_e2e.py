"""Protect reusable wire prefixes through Discord, vision, and real SDK/tool I/O.

Only external Discord I/O is faked. The changing REQUEST CONTEXT / COMMAND
suffix is deliberately outside the reusable history prefix.
"""
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import base64
import io
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from urllib.parse import urlparse

import aiohttp
from aiohttp import web
import discord
from PIL import Image
import pytest

from cogs.optional.gpt import Gpt


def wire(message):
    return json.dumps(message, sort_keys=True).encode()


def history_prefix(messages):
    """Cut at the explicit invocation suffix, never at a fixed history length."""
    end = next(i for i, m in enumerate(messages)
               if isinstance(m.get('content'), str)
               and m['content'].startswith('[REQUEST CONTEXT]'))
    return messages[:end]


def image_urls(messages):
    return [part['image_url']['url'] for m in messages
            if isinstance(m.get('content'), list) for part in m['content']
            if part['type'] == 'image_url']


@asynccontextmanager
async def provider_server():
    requests, downloads, images = [], Counter(), {}
    # Distinct, non-normalized inputs prove that actual Pillow re-encoding runs.
    for name, color in [('attachment', 'red'), ('embed', 'green'), ('bot', 'blue')]:
        buf = io.BytesIO()
        Image.new('RGB', (24, 16), color).save(buf, 'BMP')
        images[name] = buf.getvalue()

    async def image(request):
        name = request.match_info['name']
        downloads[name] += 1
        return web.Response(body=images[name], content_type='image/bmp')

    async def complete(request):
        body = await request.json()
        requests.append(body)
        messages = body['messages']
        enabled = any(t['function']['name'] == 'send_message' for t in body.get('tools', []))
        if enabled and messages[-1]['role'] != 'tool':
            message = {'role': 'assistant', 'content': None, 'tool_calls': [{
                'id': f'action-{len(requests)}', 'type': 'function',
                'function': {'name': 'send_message', 'arguments': json.dumps({
                    'channel_id': '10', 'content': 'Tool action complete'})}}]}
            finish = 'tool_calls'
        else:
            message = {'role': 'assistant', 'content': 'I can see the conversation.'}
            finish = 'stop'
        return web.json_response({
            'id': f'reply-{len(requests)}', 'object': 'chat.completion',
            'created': 1, 'model': 'cache-test', 'choices': [{
                'index': 0, 'message': message, 'finish_reason': finish}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 10, 'total_tokens': 110}})

    app = web.Application()
    # LLMClient builds OpenAIChatModel, so /responses is intentionally unnecessary.
    app.router.add_post('/v1/chat/completions', complete)
    app.router.add_get('/images/{name}', image)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        yield NS(url=f'http://127.0.0.1:{port}', requests=requests,
                 downloads=downloads, images=images)
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize('tools_enabled', [False, True], ids=['no-tools', 'tools'])
async def test_prompt_cache_discord_to_provider(config, discord_environment, monkeypatch, tools_enabled):
    bot, guild = discord_environment
    channel, author = guild.get_channel(10), guild.get_member(1)
    messages, sent, calls = [], [], []

    def post(content, *, author=author, attachments=(), embeds=()):
        msg = NS(id=len(messages) + 1, content=content, author=author,
                 channel=channel, guild=guild, attachments=list(attachments),
                 embeds=list(embeds), reference=None, mentions=[],
                 created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        messages.append(msg)
        return msg

    async def history(self, *, limit, before):
        for msg in [m for m in messages if m.id < before.id][-limit:][::-1]:
            yield msg

    async def send(self, content, **kwargs):
        sent.append(content)
        return post(content, author=bot.user)

    @asynccontextmanager
    async def typing():
        yield

    monkeypatch.setattr(discord.TextChannel, 'history', history)
    monkeypatch.setattr(discord.TextChannel, 'send', send)
    config.set_global('cooldown_tier_bases', {'cheap': 0})
    config.set(guild.id, 'ai_history_min_messages', 4)
    config.set(guild.id, 'ai_history_max_messages', 16)
    if tools_enabled:
        config.set_global('agent_ops_whitelist', {'send_message': True})
        config.set(guild.id, 'agent_ops_gate', {'send_message': 'everyone'})

    async with provider_server() as server:
        config.set_global('XAI_API_KEY', 'dummy-local-only')
        config.set_global('ai_providers', {'xai': {
            'base_url': server.url + '/v1', 'default_model': 'cache-test',
            'models': {'cache-test': {'vision': True, 'cost_per_mtok_output': 0,
                                     'cache_input_ratio': 0, 'cache_ttl_seconds': 300}}}})
        config.set(guild.id, 'current_ai_provider', 'xai')
        config.set(guild.id, 'current_ai_model', 'cache-test')
        # Keep production URL validation, download limits, normalization, and LRU.
        # Redirect only the external HTTP boundary, compatible with HEAD's downloader.
        get = aiohttp.ClientSession.get

        def local_get(session, url, **kwargs):
            parsed = urlparse(str(url))
            assert parsed.hostname == 'cdn.discordapp.com', 'unexpected image network I/O'
            return get(session, server.url + parsed.path, **kwargs)

        monkeypatch.setattr(aiohttp.ClientSession, 'get', local_get)

        def attachment(name, identity):
            return NS(id=identity, filename=name + '.bmp', content_type='image/bmp',
                      url=f'https://cdn.discordapp.com/images/{name}?signed=original')

        post('Earlier user attachment ' * 30, attachments=[attachment('attachment', 100)])
        embed = discord.Embed(title='Earlier preview')
        embed.set_image(url='https://cdn.discordapp.com/images/embed')
        post('Earlier user embed ' * 30, embeds=[embed])
        post('Earlier bot image ' * 30, author=bot.user, attachments=[attachment('bot', 101)])
        cog = Gpt(bot)
        chat = AsyncMock(wraps=cog.llm.chat)
        monkeypatch.setattr(cog.llm, 'chat', chat)
        ctx = NS(bot=bot, guild=guild, channel=channel, author=author,
                 typing=typing, send=lambda text, **kw: send(channel, text, **kw))

        # Four warm turns, then exceed max=16 to force a baseline reset, then
        # two more turns to prove extension resumes from that new start.
        for turn in range(7):
            if turn == 4:
                for _ in range(20):
                    post('Advance the inchworm window ' * 30)
            ctx.message = post(f'Follow-up {turn}: explain the pictures ' * 30)
            start = len(server.requests)
            await cog._process_askgpt(ctx, ctx.message.content)
            round_requests = server.requests[start:]
            assert len(round_requests) == (2 if tools_enabled else 1), sent
            calls.append(round_requests[0])
            if tools_enabled:
                first, final = (r['messages'] for r in round_requests)
                assert [wire(m) for m in final[:len(first)]] == [wire(m) for m in first]
                assert final[-2]['tool_calls'][0]['function']['name'] == 'send_message'
                assert final[-1]['role'] == 'tool'
                assert json.loads(final[-1]['content'])['ok'] is True

        # Historical pixels must survive real _process_askgpt follow-ups, rather
        # than moving to the latest request or becoming mere URL metadata.
        first_images = image_urls(history_prefix(calls[0]['messages']))
        assert len(first_images) == 3, 'historical attachment/embed/bot pixels missing from provider request'
        for call in calls[:4]:
            assert image_urls(history_prefix(call['messages'])) == first_images
        for url in first_images:
            assert url.startswith('data:image/jpeg;base64,')
            with Image.open(io.BytesIO(base64.b64decode(url.split(',', 1)[1]))) as image:
                assert image.format == 'JPEG' and image.size == (24, 16)
        assert len(set(first_images)) == 3

        prefixes = [history_prefix(c['messages']) for c in calls]
        for index, (previous, current) in enumerate(zip(prefixes, prefixes[1:]), 1):
            if index == 4:
                continue  # Explicitly checked reset below.
            assert len(previous) < len(current)
            assert [wire(m) for m in previous] == [wire(m) for m in current[:len(previous)]]
        assert prefixes[0][1]['content'][0]['text'].startswith('[msg_id: 1]')
        assert len(prefixes[4]) == 5  # system + fresh minimum of four Discord messages
        reset_trigger = int(calls[4]['metadata']['message'])
        assert f'[msg_id: {reset_trigger - 3}]' in prefixes[4][1]['content']
        assert not image_urls(prefixes[4])
        assert all(wire(c['messages'][0]) == wire(calls[0]['messages'][0]) for c in server.requests)
        chat.assert_not_awaited()
        assert server.downloads == {'attachment': 1, 'embed': 1, 'bot': 1}
        assert sent.count('Tool action complete') == (7 if tools_enabled else 0)
        assert sent.count('I can see the conversation.') == 7

        # Fill all eight slots, then introduce a ninth image in the trigger.
        # Evicting the oldest pixels changes the prefix once; the next plain
        # follow-up must resume extension from the resulting window.
        cap_calls = []
        for turn in range(3):
            attachments = ([attachment('attachment', 200 + i) for i in range(8)] if turn == 0
                           else [attachment('embed', 208)] if turn == 1 else [])
            ctx.message = post('Inspect the newest images ' * 30, attachments=attachments)
            start = len(server.requests)
            await cog._process_askgpt(ctx, ctx.message.content)
            cap_calls.append(server.requests[start])
        before, evicted, resumed = [history_prefix(c['messages']) for c in cap_calls]
        assert len(image_urls(before)) == len(image_urls(evicted)) == 8
        assert image_urls(evicted)[-1] != image_urls(before)[-1]  # Ninth trigger is pixels.
        assert '[image: attachment.bmp]' in json.dumps(evicted)  # Oldest pin is metadata.
        assert [wire(m) for m in before] != [wire(m) for m in evicted[:len(before)]]
        assert [wire(m) for m in evicted] == [wire(m) for m in resumed[:len(evicted)]]
        assert image_urls(resumed) == image_urls(evicted)
        assert all('_selected_images' not in json.dumps(request) for request in server.requests)
        assert all('_selected_images' not in turn
                   for state in cog._history_windows.states.values() for turn in state.messages)
