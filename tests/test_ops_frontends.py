"""Agent and settings boundaries: live permissions, budgets, and stored choices."""
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from starlette.requests import Request
from starlette.responses import Response

from cogs.optional.gpt import AiSettingsView, Gpt
from core.agent_gate import agent_universe, call_requires_admin
from core.agent_loop import build_agent_tools
from core import mcp_server
from core.ops import OpContext, registry


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

    def responses(parts):
        pending = iter(parts)
        requests = []

        def respond(messages, info):
            requests.append(info)
            return ModelResponse(parts=next(pending))

        monkeypatch.setattr(gpt.llm, '_build_model', lambda *args: FunctionModel(respond))
        return requests

    return gpt, ctx, responses


@pytest.mark.asyncio
@pytest.mark.parametrize('route', ['direct', 'after_action', 'parallel', 'nudge', 'budget'])
async def test_explicit_quiet_ends_run_without_posting_or_leaking_to_next_turn(config, gpt_run, route):
    gpt, ctx, responses = gpt_run
    config.set_global('agent_ops_whitelist', {'send_message': True})
    config.set(7, 'agent_ops_gate', {'send_message': 'everyone'})
    actions = 8 if route == 'budget' else int(route in ('after_action', 'parallel'))
    script = [[ToolCallPart('send_message', {'channel_id': '10', 'content': 'Done'})]
              for _ in range(actions)]
    if route == 'nudge':
        script.append([TextPart('I will call stay_quiet')])
    script.append([ToolCallPart('stay_quiet', {})])
    if route == 'parallel':
        # Even if the model batches an action with the terminal output, the
        # Agent must finish the action rather than skip it on early exit.
        script = [script[0] + script[1]]
    requests = responses(script)

    await gpt.process_askgpt(ctx, 'Do it quietly')

    assert len(requests) == len(script)  # No model request after the terminal call.
    ctx.send.assert_not_awaited()  # Includes the empty-response diagnostic.
    assert ctx.channel.send.await_count == actions  # Prior actions still happened.
    assert gpt._history_windows.states  # Successful silence still accounts for the run.

    # A quiet decision must be scoped to one invocation, never the cog or client.
    gpt._call_history.clear()
    requests = responses([[TextPart('Hello again')]])
    await gpt.process_askgpt(ctx, 'hello again')
    assert len(requests) == 1
    assert ctx.send.await_args.args[0] == 'Hello again'


@pytest.mark.asyncio
@pytest.mark.parametrize('agentic', [False, True])
async def test_unintentional_blank_is_not_silence_and_plain_chat_never_loops(config, gpt_run, monkeypatch, agentic):
    gpt, ctx, responses = gpt_run
    if agentic:
        config.set_global('agent_ops_whitelist', {'send_message': True})
        config.set(7, 'agent_ops_gate', {'send_message': 'everyone'})
    else:
        def unexpected_agent(**kwargs):
            pytest.fail('Plain chat must not construct an Agent')
        monkeypatch.setattr('core.llm.client.Agent', unexpected_agent)
    requests = responses([[TextPart('   ')]] if agentic else [[]])

    await gpt.process_askgpt(ctx, 'hello')

    assert len(requests) == 1
    ctx.send.assert_awaited_once()
    assert 'empty response' in ctx.send.await_args.args[0]
    if not agentic:
        assert not requests[0].function_tools and not requests[0].output_tools


def test_agent_exposure_requires_both_gates_and_never_includes_dm_or_global():
    whitelist = {'send_message': True, 'send_dm': True, 'list_guilds': True}
    gates = {name: 'everyone' for name in whitelist}
    assert agent_universe(None, gates) == []
    assert agent_universe(whitelist, None) == []
    assert agent_universe(whitelist, {'send_message': 'invalid'}) == []
    assert agent_universe(whitelist, gates) == ['send_message']
    assert agent_universe(whitelist, None, is_superadmin_actor=True) == ['send_message']
    assert agent_universe({}, gates, is_superadmin_actor=True) == []


@pytest.mark.asyncio
async def test_agent_rechecks_live_gate_and_shares_budget_across_tools(config):
    guild = NS(id=7, owner=None)
    sent = NS(id=22, attachments=[])
    channel = NS(id=10, guild=guild, send=AsyncMock(return_value=sent), typing=AsyncMock())
    ctx = OpContext(bot=NS(config=config, get_channel=lambda cid: channel), author=NS(id=1), guild=guild)
    whitelist = {'send_message': True, 'trigger_typing': True}
    config.set(7, 'agent_ops_gate', {name: 'everyone' for name in whitelist})

    def gate(name):
        return call_requires_admin(registry.require(name), whitelist, config.get(7, 'agent_ops_gate'))

    tools = build_agent_tools(ctx, logging.getLogger('test.agent'), list(whitelist),
                              tool_budget=2, gate_check=gate)
    assert (await tools[0].function(channel_id='10', content='hello'))['ok']
    config.set(7, 'agent_ops_gate', {'trigger_typing': 'admin'})
    assert not (await tools[1].function(channel_id='10'))['ok']
    channel.typing.assert_not_called()
    config.set(7, 'agent_ops_gate', {'trigger_typing': 'everyone'})
    assert (await tools[1].function(channel_id='10'))['ok']
    exhausted = await tools[1].function(channel_id='10')
    assert not exhausted['ok'] and 'budget' in exhausted['error'].lower()
    channel.typing.assert_awaited_once()


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
    # Build the actual view: helper-only checks missed the 40-component outage.
    config.set_global('superadmins', [1])
    config.set_global('agent_ops_whitelist', {name: True for name in registry.names()})
    view, _ = panel(config)
    view.page = page
    view._build()
    assert view.page == page
    assert view.total_children_count <= 40
    assert view.to_components()
    selects = [child for child in view.walk_children() if isinstance(child, discord.ui.Select)]
    assert selects
    assert all(1 <= len(select.options) <= 25 for select in selects)
    view.stop()


@pytest.mark.asyncio
async def test_whitelist_save_preserves_disabled_cog_choices_and_rechecks_auth(config):
    config.set_global('superadmins', [1])
    config.set_global('agent_ops_whitelist', {'ghost_tool': True, 'send_message': True})
    view, interaction = panel(config)
    await view._save_whitelist(interaction, ['read_history'], registry.guild_agent_names())
    assert config.get_global('agent_ops_whitelist') == {'ghost_tool': True, 'read_history': True}
    config.set_global('superadmins', [])
    config.set(7, 'admins', [1])
    await view._save_whitelist(interaction, ['delete_message'], registry.guild_agent_names())
    assert config.get_global('agent_ops_whitelist') == {'ghost_tool': True, 'read_history': True}
    interaction.response.send_message.assert_awaited_once()
    config.flush()
    view.stop()


@pytest.mark.asyncio
async def test_mcp_select_save_preserves_offline_choices_but_clear_all_removes_them(config):
    config.set_global('superadmins', [1])
    config.set_global('mcp_tools_enabled', ['ghost_tool', 'send_message'])
    view, interaction = panel(config)
    await view._save_mcp_tools(interaction, ['read_history'], registry.names())
    assert set(config.get_global('mcp_tools_enabled')) == {'ghost_tool', 'read_history'}
    assert mcp_server.resolve_mcp_tools(config) == ['read_history']
    await view._clear_mcp_tools(interaction, [])
    assert config.get_global('mcp_tools_enabled') == []
    assert mcp_server.resolve_mcp_tools(config) == []
    view.stop()


def test_mcp_settings_generate_a_stable_secret_without_logging_or_copying_env(config, monkeypatch, caplog):
    for name in (mcp_server.TOKEN_ENV_VAR, mcp_server.PORT_ENV_VAR, mcp_server.HOST_ENV_VAR):
        monkeypatch.delenv(name, raising=False)
    assert not mcp_server.is_enabled(config)
    monkeypatch.setenv(mcp_server.HOST_ENV_VAR, '0.0.0.0')
    with pytest.raises(RuntimeError, match='loopback'):
        mcp_server._load_settings(config)
    assert not config.has_global('mcp_ops_token')
    monkeypatch.delenv(mcp_server.HOST_ENV_VAR)
    monkeypatch.setenv(mcp_server.TOKEN_ENV_VAR, 'env-secret')
    assert mcp_server.load_token(config) == 'env-secret'
    assert not config.has_global('mcp_ops_token')
    monkeypatch.delenv(mcp_server.TOKEN_ENV_VAR)
    with caplog.at_level(logging.WARNING):
        token = mcp_server.load_token(config)
    assert len(token) >= 32 and token not in caplog.text
    monkeypatch.setenv(mcp_server.TOKEN_ENV_VAR, 'another-env-secret')
    assert mcp_server.load_token(config) == token
    config.flush()
    assert config.get_global('mcp_ops_token') == token


@pytest.mark.asyncio
async def test_mcp_bearer_auth_refuses_missing_or_wrong_credentials():
    middleware = mcp_server.BearerTokenMiddleware(None, 'test-token')
    downstream = AsyncMock(return_value=Response(status_code=204))
    for header in (b'', b'Bearer wrong', b'Basic test-token'):
        request = Request({'type': 'http', 'headers': [(b'authorization', header)]})
        assert (await middleware.dispatch(request, downstream)).status_code == 401
    downstream.assert_not_called()
    request = Request({'type': 'http', 'headers': [(b'authorization', b'Bearer test-token')]})
    assert (await middleware.dispatch(request, downstream)).status_code == 204
    downstream.assert_awaited_once()
