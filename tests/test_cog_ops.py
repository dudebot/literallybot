"""Cog policies whose failure changes permissions or makes recovery impossible."""
import logging
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from cogs.optional.danbooru import Danbooru
from cogs.optional.setrole import SetRole
from core.ops import OpResult, OpsRegistry
from core.utils import list_cog_modules


@pytest.mark.asyncio
async def test_role_conflict_preserves_stored_binding_and_reaches_frontend(config):
    bot = NS(config=config, logger=logging.getLogger('test.roles'))
    guild = NS(id=7, get_emoji=lambda eid: None)
    message = NS(reactions=[], add_reaction=AsyncMock())
    channel = NS(id=10, guild=guild, fetch_message=AsyncMock(return_value=message))
    original, replacement = NS(id=20), NS(id=21)
    cog = SetRole(bot)
    reg = OpsRegistry()
    reg.register_cog_ops(cog)
    operation = reg.require('add_emoji_role_toggle')
    await operation.impl(None, channel, 22, '👍', original)
    result = await operation.impl(None, channel, 22, '👍', replacement)
    payload = operation.result_payload(OpResult(ok=True, value=result))
    assert payload['status'] == 'exists'
    assert payload['old_role_id'] == '20'
    assert config.get(7, 'emoji_role_toggles')[0]['role_id'] == 20
    confirmed = await operation.impl(None, channel, 22, '👍', replacement, replace_existing=True)
    assert confirmed['status'] == 'updated'
    config.flush()
    assert config.get(7, 'emoji_role_toggles') == [
        {'channel_id': 10, 'message_id': 22, 'emoji': '👍', 'role_id': 21}]


@pytest.mark.asyncio
async def test_danbooru_service_strips_explicit_rating_before_request(config, monkeypatch):
    cog = Danbooru(NS(config=config, logger=logging.getLogger('test.search')))
    request = Mock(return_value=NS(json=lambda: [{'id': 1, 'file_url': 'https://example.invalid/image.png'}]))
    monkeypatch.setattr('cogs.optional.danbooru.requests.get', request)
    result = await cog.search(['cat', 'rating:explicit', 'RATING:questionable'], NS(id=10, is_nsfw=lambda: False))
    assert result['status'] == 'ok'
    url = request.call_args.args[0]
    assert 'rating%3Asafe' in url or 'rating:safe' in url
    assert 'explicit' not in url and 'questionable' not in url


def test_disabled_cogs_cannot_remove_the_recovery_surface(config, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for group, names in [('core', ['control', 'admin']), ('optional', ['gpt', 'points'])]:
        directory = tmp_path / 'cogs' / group
        directory.mkdir(parents=True)
        for name in names:
            (directory / f'{name}.py').touch()
    config.set_global('disabled_cogs', ['control', 'admin', 'gpt'])
    assert set(list_cog_modules('core', config)) == {'cogs.core.control', 'cogs.core.admin'}
    assert list_cog_modules('optional', config) == ['cogs.optional.points']


@pytest.mark.asyncio
async def test_cog_registration_is_atomic_and_binds_the_live_instance():
    from core.ops import OpContext, PermissionLevel, op

    class Working:
        @op('probe', 'Return instance state.', PermissionLevel.EVERYONE, group='test')
        async def probe(self, ctx):
            return {'value': self.value}

    class Collision:
        @op('new_probe', 'Would register before the collision.', PermissionLevel.EVERYONE)
        async def a(self, ctx):
            pass

        @op('probe', 'Collides with the loaded cog.', PermissionLevel.EVERYONE)
        async def z(self, ctx):
            pass

    registry = OpsRegistry()
    cog = Working()
    cog.value = 42
    registry.register_cog_ops(cog)
    with pytest.raises(ValueError, match='already registered'):
        registry.register_cog_ops(Collision())
    assert registry.get('new_probe') is None
    result = await registry.call('probe', OpContext(bot=None, author=None, guild=None))
    assert registry.require('probe').result_payload(result) == {'ok': True, 'value': 42}
    registry.unregister_owner(cog)
    assert registry.names() == []
