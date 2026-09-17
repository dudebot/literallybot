"""Logging contract: shortcuts are quiet; exceptions and denials stay visible."""
import asyncio
import logging
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
from discord import app_commands
from discord.ext import commands
import pytest

from core.config import Config
from core import error_handler as eh
from core.utils import is_admin, is_superadmin, PANEL_SLASH_PERMISSIONS
from cogs.optional.rng import RNG
from cogs.optional.error_handler import ErrorLoggingAdmin, LogSettingsView, _RateLimitModal


@pytest.fixture
def config(tmp_path):
    store = Config(str(tmp_path / 'configs'))
    yield store
    store.flush()
    if store._reload_timer:
        store._reload_timer.cancel()
    if store._save_timer:
        store._save_timer.cancel()


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
@pytest.mark.parametrize('text,handled', [
    ('!99d67', True), ('!999d67', True), ('!d6', True), ('!2D20', True),
    ('!2d101', True), ('! 2d20 ', True), ('!2d20 extra', False),
    ('!2d20\nextra', False), ('!nonsense', False), ('!dice 2d6', False)])
async def test_regex_matches_listener(config, text, handled):
    bot, ctx, _, channels = environment(config)
    bot.get_prefix = AsyncMock(return_value='!')
    rng = RNG(bot)
    bot.cogs['RNG'] = rng
    ctx.message.content = text
    await rng.on_message(ctx.message)
    assert channels[10].send.called == handled
    assert eh._suppresses_command_not_found(bot, ctx, commands.CommandNotFound()) == handled
    assert not eh._suppresses_command_not_found(bot, ctx, RuntimeError('dice failed'))
    assert not eh._suppresses_command_not_found(bot, ctx, commands.CommandInvokeError(RuntimeError('dice failed')))


@pytest.mark.asyncio
async def test_regex_follows_real_cog_lifecycle_and_prefix(config):
    bot = commands.Bot(command_prefix=['!', '?'], intents=discord.Intents.none())
    bot.logger = logging.getLogger('test.logs')
    _, ctx, _, _ = environment(config)
    ctx.message.content = '?2d20'
    ctx.prefix = '?'
    await bot.add_cog(RNG(bot))
    assert eh._suppresses_command_not_found(bot, ctx, commands.CommandNotFound())
    await bot.remove_cog('RNG')
    assert not eh._suppresses_command_not_found(bot, ctx, commands.CommandNotFound())
    await bot.close()


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
    assert embed.title == 'ℹ️ Command not found'
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
    assert embed.title == '⚠️ Command denied'
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
    assert embed.title == '❌ Error detected'
    fields = {f.name: f.value for f in embed.fields}
    assert 'RuntimeError' in fields['Error Type']
    assert 'real dice bug' in fields['Traceback']


@pytest.mark.asyncio
async def test_event_exception_survives_unknown_toggle(config):
    bot, ctx, _, channels = environment(config)
    config.set_global('error_logging', {'default_channel': 20})
    try:
        raise RuntimeError('listener failed')
    except RuntimeError:
        await eh.handle_event_error(bot, 'on_message', ctx.message)
    await asyncio.sleep(0)
    assert channels[20].send.await_count == 1
    assert 'listener failed' in str(channels[20].send.call_args.kwargs['embed'].to_dict())


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
async def test_broken_hook_is_reported(config):
    bot, ctx, _, channels = environment(config)
    config.set_global('error_logging', {'default_channel': 20})
    def broken(ctx, error):
        raise RuntimeError('broken hook')
    eh.register_error_whitelist_hook(broken)
    await eh.handle_command_error(bot, ctx, commands.CommandNotFound('99d67'))
    await asyncio.sleep(0)
    assert channels[20].send.await_count == 1
    assert 'broken hook' in str(channels[20].send.call_args.kwargs['embed'].to_dict())


def test_bot_permission_failures_are_errors():
    for err in [commands.BotMissingPermissions(['send_messages']),
                app_commands.BotMissingPermissions(['send_messages'])]:
        assert eh._determine_severity(err) == eh.ErrorSeverity.ERROR


@pytest.mark.asyncio
async def test_panel_auth_scopes_and_store_preservation(config):
    bot, ctx, ix, channels = environment(config)
    config.set(42, 'admins', [1])
    config.set(42, 'error_logging', {'default_channel': 10, 'category_channels': {'event_error': 30}})
    panel = LogSettingsView(bot, ctx.author, ctx.guild)
    assert not any(getattr(c, 'label', '') == 'Global' for c in panel.walk_children())
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


@pytest.mark.asyncio
async def test_panel_channel_permissions_and_foreign_invoker(config):
    bot, ctx, ix, channels = environment(config)
    config.set_global('superadmins', [1])
    panel = LogSettingsView(bot, ctx.author, ctx.guild)
    channels[20].permissions_for = lambda member: discord.Permissions.none()
    await panel.set_channel(ix, 'global', 'default_channel', 20)
    assert config.get_global('error_logging') is None
    ix.user = NS(id=2)
    await panel.set_channel(ix, 'global', 'default_channel', 30)
    assert config.get_global('error_logging') is None


@pytest.mark.asyncio
async def test_panel_controls_and_modal_persist(config):
    bot, ctx, ix, _ = environment(config)
    config.set_global('superadmins', [1])
    config.set_global('error_logging', {'default_channel': 20, 'severity_channels': {'critical': 30}})
    panel = LogSettingsView(bot, ctx.author, ctx.guild)
    panel.page = 'global'
    panel.build()
    assert panel.total_children_count < 40
    assert panel.to_components()
    toggle = next(c for c in panel.walk_children() if getattr(c, 'label', '') == 'Command not found: Off')
    await toggle.callback(ix)
    modal = _RateLimitModal(panel)
    modal.minutes._value = '12'
    await modal.on_submit(ix)
    assert config.get_global('error_logging') == {
        'default_channel': 20, 'severity_channels': {'critical': 30},
        'log_unknown_commands': True, 'rate_limit_minutes': 12}
    modal.minutes._value = '0'
    await modal.on_submit(ix)
    assert config.get_global('error_logging')['rate_limit_minutes'] == 12


def test_panel_entrypoints(config):
    bot, _, _, _ = environment(config)
    cog = ErrorLoggingAdmin(bot)
    slash = next(c for c in cog.get_app_commands() if c.name == 'logsettings')
    assert slash.guild_only and slash.default_permissions == PANEL_SLASH_PERMISSIONS
    assert is_admin in slash.checks
    assert is_admin in cog.logsettings.checks and cog.logsettings.hidden
    assert 'errorlog' not in cog.logsettings.aliases


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


@pytest.mark.asyncio
async def test_legacy_command_route_is_preserved(config):
    bot, ctx, _, channels = environment(config)
    config.set_global('error_logging', {'default_channel': 20, 'log_unknown_commands': True,
        'category_channels': {'command_error': 30}, 'severity_channels': {'info': 10}})
    await eh.handle_command_error(bot, ctx, commands.CommandNotFound('99d67'))
    assert channels[30].send.await_count == 1
    assert not channels[20].send.called and not channels[10].send.called


def test_media_hook_matches_full_listener_input(config, monkeypatch, tmp_path):
    from cogs.optional.media import Media
    monkeypatch.chdir(tmp_path)
    directory = tmp_path / 'media' / '42'
    directory.mkdir(parents=True)
    (directory / 'wave.png').write_text('placeholder')
    bot, ctx, _, _ = environment(config)
    media = Media(bot)
    for text, expected in [('!wave', True), ('!wa', True), ('!WAVE', True),
                           ('!wave extra', False), ('!wave ', False), ('!', False)]:
        ctx.message.content = text
        assert media._is_media_command(ctx, commands.CommandNotFound()) == expected
    media.cog_unload()
    assert not eh._command_error_whitelist_hooks


@pytest.mark.asyncio
async def test_long_unknown_command_embed_fits_discord_limits(config):
    bot, ctx, _, channels = environment(config)
    config.set_global('error_logging', {'default_channel': 20, 'log_unknown_commands': True})
    ctx.invoked_with = 'x' * 1900
    await eh.handle_command_error(bot, ctx, commands.CommandNotFound(ctx.invoked_with))
    embed = channels[20].send.call_args.kwargs['embed']
    assert all(len(f.value) <= 1024 for f in embed.fields)
    assert len(embed) <= 6000
