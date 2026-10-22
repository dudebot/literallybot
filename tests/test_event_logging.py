"""Shared routing must remain guild-scoped, live, and independent of errors."""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
import pytest
import pytest_asyncio

from cogs.error_handler import LogSettingsView
from cogs.logging import Logging
from core import error_handler
from core import event_logging as events


@pytest_asyncio.fixture
async def activity(discord_environment, monkeypatch):
    bot, guild = discord_environment
    cog = Logging(bot)
    await cog.cog_load()
    monkeypatch.setattr(discord.TextChannel, 'send', AsyncMock())
    try:
        yield cog, bot, guild
    finally:
        cog.cog_unload()


@pytest.mark.asyncio
async def test_existing_producers_follow_live_routes_and_independent_toggles(activity):
    cog, bot, guild = activity
    member = guild.get_member(1)
    await cog.on_member_join(member)
    assert discord.TextChannel.send.call_args.args[0] == 'Welcome <@1>'
    assert discord.TextChannel.send.call_args.kwargs['allowed_mentions'].to_dict() == discord.AllowedMentions.none().to_dict()
    # Record actual destinations; class-level AsyncMock doesn't bind self.
    delivered = []

    async def send(channel, text, **kwargs):
        delivered.append((channel.id, text))
    from unittest.mock import patch
    with patch.object(discord.TextChannel, 'send', send):
        events.update_route(bot, guild, 'default', default_channel=11)
        events.update_route(bot, guild, 'feature:membership', channel_id=10)
        events.update_route(bot, guild, 'category:membership.join', channel_id=13)
        await cog.on_member_join(member)
        await cog.on_member_remove(member)
        await cog.on_member_update(NS(nick='old', display_name='Old'),
                                   NS(nick='new', display_name='New', guild=guild))
        assert [cid for cid, _ in delivered] == [13, 10, 11]
        delivered.clear()
        events.update_route(bot, guild, 'category:membership.join', enabled=False)
        await cog.on_member_join(member)
        await cog.on_member_remove(member)
        assert [cid for cid, _ in delivered] == [10]
        delivered.clear()
        events.update_route(bot, guild, 'feature:membership', enabled=False)
        events.update_route(bot, guild, 'category:membership.join', enabled=True)
        await cog.on_member_join(member)
        await cog.on_member_update(NS(nick='old', display_name='Old'),
                                   NS(nick='new', display_name='New', guild=guild))
        assert [cid for cid, _ in delivered] == [11]
    # Disabled/unloaded categories retain choices when a feature comes back.
    saved = events.settings(bot, guild)
    cog.cog_unload()
    assert 'feature:membership' not in dict(events.routes())
    assert events.settings(bot, guild) == saved
    await cog.cog_load()
    assert events.route_settings(events.settings(bot, guild), 'feature:membership')['enabled'] is False


@pytest.mark.asyncio
async def test_event_delivery_failure_is_local_and_cannot_disable_errors(activity, monkeypatch, caplog):
    cog, bot, guild = activity
    discord.TextChannel.send.side_effect = discord.Forbidden(NS(status=403, reason='Forbidden'), 'Cannot send')
    assert not await events.report(bot, guild, 'membership.join', 'Hello')
    assert 'Event report delivery failed' in caplog.text
    discord.TextChannel.send.side_effect = None
    events.update_route(bot, guild, 'default', default_channel=123456)
    assert not await events.report(bot, guild, 'membership.join', 'Hello')
    assert 'No event log destination' in caplog.text
    # A configured missing target must not silently fall back to a public log.
    discord.TextChannel.send.reset_mock()
    events.update_route(bot, guild, 'default', enabled=False)
    bot.config.set(guild.id, 'error_logging', {'default_channel': 13})
    monkeypatch.setattr(error_handler, '_error_history', {})
    await error_handler.log_error_to_discord(bot, RuntimeError('required error'), 'test.events', guild_id=guild.id)
    assert discord.TextChannel.send.await_count == 1
    assert 'embed' in discord.TextChannel.send.call_args.kwargs


@pytest.mark.asyncio
async def test_activity_channel_edit_preserves_settings_and_rechecks_auth(activity):
    cog, bot, guild = activity
    panel = LogSettingsView(bot, guild.get_member(2), guild)
    panel.page, panel.activity_route = 'activity', 'category:membership.join'
    ix = NS(client=bot, user=guild.get_member(2), guild=guild, guild_id=guild.id,
            response=NS(is_done=lambda: False, send_message=AsyncMock(), edit_message=AsyncMock()))
    bot.config.set(guild.id, events.KEY, {
        'categories': {'disabled_feature.old': {'enabled': False}},
    })
    try:
        await panel.set_activity_channel(ix, panel.activity_route, 11)
        saved = events.settings(bot, guild)
        assert saved['categories'] == {
            'disabled_feature.old': {'enabled': False}, 'membership.join': {'channel_id': 11},
        }
        ix.user._roles.remove(52)
        await panel.set_activity_channel(ix, panel.activity_route, 10)
        assert events.settings(bot, guild) == saved
        assert ix.response.send_message.await_args.kwargs['ephemeral'] is True
    finally:
        panel.stop()


@pytest.mark.asyncio
async def test_bot_lifecycle_clears_registrations_on_failed_injection_and_teardown(isolated_bot_module):
    from discord import app_commands
    from discord.ext import commands
    from cogs.honeypot import Honeypot

    bot = isolated_bot_module.bot

    @app_commands.command(name='honeypot')
    async def collision(interaction: discord.Interaction):
        pass

    bot.tree.add_command(collision)
    try:
        with pytest.raises(app_commands.CommandAlreadyRegistered):
            await bot.add_cog(Honeypot(bot))
        assert events.registry.get('honeypot.incident') is None
        # discord.py leaves a prefix command behind on a slash collision;
        # remove it in this isolated test before exercising a separate owner.
        bot.remove_command('honeypot')
        bot.tree.remove_command('honeypot')
        installed = Honeypot(bot)
        await bot.add_cog(installed)
        with pytest.raises(discord.ClientException):
            await bot.add_cog(installed)
        assert events.registry.get('honeypot.incident') is not None
        await bot.remove_cog(installed.qualified_name)
        assert events.registry.get('honeypot.incident') is None

        class BrokenTeardown(commands.Cog):
            async def cog_load(self):
                events.registry.register(self, 'temporary', 'Temporary feature', {'incident': 'Incidents'})

            def cog_unload(self):
                raise RuntimeError('Teardown failed before unregister')

        cog = BrokenTeardown()
        await bot.add_cog(cog)
        assert events.registry.get('temporary.incident') is not None
        await bot.remove_cog(cog.qualified_name)
        assert events.registry.get('temporary.incident') is None
    finally:
        await bot.close()
