"""Cog policies whose failure changes permissions or makes recovery impossible."""
import logging
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from cogs.danbooru import Danbooru, _serialize_search
from cogs.setrole import SetRole
from core.ops import OpResult, OpsRegistry
from core.cog_discovery import list_cog_modules


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
        {'id': 3, 'rating': 'g', 'file_url': 'https://example.invalid/image.png',
         'tag_string': '1girl cat cat_paws', 'score': 19, 'fav_count': 7},
    ]
    request = Mock(return_value=response)
    monkeypatch.setattr('cogs.danbooru.requests.get', request)
    channel = NS(id=10, is_nsfw=lambda: False)
    tag = 'cat&tags=landscape'
    result = await cog.search([tag, 'rating:sensitive'], channel)
    assert result['url'] == 'https://example.invalid/image.png'
    assert result['tags'] == ['1girl', 'cat', 'cat_paws']
    assert result['score'] == 19 and result['favs'] == 7
    assert _serialize_search(result)['score'] == 19
    assert _serialize_search(result)['favs'] == 7
    assert _serialize_search(result)['tags'] == ['1girl', 'cat', 'cat_paws']
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
    directory = tmp_path / 'cogs'
    directory.mkdir()
    for name in ('control', 'admin', 'gpt', 'points'):
        (directory / f'{name}.py').write_text('raise RuntimeError("Discovery must not import features")')
    (directory / '_helper.py').touch()
    (directory / 'ignored').mkdir()
    config.set_global('disabled_cogs', ['control', 'cogs.core.admin', 'gpt'])
    assert list_cog_modules(config) == ['cogs.admin', 'cogs.control', 'cogs.points']
    assert set(list_cog_modules()) == {'cogs.admin', 'cogs.control', 'cogs.gpt', 'cogs.points'}


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


@pytest.mark.asyncio
@pytest.mark.parametrize('surface', ['prefix', 'panel'])
async def test_recovery_cog_cannot_be_disabled_through_operator_controls(config, discord_environment, tmp_path, monkeypatch, surface):
    from cogs.control import Control, CogsView

    bot, guild = discord_environment
    monkeypatch.chdir(tmp_path)
    directory = tmp_path / 'cogs'
    directory.mkdir()
    for name in ('control', 'admin', 'points'):
        (directory / f'{name}.py').touch()
    config.set_global('superadmins', [2])
    config.set_global('disabled_cogs', ['points'])
    cog = Control(bot)
    actor = guild.get_member(2)
    if surface == 'prefix':
        response = NS(edit=AsyncMock())
        ctx = NS(author=actor, send=AsyncMock(return_value=response),
                 message=NS(delete=AsyncMock()), channel=NS(id=10))
        await cog.disable.callback(cog, ctx, cog='cogs.core.control')
        assert 'protected' in response.edit.await_args.kwargs['content']
    else:
        panel = CogsView(cog, actor)
        try:
            panel.selected = 'control'
            panel._build()
            toggle = panel.children[1]
            assert toggle.disabled
            interaction = NS(client=bot, user=actor, guild=guild,
                             response=NS(send_message=AsyncMock()))
            # Invoke directly as well: button appearance alone is not enforcement.
            await toggle.callback(interaction)
            assert interaction.response.send_message.await_args.kwargs['ephemeral']
        finally:
            panel.stop()
    assert config.get_global('disabled_cogs') == ['points']


@pytest.mark.asyncio
async def test_points_feature_activation_owns_tools_without_removing_saved_balances(config, isolated_bot_module, tmp_path, monkeypatch):
    from core import agent_gate, mcp_server
    from core.ops import OpContext
    from core.points import add_points, get_points

    module = isolated_bot_module
    bot, registry = module.bot, module.ops_registry
    monkeypatch.setattr(agent_gate, 'registry', registry)
    monkeypatch.setattr(mcp_server, 'registry', registry)
    directory = tmp_path / 'cogs'
    directory.mkdir()
    (directory / 'points.py').touch()
    names = {'get_user_points', 'add_user_points', 'set_user_points'}
    whitelist = {name: True for name in names}
    gates = {name: 'everyone' for name in names}
    config.set_global('agent_ops_whitelist', whitelist)
    config.set_global('mcp_tools_enabled', sorted(names))
    config.set_global('disabled_cogs', ['points'])
    config.set_global('superadmins', [11])
    user = NS(id=42)
    assert add_points(bot, user, 7) == 7

    await bot.setup_hook()
    assert 'cogs.points' not in bot.extensions
    assert bot.get_command('points') is None
    assert not names.intersection(registry.names())
    assert agent_gate.agent_universe(whitelist, gates) == []
    assert mcp_server.resolve_mcp_tools(config) == []

    # The next startup selection loads the actual extension and binds its tools.
    config.set_global('disabled_cogs', [])
    await bot.setup_hook()
    assert not bot.failed_cogs
    assert bot.get_command('points') is not None
    assert names <= set(agent_gate.agent_universe(whitelist, gates))
    assert set(mcp_server.resolve_mcp_tools(config)) == names
    owner = bot.get_cog('Points')
    assert all(registry.require(name).owner is owner for name in names)
    ctx = OpContext(bot=bot, guild=NS(id=7), author=NS(id=11))
    result = await registry.call('add_user_points', ctx, user=user, amount=-3)
    assert result.ok and result.value == {'user_id': '42', 'points': 4}

    # Saving a disable is config-only; owner teardown removes the registered tools.
    config.set_global('disabled_cogs', ['points'])
    assert names <= set(registry.names())
    await bot.unload_extension('cogs.points')
    await bot.setup_hook()
    assert bot.get_command('points') is None
    assert not names.intersection(registry.names())
    assert agent_gate.agent_universe(whitelist, gates) == []
    assert mcp_server.resolve_mcp_tools(config) == []
    assert get_points(bot, user) == 4
    assert config.get_global('agent_ops_whitelist') == whitelist
    assert config.get_global('mcp_tools_enabled') == sorted(names)
