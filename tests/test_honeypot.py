"""Guard role safety and destructive cleanup, using real Config and Discord roles."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
import pytest
import pytest_asyncio

from cogs.honeypot import Honeypot, KEY, REASON
from core.ops import OpContext, registry
from core import event_logging


@pytest_asyncio.fixture
async def trap(discord_environment, monkeypatch):
    bot, guild = discord_environment
    cog = Honeypot(bot)
    await cog.cog_load()
    bot.config.set(guild.id, KEY, dict(enabled=True, channel_id=10, role_id=51, minutes=10,
                                     strip_all_other_roles=False))
    monkeypatch.setattr(discord.Member, 'add_roles', AsyncMock())
    monkeypatch.setattr(discord.TextChannel, 'send', AsyncMock())
    try:
        yield cog, guild, guild.get_member(1)
    finally:
        cog.cog_unload()


@pytest.mark.asyncio
async def test_configuration_validates_roles_and_scope_and_reenable_defaults(trap):
    cog, guild, member = trap
    config = cog.bot.config
    config.rem(guild.id, KEY)
    for role_id, error in [(None, 'Specify an existing role'), (42, 'non-managed'),
                           (52, 'non-managed'), (53, 'non-managed')]:
        with pytest.raises(ValueError, match=error):
            await cog.configure(guild, guild.get_channel(10), guild.get_role(role_id))
    assert not config.has(guild.id, KEY)
    result = await cog.configure(guild, guild.get_channel(10), guild.get_role(51),
                                 minutes=60, strip_all_other_roles=True)
    assert result['role_id'] == '51' and result['channel_id'] == '10'
    assert config.get(guild.id, KEY)['strip_all_other_roles'] is True
    await cog.configure(guild, None, enabled=False)
    disabled = config.get(guild.id, KEY)
    assert disabled == dict(enabled=False, channel_id=10, role_id=51, minutes=60,
                            strip_all_other_roles=True)
    # The op resolves the explicit channel's guild and saved role, using the
    # same validation as commands; confinement rejects another guild first.
    registry.register_cog_ops(cog)
    try:
        ctx = OpContext(bot=cog.bot, guild=guild, author=guild.get_member(2))
        refused = await registry.call_ids('configure_honeypot', ctx, allowed_guild_ids={99}, channel_id='11')
        assert not refused.ok and config.get(guild.id, KEY) == disabled
        result = await registry.call_ids('configure_honeypot', ctx, allowed_guild_ids={42}, channel_id='11')
        assert result.ok
        assert config.get(guild.id, KEY) == dict(enabled=True, channel_id=11, role_id=51, minutes=10,
                                               strip_all_other_roles=False)
    finally:
        registry.unregister_owner(cog)
    # Moderation setup does not require sending, channel management or voice
    # movement permissions in the trap.
    guild.default_role._permissions = discord.Permissions(view_channel=True, read_message_history=True).value
    guild.get_role(53)._permissions = discord.Permissions(
        manage_roles=True, manage_messages=True, view_channel=True, read_message_history=True).value
    await cog.configure(guild, guild.get_channel(10))
    assert not guild.get_channel(10).permissions_for(guild.me).send_messages
    guild.get_role(53)._permissions &= ~discord.Permissions(manage_messages=True).value
    saved = config.get(guild.id, KEY).copy()
    with pytest.raises(ValueError, match="Manage Messages"):
        await cog.configure(guild, guild.get_channel(11))
    assert config.get(guild.id, KEY) == saved
    # Editing role permissions after setup must not let a trigger grant admin.
    guild.get_role(51)._permissions = 8
    removed, failures = await cog._apply_roles(member, guild.get_role(51), True)
    discord.Member.add_roles.assert_not_awaited()
    assert not removed and failures


@pytest.mark.asyncio
async def test_trigger_exemptions_concurrency_and_retry_survive_report_failure(trap, monkeypatch):
    cog, guild, member = trap
    started, release = asyncio.Event(), asyncio.Event()

    async def cleanup(*args, **kwargs):
        started.set()
        await release.wait()
        return 2, []

    cog._cleanup = AsyncMock(side_effect=cleanup)
    message = NS(guild=guild, author=member, channel=guild.get_channel(11), id=101,
                 created_at=discord.utils.utcnow(), delete=AsyncMock())
    await cog.on_message(message)
    message.channel, message.author = guild.get_channel(10), guild.get_member(2)
    await cog.on_message(message)
    cog.bot.config.set(guild.id, 'admins', [member.id])
    message.author = member
    await cog.on_message(message)
    cog.bot.config.set(guild.id, 'admins', [])
    discord.Member.add_roles.assert_not_awaited()
    # A thread parent also triggers. Unwritable logs must not interrupt cleanup.
    message.channel = NS(id=90, parent_id=10)
    discord.TextChannel.send.side_effect = discord.Forbidden(NS(status=403, reason='Forbidden'), 'No logging access')
    discord.Member.add_roles.side_effect = discord.Forbidden(NS(status=403, reason='Forbidden'), 'No role access')
    running = asyncio.create_task(cog.on_message(message))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        await cog.on_message(message)
        message.delete.assert_awaited_once()
        assert cog._cleanup.await_count == 1
    finally:
        release.set()
        await running
    assert not cog._active
    discord.Member.add_roles.side_effect = None
    # Also works with event delivery disabled, and retries after a prior failure.
    event_logging.update_route(cog.bot, guild, 'feature:honeypot', enabled=False)
    await cog.on_message(message)
    assert discord.Member.add_roles.await_count == 2 and cog._cleanup.await_count == 2
    # A missing role still leads to cleanup rather than aborting the listener.
    guild._roles.pop(51)
    await cog.on_message(message)
    assert cog._cleanup.await_count == 3 and not cog._active


@pytest.mark.asyncio
async def test_stripping_fetches_fresh_roles_preserves_unmanageable_and_continues(trap, monkeypatch):
    cog, guild, member = trap
    # The stale event member has only its original role. REST returns additions
    # made after the message, including the newly assigned restriction role.
    fresh = discord.Member(data={'user': {'id': '1', 'username': '1', 'discriminator': '0', 'avatar': None},
                                 'roles': ['50', '51', '54', '55', '56'], 'flags': 0},
                           guild=guild, state=cog.bot._connection)
    for rid, managed, position in [(54, False, 1), (55, True, 1), (56, False, 5)]:
        guild._roles[rid] = discord.Role(guild=guild, state=cog.bot._connection, data={
            'id': str(rid), 'name': str(rid), 'permissions': '0', 'position': position, 'managed': managed})
    fetch = AsyncMock(return_value=fresh)
    remover = AsyncMock()
    monkeypatch.setattr(discord.Guild, 'fetch_member', fetch)
    monkeypatch.setattr(discord.Member, 'remove_roles', remover)
    role = guild.get_role(51)
    assert await cog._apply_roles(member, role, False) == ([], [])
    fetch.assert_not_awaited()
    discord.Member.add_roles.side_effect = discord.Forbidden(NS(status=403, reason='Forbidden'), 'Denied')
    removed, failures = await cog._apply_roles(member, role, True)
    assert not removed and failures
    remover.assert_not_awaited()
    fetch.assert_not_awaited()
    discord.Member.add_roles.side_effect = None

    async def remove(other, **kwargs):
        if other.id == 50:
            raise discord.Forbidden(NS(status=403, reason='Forbidden'), 'Denied')
    remover.side_effect = remove
    removed, failures = await cog._apply_roles(member, role, True)
    assert removed == [54]
    assert {call.args[0].id for call in remover.call_args_list} == {50, 54}
    assert all(call.kwargs == dict(reason=REASON, atomic=True) for call in remover.call_args_list)
    assert any('50: Forbidden' in failure for failure in failures)
    assert all(any(str(rid) in failure for failure in failures) for rid in (55, 56))
    settings = cog._settings(guild)
    settings['strip_all_other_roles'] = True
    cog.bot.config.set(guild.id, KEY, settings)
    cog._cleanup = AsyncMock(return_value=(1, []))
    await cog.on_message(NS(guild=guild, author=member, channel=guild.get_channel(10), id=101,
                            created_at=discord.utils.utcnow()))
    cog._cleanup.assert_awaited_once()
    reports = '\n'.join(call.args[0] for call in discord.TextChannel.send.call_args_list)
    assert 'Removed roles: 54' in reports and 'INCOMPLETE' in reports


@pytest.mark.asyncio
async def test_cleanup_bounds_threads_dedup_and_inaccessible_history(trap, monkeypatch):
    cog, guild, member = trap
    now = discord.utils.utcnow()
    start = now - timedelta(minutes=7)
    own = NS(id=201, author=member, created_at=now - timedelta(minutes=1), delete=AsyncMock())
    other = NS(id=202, author=guild.get_member(2), created_at=own.created_at, delete=AsyncMock())
    old = NS(id=203, author=member, created_at=start - timedelta(seconds=1), delete=AsyncMock())
    future = NS(id=204, author=member, created_at=now + timedelta(seconds=1), delete=AsyncMock())
    threaded = NS(id=205, author=member, created_at=own.created_at, delete=AsyncMock())
    scanned = []

    async def history(channel, **kwargs):
        scanned.append(channel.id)
        assert kwargs['after'] == start and kwargs['before'] == now
        if channel.id == 11:
            raise discord.Forbidden(NS(status=403, reason='Forbidden'), 'No access')
        for candidate in [own, other, old, future] if channel.id == 10 else [threaded] if channel.id == 90 else []:
            # External Discord history honors these query boundaries.
            if kwargs['after'] < candidate.created_at < kwargs['before']:
                yield candidate

    async def thread_history(**kwargs):
        async for candidate in history(thread, **kwargs):
            yield candidate

    thread = NS(id=90, archive_timestamp=now, created_at=now - timedelta(days=100),
                history=thread_history, permissions_for=lambda _: discord.Permissions.all())
    expired = NS(id=91, archive_timestamp=start - timedelta(seconds=1))
    queried = []

    async def archives(channel, **kwargs):
        queried.append((channel.id, kwargs.get('private', False)))
        if channel.id == 10:
            yield thread
            yield expired

    monkeypatch.setattr(discord.TextChannel, 'history', history)
    monkeypatch.setattr(discord.TextChannel, 'archived_threads', archives)
    monkeypatch.setattr(discord.Guild, 'active_threads', AsyncMock(return_value=[thread]))
    message = NS(id=101, guild=guild, channel=guild.get_channel(10), author=member,
                 created_at=now, delete=AsyncMock())
    deleted, failures = await cog._cleanup(message, 7, before=now)
    assert deleted == 3 and failures == ['history 11: Forbidden']
    assert scanned.count(90) == 1 and 91 not in scanned
    assert (10, False) in queried and (10, True) in queried
    other.delete.assert_not_awaited()
    old.delete.assert_not_awaited()
    future.delete.assert_not_awaited()
    scanned.clear()
    queried.clear()
    assert await cog._cleanup(message, 0) == (1, [])
    assert not scanned and not queried
    # Discord can return empty history for missing read access; preflight must
    # still identify incomplete coverage instead of silently claiming success.
    monkeypatch.setattr(discord.TextChannel, 'permissions_for', lambda *a: discord.Permissions.none())
    _, failures = await cog._cleanup(message, 7, before=now)
    assert any('missing View Channel' in failure for failure in failures)
