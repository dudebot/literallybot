"""Startup credentials and ownership must not strand or overwrite an install."""
import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
import pytest

from core import bootstrap


def test_env_token_wins_without_being_persisted(config):
    config.set_global('discord_token', 'stored')
    config.flush()
    bot = NS(config=config)
    seen = []
    bootstrap.run_bot(bot, run=seen.append, env={'DISCORD_TOKEN': 'environment'},
                      stdin_is_tty=False)
    assert seen == ['environment']
    assert bot._pending_token is None
    assert json.loads((Path(config.config_dir) / 'global.json').read_text())['discord_token'] == 'stored'
    assert bootstrap.resolve_token(config, env={}, stdin_is_tty=False) == ('stored', 'config')


def test_noninteractive_start_fails_without_prompting(config):
    def prompt(*args):
        pytest.fail('A service cannot answer stdin')
    with pytest.raises(SystemExit) as error:
        bootstrap.resolve_token(config, env={}, stdin_is_tty=False, prompt=prompt)
    assert error.value.code != 0


def test_rejected_prompt_token_is_not_saved_before_verified_retry(config):
    bot = NS(config=config)
    answers = iter(['typo', 'valid'])
    attempts = []

    def login(token):
        attempts.append(token)
        assert not config.has_global('discord_token')
        assert 'discord_token' not in json.loads((Path(config.config_dir) / 'global.json').read_text())
        if token == 'typo':
            raise discord.LoginFailure('bad token')
        bootstrap.persist_token(config, bot._pending_token)

    bootstrap.run_bot(bot, run=login, env={}, stdin_is_tty=True,
                      prompt=lambda _: next(answers))
    assert attempts == ['typo', 'valid']
    assert json.loads((Path(config.config_dir) / 'global.json').read_text())['discord_token'] == 'valid'


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


@pytest.mark.asyncio
async def test_owner_claim_during_lookup_is_not_overwritten(config):
    async def lookup():
        config.set_global('superadmins', [42])
        return NS(owner=NS(id=999), team=None)
    assert await bootstrap.bootstrap_superadmin(NS(config=config, application_info=lookup)) is None
    assert config.get_global('superadmins') == [42]
