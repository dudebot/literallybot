"""History privacy and destructive-operation regressions at real op boundaries."""
import asyncio

import discord
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


class _FakeMessage:
    def __init__(self, mid, channel, content):
        self.id = mid
        self.channel = channel
        self.author = type("A", (), {"id": 999})()
        self.content = content
        self.created_at = None


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
    assert "note" in res.value


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


class _NoActorCtx:
    """Bare ctx for direct-impl calls where the gate is not under test."""

    def __init__(self, bot=None):
        self.bot = bot
        self.author = None
        self.guild = None


def test_search_history_fallback_matches_embed_text(monkeypatch):
    """#log posts have empty content; Discord's index matches embed text,
    so the fallback scan must too."""
    import core.ops as ops_module

    async def broken_index(*a, **k):
        raise RuntimeError("index cold")
    monkeypatch.setattr(ops_module, "_index_search", broken_index)

    guild = _FakeGuild(1, [])
    chan = _FakeChannel(10, _Perms(True, True), guild=guild)
    hit = _FakeMessage(2, chan, "")
    hit.embeds = [discord.Embed(description="Config has no attribute get_user")]
    miss = _FakeMessage(1, chan, "")
    miss.embeds = [discord.Embed(description="something else")]
    chan._messages = [hit, miss]
    guild._channels = {10: chan}

    res = asyncio.run(registry.call(
        "search_history", _search_ctx(guild), channels=[chan],
        contains="get_user"))
    assert res.ok
    assert [m["id"] for m in res.value["messages"]] == [2]
    assert res.value["messages"][0]["embeds"][0]["description"].endswith(
        "get_user")


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


class _ForwardBot:
    def __init__(self, dest):
        self._dest = dest

    def get_channel(self, cid):
        return self._dest if cid == self._dest.id else None


class _ForwardDest:
    def __init__(self, cid, guild):
        self.id = cid
        self.guild = guild


class _ForwardMsg:
    def __init__(self, guild):
        self.guild = guild
        self.channel = type("C", (), {"id": 10, "guild": guild})()
        self.forwarded_to = None

    async def forward(self, destination):
        self.forwarded_to = destination
        return type("M", (), {
            "id": 999, "channel": type("C", (), {"id": destination.id})()})()


def test_forward_message_refuses_a_cross_guild_destination():
    """The destination is a bare snowflake, so guild confinement is enforced
    in-impl: it must belong to the SOURCE message's guild."""
    from core.ops import GuildNotAllowedError, forward_message
    source_guild = type("G", (), {"id": 1})()
    other_guild = type("G", (), {"id": 2})()
    dest = _ForwardDest(20, other_guild)
    msg = _ForwardMsg(source_guild)
    with pytest.raises(GuildNotAllowedError):
        asyncio.run(forward_message(
            _NoActorCtx(bot=_ForwardBot(dest)), msg, 20))
    assert msg.forwarded_to is None


class _AuthorCtx(_NoActorCtx):
    """Ctx with an author (for ops that stamp an audit reason string) but no
    real Member, so the visibility gate stays out of the way."""

    def __init__(self, bot=None):
        super().__init__(bot=bot)
        self.author = type("A", (), {
            "id": 321, "__str__": lambda self: "tester"})()


def _webhook(wid, name, channel_id, creator_id, creator_name):
    return type("WH", (), {
        "id": wid, "name": name, "channel_id": channel_id,
        "type": type("WT", (), {"name": "incoming"})(),
        "user": type("U", (), {"id": creator_id, "name": creator_name})(),
        "url": "https://discord.com/api/webhooks/SECRET/TOKEN",
        "token": "SECRET-TOKEN",
    })()


def test_list_webhooks_never_serializes_url_or_token():
    from core.ops import list_webhooks

    class _Guild:
        async def webhooks(self):
            return [_webhook(1, "gh-feed", 10, 42, "alice")]

    payload = asyncio.run(list_webhooks(_NoActorCtx(), _Guild()))
    assert payload["webhooks"][0]["id"] == 1
    # The credential must not appear ANYWHERE in the wire payload.
    import json
    flat = json.dumps(payload)
    assert "SECRET" not in flat and "TOKEN" not in flat


class _ModChannel:
    def __init__(self, cid=10):
        self.id = cid
        self.purge_kwargs = None

    async def purge(self, **kw):
        self.purge_kwargs = kw
        return [object(), object(), object()]

def test_purge_messages_filters_by_author():
    from core.ops import purge_messages
    chan = _ModChannel()
    author = type("M", (), {"id": 42})()
    payload = asyncio.run(purge_messages(_AuthorCtx(), chan, 50,
                                         author=author))
    assert chan.purge_kwargs["limit"] == 50
    check = chan.purge_kwargs["check"]
    assert check(type("Msg", (), {"author": author})())
    assert not check(type("Msg", (), {
        "author": type("A", (), {"id": 99})()})())
    assert payload == {"channel_id": 10, "deleted_count": 3}


@pytest.mark.asyncio
async def test_admin_gate_runs_before_resolution_and_rechecks_revocation(config):
    from types import SimpleNamespace as NS
    from unittest.mock import AsyncMock, Mock

    guild = NS(id=7, owner=None)
    message = NS(id=22, delete=AsyncMock())
    channel = NS(id=10, guild=guild, fetch_message=AsyncMock(return_value=message))
    bot = NS(config=config, user=NS(id=999), get_channel=Mock(return_value=channel))
    ctx = OpContext(bot=bot, author=NS(id=1), guild=guild)
    denied = await registry.call_ids('delete_message', ctx, channel_id='10', message_id='22')
    assert not denied.ok and 'admin' in denied.error
    bot.get_channel.assert_not_called()
    config.set(7, 'admins', [1])
    assert (await registry.call_ids('delete_message', ctx, channel_id='10', message_id='22')).ok
    message.delete.assert_awaited_once()
    config.set(7, 'admins', [])
    assert not (await registry.call('delete_message', ctx, message=message)).ok
    message.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_guild_admin_cannot_run_superadmin_destruction(config):
    from types import SimpleNamespace as NS
    from unittest.mock import AsyncMock

    guild = NS(id=7, owner=None)
    bot = NS(config=config, user=NS(id=999))
    ctx = OpContext(bot=bot, author=NS(id=1), guild=guild)
    channel = NS(id=10, purge=AsyncMock(return_value=[]))
    config.set(7, 'admins', [1])
    denied = await registry.call('purge_messages', ctx, channel=channel, limit=10)
    assert not denied.ok and 'superadmin' in denied.error
    channel.purge.assert_not_called()
    config.set_global('superadmins', [1])
    assert (await registry.call('purge_messages', ctx, channel=channel, limit=10)).ok
    channel.purge.assert_awaited_once()


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


@pytest.mark.asyncio
async def test_snowflake_wire_values_preserve_precision_and_hide_internal_controls():
    from core.ops import HISTORY_LIMIT_MAX

    search = registry.require('search_history')
    assert search.to_json_schema()['properties']['author_id']['type'] == 'string'
    resolved = await search.resolve_kwargs(None, None,
        {'author_id': '1208839321801465886', 'limit': 9999}, frozenset())
    assert resolved['author_id'] == 1208839321801465886
    assert resolved['limit'] == HISTORY_LIMIT_MAX
    send = registry.require('send_message').to_json_schema()
    assert send['properties']['channel_id']['type'] == 'string'
    assert 'allowed_mentions' not in send['properties']


@pytest.mark.asyncio
async def test_public_send_cannot_ping_or_read_host_attachments(config, tmp_path):
    from types import SimpleNamespace as NS
    from unittest.mock import AsyncMock

    guild = NS(id=7, owner=None)
    channel = NS(id=10, guild=guild, send=AsyncMock(return_value=NS(id=22, attachments=[])))
    ctx = OpContext(bot=NS(config=config, user=NS(id=999)), author=NS(id=1), guild=guild)
    assert (await registry.call('send_message', ctx, channel=channel, content='@everyone <@1>')).ok
    mentions = channel.send.call_args.kwargs['allowed_mentions']
    assert not mentions.everyone and not mentions.users and not mentions.roles
    attachment = tmp_path / 'private.png'
    attachment.write_bytes(b'private data')
    denied = await registry.call('send_message', ctx, channel=channel, file_paths=[str(attachment)])
    assert not denied.ok and 'admin' in denied.error.lower()
    channel.send.assert_awaited_once()
