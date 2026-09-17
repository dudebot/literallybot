"""Failures and permission denials must remain visible, with independent routes."""
import logging
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
from discord import app_commands
from discord.ext import commands
import pytest

from core import error_handler as eh
from core.utils import is_admin, is_superadmin
from cogs.optional.rng import RNG
from cogs.optional.error_handler import LogSettingsView, _RateLimitModal


@pytest.fixture(autouse=True)
def clean_reporting(monkeypatch):
    monkeypatch.setattr(eh, '_error_history', {})
    monkeypatch.setattr(eh, '_command_error_whitelist_hooks', [])


def environment(config):
    guild = NS(id=42, name='Test guild', owner=None, me=NS(id=999))
    channels = {i: NS(id=i, guild=guild, send=AsyncMock(), permissions_for=lambda member:
                     discord.Permissions(view_channel=True, send_messages=True, embed_links=True))
                for i in (10, 20, 30)}
    bot = NS(config=config, user=NS(id=999), logger=logging.getLogger('test.logs'),
             cogs={}, get_guild=lambda gid: guild, get_channel=channels.get)
    actor = NS(id=1, bot=False)
    message = NS(content='!99d67', author=actor, channel=channels[10],
                 jump_url='https://discord.com/channels/42/10/123')
    ctx = NS(bot=bot, author=actor, guild=guild, channel=channels[10], message=message,
             prefix='!', command=None, invoked_with='99d67', send=AsyncMock())
    interaction = NS(client=bot, user=actor, guild=guild, guild_id=42,
                     channel=channels[10], command=None,
                     response=NS(is_done=lambda: False, send_message=AsyncMock(), edit_message=AsyncMock()),
                     followup=NS(send=AsyncMock()))
    return bot, ctx, interaction, channels


@pytest.mark.asyncio
async def test_unknown_opt_in_is_independent_for_each_destination(config, caplog):
    bot, ctx, _, channels = environment(config)
    config.set_global('error_logging', {'default_channel': 20})
    config.set(42, 'error_logging', {'default_channel': 30, 'log_unknown_commands': True})
    ctx.invoked_with = 'typo'
    ctx.message.content = '!typo'
    with caplog.at_level(logging.INFO):
        await eh.handle_command_error(bot, ctx, commands.CommandNotFound('typo'))
    assert channels[30].send.await_count == 1
    assert channels[20].send.await_count == 0
    embed = channels[30].send.call_args.kwargs['embed']
    assert 'Traceback' not in [f.name for f in embed.fields]
    assert 'typo' in caplog.text and 'https://discord.com/channels/42/10/123' in caplog.text
    assert all(r.levelno < logging.ERROR for r in caplog.records)
    # Enabling the other destination immediately must not be blocked by the first's cooldown.
    config.set_global('error_logging', {'default_channel': 20, 'log_unknown_commands': True})
    await eh.handle_command_error(bot, ctx, commands.CommandNotFound('typo'))
    assert channels[20].send.await_count == 1
    assert channels[30].send.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('slash', [False, True])
async def test_denial_uses_decorator_and_actual_level(config, slash):
    bot, ctx, ix, channels = environment(config)
    config.set_global('error_logging', {'default_channel': 20, 'log_unknown_commands': False})
    config.set(42, 'admins', [1])
    parent = NS(checks=[is_superadmin], parent=None)
    command = NS(name='private', qualified_name='group private', checks=[], parent=parent)
    ctx.command = ix.command = command
    if slash:
        await eh.handle_app_command_error(bot, ix, app_commands.CheckFailure('no'))
        message = ix.response.send_message.call_args.args[0]
    else:
        await eh.handle_command_error(bot, ctx, commands.CheckFailure('no'))
        message = ctx.send.call_args.args[0]
    assert 'Required: superadmin' in message and 'User level: admin' in message
    embed = channels[20].send.call_args.kwargs['embed']
    info = next(f.value for f in embed.fields if f.name == 'Additional Info')
    assert 'Required: superadmin' in info and 'User level: admin' in info
    assert 'Traceback' not in [f.name for f in embed.fields]


@pytest.mark.asyncio
@pytest.mark.parametrize('slash', [False, True])
async def test_real_command_exception_survives_regex_and_unknown_toggle(config, slash):
    bot, ctx, ix, channels = environment(config)
    config.set_global('error_logging', {'default_channel': 20, 'log_unknown_commands': False})
    bot.cogs['RNG'] = RNG(bot)
    try:
        raise RuntimeError('real dice bug')
    except RuntimeError as original:
        error = (app_commands.CommandInvokeError(NS(name='dice'), original) if slash
                 else commands.CommandInvokeError(original))
    if slash:
        await eh.handle_app_command_error(bot, ix, error)
    else:
        await eh.handle_command_error(bot, ctx, error)
    embed = channels[20].send.call_args.kwargs['embed']
    fields = {f.name: f.value for f in embed.fields}
    assert 'RuntimeError' in fields['Error Type']
    assert 'real dice bug' in fields['Traceback']


@pytest.mark.asyncio
async def test_routing_and_dedup_and_retry(config):
    bot, _, _, channels = environment(config)
    config.set_global('error_logging', {'default_channel': 20,
        'category_channels': {'command_denied': 30}, 'severity_channels': {'warning': 10}})
    config.set(42, 'error_logging', {'default_channel': 30})
    await eh.log_error_to_discord(bot, commands.CheckFailure('no'), 'test',
        category=eh.ErrorCategory.COMMAND_DENIED, severity=eh.ErrorSeverity.WARNING, guild_id=42)
    assert channels[30].send.await_count == 1
    assert not channels[10].send.called and not channels[20].send.called
    channels[20].send.side_effect = RuntimeError('temporary send failure')
    await eh.log_error_to_discord(bot, RuntimeError('bug'), 'retry')
    channels[20].send.side_effect = None
    await eh.log_error_to_discord(bot, RuntimeError('bug'), 'retry')
    assert channels[20].send.await_count == 2


@pytest.mark.asyncio
async def test_panel_auth_scopes_and_store_preservation(config):
    bot, ctx, ix, channels = environment(config)
    config.set(42, 'admins', [1])
    config.set(42, 'error_logging', {'default_channel': 10, 'category_channels': {'event_error': 30}})
    panel = LogSettingsView(bot, ctx.author, ctx.guild)
    await panel.set_channel(ix, 'global', 'default_channel', 20)
    assert config.get_global('error_logging') is None
    await panel.set_channel(ix, 'server', 'default_channel', 20)
    assert config.get(42, 'error_logging') == {'default_channel': 20, 'category_channels': {'event_error': 30}}
    # Modal opened while superadmin; submitting after demotion must not write.
    config.set_global('superadmins', [1])
    panel.page = 'global'
    panel.build()
    modal = _RateLimitModal(panel)
    modal.minutes._value = '12'
    config.set_global('superadmins', [])
    await modal.on_submit(ix)
    assert config.get_global('error_logging') is None
    # Guild permission revocation also applies to an already open panel.
    config.set(42, 'admins', [])
    await panel.set_channel(ix, 'server', 'default_channel', 30)
    assert config.get(42, 'error_logging')['default_channel'] == 20
    panel.stop()


@pytest.mark.asyncio
async def test_denials_do_not_hide_other_users(config):
    bot, ctx, _, channels = environment(config)
    config.set_global('error_logging', {'default_channel': 20})
    ctx.command = NS(name='admin', checks=[is_admin], parent=None)
    await eh.handle_command_error(bot, ctx, commands.CheckFailure('no'))
    await eh.handle_command_error(bot, ctx, commands.CheckFailure('no'))
    assert channels[20].send.await_count == 1
    ctx.author = NS(id=2, bot=False)
    await eh.handle_command_error(bot, ctx, commands.CheckFailure('no'))
    assert channels[20].send.await_count == 2
