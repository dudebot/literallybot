"""Team-owned applications must bootstrap the real human owner."""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
import pytest

from core import bootstrap


@pytest.mark.asyncio
async def test_first_run_grants_real_team_owner_and_preserves_existing_owner(config):
    team = discord.Team(state=None, data={
        'id': '123', 'name': 'Team', 'icon': None, 'owner_user_id': '777', 'members': []})
    bot = NS(config=config, application_info=AsyncMock(return_value=NS(owner=NS(id=555), team=team)))
    assert await bootstrap.bootstrap_superadmin(bot) == 777
    assert config.get_global('superadmins') == [777]
    bot.application_info.return_value = NS(owner=NS(id=999), team=None)
    assert await bootstrap.bootstrap_superadmin(bot) is None
    assert config.get_global('superadmins') == [777]
    assert bot.application_info.await_count == 1


def test_rejected_prompted_token_exits_without_saving_or_rerunning(config):
    # A closed discord.Client cannot run again; a retry logged in and exited 0 unconnected.
    bot = NS(config=config)
    attempts = []

    def run(token):
        attempts.append(token)
        raise discord.LoginFailure('Improper token has been passed.')

    with pytest.raises(SystemExit) as exited:
        bootstrap.run_bot(bot, run=run, env={}, stdin_is_tty=True,
                          prompt=lambda _: 'typo-token')
    assert exited.value.code == 1
    assert attempts == ['typo-token']
    assert bot._pending_token is None
    config.flush()
    assert config.get_global(bootstrap.TOKEN_CONFIG_KEY) is None
