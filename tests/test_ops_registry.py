"""Discord history privacy and guild confinement."""
import asyncio

import pytest

from core.ops import OpContext, registry


class _Perms:
    def __init__(self, read_messages=True, read_message_history=True):
        self.read_messages = read_messages
        self.read_message_history = read_message_history


class _FakeMember:
    """hasattr(actor, 'guild_permissions') marks a real Member to the gates."""
    guild_permissions = object()


class _FakeGuild:
    def __init__(self, gid, channels):
        self.id = gid
        self._channels = {c.id: c for c in channels}

    def get_channel(self, cid):
        return self._channels.get(cid)

    def get_channel_or_thread(self, cid):
        return self._channels.get(cid)


class _FakeChannel:
    def __init__(self, cid, perms, messages=(), guild=None):
        self.id = cid
        self._perms = perms
        self._messages = list(messages)
        self.guild = guild
        self.history_calls = 0

    def permissions_for(self, member):
        return self._perms

    def history(self, limit=100):
        self.history_calls += 1
        messages = self._messages[:limit]

        async def gen():
            for m in messages:
                yield m
        return gen()


def _search_ctx(guild, member=None):
    return OpContext(bot=None, author=member or _FakeMember(), guild=guild)


def test_search_history_drops_hits_where_actor_lacks_history_perm(monkeypatch):
    """Index path: a member with View Channel but NOT Read Message History
    must not receive hits from that channel, and the unfiltered index total
    must be omitted (it counts hidden-channel matches too)."""
    import core.ops as ops_module
    guild = _FakeGuild(1, [])
    visible = _FakeChannel(10, _Perms(True, True), guild=guild)
    denied = _FakeChannel(20, _Perms(True, False), guild=guild)  # the #71 combo
    guild._channels = {10: visible, 20: denied}
    hit_a = {"id": 1, "channel_id": 10, "content": "ok"}
    hit_b = {"id": 2, "channel_id": 20, "content": "secret"}

    async def fake_index(bot, gid, cids, limit, author_id, contains):
        return [hit_a, hit_b], 42
    monkeypatch.setattr(ops_module, "_index_search", fake_index)

    res = asyncio.run(registry.call("search_history", _search_ctx(guild)))
    assert res.ok
    assert res.value["messages"] == [hit_a]
    assert res.value["count"] == 1
    assert "total_matches" not in res.value


def test_search_history_fallback_refuses_history_denied_actor(monkeypatch):
    """Fallback path: the recent-window scan reads with the bot's perms and
    bypasses per-hit filtering, so a history-denied member must be refused
    outright — and the channel's history must never be iterated."""
    import core.ops as ops_module

    async def broken_index(*a, **k):
        raise RuntimeError("index cold")
    monkeypatch.setattr(ops_module, "_index_search", broken_index)

    guild = _FakeGuild(1, [])
    denied = _FakeChannel(20, _Perms(True, False), guild=guild)
    guild._channels = {20: denied}

    res = asyncio.run(registry.call(
        "search_history", _search_ctx(guild), channels=[denied]))
    assert res.ok
    assert res.value["messages"] == []
    assert res.value["count"] == 0
    assert "Read Message History" in res.value["note"]
    assert denied.history_calls == 0


class _HistoryChannel(_FakeChannel):
    """_FakeChannel whose history() honors the read_history signature and
    yields newest-first, like Discord's default."""

    def history(self, limit=100, before=None, after=None):
        self.history_calls += 1
        self.seen_cursors = (before, after)
        messages = self._messages[:limit]

        async def gen():
            for m in messages:
                yield m
        return gen()


def test_read_history_refuses_history_denied_actor():
    """Same #71 policy as search_history's fallback: the generic gate only
    checks read_messages, so the op itself must enforce Read Message History
    — and the channel's history must never be iterated on a refusal."""
    guild = _FakeGuild(1, [])
    denied = _HistoryChannel(20, _Perms(True, False), guild=guild)
    guild._channels = {20: denied}
    res = asyncio.run(registry.call("read_history", _search_ctx(guild),
                                    channel=denied))
    assert res.ok is False
    assert "Read Message History" in res.error
    assert denied.history_calls == 0


@pytest.mark.asyncio
async def test_channel_resolution_cannot_escape_the_invoking_guild():
    from types import SimpleNamespace as NS
    from unittest.mock import AsyncMock

    channel = NS(id=10, guild=NS(id=8), send=AsyncMock())
    bot = NS(get_channel=lambda cid: channel)
    ctx = OpContext(bot=bot, author=NS(id=1), guild=NS(id=7))
    denied = await registry.call_ids('send_message', ctx, allowed_guild_ids={7},
                                     channel_id='10', content='private')
    assert not denied.ok
    channel.send.assert_not_called()
    channel.guild = None
    denied = await registry.call_ids('send_message', ctx, allowed_guild_ids={7},
                                     channel_id='10', content='private')
    assert not denied.ok
    channel.send.assert_not_called()
