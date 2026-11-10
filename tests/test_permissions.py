"""Every admin path the bot honours, and the denials that bound them."""
from types import SimpleNamespace as NS

import discord

from core.permissions import is_admin, is_superadmin


def test_admin_and_superadmin_matrix(discord_environment, config):
    bot, guild = discord_environment
    guild._add_member(discord.Member(state=bot._connection, guild=guild, data=dict(
        user=dict(id='1000', username='owner', discriminator='0', avatar=None),
        roles=['50'], joined_at='2026-01-01T00:00:00+00:00', flags=0)))
    config.set_global('superadmins', [7])
    config.set(guild.id, 'admins', [3])

    def ctx(actor, where=guild):
        return NS(bot=bot, author=actor, guild=where)

    superadmin, listed = NS(id=7), NS(id=3)
    plain, administrator, owner, own_account = (guild.get_member(i) for i in (1, 2, 1000, 999))

    # A wrong grant here hands a stranger the admin panels; a wrong denial
    # locks every path the docs promise out of them.
    assert is_superadmin(ctx(superadmin)) and is_admin(ctx(superadmin))
    assert is_superadmin(ctx(superadmin, None)) and is_admin(ctx(superadmin, None))
    assert is_admin(ctx(listed)) and not is_superadmin(ctx(listed))
    assert is_admin(ctx(administrator)) and not is_superadmin(ctx(administrator))
    assert is_admin(ctx(owner))
    assert not is_admin(ctx(plain))
    assert not is_admin(ctx(listed, None)) and not is_admin(ctx(administrator, None))
    # The bot's own Administrator role must not let a self-authored command escalate.
    assert own_account.guild_permissions.administrator
    assert not is_admin(ctx(own_account)) and not is_superadmin(ctx(own_account))
