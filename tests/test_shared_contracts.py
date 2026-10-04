"""Shared bot wiring and stored configuration compatibility."""
import logging
from types import SimpleNamespace as NS
import discord
import pytest
from discord.ext import commands
from core.ops import op, PermissionLevel, registry
from core.utils import get_superadmins, is_superadmin, is_admin
from cogs.optional.setrole import SetRole, TOGGLES_KEY, _emoji_matches


@pytest.mark.asyncio
async def test_bot_cog_lifecycle_registers_and_removes_operations(config):
    from bot import LiterallyBot
    class Example(commands.Cog):
        @op('test_example_action', 'Example action.', PermissionLevel.EVERYONE,
            params=[], serialize=lambda result: result)
        async def action(self, ctx):
            return {'value': 1}
    bot = LiterallyBot(command_prefix='!', intents=discord.Intents.none())
    bot.config = config
    bot.logger = logging.getLogger('test')
    before = set(registry.names())
    cog = Example()
    try:
        await bot.add_cog(cog)
        assert registry.require('test_example_action').owner is cog
        assert registry.require('test_example_action').origin == 'cog'
    finally:
        await bot.remove_cog(cog.qualified_name)
    assert set(registry.names()) == before


def test_legacy_reaction_roles_migrate_losslessly(config):
    config.set(10, TOGGLES_KEY, {'100': {'✅': 20, '100000000000000200': 30}})
    cog = SetRole(NS(config=config, logger=logging.getLogger('test')))
    entries = cog._entries(10)
    by_role = {entry['role_id']: entry for entry in entries}
    assert by_role[20] == {'channel_id': None, 'message_id': 100, 'emoji': '✅', 'role_id': 20}
    assert discord.PartialEmoji.from_str(by_role[30]['emoji']).id == 100000000000000200
    assert cog._entries(10) == entries
    assert config.get(10, TOGGLES_KEY) == entries


def test_reaction_identity_survives_custom_emoji_rename():
    assert _emoji_matches('<:old_name:100000000000000200>', discord.PartialEmoji(name='new_name', id=100000000000000200))
    assert not _emoji_matches('<:old_name:100000000000000200>', discord.PartialEmoji(name='old_name', id=100000000000000201))
    assert _emoji_matches('✅', discord.PartialEmoji(name='✅'))
    assert not _emoji_matches('✅', discord.PartialEmoji(name='✅', id=100000000000000200))


def test_legacy_superadmin_scalar_normalizes(config):
    config.set_global('superadmins', 42)
    assert get_superadmins(config) == [42]
    assert is_superadmin(config, 42)
    assert not is_superadmin(config, 43)


def test_guild_admin_does_not_gain_access_elsewhere(config):
    author = NS(id=42, guild_permissions=NS(administrator=False))
    bot = NS(config=config)
    config.set(10, 'admins', [42])
    ctx = NS(author=author, bot=bot, guild=NS(id=10, owner=NS(id=99)))
    assert is_admin(ctx)
    ctx.guild = NS(id=11, owner=NS(id=99))
    assert not is_admin(ctx)
