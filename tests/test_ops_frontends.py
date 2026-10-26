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

from cogs.gpt import AiSettingsView, Gpt
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
    monkeypatch.setattr(gpt.llm, "chat", AsyncMock(side_effect=AssertionError("chat called")))

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
@pytest.mark.parametrize('route', ['direct', 'after_action', 'parallel', 'nudge'])
async def test_explicit_quiet_ends_run_without_posting_or_leaking_to_next_turn(config, gpt_run, route):
    gpt, ctx, responses = gpt_run
    config.set_global('agent_ops_whitelist', {'send_message': True})
    config.set(7, 'agent_ops_gate', {'send_message': 'everyone'})
    actions = int(route in ('after_action', 'parallel'))
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
async def test_unintentional_blank_is_not_silence_and_uses_agent(gpt_run):
    gpt, ctx, responses = gpt_run
    requests = responses([[TextPart('   ')]])

    await gpt.process_askgpt(ctx, 'hello')

    assert len(requests) == 1
    ctx.send.assert_awaited_once()
    assert ctx.send.await_args.args[0].strip()
    assert not requests[0].function_tools
    assert [tool.name for tool in requests[0].output_tools] == ['stay_quiet']


@pytest.mark.asyncio
async def test_budget_fallback_uses_agent_without_bot_tools(config, gpt_run, monkeypatch):
    from pydantic_ai.exceptions import UsageLimitExceeded
    gpt, ctx, _ = gpt_run
    config.set_global('agent_ops_whitelist', {'send_message': True})
    config.set(7, 'agent_ops_gate', {'send_message': 'everyone'})
    config.set(7, 'ai_tool_budget', 12)
    run = AsyncMock(side_effect=[UsageLimitExceeded('test budget'), NS(text='Best effort', usage=None)])
    monkeypatch.setattr(gpt.llm, 'run_agent', run)
    await gpt.process_askgpt(ctx, 'hello')
    first, fallback = run.await_args_list
    assert first.kwargs['tools']
    assert first.kwargs['max_tool_calls'] == 24
    assert fallback.kwargs['tools'] == []
    assert '[SYSTEM] The tool budget ran out' in fallback.args[1][-1]['content']
    assert fallback.kwargs['output_type'][1].name == 'stay_quiet'
    assert ctx.send.await_args.args[0] == 'Best effort'


@pytest.mark.asyncio
async def test_provider_image_400_after_action_does_not_replay(config, gpt_run, monkeypatch):
    from pydantic_ai.exceptions import ModelHTTPError
    gpt, ctx, _ = gpt_run
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
    for name in (mcp_server.TOKEN_ENV_VAR, mcp_server.PORT_ENV_VAR,
                 mcp_server.HOST_ENV_VAR, mcp_server.TRUSTED_HOSTS_ENV_VAR):
        monkeypatch.delenv(name, raising=False)
    assert not mcp_server.is_enabled(config)
    monkeypatch.setenv(mcp_server.HOST_ENV_VAR, '0.0.0.0')
    with pytest.raises(RuntimeError, match='MCP_OPS_TRUSTED_HOSTS'):
        mcp_server._load_settings(config)
    assert not config.has_global('mcp_ops_token')
    monkeypatch.setenv(mcp_server.TRUSTED_HOSTS_ENV_VAR, '192.0.2.10')
    monkeypatch.setenv(mcp_server.TOKEN_ENV_VAR, 'env-secret')
    assert mcp_server._load_settings(config)[2] == '0.0.0.0'
    assert not config.has_global('mcp_ops_token')
    monkeypatch.setenv(mcp_server.HOST_ENV_VAR, 'not-a-bind')
    with pytest.raises(RuntimeError, match='not an IP address'):
        mcp_server._load_settings(config)
    assert not config.has_global('mcp_ops_token')
    monkeypatch.delenv(mcp_server.HOST_ENV_VAR)
    monkeypatch.delenv(mcp_server.TRUSTED_HOSTS_ENV_VAR)
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


@pytest.fixture
def self_surface(config, monkeypatch):
    from core import agent_gate, agent_loop
    from core.ops import OpsRegistry, OpScope, PermissionLevel, OpParam, ParamKind
    reg = OpsRegistry()
    writes = []

    @reg.op('save_value', 'Save own text.', PermissionLevel.EVERYONE,
            scope=OpScope.SELF, sequential=True,
            params=[OpParam('text', ParamKind.STRING)], serialize=lambda value: value)
    async def save(ctx, text):
        if text == 'sensitive-failure':
            raise ValueError(text)
        writes.append((ctx.author.id, text))
        return {'text': text}

    @reg.op('guild_probe', 'Probe guild.', PermissionLevel.EVERYONE)
    async def probe(ctx):
        writes.append(('guild', ctx.guild.id))
        return {}

    for module in (agent_gate, agent_loop, mcp_server):
        monkeypatch.setattr(module, 'registry', reg)
    config.set_global('agent_ops_whitelist', {name: True for name in reg.names()})
    ctx = NS(bot=NS(config=config, logger=logging.getLogger('test.self')),
             author=NS(id=1, bot=False), guild=None, channel=NS(id=10))
    return reg, ctx, writes


@pytest.mark.asyncio
async def test_self_policy_revocation_privileged_invokers_and_log_redaction(config, self_surface, caplog):
    from core.agent_gate import resolve_agent_tools
    reg, ctx, writes = self_surface
    config.set_global('superadmins', [1])
    assert resolve_agent_tools(ctx) == ['save_value']  # global absent => on; DM SELF only
    with pytest.raises(ValueError, match='Unsupported'):
        build_agent_tools(ctx, ctx.bot.logger, ['guild_probe'])
    caplog.set_level(logging.INFO)
    tool = build_agent_tools(ctx, ctx.bot.logger, ['save_value'])[0]
    assert tool.sequential
    assert (await tool.function(text='sensitive-success'))['text'] == 'sensitive-success'
    assert 'sensitive-failure' in (await tool.function(text='sensitive-failure'))['error']
    assert 'sensitive-' not in caplog.text
    config.set_global('ai_self_tools_enabled', False)
    with pytest.raises(ValueError, match='SELF'):
        build_agent_tools(ctx, ctx.bot.logger, ['save_value'])
    assert not (await tool.function(text='must-not-write'))['ok']
    config.set_global('ai_self_tools_enabled', True)
    config.set_global('agent_ops_whitelist', {})
    assert not (await tool.function(text='must-not-write'))['ok']
    config.set_global('agent_ops_whitelist', {'save_value': True, 'guild_probe': True})
    ctx.guild = NS(id=7, owner=None)
    assert 'save_value' not in resolve_agent_tools(ctx)  # public absent => off even for owner
    config.set(7, 'ai_self_tools_enabled', True)
    assert 'save_value' not in resolve_agent_tools(ctx)  # default per-op off
    config.set(7, 'agent_ops_gate', {'save_value': 'everyone'})
    tool = build_agent_tools(ctx, ctx.bot.logger, ['save_value'])[0]
    config.set(7, 'ai_self_tools_enabled', False)
    assert not (await tool.function(text='must-not-write'))['ok']
    config.set(7, 'ai_self_tools_enabled', True)
    config.set(7, 'agent_ops_gate', {'save_value': 'off'})
    assert not (await tool.function(text='must-not-write'))['ok']
    assert writes == [(1, 'sensitive-success')]


@pytest.mark.asyncio
async def test_mcp_self_exclusion_and_stale_dispatch_before_identity_lookup(config, self_surface):
    from core.ops import OpScope
    reg, ctx, writes = self_surface
    for stored in (None, ['save_value'], ['save_value', 'guild_probe'], []):
        if stored is not None:
            config.set_global('mcp_tools_enabled', stored)
        assert 'save_value' not in mcp_server.resolve_mcp_tools(config)
    assert 'save_value' not in mcp_server.resolve_mcp_tools(None)
    server = mcp_server.build_server()
    assert 'save_value' not in {t.name for t in await server.list_tools()}
    with pytest.raises(ValueError, match='SELF'):
        mcp_server._make_mcp_tool(None, reg.require('save_value'))
    # Existing wrapper and mutable/stale captured declaration must both fail
    # before resolving forged actor/guild ids, even in a schema-only build.
    op = reg.require('guild_probe')
    wrapper = mcp_server._make_mcp_tool(None, op)
    from dataclasses import replace
    reg._ops[op.name] = replace(op, scope=OpScope.SELF)
    with pytest.raises(mcp_server.BotUnavailableError, match='SELF'):
        await wrapper(actor_id='999', guild_id='987')
    reg._ops[op.name] = op
    op.scope = OpScope.SELF
    with pytest.raises(mcp_server.BotUnavailableError, match='SELF'):
        await wrapper(actor_id='999', guild_id='987')
    assert not writes


@pytest.mark.asyncio
async def test_shared_runner_mixed_budget_survives_nudge(config, self_surface, gpt_run):
    from core.agent_loop import run_agent_turn
    reg, ctx, writes = self_surface
    gpt, _, responses = gpt_run
    ctx.guild = NS(id=7, owner=None)
    config.set(7, 'ai_self_tools_enabled', True)
    config.set(7, 'agent_ops_gate', {'save_value': 'everyone', 'guild_probe': 'everyone'})
    requests = responses([
        [TextPart('run tool guild_probe')],
        [ToolCallPart('save_value', {'text': 'one'}, 'save'),
         ToolCallPart('guild_probe', {}, 'guild')],
        [ToolCallPart('save_value', {'text': 'over-budget'}, 'extra')],
        [TextPart('finished')],
    ])
    result = await run_agent_turn(gpt.llm, gpt.get_provider_config(ctx), [], ctx,
                                  ['save_value', 'guild_probe'], 'request', tool_budget=2)
    assert result == 'finished'
    assert set(writes) == {(1, 'one'), ('guild', 7)}
    assert len(requests) == 4


@pytest.mark.asyncio
async def test_self_provider_failures_cannot_escape_as_sensitive_tracebacks(config, self_surface, caplog):
    import traceback
    from core.agent_loop import run_agent_turn
    reg, ctx, _ = self_surface

    async def fail(*args, **kwargs):
        reg._ops.clear()  # prior exchanges remain private after withdrawal
        raise ValueError('sensitive-tool-exchange')

    with pytest.raises(RuntimeError) as caught:
        await run_agent_turn(NS(run_agent=fail), NS(provider='test', model='test'),
                             [], ctx, ['save_value'], 'request')
    rendered = ''.join(traceback.format_exception(caught.value))
    assert 'sensitive-tool-exchange' not in rendered
    assert 'sensitive-tool-exchange' not in caplog.text


@pytest.mark.asyncio
async def test_dm_default_budget_and_live_turn_validity(config, self_surface, gpt_run):
    from core.agent_loop import run_agent_turn
    reg, ctx, writes = self_surface
    gpt, _, responses = gpt_run
    responses([
        [ToolCallPart('save_value', {'text': str(i)}, str(i)) for i in range(9)],
        [ToolCallPart('stay_quiet', {}, 'quiet')],
    ])
    assert await run_agent_turn(gpt.llm, gpt.get_provider_config(ctx), [], ctx,
                                ['save_value'], 'request') is None
    assert writes == [(1, str(i)) for i in range(8)]
    valid = True
    tools = build_agent_tools(ctx, ctx.bot.logger, ['save_value'],
                              validity_check=lambda: valid)
    valid = False
    assert not (await tools[0].function(text='cancelled'))['ok']
    assert len(writes) == 8
