"""Recognized shortcut syntax must not suppress actual command failures."""
import logging
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
from discord import app_commands
from discord.ext import commands
import pytest

from core import error_handler as eh
from cogs.rng import RNG


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
