"""Destructive cleanup must stay within the target author and time window."""
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
import pytest
import pytest_asyncio

from cogs.honeypot import Honeypot, KEY


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
