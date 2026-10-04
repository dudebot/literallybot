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
async def test_danbooru_service_enforces_rating_on_query_and_response(config, monkeypatch):
    import requests
    from urllib.parse import parse_qs, urlsplit

    cog = Danbooru(NS(config=config, logger=logging.getLogger('test.search')))
    # Exercise the real service; only the HTTP boundary is replaced.
    response = Mock()
    response.json.return_value = [
        {'id': 1, 'rating': 's', 'file_url': 'https://example.invalid/filtered.png'},
        {'id': 2, 'file_url': 'https://example.invalid/unverified.png'},
        {'id': 3, 'rating': 'g', 'file_url': 'https://example.invalid/image.png'},
    ]
    request = Mock(return_value=response)
    monkeypatch.setattr('cogs.optional.danbooru.requests.get', request)
    channel = NS(id=10, is_nsfw=lambda: False)
    tag = 'cat&tags=landscape'
    result = await cog.search([tag, 'rating:sensitive'], channel)
    assert result['url'] == 'https://example.invalid/image.png'
    prepared = requests.Request('GET', request.call_args.args[0],
                                params=request.call_args.kwargs['params']).prepare()
    assert parse_qs(urlsplit(prepared.url).query)['tags'] == [tag + ' rating:general']
    assert cog.posted_danbooru == {3}

    # API error objects and HTTP failures are errors, not post lists or no-results.
    response.json.return_value = {'success': False, 'message': 'request rejected'}
    assert (await cog.search(['cat'], channel))['status'] == 'error'
    response.raise_for_status.side_effect = requests.HTTPError('unavailable')
    assert (await cog.search(['cat'], channel))['status'] == 'error'


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
