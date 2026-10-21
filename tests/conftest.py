"""Real config stores in temporary directories; drive timer callbacks explicitly."""
import pytest
import pytest_asyncio

from core.config import Config


class ManualTimer:
    def __init__(self, interval, function):
        self.function = function
        self.cancelled = False

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True


@pytest.fixture
def config_factory(tmp_path, monkeypatch):
    monkeypatch.setattr('core.config.Timer', ManualTimer)
    stores = []

    def create(path=None):
        store = Config(str(path or tmp_path / 'configs'))
        stores.append(store)
        return store

    yield create
    for store in stores:
        store.shutdown()


@pytest.fixture
def config(config_factory):
    return config_factory()


@pytest.fixture
def discord_environment(config):
    """Local Discord permission/hierarchy behavior; network calls stay mocked."""
    import logging
    import discord
    from discord.ext import commands

    bot = commands.Bot(command_prefix='!', intents=discord.Intents(guilds=True, members=True))
    bot.config = config
    bot.logger = logging.getLogger('test.guild_events')
    bot._connection.user = discord.ClientUser(state=bot._connection, data={
        'id': '999', 'username': 'bot', 'discriminator': '0', 'avatar': None})
    roles = [dict(id=str(rid), name=name, position=position, permissions=str(permissions),
                  color=0, hoist=False, managed=False, mentionable=False)
             for rid, name, position, permissions in [
                 (42, '@everyone', 0, 1024 | 2048 | 65536), (50, 'Member', 1, 0),
                 (51, 'Restricted', 2, 0), (52, 'Administrator', 3, 8), (53, 'Bot', 4, 8)]]
    guild = discord.Guild(state=bot._connection, data={
        'id': '42', 'name': 'Test guild', 'owner_id': '1000', 'roles': roles,
        'members': [dict(user=dict(id=str(uid), username=str(uid), discriminator='0', avatar=None),
                         roles=role_ids, joined_at='2026-01-01T00:00:00+00:00', flags=0)
                    for uid, role_ids in [(1, ['50']), (2, ['52']), (999, ['53'])]],
        'channels': [dict(id=str(cid), name=name, type=0, position=cid, permission_overwrites=[])
                     for cid, name in [(10, 'trap'), (11, 'other'), (13, 'log')]]})
    bot._connection._add_guild(guild)
    return bot, guild


@pytest_asyncio.fixture
async def isolated_bot_module(config, tmp_path, monkeypatch):
    """Load the real entrypoint without opening deployment config or logs."""
    import importlib.util
    import logging
    from pathlib import Path
    from core.ops import OpsRegistry

    source = Path(__file__).resolve().parents[1] / 'bot.py'
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('core.config.Config', lambda: config)
    root_logger = logging.getLogger()
    prior_handlers = set(root_logger.handlers)
    prior_level = root_logger.level
    spec = importlib.util.spec_from_file_location('isolated_test_bot', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'ops_registry', OpsRegistry())
    try:
        yield module
    finally:
        await module.bot.close()
        for handler in set(root_logger.handlers) - prior_handlers:
            root_logger.removeHandler(handler)
            handler.close()
        root_logger.setLevel(prior_level)
