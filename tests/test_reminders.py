"""Due reminders must survive transient delivery failures."""
import logging
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
import pytest

from cogs.reminders import Reminders


@pytest.mark.asyncio
async def test_due_reminder_survives_transient_failure_and_is_dropped_on_forbidden(config):
    # Any send exception used to drop the reminder, so a Discord 5xx lost it for good.
    row = {"user_id": 1, "timestamp": 0, "text": "check the oven", "delay": 600}
    config.set(None, "reminders", [row])
    user = NS(send=AsyncMock(side_effect=discord.DiscordServerError(
        NS(status=503, reason="Service Unavailable"), "unavailable")))
    bot = NS(config=config, logger=logging.getLogger("test.reminders"),
             get_user=lambda user_id: user)
    cog = Reminders.__new__(Reminders)
    cog.bot, cog.logger = bot, bot.logger

    await cog.check_reminders.coro(cog)
    assert config.get(None, "reminders") == [row]

    user.send.side_effect = discord.Forbidden(NS(status=403, reason="Forbidden"), "cannot DM")
    await cog.check_reminders.coro(cog)
    assert config.get(None, "reminders") == []
    assert user.send.await_count == 2
