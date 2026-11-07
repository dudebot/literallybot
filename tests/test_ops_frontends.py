"""Agent replay and settings component-limit failures."""
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from cogs.gpt import AiSettingsView, Gpt


@pytest.fixture
def gpt_run(config, monkeypatch):
    """Real GPT routing and LLM client, with only model/Discord I/O replaced."""
    config.set_global('XAI_API_KEY', 'test-only')
    guild = NS(id=7, owner=None)
    author = NS(id=1, bot=False, display_name='User')
    message = NS(id=20, author=author, content='hello', reference=None,
                 mentions=[], created_at=datetime.now(timezone.utc))

    async def history(**kwargs):
        yield message

    @asynccontextmanager
    async def typing():
        yield

    channel = NS(id=10, guild=guild, history=history,
                 send=AsyncMock(return_value=NS(id=21, attachments=[])))
    bot = NS(config=config, logger=logging.getLogger('test.gpt'),
             user=NS(id=999, display_name='Bot'), get_channel=lambda cid: channel)
    ctx = NS(bot=bot, guild=guild, channel=channel, author=author,
             message=message, typing=typing, send=AsyncMock())
    gpt = Gpt(bot)
    return gpt, ctx


@pytest.mark.asyncio
async def test_provider_image_400_after_action_does_not_replay(config, gpt_run, monkeypatch):
    from pydantic_ai.exceptions import ModelHTTPError
    gpt, ctx = gpt_run
    config.set_global('agent_ops_whitelist', {'send_message': True})
    config.set(7, 'agent_ops_gate', {'send_message': 'everyone'})
    messages = [{'role': 'user', 'content': 'send it',
                 'images': [{'data': b'jpeg', 'media_type': 'image/jpeg'}]}]
    monkeypatch.setattr(gpt, '_prepare_cached_history', AsyncMock(return_value=(messages, ('key',), 18, 0)))
    requests = 0

    def respond(messages, info):
        nonlocal requests
        requests += 1
        if requests == 2:
            assert ctx.channel.send.await_count == 1
            raise ModelHTTPError(400, 'test-model', 'invalid image')
        return ModelResponse(parts=[ToolCallPart('send_message', {'channel_id': '10', 'content': 'Done'})])

    monkeypatch.setattr(gpt.llm, '_build_model', lambda *args: FunctionModel(respond))
    await gpt.process_askgpt(ctx, 'send it')

    assert requests == 2
    ctx.channel.send.assert_awaited_once()
    assert 'Error calling' in ctx.send.await_args.args[0]
    assert not gpt._history_windows.states


def panel(config):
    bot = NS(config=config, logger=logging.getLogger('test.panel'), user=NS(id=999))
    guild, user = NS(id=7, name='Test', owner=None), NS(id=1)
    view = AiSettingsView(Gpt(bot), user, guild)
    view.rerender = AsyncMock()
    interaction = NS(client=bot, user=user, guild=guild, response=NS(send_message=AsyncMock()))
    return view, interaction


@pytest.mark.asyncio
@pytest.mark.parametrize('page', ['server', 'agentops', 'mcp'])
async def test_full_settings_page_fits_discord_limits(config, page):
    # Render the actual registry: helper-only checks missed the 40-component outage.
    config.set_global('superadmins', [1])
    from core.ops import registry
    config.set_global('agent_ops_whitelist', {name: True for name in registry.names()})
    view, _ = panel(config)
    view.page = page
    try:
        view._build()
        assert view.total_children_count <= 40
        assert view.to_components()
        selects = [child for child in view.walk_children() if isinstance(child, discord.ui.Select)]
        assert all(1 <= len(select.options) <= 25 for select in selects)
    finally:
        view.stop()
