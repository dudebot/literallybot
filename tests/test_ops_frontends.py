"""Agent replay and settings component-limit failures."""
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
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


def _use_model(gpt, config, monkeypatch, respond):
    config.set_global('agent_ops_whitelist', {'send_message': True, 'add_role': True})
    monkeypatch.setattr(gpt, '_prepare_cached_history', AsyncMock(
        return_value=([{'role': 'user', 'content': 'go'}], ('key',), 18, 0)))
    monkeypatch.setattr(gpt.llm, '_build_model', lambda *args: FunctionModel(respond))
    monkeypatch.setattr(gpt, '_check_cooldown', lambda ctx: None)


@pytest.mark.asyncio
async def test_oversized_parallel_batch_runs_up_to_budget_and_still_replies(
        config, gpt_run, monkeypatch, tool_telemetry_dir):
    # A tool-call limit used to reject the whole batch before any call ran.
    import json
    gpt, ctx = gpt_run
    config.set(7, 'agent_ops_gate', {'send_message': 'everyone'})

    def respond(messages, info):
        if len(messages) > 1 and messages[-1].parts[0].part_kind == 'tool-return':
            return ModelResponse(parts=[TextPart('done')])
        return ModelResponse(parts=[
            ToolCallPart('send_message', {'channel_id': '10', 'content': str(n)})
            for n in range(10)])

    _use_model(gpt, config, monkeypatch, respond)
    await gpt.process_askgpt(ctx, 'go')

    assert ctx.channel.send.await_count == 8
    assert ctx.send.await_args.args[0] == 'done'
    row = json.loads(next(tool_telemetry_dir.glob('*.jsonl')).read_text())
    assert [c['status'] for c in row['calls']].count('refused_budget') == 2
    assert row['requests'] == 2 and row['outcome'] == 'reply'


@pytest.mark.asyncio
async def test_only_tools_the_invoker_may_call_are_offered(config, gpt_run, monkeypatch):
    # Admin-gated tools were sent to everyone and refused when called.
    gpt, ctx = gpt_run
    config.set(7, 'agent_ops_gate', {'send_message': 'everyone', 'add_role': 'admin'})
    offered = []

    def respond(messages, info):
        offered.append(sorted(tool.name for tool in info.function_tools))
        return ModelResponse(parts=[TextPart('hi')])

    _use_model(gpt, config, monkeypatch, respond)
    await gpt.process_askgpt(ctx, 'go')
    config.set(7, 'admins', [1])
    await gpt.process_askgpt(ctx, 'go')

    assert offered == [['send_message'], ['add_role', 'send_message']]


@pytest.mark.asyncio
async def test_chat_cannot_attach_host_files_outside_the_media_library(
        config, gpt_run, monkeypatch, tmp_path):
    # Configured access says who may send, not which host files they may read.
    gpt, ctx = gpt_run
    config.set(7, 'agent_ops_gate', {'send_message': 'everyone'})
    secret = tmp_path / 'notes.txt'
    secret.write_text('private')
    results = []

    def respond(messages, info):
        last = messages[-1].parts[0]
        if last.part_kind == 'tool-return':
            results.append(last.content)
            return ModelResponse(parts=[TextPart('no')])
        return ModelResponse(parts=[ToolCallPart(
            'send_message', {'channel_id': '10', 'file_paths': [str(secret)]})])

    _use_model(gpt, config, monkeypatch, respond)
    await gpt.process_askgpt(ctx, 'go')

    ctx.channel.send.assert_not_awaited()
    assert results[0]['ok'] is False and 'media library' in results[0]['error']


@pytest.mark.asyncio
async def test_opening_a_marked_op_to_everyone_needs_confirmation(config):
    # Operators may open anything; a stray click must not be enough.
    config.set_global('agent_ops_whitelist', {'add_role': True, 'send_message': True})
    view, interaction = panel(config)
    config.set(7, 'admins', [1])
    interaction.response.send_modal = AsyncMock()
    try:
        await view._save_gate_bucket(interaction, 'everyone', ['add_role', 'send_message'])
        assert not config.get(7, 'agent_ops_gate')
        modal = interaction.response.send_modal.await_args.args[0]
        modal.confirm._value = 'yes'
        await modal.on_submit(interaction)
        assert config.get(7, 'agent_ops_gate') == {
            'add_role': 'everyone', 'send_message': 'everyone'}
        modal.stop()
    finally:
        view.stop()


def test_cost_uses_operator_prices_for_a_model_the_table_does_not_know():
    # Cost was null for every run on a model newer than the built-in table.
    from core.llm.usage import UsageRecord, estimate_cost
    record = UsageRecord('xai', 'not-in-any-table', 1_000_000, 1_000_000, 2_000_000,
                         cached_prompt_tokens=500_000)
    assert estimate_cost(record) is None
    assert estimate_cost(record, {'cost_per_mtok_input': 2, 'cost_per_mtok_output': 10,
                                  'cache_input_ratio': 0.25}) == 11.25
