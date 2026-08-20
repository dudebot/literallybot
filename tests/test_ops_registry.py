"""Invariant tests for the ops registry (core/ops.py).

Ported from the former in-file `_smoke_test`, with one deliberate change of
approach: the hand-maintained literal set of expected op names is GONE.
A list of every op name, duplicated in a test, only ever fails when someone
adds an op — which is not a defect. These tests assert PROPERTIES that must
hold for every op, now and for ops not yet written:

- every op declares a scope and a known group;
- Discord snowflakes travel as strings (the 2**53 rounding trap);
- permission checks fail closed and surface as OpResult, never as raises;
- id resolution reports failures as errors, never exceptions.

Op-specific schema assertions remain only where the SHAPE encodes a
decision that would be silently reverted otherwise (DM ops take a user_id
and never a guild_id/channel_id; MESSAGE params imply channel_id; INTERNAL
params stay off the wire).

The 25-option select tripwire lives here too. It used to live in core/ops.py,
which had to `from cogs.optional.gpt import ...` to run it — core importing a
cog, the exact dependency inversion the ops layer exists to prevent. A test
importing a cog is fine; the module under test doing it is not.
"""

import asyncio

import discord
import pytest

from core.ops import (
    HISTORY_LIMIT_MAX,
    OP_GROUPS,
    ORIGIN_COG,
    ORIGIN_CORE,
    OpContext,
    OpParam,
    OpScope,
    OpsRegistry,
    ParamKind,
    PermissionLevel,
    load_discord_attachments,
    op,
    registry,
)
from core.agent_loop import agent_ops

ALL_OPS = sorted(registry.names())


# --------------------------------------------------------------------------
# Registry-wide properties — hold for every op, including ops added later.
# --------------------------------------------------------------------------

def test_registry_is_not_empty():
    assert ALL_OPS, "no ops registered — core/ops.py failed to import cleanly?"


@pytest.mark.parametrize("name", ALL_OPS)
def test_every_op_declares_scope_and_known_group(name):
    o = registry.require(name)
    assert isinstance(o.scope, OpScope)
    assert o.group in OP_GROUPS, (
        f"op '{name}' declares group '{o.group}', which is not in OP_GROUPS — "
        f"add it there (with a display label) or use an existing group")


@pytest.mark.parametrize("name", ALL_OPS)
def test_core_registrations_are_stamped_core(name):
    """Origin comes from the registration PATH. Everything registered inline
    in core/ops.py is 'core'; nothing here may claim 'cog'."""
    o = registry.require(name)
    assert o.origin == ORIGIN_CORE
    assert o.owner is None


@pytest.mark.parametrize("name", ALL_OPS)
def test_every_op_has_a_usable_schema(name):
    o = registry.require(name)
    assert isinstance(o.description, str) and o.description
    assert o.permission in PermissionLevel
    schema = o.to_json_schema()
    assert schema["type"] == "object"
    assert isinstance(schema["properties"], dict)
    assert isinstance(schema["required"], list)
    # Everything required must actually be a declared property.
    assert set(schema["required"]) <= set(schema["properties"])


@pytest.mark.parametrize("name", ALL_OPS)
def test_snowflake_params_travel_as_strings(name):
    """Discord ids exceed 2**53 and round in JSON doubles if typed "integer"
    (1208839321801465886 -> ...900), resolving to the wrong entity. Every id
    param must be a string on the wire; genuine scalar ints stay integers."""
    o = registry.require(name)
    schema = o.to_json_schema()
    for wp in o.wire_params():
        prop = schema["properties"][wp.name]
        if wp.name.endswith("_id") or wp.name == "id":
            assert prop["type"] == "string", (
                f"{name}.{wp.name} is typed {prop['type']!r}; snowflakes must "
                f"be strings — see _SNOWFLAKE_JSON_TYPE")
        if prop["type"] == "array":
            # One items type serves both array kinds; a non-string items type
            # needs a new facet on WireParam (and an mcp mapping update).
            assert prop["items"] == {"type": "string"}


@pytest.mark.parametrize("name", ALL_OPS)
def test_internal_params_never_reach_the_wire(name):
    o = registry.require(name)
    wire_names = {wp.name for wp in o.wire_params()}
    for p in o.params:
        if p.kind == ParamKind.INTERNAL:
            assert p.name not in wire_names


def test_list_tools_covers_every_op():
    tools = registry.list_tools()
    assert {t["name"] for t in tools} == set(ALL_OPS)
    for tool in tools:
        assert tool["permission"] in {"EVERYONE", "ADMIN", "SUPERADMIN"}
        assert tool["scope"] in {s.value for s in OpScope}
        assert tool["origin"] in {ORIGIN_CORE, ORIGIN_COG}


# --------------------------------------------------------------------------
# Live queries — the replacement for the deleted `agent=True` flag.
# --------------------------------------------------------------------------

def test_guild_agent_universe_is_exactly_the_guild_scoped_ops():
    universe = set(registry.guild_agent_names())
    assert universe == {o.name for o in registry.ops() if o.scope == OpScope.GUILD}
    # DM and global ops are guild-confinement-incompatible and must not leak
    # into a guild-confined, user-actored loop.
    for name in ("send_dm", "read_dms", "fetch_dms", "delete_dm", "edit_dm",
                 "list_dm_conversations", "add_dm_reaction",
                 "remove_dm_reaction", "list_dm_pins",
                 "list_guilds", "get_user"):
        assert name not in universe


def test_scope_assignments():
    assert {o.name for o in registry.ops(scope=OpScope.DM)} == {
        "send_dm", "read_dms", "fetch_dms", "delete_dm", "edit_dm",
        "list_dm_conversations", "add_dm_reaction", "remove_dm_reaction",
        "list_dm_pins"}
    assert {o.name for o in registry.ops(scope=OpScope.GLOBAL)} == {
        "list_guilds", "get_user"}


def test_grouped_partitions_every_op_exactly_once():
    grouped = registry.grouped()
    seen = [o.name for _gid, _label, ops in grouped for o in ops]
    assert sorted(seen) == ALL_OPS, "grouped() dropped or duplicated an op"
    for gid, label, ops in grouped:
        assert ops, "grouped() should omit empty groups"
        assert label == OP_GROUPS[gid]


def test_group_filter_matches_grouped():
    for gid, _label, ops in registry.grouped():
        assert [o.name for o in registry.ops(group=gid)] == [o.name for o in ops]


# --------------------------------------------------------------------------
# Shape decisions worth locking (these encode choices, not inventory).
# --------------------------------------------------------------------------

def test_message_param_implies_channel_id():
    edit = registry.get("edit_message").to_json_schema()
    assert set(edit["properties"]) == {"channel_id", "message_id", "content"}


def test_send_message_shape():
    send = registry.get("send_message").to_json_schema()
    assert set(send["properties"]) == {
        "channel_id", "content", "reference_message_id", "file_paths",
        "sticker_id"}
    # content is optional: a message may be attachment- or sticker-only.
    assert send["required"] == ["channel_id"]
    assert send["properties"]["file_paths"]["type"] == "array"
    assert send["properties"]["sticker_id"]["type"] == "string"


def test_search_history_is_guild_wide_by_default():
    search = registry.get("search_history").to_json_schema()
    assert set(search["properties"]) == {
        "guild_id", "channel_ids", "limit", "author_id", "contains"}
    # No required scope => omitting channel_ids searches the whole guild.
    assert search["required"] == []
    assert search["properties"]["limit"]["maximum"] == HISTORY_LIMIT_MAX
    assert search["properties"]["limit"]["type"] == "integer"


def test_role_ops_take_guild_from_wire_or_ambient_context():
    roles = registry.get("add_role").to_json_schema()
    assert set(roles["properties"]) == {"guild_id", "user_id", "role_id"}
    assert set(roles["required"]) == {"user_id", "role_id"}


@pytest.mark.parametrize("name,required", [
    ("send_dm", {"user_id"}),
    ("read_dms", {"user_id"}),
    ("fetch_dms", {"user_id"}),
    ("delete_dm", {"user_id", "message_id"}),
    ("edit_dm", {"user_id", "message_id", "content"}),
    ("add_dm_reaction", {"user_id", "message_id", "emoji"}),
    ("remove_dm_reaction", {"user_id", "message_id", "emoji"}),
    ("list_dm_pins", {"user_id"}),
])
def test_dm_ops_take_a_user_id_and_nothing_guild_shaped(name, required):
    """DM ops resolve a guild-independent USER, one-to-one with the DM API.
    A raw DM channel id is never accepted, and the removed guild_id must not
    creep back in (see the DM section comment in core/ops.py). delete_dm's
    message_id is a SNOWFLAKE scalar, not a MESSAGE param — MESSAGE would
    drag in a required channel_id and the guild-refusing resolver."""
    schema = registry.get(name).to_json_schema()
    assert "user_id" in schema["properties"]
    assert schema["properties"]["user_id"]["type"] == "string"
    assert "channel_id" not in schema["properties"]
    assert "guild_id" not in schema["properties"]
    assert set(schema["required"]) == required


def test_forward_message_destination_is_a_snowflake_not_a_channel_param():
    """A second CHANNEL param would collide with MESSAGE's implied channel_id
    wire name (same dodge delete_dm documents), so the destination travels as
    its own snowflake string and is resolved + guild-confined in-impl."""
    schema = registry.get("forward_message").to_json_schema()
    assert set(schema["properties"]) == {
        "channel_id", "message_id", "destination_channel_id"}
    assert schema["properties"]["destination_channel_id"]["type"] == "string"
    assert set(schema["required"]) == {
        "channel_id", "message_id", "destination_channel_id"}


def test_read_history_shape_and_clamps():
    """Plain chronological read: cursor pagination like fetch_dms, limit
    clamped to the shared history cap."""
    schema = registry.get("read_history").to_json_schema()
    assert set(schema["properties"]) == {
        "channel_id", "limit", "before_message_id", "after_message_id"}
    assert schema["required"] == ["channel_id"]
    assert schema["properties"]["limit"]["maximum"] == HISTORY_LIMIT_MAX
    assert schema["properties"]["before_message_id"]["type"] == "string"


def test_list_pins_limit_clamps_to_discords_pin_cap():
    schema = registry.get("list_pins").to_json_schema()
    assert set(schema["properties"]) == {"channel_id", "limit"}
    assert schema["properties"]["limit"]["maximum"] == 50


def test_send_poll_shape():
    schema = registry.get("send_poll").to_json_schema()
    assert set(schema["properties"]) == {
        "channel_id", "question", "answers", "duration_hours", "multiselect"}
    assert schema["properties"]["answers"]["type"] == "array"
    assert schema["properties"]["duration_hours"]["maximum"] == 168
    assert set(schema["required"]) == {"channel_id", "question", "answers"}


def test_emoji_ids_are_snowflake_strings():
    create = registry.get("create_emoji").to_json_schema()
    assert set(create["properties"]) == {"guild_id", "name", "file_path"}
    edit = registry.get("edit_emoji").to_json_schema()
    assert edit["properties"]["emoji_id"]["type"] == "string"


# --------------------------------------------------------------------------
# Coercion, permissions, resolution.
# --------------------------------------------------------------------------

def test_snowflake_scalars_coerce_and_integers_clamp():
    """SNOWFLAKE scalars arrive as decimal strings and become ints centrally,
    so op impls never coerce their own ids; INTEGER scalars are clamped."""
    coerced = asyncio.run(registry.get("search_history").resolve_kwargs(
        bot=None, guild=None,
        raw={"author_id": "1208839321801465886", "limit": 9999},
        allowed_guild_ids=frozenset()))
    assert coerced["author_id"] == 1208839321801465886
    assert coerced["limit"] == HISTORY_LIMIT_MAX


def test_permission_check_fails_closed_without_an_actor():
    """A no-actor context must fail closed on anything above EVERYONE, and
    must never raise — failures surface as OpResult.error."""
    ctx = OpContext(bot=None, author=None, guild=None)
    result = asyncio.run(registry.call("delete_message", ctx, message=None))
    assert result.ok is False
    assert "actor" in (result.error or "").lower()


def test_unknown_op_is_an_error_not_a_raise():
    ctx = OpContext(bot=None, author=None, guild=None)
    result = asyncio.run(registry.call("not_a_real_op", ctx))
    assert result.ok is False
    assert "Unknown op" in (result.error or "")


class _FakeBot:
    """Bot stub with no gateway: every resolution attempt fails."""

    def get_channel(self, cid):
        return None

    async def fetch_channel(self, cid):
        raise RuntimeError("no gateway in tests")


def test_call_ids_surfaces_resolution_failure_as_an_error():
    ctx = OpContext(bot=_FakeBot(), author=None, guild=None)
    res = asyncio.run(registry.call_ids("send_message", ctx,
                                        channel_id=123, content="hi"))
    assert res.ok is False
    assert "Could not resolve channel" in res.error


def test_call_ids_rejects_unknown_parameters():
    ctx = OpContext(bot=_FakeBot(), author=None, guild=None)
    res = asyncio.run(registry.call_ids("list_guilds", ctx, bogus=1))
    assert res.ok is False
    assert "Unexpected parameter" in res.error


# --------------------------------------------------------------------------
# Attachment path validation (no Discord needed).
# --------------------------------------------------------------------------

def test_attachment_loads_a_valid_file(tmp_path):
    good = tmp_path / "ok.gif"
    good.write_bytes(b"GIF89a-fake")
    files = load_discord_attachments([str(good)])
    assert len(files) == 1
    files[0].close()


# --------------------------------------------------------------------------
# search_history actor-permission enforcement (#71). History is gated on
# BOTH View Channel and Read Message History — Discord treats them as
# distinct permissions, and search_history reads with the BOT's perms, so
# the invoking member's own history permission must be enforced in the op.
# --------------------------------------------------------------------------

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


def test_search_history_allowed_actor_gets_hits_and_total(monkeypatch):
    """A member with both permissions everywhere is unaffected: all hits
    returned, total_matches present, no filtering note."""
    import core.ops as ops_module
    guild = _FakeGuild(1, [])
    chan = _FakeChannel(10, _Perms(True, True), guild=guild)
    guild._channels = {10: chan}
    hits = [{"id": 1, "channel_id": 10, "content": "ok"}]

    async def fake_index(bot, gid, cids, limit, author_id, contains):
        return list(hits), 42
    monkeypatch.setattr(ops_module, "_index_search", fake_index)

    res = asyncio.run(registry.call("search_history", _search_ctx(guild)))
    assert res.ok
    assert res.value["messages"] == hits
    assert res.value["total_matches"] == 42
    assert "note" not in res.value


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


def test_search_history_fallback_matches_whole_words(monkeypatch):
    """Fallback path: `contains` promises whole-word semantics (the index
    path's behavior); a bare substring test would silently change match
    semantics between the two paths."""
    import core.ops as ops_module

    async def broken_index(*a, **k):
        raise RuntimeError("index cold")
    monkeypatch.setattr(ops_module, "_index_search", broken_index)

    guild = _FakeGuild(1, [])
    chan = _FakeChannel(10, _Perms(True, True), guild=guild)
    chan._messages = [
        _FakeMessage(1, chan, "let us concatenate strings"),  # substring only
        _FakeMessage(2, chan, "a CAT sat here"),              # whole word
    ]
    guild._channels = {10: chan}

    res = asyncio.run(registry.call(
        "search_history", _search_ctx(guild), channels=[chan], contains="cat"))
    assert res.ok
    assert [m["id"] for m in res.value["messages"]] == [2]
    assert "whole-word" in res.value["note"]


def test_attachment_rejects_missing_file():
    with pytest.raises(ValueError, match="(?i)not found"):
        load_discord_attachments(["/no/such/file.gif"])


def test_attachment_rejects_disallowed_extension(tmp_path):
    bad = tmp_path / "payload.exe"
    bad.write_bytes(b"MZ-fake")
    with pytest.raises(ValueError, match="(?i)extension"):
        load_discord_attachments([str(bad)])


def test_attachment_batch_rejects_before_opening_anything(tmp_path):
    """One bad path must reject the whole batch BEFORE any file is opened —
    a good path listed first must not leave a dangling handle."""
    good = tmp_path / "ok.gif"
    good.write_bytes(b"GIF89a-fake")
    with pytest.raises(ValueError):
        load_discord_attachments([str(good), "/no/such/file.gif"])


# --------------------------------------------------------------------------
# Cog op lifecycle: declare with @op, register per instance, remove on unload.
# --------------------------------------------------------------------------

class _FakeCog:
    @op("fake_ping", "Test op.", PermissionLevel.EVERYONE, group="messaging")
    async def ping(self, ctx):
        return "pong"

    @op("fake_dm", "Test DM op.", PermissionLevel.ADMIN,
        scope=OpScope.DM, group="dm")
    async def dm(self, ctx):
        return "dm"


class _CollidingCog:
    @op("fake_ping", "Collides with _FakeCog.", PermissionLevel.EVERYONE)
    async def ping(self, ctx):
        return "nope"


class _SelfCollidingCog:
    @op("dupe", "First.", PermissionLevel.EVERYONE)
    async def a(self, ctx):
        return 1

    @op("dupe", "Second.", PermissionLevel.EVERYONE)
    async def b(self, ctx):
        return 2


@pytest.fixture
def reg():
    """An isolated registry — never mutate the shared module-level one."""
    return OpsRegistry()


def test_decorator_does_not_touch_the_registry_at_import_time():
    """A spec is inert until a cog INSTANCE is registered; otherwise merely
    importing a cog module would leak ops no live cog backs."""
    assert registry.get("fake_ping") is None
    assert registry.get("fake_dm") is None


def test_register_cog_ops_stamps_cog_origin_and_owner(reg):
    cog = _FakeCog()
    names = reg.register_cog_ops(cog)
    assert sorted(names) == ["fake_dm", "fake_ping"]
    for name in names:
        o = reg.require(name)
        assert o.origin == ORIGIN_COG
        assert o.owner is cog
    # Declared metadata survives registration.
    assert reg.require("fake_dm").scope == OpScope.DM
    assert reg.require("fake_ping").scope == OpScope.GUILD


def test_registered_cog_op_is_callable_with_cog_self(reg):
    reg.register_cog_ops(_FakeCog())
    ctx = OpContext(bot=None, author=None, guild=None)
    result = asyncio.run(reg.call("fake_ping", ctx))
    assert result.ok is True
    assert result.value == "pong"


def test_cog_ops_join_the_live_queries(reg):
    reg.register_cog_ops(_FakeCog())
    assert "fake_ping" in reg.guild_agent_names()
    # DM-scoped cog ops stay out of the guild agent universe, same as core.
    assert "fake_dm" not in reg.guild_agent_names()
    assert [o.name for o in reg.ops(origin=ORIGIN_COG)] == ["fake_dm", "fake_ping"]


def test_unregister_owner_removes_exactly_that_batch(reg):
    cog = _FakeCog()
    reg.register_cog_ops(cog)
    removed = reg.unregister_owner(cog)
    assert sorted(removed) == ["fake_dm", "fake_ping"]
    assert reg.names() == []


def test_unregister_owner_is_safe_for_an_owner_that_registered_nothing(reg):
    """Cog teardown can run after a partially failed setup, so unregistration
    must be callable unconditionally."""
    assert reg.unregister_owner(_FakeCog()) == []


def test_unregister_owner_is_identity_based_not_class_based(reg):
    """Two instances of the same cog class must not evict each other."""
    first = _FakeCog()
    reg.register_cog_ops(first)
    second = _FakeCog()
    assert reg.unregister_owner(second) == []
    assert sorted(reg.names()) == ["fake_dm", "fake_ping"]


def test_registration_is_all_or_none_on_collision_with_existing_op(reg):
    reg.register_cog_ops(_FakeCog())
    before = sorted(reg.names())
    with pytest.raises(ValueError, match="already registered"):
        reg.register_cog_ops(_CollidingCog())
    assert sorted(reg.names()) == before


def test_registration_is_all_or_none_on_duplicate_within_one_cog(reg):
    with pytest.raises(ValueError, match="twice"):
        reg.register_cog_ops(_SelfCollidingCog())
    assert reg.names() == [], "a failed batch must register zero ops"


def test_register_unregister_register_restores_the_batch(reg):
    """unregister_owner must free every name it took, so the same owner can
    register its batch again cleanly. That is what makes cog teardown safe:
    a name left reserved would fail the next registration of that cog."""
    cog = _FakeCog()
    reg.register_cog_ops(cog)
    reg.unregister_owner(cog)
    reg.register_cog_ops(cog)
    assert sorted(reg.names()) == ["fake_dm", "fake_ping"]


def test_op_decorator_rejects_a_sync_function():
    with pytest.raises(TypeError, match="async"):
        @op("sync_op", "Not async.", PermissionLevel.EVERYONE)
        def not_async(self, ctx):
            return None


def test_core_decorator_refuses_duplicate_names(reg):
    @reg.op("solo", "First.", PermissionLevel.EVERYONE)
    async def first(ctx):
        return 1

    with pytest.raises(ValueError, match="already registered"):
        @reg.op("solo", "Second.", PermissionLevel.EVERYONE)
        async def second(ctx):
            return 2


def test_cog_op_params_are_declared_normally(reg):
    class _ParamCog:
        @op("fake_echo", "Echo.", PermissionLevel.EVERYONE,
            params=[OpParam("text", ParamKind.STRING, "Text to echo.")])
        async def echo(self, ctx, text):
            return text

    reg.register_cog_ops(_ParamCog())
    schema = reg.require("fake_echo").to_json_schema()
    assert set(schema["properties"]) == {"text"}
    assert schema["required"] == ["text"]


# --------------------------------------------------------------------------
# Panel rendering tripwire — moved out of core/ops.py (see module docstring).
# --------------------------------------------------------------------------

def test_op_universe_chunks_under_discords_select_cap():
    """Frontends render the op universe into Discord select menus, which
    accept at most 25 options EACH. discord.py does NOT validate this — an
    over-long select builds fine and fails with an HTTP 400 when the panel
    opens. This is the tripwire that says WHY if the chunking is removed."""
    from cogs.optional.gpt import SELECT_MAX_OPTIONS, _tool_selects

    selects = _tool_selects(ALL_OPS, [], None)
    for sel in selects:
        assert len(sel.options) <= SELECT_MAX_OPTIONS, (
            f"select renders {len(sel.options)} options — Discord's cap is "
            f"{SELECT_MAX_OPTIONS} and discord.py will NOT catch this")
    assert sum(len(s.options) for s in selects) == len(ALL_OPS), \
        "chunking dropped ops — every op must remain selectable"


def test_each_group_fits_in_one_select():
    """The panel renders one select per GROUP, so a group that outgrows the
    25-option cap must be split into two groups (not silently truncated)."""
    from cogs.optional.gpt import SELECT_MAX_OPTIONS

    for gid, _label, ops in registry.grouped():
        assert len(ops) <= SELECT_MAX_OPTIONS, (
            f"group '{gid}' has {len(ops)} ops, over Discord's "
            f"{SELECT_MAX_OPTIONS}-option select cap — split the group")


def _panel_sections(scope=None):
    return [
        ("**Core tools**", registry.grouped(scope=scope, origin=ORIGIN_CORE)),
        ("**Cog tools**", registry.grouped(scope=scope, origin=ORIGIN_COG)),
    ]


def test_grouped_sections_render_whole_universe_once():
    """Every op in a tab's universe must be selectable exactly once — a
    missing op cannot be enabled, a duplicated one makes the cross-select
    merge ambiguous."""
    from cogs.optional.gpt import _grouped_tool_sections

    for scope in (None, OpScope.GUILD):
        rendered = [
            o.value
            for kind, payload in _grouped_tool_sections(
                _panel_sections(scope), [], None)
            if kind == "select"
            for o in payload.options
        ]
        expected = registry.op_names(scope=scope)
        assert sorted(rendered) == sorted(expected)
        assert len(rendered) == len(set(rendered))


def _server_gate_view(whitelist):
    """A bare AiSettingsView wired just enough to call _server_gate_ops:
    the whitelist ceiling and an empty per-guild gate override map."""
    from cogs.optional.gpt import AiSettingsView

    view = AiSettingsView.__new__(AiSettingsView)
    view._whitelist = lambda: dict(whitelist)
    view._gate_cfg = lambda: {}
    return view


def test_server_tab_universe_is_exactly_guild_scope():
    """The server tab is the in-guild agent surface: WHITELISTED guild-scoped
    ops only. A DM or GLOBAL op leaking in would let a guild admin enable an
    op that reaches outside their guild; a NON-whitelisted op leaking in would
    let a guild admin grant access the super-admin withheld."""
    guild_ops = set(registry.op_names(scope=OpScope.GUILD))
    # Whitelist EVERYTHING (all scopes) so the only thing narrowing the server
    # tab is the guild-scope + whitelist filter, not a sparse whitelist.
    whitelist = {name: True for name in registry.names()}
    view = _server_gate_view(whitelist)

    rendered = {op.name for _label, ops in view._server_gate_ops() for op in ops}
    assert rendered == guild_ops
    assert not rendered & set(registry.op_names(scope=OpScope.DM))
    assert not rendered & set(registry.op_names(scope=OpScope.GLOBAL))


def test_server_tab_renders_only_whitelisted_ops():
    """A guild-scoped op that the super-admin whitelist does not enable must
    not render on the Server tab at all — it is disabled everywhere, and
    rendering it would both bloat the panel and imply a guild admin could
    turn it on."""
    guild_ops = registry.op_names(scope=OpScope.GUILD)
    assert len(guild_ops) >= 2, "need >=2 guild ops to test the filter"
    chosen = guild_ops[0]
    view = _server_gate_view({chosen: True})

    rendered = {op.name for _label, ops in view._server_gate_ops() for op in ops}
    assert rendered == {chosen}
    # Everything else the registry offers is withheld.
    assert not rendered & (set(guild_ops) - {chosen})

    # Empty whitelist => nothing renders (the safe default).
    empty = _server_gate_view({})
    assert empty._server_gate_ops() == []


def test_cross_select_merge_keeps_other_groups_enabled():
    """Each select speaks only for its own group. Saving one must carry
    through names enabled in every other group — the allowlist-editor bug
    that chunking exists to prevent, now across groups."""
    from cogs.optional.gpt import _grouped_tool_sections

    current = list(agent_ops())
    selects = [p for k, p in _grouped_tool_sections(
        _panel_sections(OpScope.GUILD), current, None) if k == "select"]
    assert len(selects) > 1, "need multiple groups to test the merge"

    target = selects[0]
    mine = {o.value for o in target.options}
    # What `callback` carries through when the user clears this select
    # entirely (values == []): everything enabled outside this group.
    kept = [n for n in target._current if n in target._elsewhere]
    assert set(kept) == set(current) - mine, \
        "clearing one group's select dropped ops owned by other groups"


def test_save_preserves_names_whose_op_is_unregistered():
    """A name whitelisted while its cog was loaded must survive a whitelist
    save made while that cog is unloaded — the select cannot render it, so a
    naive filter would silently and permanently destroy the super-admin's
    choice. The whitelist universe is the WHOLE registry (all scopes), since
    the Agent Ops tab is a plain global allowlist. (This property used to live
    on the per-guild bot_tools_enabled list, which no longer exists.)"""
    from cogs.optional.gpt import AiSettingsView

    universe = registry.names()
    merged = AiSettingsView._merge_stored(
        ["ghost_op", "search_history"], ["list_channels"], universe)
    assert "ghost_op" in merged
    assert merged.count("ghost_op") == 1
    assert set(merged) == {"ghost_op", "list_channels"}

    # An explicit "clear all" still persists as empty (not "unset") — but only
    # for names the universe can render. An OFFLINE name survives a merge of
    # [], which is exactly why the panel's "Clear all" button must not route
    # through _merge_stored (see test_clear_all_clears_names_whose_cog_is_unloaded).
    assert AiSettingsView._merge_stored(
        ["search_history"], [], universe) == []
    assert AiSettingsView._merge_stored(
        ["ghost_op"], [], universe) == ["ghost_op"]


def test_clear_all_clears_names_whose_cog_is_unloaded():
    """"Clear all" on the MCP tab must clear ALL, including names whose cog is
    currently unloaded. The offline-preserving merge is right for a per-group
    select edit (it speaks only for rendered options) but wrong here: an
    operator locking the MCP surface down before exposing the loopback port is
    speaking for everything, and a carried-through name would sit silently
    armed to return on the next restart with nothing in the UI revealing it."""
    import asyncio

    from cogs.optional.gpt import AiSettingsView

    view = AiSettingsView.__new__(AiSettingsView)
    saved = {}

    class _Config:
        def get_global(self, key, default=None):
            return ["ghost_op", "send_message"]

        def set_global(self, key, value):
            saved[key] = value

    class _Bot:
        config = _Config()

    view.bot = _Bot()
    view._flash = None
    view.flash = lambda text: setattr(view, "_flash", text)

    rendered = []

    async def fake_rerender(interaction):
        rendered.append(interaction)

    view.rerender = fake_rerender

    class _Interaction:
        class response:
            @staticmethod
            async def send_message(*a, **kw):
                raise AssertionError("superadmin gate should have passed")

    import cogs.optional.gpt as gpt_mod
    original = gpt_mod.is_superadmin
    gpt_mod.is_superadmin = lambda interaction: True
    try:
        asyncio.run(view._clear_mcp_tools(_Interaction(), []))
    finally:
        gpt_mod.is_superadmin = original

    assert saved["mcp_tools_enabled"] == [], \
        "clear all must write an empty list, not carry offline names through"
    # The surviving-name case is the one an operator could never see, so the
    # flash has to name it rather than reporting a bare success.
    assert "ghost_op" in (view._flash or "")


# --------------------------------------------------------------------------
# delete_dm implementation: bot-author-only retraction (no Discord needed).
# --------------------------------------------------------------------------

class _FakeDMChannel:
    def __init__(self, message):
        self._message = message

    async def fetch_message(self, message_id):
        return self._message


class _FakeDMMessage:
    def __init__(self, author_id):
        self.author = type("A", (), {"id": author_id})()
        self.deleted = False

    async def delete(self):
        self.deleted = True


def _dm_ctx_and_user(message, bot_id=555):
    bot = type("B", (), {"user": type("U", (), {"id": bot_id})()})()
    user = type("Target", (), {"dm_channel": _FakeDMChannel(message)})()
    return OpContext(bot=bot, author=None, guild=None), user


def test_delete_dm_deletes_a_bot_authored_message():
    from core.ops import delete_dm
    msg = _FakeDMMessage(author_id=555)
    ctx, user = _dm_ctx_and_user(msg, bot_id=555)
    assert asyncio.run(delete_dm(ctx, user, 123)) is True
    assert msg.deleted


def test_delete_dm_refuses_the_other_participants_message():
    """The refusal is LOCAL (a pre-check before any delete call), not a
    relayed Discord 403 — the op never even attempts the API delete."""
    from core.ops import delete_dm
    msg = _FakeDMMessage(author_id=999)
    ctx, user = _dm_ctx_and_user(msg, bot_id=555)
    with pytest.raises(ValueError, match="bot's own"):
        asyncio.run(delete_dm(ctx, user, 123))
    assert not msg.deleted


# --------------------------------------------------------------------------
# edit_dm implementation: bot-author-only edit + transcript update note.
# --------------------------------------------------------------------------

class _FakeEditableDMMessage(_FakeDMMessage):
    def __init__(self, author_id):
        super().__init__(author_id)
        import datetime as _dt
        self.id = 123
        self.content = "original"
        self.attachments = []
        self.created_at = _dt.datetime.now(_dt.timezone.utc)

    async def edit(self, *, content):
        self.content = content
        return self


def test_edit_dm_edits_a_bot_authored_message_and_logs_a_note(monkeypatch):
    import core.ops as ops_module
    from core.ops import edit_dm
    logged = []
    monkeypatch.setattr(ops_module, "log_dm",
                        lambda uid, row: logged.append((uid, row)))
    msg = _FakeEditableDMMessage(author_id=555)
    ctx, user = _dm_ctx_and_user(msg, bot_id=555)
    user.id = 42
    edited = asyncio.run(edit_dm(ctx, user, 123, "fixed"))
    assert edited.content == "fixed"
    # Transcript gains an update note marked edited:true (the original row
    # stays as the audit record of what was first sent).
    assert len(logged) == 1
    uid, row = logged[0]
    assert uid == 42
    assert row["edited"] is True
    assert row["content"] == "fixed"
    assert row["direction"] == "out"


def test_edit_dm_refuses_the_other_participants_message(monkeypatch):
    import core.ops as ops_module
    from core.ops import edit_dm
    monkeypatch.setattr(ops_module, "log_dm",
                        lambda uid, row: pytest.fail("must not log a refusal"))
    msg = _FakeEditableDMMessage(author_id=999)
    ctx, user = _dm_ctx_and_user(msg, bot_id=555)
    user.id = 42
    with pytest.raises(ValueError, match="bot's own"):
        asyncio.run(edit_dm(ctx, user, 123, "hijack"))
    assert msg.content == "original"


def test_edit_dm_requires_nonempty_content():
    from core.ops import edit_dm
    msg = _FakeEditableDMMessage(author_id=555)
    ctx, user = _dm_ctx_and_user(msg, bot_id=555)
    with pytest.raises(ValueError, match="non-empty"):
        asyncio.run(edit_dm(ctx, user, 123, "   "))
    assert msg.content == "original"


# --------------------------------------------------------------------------
# Users & DMs domain pass (2026-08): DM reactions/pins/conversation listing
# and the global get_user read. Impl behavior with fakes (no Discord needed).
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "list_dm_conversations", "add_dm_reaction", "remove_dm_reaction",
    "list_dm_pins", "get_user",
])
def test_users_dms_domain_ops_are_admin_gated(name):
    """Same privacy class as the existing DM ops: everything that reads or
    touches a private conversation — or looks up arbitrary user ids — is
    ADMIN, keeping enumeration off the EVERYONE surface."""
    assert registry.require(name).permission == PermissionLevel.ADMIN


def test_list_dm_conversations_shape():
    schema = registry.get("list_dm_conversations").to_json_schema()
    assert set(schema["properties"]) == {"limit"}
    assert schema["required"] == []
    assert schema["properties"]["limit"]["default"] == 100


def test_get_user_shape():
    schema = registry.get("get_user").to_json_schema()
    assert set(schema["properties"]) == {"user_id"}
    assert schema["required"] == ["user_id"]
    assert schema["properties"]["user_id"]["type"] == "string"


class _ReactableDMMessage(_FakeDMMessage):
    def __init__(self, author_id):
        super().__init__(author_id)
        self.added = []
        self.removed = []

    async def add_reaction(self, emoji):
        self.added.append(emoji)

    async def remove_reaction(self, emoji, member):
        self.removed.append((emoji, member))


def test_add_dm_reaction_reacts_to_the_dm_message():
    from core.ops import add_dm_reaction
    msg = _ReactableDMMessage(author_id=42)  # the USER's message — allowed
    ctx, user = _dm_ctx_and_user(msg, bot_id=555)
    assert asyncio.run(add_dm_reaction(ctx, user, 123, "💨")) is True
    assert msg.added == ["💨"]


def test_remove_dm_reaction_removes_only_the_bots_own_reaction():
    """The bot's user object is passed to remove_reaction, so the API call
    is structurally incapable of touching the other participant's side."""
    from core.ops import remove_dm_reaction
    msg = _ReactableDMMessage(author_id=42)
    ctx, user = _dm_ctx_and_user(msg, bot_id=555)
    assert asyncio.run(remove_dm_reaction(ctx, user, 123, "💨")) is True
    assert len(msg.removed) == 1
    emoji, member = msg.removed[0]
    assert emoji == "💨"
    assert member is ctx.bot.user


class _PinnedDMChannel(_FakeDMChannel):
    def pins(self):
        message = self._message

        async def gen():
            yield message
        return gen()


def test_list_dm_pins_returns_transcript_shaped_rows():
    from core.ops import list_dm_pins
    msg = _FakeEditableDMMessage(author_id=42)  # has id/content/created_at
    ctx, user = _dm_ctx_and_user(msg, bot_id=555)
    user.dm_channel = _PinnedDMChannel(msg)
    user.id = 42
    rows = asyncio.run(list_dm_pins(ctx, user))
    assert len(rows) == 1
    # Same row shape as read_dms/fetch_dms: direction derived from author.
    assert rows[0]["message_id"] == 123
    assert rows[0]["content"] == "original"
    assert rows[0]["direction"] == "in"
    payload = registry.get("list_dm_pins").serialize_result(rows)
    assert payload == {"messages": rows, "count": 1}


def test_list_dm_conversations_reads_storage_and_annotates_names(monkeypatch):
    import core.ops as ops_module
    from core.ops import list_dm_conversations
    monkeypatch.setattr(ops_module, "list_dm_users", lambda: [42, 77])
    monkeypatch.setattr(
        ops_module, "load_dms",
        lambda uid, limit=None: (
            [{"message_id": 9, "timestamp": "2026-08-19T10:00:00"}]
            if uid == 42 else []))
    known = type("U", (), {"name": "alice"})()
    bot = type("B", (), {
        "get_user": staticmethod(lambda uid: known if uid == 42 else None)})()
    ctx = OpContext(bot=bot, author=None, guild=None)
    payload = asyncio.run(list_dm_conversations(ctx))
    assert payload["count"] == 2
    assert payload["conversations"][0] == {
        "user_id": 42, "user_name": "alice",
        "last_message_at": "2026-08-19T10:00:00"}
    # Uncached user: name null, conversation still listed (the transcript
    # is the source of truth, not the gateway cache).
    assert payload["conversations"][1] == {
        "user_id": 77, "user_name": None, "last_message_at": None}


def test_list_dm_conversations_respects_the_limit(monkeypatch):
    import core.ops as ops_module
    from core.ops import list_dm_conversations
    monkeypatch.setattr(ops_module, "list_dm_users", lambda: [1, 2, 3])
    monkeypatch.setattr(ops_module, "load_dms", lambda uid, limit=None: [])
    bot = type("B", (), {"get_user": staticmethod(lambda uid: None)})()
    ctx = OpContext(bot=bot, author=None, guild=None)
    payload = asyncio.run(list_dm_conversations(ctx, limit=2))
    assert payload["count"] == 2
    assert [c["user_id"] for c in payload["conversations"]] == [1, 2]


class _FakeGlobalUser:
    """Cache-resolved user: no banner/accent (the gateway never sends them)."""

    def __init__(self, uid=42):
        self.id = uid
        self.name = "alice"


def test_get_user_refetches_for_the_full_profile():
    from core.ops import get_user
    resolved = _FakeGlobalUser()
    fetched = _FakeGlobalUser()

    class _Bot:
        async def fetch_user(self, uid):
            assert uid == 42
            return fetched

    ctx = OpContext(bot=_Bot(), author=None, guild=None)
    assert asyncio.run(get_user(ctx, resolved)) is fetched


def test_get_user_falls_back_to_the_resolved_user_when_fetch_fails():
    from core.ops import get_user
    resolved = _FakeGlobalUser()

    class _Bot:
        async def fetch_user(self, uid):
            raise RuntimeError("REST down")

    ctx = OpContext(bot=_Bot(), author=None, guild=None)
    assert asyncio.run(get_user(ctx, resolved)) is resolved


def test_get_user_serializer_ships_the_global_profile_fields():
    import datetime as _dt

    _flag = type("F", (), {"name": "hypesquad"})()
    _flags = type("PF", (), {"all": lambda self: [_flag]})()
    _accent = type("C", (), {"value": 0x5865F2})()
    _asset = type("As", (), {"__str__": lambda self: "https://cdn/u/42.png"})()
    _guild = type("G", (), {"id": 7, "name": "The Guild"})()

    class _User:
        id = 42
        name = "alice"
        global_name = "Alice"
        display_name = "Alice"
        bot = False
        system = False
        created_at = _dt.datetime(2020, 6, 7, 8, 9, 10)
        display_avatar = _asset
        banner = _asset
        accent_colour = _accent
        public_flags = _flags
        mutual_guilds = [_guild]

    payload = registry.get("get_user").serialize_result(_User())
    assert payload["id"] == 42
    assert payload["global_name"] == "Alice"
    assert payload["system"] is False
    assert payload["created_at"] == "2020-06-07T08:09:10"
    assert payload["avatar_url"] == "https://cdn/u/42.png"
    assert payload["banner_url"] == "https://cdn/u/42.png"
    assert payload["accent_color"] == "#5865F2"
    assert payload["public_flags"] == ["hypesquad"]
    assert payload["mutual_guilds"] == [{"id": 7, "name": "The Guild"}]


def test_get_user_serializer_tolerates_a_bare_cache_user():
    """A cache-built user (fetch failed) has no banner/accent/flags — the
    serializer must not blow up, it degrades to nulls/empties."""
    payload = registry.get("get_user").serialize_result(_FakeGlobalUser())
    assert payload["banner_url"] is None
    assert payload["accent_color"] is None
    assert payload["public_flags"] == []
    assert payload["mutual_guilds"] == []


def test_get_member_serializer_ships_the_presence_fields():
    """The users&DMs pass folded the get_member_info spec into get_member's
    serializer: global_name, top_role, per-platform client_status,
    activities, and the per-guild avatar override."""
    _top = type("R", (), {"id": 2, "name": "Mods"})()
    _atype = type("AT", (), {"name": "playing"})()
    _activity = type("Act", (), {
        "type": _atype, "name": "Factorio", "details": "1.1"})()
    _guild_av = type("Av", (), {
        "__str__": lambda self: "https://cdn/guilds/1/users/42.png"})()

    class _Member:
        id = 42
        name = "alice"
        global_name = "Alice"
        display_name = "Alice"
        nick = None
        bot = False
        roles = []
        top_role = _top
        joined_at = None
        created_at = None
        premium_since = None
        timed_out_until = None
        status = "online"
        desktop_status = "online"
        mobile_status = "idle"
        web_status = "offline"
        activities = [_activity]
        display_avatar = None
        guild_avatar = _guild_av
        pending = False

    payload = registry.get("get_member").serialize_result(_Member())
    assert payload["global_name"] == "Alice"
    assert payload["top_role"] == {"id": 2, "name": "Mods"}
    assert payload["client_status"] == {
        "desktop": "online", "mobile": "idle", "web": "offline"}
    assert payload["activities"] == [
        {"type": "playing", "name": "Factorio", "details": "1.1"}]
    assert payload["guild_avatar_url"].endswith("/users/42.png")


# --------------------------------------------------------------------------
# Messaging read/write ops added in the 2026-08 gap pass (no Discord needed).
# --------------------------------------------------------------------------

class _NoActorCtx:
    """Bare ctx for direct-impl calls where the gate is not under test."""

    def __init__(self, bot=None):
        self.bot = bot
        self.author = None
        self.guild = None


def test_get_message_serializer_ships_the_inspection_fields():
    class _Att:
        filename = "a.png"
        url = "https://cdn/a.png"

    class _Msg:
        id = 7
        channel = type("C", (), {"id": 8})()
        author = type("A", (), {"id": 9})()
        content = "hello"
        created_at = None
        attachments = [_Att()]
        embeds = [object(), object()]
        pinned = True
        jump_url = "https://discord.com/channels/1/8/7"

    payload = registry.get("get_message").serialize_result(_Msg())
    assert payload["content"] == "hello"
    assert payload["attachments"] == [{"filename": "a.png",
                                       "url": "https://cdn/a.png"}]
    assert payload["embeds"] == 2
    assert payload["pinned"] is True
    assert payload["jump_url"].endswith("/1/8/7")


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


def test_read_history_returns_oldest_first():
    guild = _FakeGuild(1, [])
    chan = _HistoryChannel(10, _Perms(True, True), guild=guild)
    chan._messages = [_FakeMessage(3, chan, "newest"),
                      _FakeMessage(2, chan, "mid"),
                      _FakeMessage(1, chan, "oldest")]
    guild._channels = {10: chan}
    res = asyncio.run(registry.call("read_history", _search_ctx(guild),
                                    channel=chan, limit=50))
    assert res.ok
    assert [m["id"] for m in res.value["messages"]] == [1, 2, 3]
    assert res.value["count"] == 3


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


def test_unpin_message_unpins():
    from core.ops import unpin_message

    class _Msg:
        unpinned = False

        async def unpin(self):
            self.unpinned = True

    msg = _Msg()
    assert asyncio.run(unpin_message(_NoActorCtx(), msg)) is True
    assert msg.unpinned


class _PinsChannel(_FakeChannel):
    def pins(self, limit=50):
        self.pins_limit = limit
        messages = self._messages[:limit]

        async def gen():
            for m in messages:
                yield m
        return gen()


def test_list_pins_returns_rows_with_pinned_at():
    import datetime as _dt
    guild = _FakeGuild(1, [])
    chan = _PinsChannel(10, _Perms(True, True), guild=guild)
    pinned = _FakeMessage(1, chan, "keep me")
    pinned.pinned_at = _dt.datetime(2026, 8, 20, 12, 0, 0)
    chan._messages = [pinned]
    guild._channels = {10: chan}
    res = asyncio.run(registry.call("list_pins", _search_ctx(guild),
                                    channel=chan, limit=50))
    assert res.ok
    assert res.value["count"] == 1
    row = res.value["messages"][0]
    assert row["content"] == "keep me"
    assert row["pinned_at"] == "2026-08-20T12:00:00"


def test_list_pins_refuses_history_denied_actor():
    """Discord gates the pins endpoint on Read Message History; the actor
    must hold it too (the bot reads with its own broader perms)."""
    guild = _FakeGuild(1, [])
    denied = _PinsChannel(20, _Perms(True, False), guild=guild)
    guild._channels = {20: denied}
    res = asyncio.run(registry.call("list_pins", _search_ctx(guild),
                                    channel=denied))
    assert res.ok is False
    assert "Read Message History" in res.error


class _FakeReaction:
    def __init__(self, emoji, count, me, user_ids=()):
        self.emoji = emoji
        self.count = count
        self.me = me
        self._user_ids = list(user_ids)

    def users(self, limit=None):
        ids = self._user_ids[:limit]

        async def gen():
            for uid in ids:
                yield type("U", (), {"id": uid})()
        return gen()


def test_list_reactions_tallies_and_enumerates_reactors():
    from core.ops import list_reactions

    class _Msg:
        reactions = [_FakeReaction("👍", 2, True, [111, 222]),
                     _FakeReaction("👎", 1, False, [333])]

    tallies = asyncio.run(list_reactions(_NoActorCtx(), _Msg()))
    assert tallies == {"reactions": [
        {"emoji": "👍", "count": 2, "me": True},
        {"emoji": "👎", "count": 1, "me": False}]}

    picked = asyncio.run(list_reactions(_NoActorCtx(), _Msg(), emoji="👍"))
    assert picked["users"] == [111, 222]
    # An emoji nobody used still answers, with an empty reactor list.
    none = asyncio.run(list_reactions(_NoActorCtx(), _Msg(), emoji="🤷"))
    assert none["users"] == []


def test_list_reactions_matches_custom_emoji_by_reaction_form():
    """Callers hold custom emoji as either '<:name:id>' (str form) or
    'name:id' (add_reaction's form); both must select the same reaction."""
    from core.ops import list_reactions

    class _CustomEmoji:
        name = "blob"
        id = 42

        def __str__(self):
            return "<:blob:42>"

    class _Msg:
        reactions = [_FakeReaction(_CustomEmoji(), 1, False, [777])]

    for form in ("<:blob:42>", "blob:42"):
        res = asyncio.run(list_reactions(_NoActorCtx(), _Msg(), emoji=form))
        assert res["users"] == [777], form


def test_trigger_typing_awaits_the_indicator():
    from core.ops import trigger_typing

    class _Chan:
        typed = 0

        async def typing(self):
            self.typed += 1

    chan = _Chan()
    assert asyncio.run(trigger_typing(_NoActorCtx(), chan)) is True
    assert chan.typed == 1


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


def test_forward_message_forwards_within_the_guild():
    guild = type("G", (), {"id": 1})()
    dest = _ForwardDest(20, guild)
    msg = _ForwardMsg(guild)
    from core.ops import forward_message
    result = asyncio.run(forward_message(
        _NoActorCtx(bot=_ForwardBot(dest)), msg, 20))
    assert msg.forwarded_to is dest
    payload = registry.get("forward_message").serialize_result(result)
    assert payload == {"message_id": 999, "channel_id": 20}


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


def test_suppress_embeds_toggles_both_ways():
    from core.ops import suppress_embeds

    class _Msg:
        seen = None

        async def edit(self, *, suppress):
            self.seen = suppress

    msg = _Msg()
    assert asyncio.run(suppress_embeds(_NoActorCtx(), msg)) is True
    assert msg.seen is True
    asyncio.run(suppress_embeds(_NoActorCtx(), msg, suppress=False))
    assert msg.seen is False


class _SendCaptureChannel:
    def __init__(self):
        self.kwargs = None

    async def send(self, content=None, **kwargs):
        self.kwargs = kwargs
        return type("M", (), {"id": 1, "attachments": []})()


def test_send_embed_builds_the_embed_and_never_pings():
    import discord
    from core.ops import send_embed
    chan = _SendCaptureChannel()
    asyncio.run(send_embed(_NoActorCtx(), chan, title="T",
                           description="D", color="#5865F2",
                           image_url="https://x/i.png", footer="F"))
    embed = chan.kwargs["embed"]
    assert embed.title == "T" and embed.description == "D"
    assert embed.colour.value == 0x5865F2
    assert embed.image.url == "https://x/i.png"
    assert embed.footer.text == "F"
    assert chan.kwargs["allowed_mentions"].everyone is False


def test_send_embed_requires_some_visible_content():
    from core.ops import send_embed
    chan = _SendCaptureChannel()
    with pytest.raises(ValueError, match="at least one"):
        asyncio.run(send_embed(_NoActorCtx(), chan, url="https://x",
                               footer="only chrome"))
    assert chan.kwargs is None


def test_send_poll_builds_a_native_poll():
    from core.ops import send_poll
    chan = _SendCaptureChannel()
    asyncio.run(send_poll(_NoActorCtx(), chan, "Best snack?",
                          ["chips", "fruit", "cheese"],
                          duration_hours=48, multiselect=True))
    poll = chan.kwargs["poll"]
    assert poll.question == "Best snack?"
    assert [a.text for a in poll.answers] == ["chips", "fruit", "cheese"]
    assert poll.duration.total_seconds() == 48 * 3600
    assert poll.multiple is True


@pytest.mark.parametrize("answers", [[], ["solo"], [str(i) for i in range(11)]])
def test_send_poll_rejects_bad_answer_counts(answers):
    from core.ops import send_poll
    chan = _SendCaptureChannel()
    with pytest.raises(ValueError, match="2-10"):
        asyncio.run(send_poll(_NoActorCtx(), chan, "Q?", answers))
    assert chan.kwargs is None


def test_send_poll_rejects_an_over_long_question():
    from core.ops import send_poll
    with pytest.raises(ValueError, match="300"):
        asyncio.run(send_poll(_NoActorCtx(), _SendCaptureChannel(),
                              "x" * 301, ["a", "b"]))


class _FakePoll:
    def __init__(self, finalized=False):
        self.question = "Best snack?"
        self.answers = [type("A", (), {"text": "chips", "vote_count": 3})(),
                        type("A", (), {"text": "fruit", "vote_count": 1})()]
        self.expires_at = None
        self._finalized = finalized

    def is_finalized(self):
        return self._finalized


def test_get_poll_results_reads_the_tallies():
    from core.ops import get_poll_results

    class _Msg:
        poll = _FakePoll()

    res = asyncio.run(get_poll_results(_NoActorCtx(), _Msg()))
    assert res == {"question": "Best snack?",
                   "answers": [{"text": "chips", "count": 3},
                               {"text": "fruit", "count": 1}],
                   "expires_at": None, "finalized": False}


def test_get_poll_results_refuses_a_poll_less_message():
    from core.ops import get_poll_results

    class _Msg:
        poll = None

    with pytest.raises(ValueError, match="no poll"):
        asyncio.run(get_poll_results(_NoActorCtx(), _Msg()))


def _poll_msg(author_id):
    class _Msg:
        poll = _FakePoll()
        author = type("A", (), {"id": author_id})()
        ended = False

        async def end_poll(self):
            self.ended = True
            return type("M", (), {"id": 5})()

    return _Msg()


def _bot_ctx(bot_id=555):
    bot = type("B", (), {"user": type("U", (), {"id": bot_id})()})()
    return _NoActorCtx(bot=bot)


def test_end_poll_ends_the_bots_own_poll():
    from core.ops import end_poll
    msg = _poll_msg(author_id=555)
    result = asyncio.run(end_poll(_bot_ctx(555), msg))
    assert msg.ended
    assert registry.get("end_poll").serialize_result(result) == {"message_id": 5}


def test_end_poll_refuses_someone_elses_poll():
    """Mirrors edit_message's own-message discipline — the refusal is a
    local pre-check, never an attempted API call."""
    from core.ops import end_poll
    msg = _poll_msg(author_id=999)
    with pytest.raises(ValueError, match="bot's own"):
        asyncio.run(end_poll(_bot_ctx(555), msg))
    assert not msg.ended


# --------------------------------------------------------------------------
# Channels & threads ops added in the 2026-08 channels-domain gap pass.
# Wire shapes first (decisions worth locking), then impl behavior with
# fakes (no Discord needed). Thread/forum fakes SUBCLASS the real discord.py
# types without calling their __init__, because the ops' isinstance guards
# are part of the behavior under test.
# --------------------------------------------------------------------------

def test_get_channel_info_shape():
    schema = registry.get("get_channel_info").to_json_schema()
    assert set(schema["properties"]) == {"channel_id"}
    assert schema["required"] == ["channel_id"]


def test_list_threads_shape():
    schema = registry.get("list_threads").to_json_schema()
    assert set(schema["properties"]) == {
        "guild_id", "channel_id", "include_archived", "limit"}
    assert set(schema["required"]) == {"guild_id"}
    assert schema["properties"]["limit"]["maximum"] == 500


@pytest.mark.parametrize("name", ["list_thread_members", "join_thread",
                                  "leave_thread"])
def test_thread_membership_ops_take_one_channel_id(name):
    """Threads are channels on the wire — one channel_id, nothing else."""
    schema = registry.get(name).to_json_schema()
    assert set(schema["properties"]) == {"channel_id"}
    assert schema["required"] == ["channel_id"]
    assert schema["properties"]["channel_id"]["type"] == "string"


def test_edit_thread_shape():
    schema = registry.get("edit_thread").to_json_schema()
    assert set(schema["properties"]) == {
        "channel_id", "name", "archived", "locked", "slowmode_delay",
        "auto_archive_duration"}
    assert schema["required"] == ["channel_id"]
    assert schema["properties"]["slowmode_delay"]["maximum"] == 21600


def test_set_slowmode_shape():
    schema = registry.get("set_slowmode").to_json_schema()
    assert set(schema["properties"]) == {"channel_id", "seconds"}
    assert set(schema["required"]) == {"channel_id", "seconds"}
    assert schema["properties"]["seconds"]["minimum"] == 0
    assert schema["properties"]["seconds"]["maximum"] == 21600


def test_edit_channel_shape():
    schema = registry.get("edit_channel").to_json_schema()
    assert set(schema["properties"]) == {"channel_id", "name", "topic", "nsfw"}
    assert schema["required"] == ["channel_id"]


def test_get_member_permissions_shape():
    schema = registry.get("get_member_permissions").to_json_schema()
    assert set(schema["properties"]) == {"channel_id", "user_id"}
    assert set(schema["required"]) == {"channel_id", "user_id"}


def test_create_forum_post_shape():
    schema = registry.get("create_forum_post").to_json_schema()
    assert set(schema["properties"]) == {
        "channel_id", "name", "content", "tag_ids"}
    assert set(schema["required"]) == {"channel_id", "name", "content"}
    assert schema["properties"]["tag_ids"]["type"] == "array"


class _AuthorCtx(_NoActorCtx):
    """Ctx with an author (for ops that stamp an audit reason string) but no
    real Member, so the visibility gate stays out of the way."""

    def __init__(self, bot=None):
        super().__init__(bot=bot)
        self.author = type("A", (), {
            "id": 321, "__str__": lambda self: "tester"})()


class _FakeThread(discord.Thread):
    """A real discord.Thread (isinstance guards must pass) with none of the
    gateway state — __init__ is deliberately not chained."""

    def __init__(self, **attrs):  # noqa: D107 - test fake
        defaults = {"id": 111, "name": "topic-drift", "parent_id": 10,
                    "owner_id": 42, "archived": False, "locked": False,
                    "member_count": 2, "message_count": 5,
                    "slowmode_delay": 0, "auto_archive_duration": 1440,
                    "guild": None,
                    # Thread.type is a property over the _type slot.
                    "_type": "public_thread"}
        defaults.update(attrs)
        for key, value in defaults.items():
            setattr(self, key, value)
        self.joined = False
        self.left = False
        self.edit_kwargs = None
        self._thread_members = []

    async def join(self):
        self.joined = True

    async def leave(self):
        self.left = True

    async def edit(self, *, reason=None, **kwargs):
        self.edit_kwargs = dict(kwargs)
        self.edit_reason = reason
        for key, value in kwargs.items():
            setattr(self, key, value)
        return self

    async def fetch_members(self):
        return list(self._thread_members)


class _NotAThread:
    id = 999


def test_get_channel_info_reads_a_text_channel():
    import datetime as _dt
    from core.ops import get_channel_info

    _category = type("Cat", (), {"name": "Main"})()

    class _Chan:
        # NOTE: `type` the builtin is shadowed by the `type` attribute from
        # here on in this class body — build helper objects above.
        id = 10
        name = "general"
        type = "text"
        topic = "the topic"
        nsfw = False
        category_id = 5
        category = _category
        position = 3
        created_at = _dt.datetime(2026, 1, 2, 3, 4, 5)
        slowmode_delay = 15
        default_auto_archive_duration = 1440

    payload = asyncio.run(get_channel_info(_NoActorCtx(), _Chan()))
    assert payload["id"] == 10
    assert payload["topic"] == "the topic"
    assert payload["category_name"] == "Main"
    assert payload["created_at"] == "2026-01-02T03:04:05"
    assert payload["slowmode_delay"] == 15
    # Facets a text channel doesn't carry must be absent, not null.
    assert "bitrate" not in payload
    assert "available_tags" not in payload
    assert "thread_parent_id" not in payload


def test_get_channel_info_carries_type_specific_facets():
    from core.ops import get_channel_info

    class _Voice:
        id = 11
        name = "voice"
        type = "voice"
        bitrate = 64000
        user_limit = 5

    voice = asyncio.run(get_channel_info(_NoActorCtx(), _Voice()))
    assert voice["bitrate"] == 64000 and voice["user_limit"] == 5

    _solved_tag = type("T", (), {"id": 1, "name": "solved"})()

    class _Forum:
        id = 12
        name = "help"
        type = "forum"
        available_tags = [_solved_tag]

    forum = asyncio.run(get_channel_info(_NoActorCtx(), _Forum()))
    assert forum["available_tags"] == [{"id": 1, "name": "solved"}]

    thread = asyncio.run(get_channel_info(
        _NoActorCtx(), _FakeThread(parent_id=10)))
    assert thread["thread_parent_id"] == 10


class _ThreadGuild(_FakeGuild):
    def __init__(self, gid, channels, threads):
        super().__init__(gid, channels)
        self._threads = list(threads)

    async def active_threads(self):
        return list(self._threads)


def test_list_threads_guild_wide_drops_hidden_parents():
    """A real Member must not see threads whose parent channel they cannot
    read — mirror of search_history's per-hit visibility policy."""
    guild = _ThreadGuild(1, [], [])
    visible_parent = _FakeChannel(10, _Perms(True, True), guild=guild)
    hidden_parent = _FakeChannel(20, _Perms(False, False), guild=guild)
    guild._channels = {10: visible_parent, 20: hidden_parent}
    guild._threads = [_FakeThread(id=111, parent_id=10),
                      _FakeThread(id=222, parent_id=20)]

    res = asyncio.run(registry.call("list_threads", _search_ctx(guild),
                                    guild=guild))
    assert res.ok
    assert [t["id"] for t in res.value["threads"]] == [111]
    assert res.value["count"] == 1


class _ThreadParentChannel(_FakeChannel):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.threads = []
        self.archived = []

    def archived_threads(self, limit=100):
        rows = self.archived[:limit]

        async def gen():
            for t in rows:
                yield t
        return gen()


def test_list_threads_per_channel_includes_archived_on_request():
    guild = _ThreadGuild(1, [], [])
    parent = _ThreadParentChannel(10, _Perms(True, True), guild=guild)
    parent.threads = [_FakeThread(id=111, parent_id=10)]
    parent.archived = [_FakeThread(id=222, parent_id=10, archived=True)]
    guild._channels = {10: parent}

    active_only = asyncio.run(registry.call(
        "list_threads", _search_ctx(guild), guild=guild, channel=parent))
    assert [t["id"] for t in active_only.value["threads"]] == [111]

    both = asyncio.run(registry.call(
        "list_threads", _search_ctx(guild), guild=guild, channel=parent,
        include_archived=True))
    assert [t["id"] for t in both.value["threads"]] == [111, 222]
    assert both.value["threads"][1]["archived"] is True


def test_list_threads_refuses_archived_without_a_channel():
    guild = _ThreadGuild(1, [], [])
    res = asyncio.run(registry.call("list_threads", _search_ctx(guild),
                                    guild=guild, include_archived=True))
    assert res.ok is False
    assert "channel_id" in res.error


def test_list_thread_members_resolves_display_names():
    import datetime as _dt
    from core.ops import list_thread_members

    class _MemberGuild:
        @staticmethod
        def get_member(uid):
            if uid == 42:
                return type("M", (), {"display_name": "Alice"})()
            return None

    thread = _FakeThread(guild=_MemberGuild())
    thread._thread_members = [
        type("TM", (), {"id": 42,
                        "joined_at": _dt.datetime(2026, 8, 1, 9, 0, 0)})(),
        type("TM", (), {"id": 43, "joined_at": None})(),
    ]
    payload = asyncio.run(list_thread_members(_NoActorCtx(), thread))
    assert payload["count"] == 2
    assert payload["members"][0] == {
        "id": 42, "display_name": "Alice",
        "joined_at": "2026-08-01T09:00:00"}
    # Uncached member: id survives, display_name is None (never invented).
    assert payload["members"][1] == {
        "id": 43, "display_name": None, "joined_at": None}


def test_list_thread_members_refuses_a_non_thread():
    from core.ops import list_thread_members
    with pytest.raises(ValueError, match="not a thread"):
        asyncio.run(list_thread_members(_NoActorCtx(), _NotAThread()))


def test_join_and_leave_thread_change_only_bot_membership():
    from core.ops import join_thread, leave_thread
    thread = _FakeThread()
    assert asyncio.run(join_thread(_NoActorCtx(), thread)) is True
    assert thread.joined
    assert asyncio.run(leave_thread(_NoActorCtx(), thread)) is True
    assert thread.left


@pytest.mark.parametrize("opname", ["join_thread", "leave_thread"])
def test_join_leave_thread_refuse_a_non_thread(opname):
    import core.ops as ops_module
    impl = getattr(ops_module, opname)
    with pytest.raises(ValueError, match="not a thread"):
        asyncio.run(impl(_NoActorCtx(), _NotAThread()))


def test_edit_thread_applies_partial_edits_with_audit_reason():
    from core.ops import edit_thread
    thread = _FakeThread()
    payload = asyncio.run(edit_thread(_AuthorCtx(), thread,
                                      name="renamed", archived=True))
    assert thread.edit_kwargs == {"name": "renamed", "archived": True}
    assert "edit_thread op by tester (321)" == thread.edit_reason
    assert payload["thread_id"] == 111
    assert payload["name"] == "renamed"
    assert payload["archived"] is True
    assert payload["locked"] is False


def test_edit_thread_requires_at_least_one_field():
    from core.ops import edit_thread
    thread = _FakeThread()
    with pytest.raises(ValueError, match="Nothing to edit"):
        asyncio.run(edit_thread(_AuthorCtx(), thread))
    assert thread.edit_kwargs is None


def test_edit_thread_rejects_an_invalid_auto_archive_duration():
    from core.ops import edit_thread
    thread = _FakeThread()
    with pytest.raises(ValueError, match="auto_archive_duration"):
        asyncio.run(edit_thread(_AuthorCtx(), thread,
                                auto_archive_duration=120))
    assert thread.edit_kwargs is None


def test_edit_thread_refuses_a_non_thread():
    from core.ops import edit_thread
    with pytest.raises(ValueError, match="not a thread"):
        asyncio.run(edit_thread(_AuthorCtx(), _NotAThread(), name="x"))


class _EditableChannel:
    def __init__(self, **attrs):
        self.id = attrs.pop("id", 10)
        self.name = attrs.pop("name", "general")
        self.type = attrs.pop("type", "text")
        self.edit_kwargs = None
        self.edit_reason = None
        for key, value in attrs.items():
            setattr(self, key, value)

    async def edit(self, *, reason=None, **kwargs):
        self.edit_kwargs = dict(kwargs)
        self.edit_reason = reason
        for key, value in kwargs.items():
            setattr(self, key, value)
        return self


def test_set_slowmode_edits_the_channel_with_audit_reason():
    from core.ops import set_slowmode
    chan = _EditableChannel(slowmode_delay=0)
    payload = asyncio.run(set_slowmode(_AuthorCtx(), chan, 30))
    assert chan.edit_kwargs == {"slowmode_delay": 30}
    assert chan.edit_reason == "set_slowmode op by tester (321)"
    assert payload == {"channel_id": 10, "slowmode_delay": 30}


def test_set_slowmode_refuses_threads_and_slowmodeless_channels():
    from core.ops import set_slowmode
    with pytest.raises(ValueError, match="edit_thread"):
        asyncio.run(set_slowmode(_AuthorCtx(), _FakeThread(), 30))

    class _Category:
        id = 9
    with pytest.raises(ValueError, match="no.*slowmode|slowmode"):
        asyncio.run(set_slowmode(_AuthorCtx(), _Category(), 30))


def test_edit_channel_applies_partial_edits():
    from core.ops import edit_channel
    chan = _EditableChannel(topic="old", nsfw=False)
    payload = asyncio.run(edit_channel(_AuthorCtx(), chan,
                                       topic="new topic", nsfw=True))
    assert chan.edit_kwargs == {"topic": "new topic", "nsfw": True}
    assert chan.edit_reason == "edit_channel op by tester (321)"
    assert payload == {"id": 10, "name": "general", "topic": "new topic",
                       "nsfw": True, "type": "text"}


def test_edit_channel_requires_at_least_one_field():
    from core.ops import edit_channel
    chan = _EditableChannel(topic="old")
    with pytest.raises(ValueError, match="Nothing to edit"):
        asyncio.run(edit_channel(_AuthorCtx(), chan))
    assert chan.edit_kwargs is None


def test_edit_channel_refuses_threads_and_topicless_topic_edits():
    from core.ops import edit_channel
    with pytest.raises(ValueError, match="edit_thread"):
        asyncio.run(edit_channel(_AuthorCtx(), _FakeThread(), name="x"))
    voice = _EditableChannel(type="voice")  # no topic attribute
    with pytest.raises(ValueError, match="topic"):
        asyncio.run(edit_channel(_AuthorCtx(), voice, topic="nope"))
    assert voice.edit_kwargs is None


def test_get_member_permissions_returns_granted_names_only():
    from core.ops import get_member_permissions

    class _Chan:
        id = 10

        def permissions_for(self, member):
            return [("read_messages", True), ("send_messages", True),
                    ("manage_guild", False)]

    member = type("M", (), {"id": 42})()
    payload = asyncio.run(get_member_permissions(
        _NoActorCtx(), _Chan(), member))
    assert payload == {"channel_id": 10, "user_id": 42,
                       "permissions": ["read_messages", "send_messages"]}


class _FakeForum(discord.ForumChannel):
    """A real discord.ForumChannel (isinstance guard must pass) with none of
    the gateway state — __init__ is deliberately not chained."""

    def __init__(self, tags=()):  # noqa: D107 - test fake
        self.id = 30
        self.name = "help-forum"
        self._tags = list(tags)
        self.created_with = None

    @property
    def available_tags(self):
        return self._tags

    async def create_thread(self, **kwargs):
        self.created_with = dict(kwargs)
        thread = type("T", (), {"id": 77, "name": kwargs["name"]})()
        message = type("M", (), {"id": 88})()
        return type("TM", (), {"thread": thread, "message": message})()


def _forum_tag(tid, name):
    return type("Tag", (), {"id": tid, "name": name})()


def test_create_forum_post_creates_thread_with_tags_and_no_pings():
    from core.ops import create_forum_post
    forum = _FakeForum(tags=[_forum_tag(1, "solved"), _forum_tag(2, "bug")])
    result = asyncio.run(create_forum_post(
        _NoActorCtx(), forum, "Help please", "It broke.", tag_ids=["2"]))
    assert forum.created_with["name"] == "Help please"
    assert forum.created_with["content"] == "It broke."
    assert [t.id for t in forum.created_with["applied_tags"]] == [2]
    # Same never-ping policy as every send-class op.
    assert forum.created_with["allowed_mentions"].everyone is False
    payload = registry.get("create_forum_post").serialize_result(result)
    assert payload == {"thread_id": 77, "name": "Help please",
                       "message_id": 88}


def test_create_forum_post_rejects_an_unknown_tag_id():
    from core.ops import create_forum_post
    forum = _FakeForum(tags=[_forum_tag(1, "solved")])
    with pytest.raises(ValueError, match="forum tag"):
        asyncio.run(create_forum_post(_NoActorCtx(), forum, "T", "body",
                                      tag_ids=["999"]))
    assert forum.created_with is None


def test_create_forum_post_refuses_non_forum_and_empty_content():
    from core.ops import create_forum_post

    class _Text:
        id = 10
    with pytest.raises(ValueError, match="not a.*forum"):
        asyncio.run(create_forum_post(_NoActorCtx(), _Text(), "T", "body"))
    forum = _FakeForum()
    with pytest.raises(ValueError, match="non-empty content"):
        asyncio.run(create_forum_post(_NoActorCtx(), forum, "T", "   "))
    assert forum.created_with is None


# --------------------------------------------------------------------------
# Guild & members ops added in the 2026-08 guild-domain gap pass.
# Wire shapes and permission tiers first (decisions worth locking), then
# impl behavior with fakes (no Discord needed).
# --------------------------------------------------------------------------

def test_get_member_shape():
    schema = registry.get("get_member").to_json_schema()
    assert set(schema["properties"]) == {"guild_id", "user_id"}
    # guild_id is ambient-optional, same as add_role.
    assert schema["required"] == ["user_id"]


def test_get_guild_info_shape():
    schema = registry.get("get_guild_info").to_json_schema()
    assert set(schema["properties"]) == {"guild_id"}
    assert schema["required"] == ["guild_id"]


def test_search_members_shape():
    schema = registry.get("search_members").to_json_schema()
    assert set(schema["properties"]) == {"guild_id", "query", "limit"}
    assert set(schema["required"]) == {"guild_id", "query"}
    assert schema["properties"]["limit"]["maximum"] == 100
    assert schema["properties"]["limit"]["default"] == 10


def test_set_nickname_shape():
    schema = registry.get("set_nickname").to_json_schema()
    assert set(schema["properties"]) == {"guild_id", "user_id", "nick",
                                         "reason"}
    # nick is optional ON PURPOSE: omitting it clears the nickname.
    assert schema["required"] == ["user_id"]


def test_timeout_member_shape_and_28_day_cap():
    from core.ops import TIMEOUT_MAX_MINUTES
    schema = registry.get("timeout_member").to_json_schema()
    assert set(schema["properties"]) == {"guild_id", "user_id",
                                         "duration_minutes", "reason"}
    assert set(schema["required"]) == {"user_id", "duration_minutes"}
    assert schema["properties"]["duration_minutes"]["maximum"] == 40320
    assert TIMEOUT_MAX_MINUTES == 40320  # Discord's 28-day cap
    assert schema["properties"]["duration_minutes"]["minimum"] == 1


def test_remove_timeout_shape():
    schema = registry.get("remove_timeout").to_json_schema()
    assert set(schema["properties"]) == {"guild_id", "user_id", "reason"}
    assert schema["required"] == ["user_id"]


def test_list_bans_shape():
    schema = registry.get("list_bans").to_json_schema()
    assert set(schema["properties"]) == {"guild_id", "limit", "after_user_id"}
    assert schema["required"] == ["guild_id"]
    assert schema["properties"]["limit"]["maximum"] == 1000
    assert schema["properties"]["after_user_id"]["type"] == "string"


def test_fetch_audit_logs_shape():
    schema = registry.get("fetch_audit_logs").to_json_schema()
    assert set(schema["properties"]) == {"guild_id", "limit", "user_id",
                                         "action", "before"}
    assert schema["required"] == ["guild_id"]
    assert schema["properties"]["limit"]["maximum"] == 100
    # The entry-id cursor is a snowflake and must travel as a string even
    # though its wire name doesn't end in _id.
    assert schema["properties"]["before"]["type"] == "string"


def test_estimate_prune_shape():
    schema = registry.get("estimate_prune").to_json_schema()
    assert set(schema["properties"]) == {"guild_id", "days"}
    assert schema["required"] == ["guild_id"]
    assert schema["properties"]["days"]["maximum"] == 30


@pytest.mark.parametrize("name", ["list_integrations", "list_invites"])
def test_guild_enumeration_ops_take_only_a_guild_id(name):
    schema = registry.get(name).to_json_schema()
    assert set(schema["properties"]) == {"guild_id"}
    assert schema["required"] == ["guild_id"]


@pytest.mark.parametrize("name,level", [
    # By-hand-without-a-dialog reads any member can see stay EVERYONE.
    ("get_member", PermissionLevel.EVERYONE),
    ("get_guild_info", PermissionLevel.EVERYONE),
    ("search_members", PermissionLevel.EVERYONE),
    # Admin-view reads and member-state writes carry the ADMIN gate.
    ("set_nickname", PermissionLevel.ADMIN),
    ("timeout_member", PermissionLevel.ADMIN),
    ("remove_timeout", PermissionLevel.ADMIN),
    ("list_bans", PermissionLevel.ADMIN),
    ("fetch_audit_logs", PermissionLevel.ADMIN),
    ("estimate_prune", PermissionLevel.ADMIN),
    ("list_integrations", PermissionLevel.ADMIN),
    ("list_invites", PermissionLevel.ADMIN),
])
def test_guild_domain_permission_tiers(name, level):
    op_obj = registry.require(name)
    assert op_obj.permission == level
    assert op_obj.scope == OpScope.GUILD


def test_get_member_serializer_ships_the_profile_fields():
    import datetime as _dt

    _default_role = type("R", (), {
        "id": 1, "name": "@everyone", "is_default": lambda self: True})()
    _mod_role = type("R", (), {
        "id": 2, "name": "Mods", "is_default": lambda self: False})()
    _avatar = type("Av", (), {
        "__str__": lambda self: "https://cdn/avatars/42.png"})()

    class _Member:
        id = 42
        name = "alice"
        display_name = "Alice"
        nick = "Alice"
        bot = False
        roles = [_default_role, _mod_role]
        joined_at = _dt.datetime(2025, 1, 2, 3, 4, 5)
        created_at = _dt.datetime(2020, 6, 7, 8, 9, 10)
        premium_since = None
        timed_out_until = _dt.datetime(2026, 8, 21, 0, 0, 0)
        status = "idle"
        display_avatar = _avatar
        pending = False

    payload = registry.get("get_member").serialize_result(_Member())
    assert payload["id"] == 42
    assert payload["name"] == "alice"
    assert payload["nick"] == "Alice"
    # @everyone is implicit membership, not information — excluded.
    assert payload["roles"] == [{"id": 2, "name": "Mods"}]
    assert payload["joined_at"] == "2025-01-02T03:04:05"
    assert payload["created_at"] == "2020-06-07T08:09:10"
    assert payload["premium_since"] is None
    assert payload["timed_out_until"] == "2026-08-21T00:00:00"
    assert payload["status"] == "idle"
    assert payload["avatar_url"] == "https://cdn/avatars/42.png"
    assert payload["pending"] is False


def test_get_guild_info_reads_the_cache_facts():
    import datetime as _dt
    from core.ops import get_guild_info

    _icon = type("Ic", (), {"__str__": lambda self: "https://cdn/icon.png"})()

    class _Guild:
        id = 1
        name = "Testland"
        description = "a place"
        owner_id = 42
        member_count = 250
        created_at = _dt.datetime(2019, 5, 1, 0, 0, 0)
        premium_tier = 2
        premium_subscription_count = 14
        features = ("COMMUNITY", "NEWS")
        verification_level = "medium"
        preferred_locale = "en-US"
        vanity_url_code = None
        icon = _icon
        banner = None

    payload = asyncio.run(get_guild_info(_NoActorCtx(), _Guild()))
    assert payload["name"] == "Testland"
    assert payload["owner_id"] == 42
    assert payload["member_count"] == 250
    assert payload["created_at"] == "2019-05-01T00:00:00"
    assert payload["premium_tier"] == 2
    assert payload["features"] == ["COMMUNITY", "NEWS"]
    assert payload["verification_level"] == "medium"
    assert payload["preferred_locale"] == "en-US"
    assert payload["icon_url"] == "https://cdn/icon.png"
    assert payload["banner_url"] is None


def test_search_members_queries_the_gateway_and_serializes_rows():
    from core.ops import search_members

    class _QueryGuild:
        def __init__(self):
            self.seen = None

        async def query_members(self, query=None, *, limit=5, cache=True):
            self.seen = (query, limit, cache)
            return [type("M", (), {"id": 42, "display_name": "Alice",
                                   "name": "alice", "status": "online"})()]

    guild = _QueryGuild()
    rows = asyncio.run(search_members(_NoActorCtx(), guild, "ali", limit=25))
    assert guild.seen == ("ali", 25, True)
    assert rows == [{"id": 42, "display_name": "Alice", "name": "alice",
                     "status": "online"}]
    payload = registry.get("search_members").serialize_result(rows)
    assert payload == {"members": rows, "count": 1}


class _EditableMember:
    def __init__(self, mid=42):
        self.id = mid
        self.edit_kwargs = None
        self.edit_reason = None
        self.timeout_until = "unset"
        self.timeout_reason = None

    async def edit(self, *, nick, reason=None):
        self.edit_kwargs = {"nick": nick}
        self.edit_reason = reason

    async def timeout(self, until, *, reason=None):
        self.timeout_until = until
        self.timeout_reason = reason


def test_set_nickname_sets_and_stamps_an_audit_reason():
    from core.ops import set_nickname
    member = _EditableMember()
    payload = asyncio.run(set_nickname(_AuthorCtx(), member, nick="Cool"))
    assert member.edit_kwargs == {"nick": "Cool"}
    assert member.edit_reason == "set_nickname op by tester (321)"
    assert payload == {"member_id": 42, "nick": "Cool"}


@pytest.mark.parametrize("nick", [None, "", "   "])
def test_set_nickname_omitted_or_empty_clears(nick):
    from core.ops import set_nickname
    member = _EditableMember()
    payload = asyncio.run(set_nickname(_AuthorCtx(), member, nick=nick,
                                       reason="cleanup"))
    assert member.edit_kwargs == {"nick": None}
    # A caller-supplied reason wins over the actor stamp.
    assert member.edit_reason == "cleanup"
    assert payload == {"member_id": 42, "nick": None}


def test_timeout_member_times_out_until_now_plus_duration():
    import datetime as _dt
    from core.ops import timeout_member
    member = _EditableMember()
    start = discord.utils.utcnow()
    payload = asyncio.run(timeout_member(_AuthorCtx(), member, 30,
                                         reason="spam"))
    until = member.timeout_until
    assert isinstance(until, _dt.datetime)
    delta = until - (start + _dt.timedelta(minutes=30))
    assert abs(delta.total_seconds()) < 5
    assert member.timeout_reason == "spam"
    assert payload == {"member_id": 42,
                       "timed_out_until": until.isoformat()}


def test_remove_timeout_passes_none_with_the_actor_stamp():
    from core.ops import remove_timeout
    member = _EditableMember()
    assert asyncio.run(remove_timeout(_AuthorCtx(), member)) is True
    assert member.timeout_until is None
    assert member.timeout_reason == "remove_timeout op by tester (321)"


class _BanGuild:
    def __init__(self, entries):
        self._entries = entries
        self.seen_kwargs = None

    def bans(self, **kwargs):
        self.seen_kwargs = kwargs
        entries = self._entries

        async def gen():
            for e in entries:
                yield e
        return gen()


def _ban_entry(uid, name, reason):
    user = type("U", (), {"id": uid, "name": name})()
    return type("BanEntry", (), {"user": user, "reason": reason})()


def test_list_bans_serializes_entries():
    from core.ops import list_bans
    guild = _BanGuild([_ban_entry(1, "spammer", "spam"),
                       _ban_entry(2, "raider", None)])
    rows = asyncio.run(list_bans(_NoActorCtx(), guild))
    assert rows == [{"user_id": 1, "name": "spammer", "reason": "spam"},
                    {"user_id": 2, "name": "raider", "reason": None}]
    assert guild.seen_kwargs == {"limit": 100}
    payload = registry.get("list_bans").serialize_result(rows)
    assert payload == {"bans": rows, "count": 2}


def test_list_bans_pages_by_user_id_cursor():
    from core.ops import list_bans
    guild = _BanGuild([])
    asyncio.run(list_bans(_NoActorCtx(), guild, limit=50, after_user_id=99))
    assert guild.seen_kwargs["limit"] == 50
    after = guild.seen_kwargs["after"]
    assert isinstance(after, discord.Object) and after.id == 99


class _AuditGuild:
    def __init__(self, entries):
        self._entries = entries
        self.seen_kwargs = None

    def audit_logs(self, **kwargs):
        self.seen_kwargs = kwargs
        entries = self._entries

        async def gen():
            for e in entries:
                yield e
        return gen()


def _audit_entry():
    import datetime as _dt

    class _LiveRole:
        def __str__(self):
            return "<Role Mods>"

    changes = type("Changes", (), {
        # AuditLogDiff iterates as (attribute, value) pairs; "color" only
        # exists on the after side (a create-style one-sided change), and
        # its value is a live object that must be stringified.
        "before": [("name", "old-name")],
        "after": [("name", "new-name"), ("color", _LiveRole())],
    })()
    return type("Entry", (), {
        "id": 777,
        "action": discord.AuditLogAction.role_update,
        "user": type("U", (), {"id": 42})(),
        "target": type("Role", (), {"id": 9})(),
        "reason": "tidy",
        "created_at": _dt.datetime(2026, 8, 19, 12, 0, 0),
        "changes": changes,
    })()


def test_fetch_audit_logs_serializes_entries_and_stringifies_live_objects():
    from core.ops import fetch_audit_logs
    guild = _AuditGuild([_audit_entry()])
    entries = asyncio.run(fetch_audit_logs(_NoActorCtx(), guild))
    assert guild.seen_kwargs == {"limit": 50}
    assert len(entries) == 1
    e = entries[0]
    assert e["id"] == 777
    assert e["action"] == "role_update"
    assert e["user_id"] == 42
    assert e["target_id"] == 9
    assert e["target_type"] == "Role"
    assert e["reason"] == "tidy"
    assert e["created_at"] == "2026-08-19T12:00:00"
    assert e["changes"] == [
        {"attribute": "name", "before": "old-name", "after": "new-name"},
        {"attribute": "color", "before": None, "after": "<Role Mods>"},
    ]
    payload = registry.get("fetch_audit_logs").serialize_result(entries)
    assert payload == {"entries": entries, "count": 1}


def test_fetch_audit_logs_resolves_action_names_and_passes_filters():
    from core.ops import fetch_audit_logs
    guild = _AuditGuild([])
    user = type("U", (), {"id": 42})()
    asyncio.run(fetch_audit_logs(_NoActorCtx(), guild, limit=10, user=user,
                                 action="ban", before=555))
    assert guild.seen_kwargs["limit"] == 10
    assert guild.seen_kwargs["user"] is user
    assert guild.seen_kwargs["action"] is discord.AuditLogAction.ban
    before = guild.seen_kwargs["before"]
    assert isinstance(before, discord.Object) and before.id == 555


def test_fetch_audit_logs_rejects_an_unknown_action_name():
    from core.ops import fetch_audit_logs
    guild = _AuditGuild([])
    with pytest.raises(ValueError, match="audit-log action"):
        asyncio.run(fetch_audit_logs(_NoActorCtx(), guild,
                                     action="explode_guild"))
    assert guild.seen_kwargs is None


def test_estimate_prune_is_a_dry_run_read():
    from core.ops import estimate_prune

    class _Guild:
        seen_days = None

        async def estimate_pruned_members(self, *, days):
            self.seen_days = days
            return 7

    guild = _Guild()
    payload = asyncio.run(estimate_prune(_NoActorCtx(), guild, days=14))
    assert guild.seen_days == 14
    assert payload == {"days": 14, "estimated_members": 7}


def test_list_integrations_serializes_accounts_and_bot_apps():
    from core.ops import list_integrations

    _account = type("Acc", (), {"id": "twitch-1", "name": "streamer"})()
    _plain = type("Integ", (), {
        "id": 1, "name": "Twitch", "type": "twitch", "enabled": True,
        "account": _account, "application": None})()
    _bot_app = type("App", (), {"user": type("U", (), {"id": 555})()})()
    _bot = type("Integ", (), {
        "id": 2, "name": "SomeBot", "type": "discord", "enabled": True,
        "account": None, "application": _bot_app})()

    class _Guild:
        async def integrations(self):
            return [_plain, _bot]

    rows = asyncio.run(list_integrations(_NoActorCtx(), _Guild()))
    assert rows[0] == {"id": 1, "name": "Twitch", "type": "twitch",
                       "enabled": True, "account_id": "twitch-1",
                       "account_name": "streamer"}
    # application_bot_user_id appears only on bot integrations.
    assert "application_bot_user_id" not in rows[0]
    assert rows[1]["application_bot_user_id"] == 555
    payload = registry.get("list_integrations").serialize_result(rows)
    assert payload == {"integrations": rows, "count": 2}


def test_list_invites_serializes_invite_facts():
    import datetime as _dt
    from core.ops import list_invites

    _invite = type("Inv", (), {
        "code": "abc123",
        "channel": type("C", (), {"id": 10})(),
        "inviter": type("U", (), {"id": 42, "name": "alice"})(),
        "uses": 3,
        "max_uses": 0,
        "max_age": 86400,
        "created_at": _dt.datetime(2026, 8, 1, 0, 0, 0),
        "expires_at": None,
        "temporary": False,
    })()

    class _Guild:
        vanity_url_code = "coolguild"

        async def invites(self):
            return [_invite]

    payload = asyncio.run(list_invites(_NoActorCtx(), _Guild()))
    assert payload["invites"] == [{
        "code": "abc123", "channel_id": 10, "inviter_id": 42,
        "inviter_name": "alice", "uses": 3, "max_uses": 0, "max_age": 86400,
        "created_at": "2026-08-01T00:00:00", "expires_at": None,
        "temporary": False}]
    assert payload["vanity_code"] == "coolguild"
    assert payload["count"] == 1
    # Identity serializer: the payload dict IS the wire shape.
    assert registry.get("list_invites").serialize_result(payload) is payload


# --------------------------------------------------------------------------
# Expressive-domain ops added in the 2026-08 gap pass: stickers, emoji
# role restriction + download, poll voters, invite create/revoke, webhook
# audit. Wire shapes and permission tiers first, then impl behavior with
# fakes (no Discord needed).
# --------------------------------------------------------------------------

def test_sticker_op_shapes():
    assert set(registry.get("list_stickers").to_json_schema()
               ["properties"]) == {"guild_id"}

    create = registry.get("create_sticker").to_json_schema()
    assert set(create["properties"]) == {
        "guild_id", "name", "description", "emoji", "file_path"}
    # Discord requires all of these on upload — none is optional.
    assert set(create["required"]) == {
        "guild_id", "name", "description", "emoji", "file_path"}

    edit = registry.get("edit_sticker").to_json_schema()
    assert set(edit["properties"]) == {
        "guild_id", "sticker_id", "name", "description", "emoji"}
    assert set(edit["required"]) == {"guild_id", "sticker_id"}
    assert edit["properties"]["sticker_id"]["type"] == "string"

    delete = registry.get("delete_sticker").to_json_schema()
    assert set(delete["properties"]) == {"guild_id", "sticker_id"}
    assert set(delete["required"]) == {"guild_id", "sticker_id"}


def test_download_emoji_shape():
    schema = registry.get("download_emoji").to_json_schema()
    assert set(schema["properties"]) == {"guild_id", "emoji_id", "sticker"}
    assert set(schema["required"]) == {"guild_id", "emoji_id"}
    assert schema["properties"]["emoji_id"]["type"] == "string"
    assert schema["properties"]["sticker"]["type"] == "boolean"


def test_get_poll_voters_shape():
    schema = registry.get("get_poll_voters").to_json_schema()
    assert set(schema["properties"]) == {
        "channel_id", "message_id", "answer_id", "limit"}
    assert set(schema["required"]) == {
        "channel_id", "message_id", "answer_id"}
    # Poll answer ids are small ordinals, but every *_id wire param travels
    # as a string by registry convention (the 2**53 rule is name-based).
    assert schema["properties"]["answer_id"]["type"] == "string"
    assert schema["properties"]["limit"]["maximum"] == 100


def test_create_invite_shape_and_safe_defaults():
    schema = registry.get("create_invite").to_json_schema()
    assert set(schema["properties"]) == {
        "channel_id", "max_age_seconds", "max_uses", "temporary"}
    assert schema["required"] == ["channel_id"]
    # The lazy path expires: 24h default, never-expiring only on request.
    assert schema["properties"]["max_age_seconds"]["default"] == 86400
    assert schema["properties"]["max_age_seconds"]["maximum"] == 604800
    assert schema["properties"]["max_uses"]["default"] == 0
    assert schema["properties"]["max_uses"]["maximum"] == 100


def test_revoke_invite_shape():
    schema = registry.get("revoke_invite").to_json_schema()
    assert set(schema["properties"]) == {"guild_id", "code"}
    assert set(schema["required"]) == {"guild_id", "code"}


def test_list_webhooks_shape():
    schema = registry.get("list_webhooks").to_json_schema()
    assert set(schema["properties"]) == {"guild_id", "channel_id"}
    assert schema["required"] == ["guild_id"]


def test_edit_emoji_gains_optional_role_ids():
    schema = registry.get("edit_emoji").to_json_schema()
    assert set(schema["properties"]) == {
        "guild_id", "emoji_id", "name", "role_ids"}
    assert set(schema["required"]) == {"guild_id", "emoji_id", "name"}
    assert schema["properties"]["role_ids"]["type"] == "array"


@pytest.mark.parametrize("name,level", [
    # Reads any member gets in the client stay EVERYONE.
    ("list_stickers", PermissionLevel.EVERYONE),
    ("get_poll_voters", PermissionLevel.EVERYONE),
    # Asset writes mirror the emoji precedent; invite lifecycle and the
    # webhook audit mirror the client's Manage-Server surface; download
    # writes the server filesystem (attachment-gate class).
    ("create_sticker", PermissionLevel.ADMIN),
    ("edit_sticker", PermissionLevel.ADMIN),
    ("delete_sticker", PermissionLevel.ADMIN),
    ("download_emoji", PermissionLevel.ADMIN),
    ("create_invite", PermissionLevel.ADMIN),
    ("revoke_invite", PermissionLevel.ADMIN),
    ("list_webhooks", PermissionLevel.ADMIN),
])
def test_expressive_domain_permission_tiers(name, level):
    op_obj = registry.require(name)
    assert op_obj.permission == level
    assert op_obj.scope == OpScope.GUILD


class _FakeSticker:
    def __init__(self, sid=901, name="wave", description="a wave",
                 emoji="👋", stype=None, fmt_name="png", fmt_ext="png"):
        self.id = sid
        self.name = name
        self.description = description
        self.emoji = emoji
        self.type = stype if stype is not None else discord.StickerType.guild
        self.format = type("Fmt", (), {
            "name": fmt_name, "file_extension": fmt_ext})()
        self.url = f"https://cdn/stickers/{sid}.{fmt_ext}"
        self.edit_kwargs = None
        self.edit_reason = None
        self.deleted = False

    async def edit(self, *, reason=None, **kwargs):
        self.edit_kwargs = dict(kwargs)
        self.edit_reason = reason
        for key, value in kwargs.items():
            setattr(self, key, value)
        return self

    async def delete(self, *, reason=None):
        self.deleted = True

    async def read(self):
        return b"sticker-bytes"


class _StickerGuild:
    def __init__(self, stickers=(), emojis=()):
        self.name = "Testland"
        self.stickers = list(stickers)
        self.emojis = list(emojis)
        self.created_with = None

    async def create_sticker(self, *, name, description, emoji, file,
                             reason=None):
        self.created_with = {"name": name, "description": description,
                             "emoji": emoji, "file": file, "reason": reason}
        return _FakeSticker(sid=999, name=name, description=description,
                            emoji=emoji)


class _AdminCtx:
    """Ctx that passes core.utils.is_admin via the guild admins config list
    — for direct impl calls that re-check admin internally (the attachment
    gate on create_sticker)."""

    def __init__(self):
        self.author = type("A", (), {
            "id": 321, "__str__": lambda self: "tester"})()
        self.guild = type("G", (), {"id": 1, "owner": None})()
        config = type("Cfg", (), {
            "get": lambda s, ctx, key, default=None, **kw:
                [321] if key == "admins" else default,
            "get_global": lambda s, key, default=None: default,
        })()
        self.bot = type("B", (), {
            "config": config,
            "user": type("U", (), {"id": 555})(),
        })()


def test_list_stickers_serializes_the_sticker_facts():
    from core.ops import list_stickers
    guild = _StickerGuild(stickers=[_FakeSticker()])
    rows = asyncio.run(list_stickers(_NoActorCtx(), guild))
    assert rows == [{
        "id": 901, "name": "wave", "description": "a wave", "emoji": "👋",
        "format": "png", "url": "https://cdn/stickers/901.png"}]
    payload = registry.get("list_stickers").serialize_result(rows)
    assert payload == {"stickers": rows, "count": 1}


def test_create_sticker_uploads_a_validated_file(tmp_path):
    from core.ops import create_sticker
    img = tmp_path / "wave.png"
    img.write_bytes(b"PNG-fake")
    guild = _StickerGuild()
    result = asyncio.run(create_sticker(_AdminCtx(), guild, "wave",
                                        "a wave", "👋", str(img)))
    assert guild.created_with["name"] == "wave"
    assert guild.created_with["description"] == "a wave"
    assert guild.created_with["emoji"] == "👋"
    assert guild.created_with["file"].filename == "wave.png"
    assert "create_sticker op by tester (321)" == guild.created_with["reason"]
    payload = registry.get("create_sticker").serialize_result(result)
    assert payload == {"id": 999, "name": "wave",
                       "url": "https://cdn/stickers/999.png"}


def test_create_sticker_rejects_bad_names_and_files(tmp_path):
    from core.ops import STICKER_MAX_BYTES, create_sticker, load_sticker_file
    guild = _StickerGuild()
    img = tmp_path / "ok.png"
    img.write_bytes(b"PNG-fake")
    with pytest.raises(ValueError, match="2-30"):
        asyncio.run(create_sticker(_AdminCtx(), guild, "x", "d", "👋",
                                   str(img)))
    assert guild.created_with is None

    with pytest.raises(ValueError, match="(?i)not found"):
        load_sticker_file("/no/such/sticker.png")
    bad_ext = tmp_path / "sticker.webp"  # valid for emoji, NOT for stickers
    bad_ext.write_bytes(b"RIFF-fake")
    with pytest.raises(ValueError, match="(?i)extension"):
        load_sticker_file(str(bad_ext))
    huge = tmp_path / "huge.png"
    huge.write_bytes(b"x" * (STICKER_MAX_BYTES + 1))
    with pytest.raises(ValueError, match="(?i)too large"):
        load_sticker_file(str(huge))
    # Lottie json is allowed — the sticker list differs from the emoji one.
    lottie = tmp_path / "anim.json"
    lottie.write_bytes(b"{}")
    assert load_sticker_file(str(lottie)) == lottie.resolve()


def test_edit_sticker_applies_partial_edits_with_guards():
    from core.ops import edit_sticker
    sticker = _FakeSticker()
    guild = _StickerGuild(stickers=[sticker])
    result = asyncio.run(edit_sticker(_AuthorCtx(), guild, 901,
                                      name="waving", emoji="🌊"))
    assert sticker.edit_kwargs == {"name": "waving", "emoji": "🌊"}
    assert sticker.edit_reason == "edit_sticker op by tester (321)"
    payload = registry.get("edit_sticker").serialize_result(result)
    assert payload == {"id": 901, "name": "waving", "description": "a wave"}

    with pytest.raises(ValueError, match="Nothing to edit"):
        asyncio.run(edit_sticker(_AuthorCtx(), guild, 901))
    with pytest.raises(ValueError, match="list_stickers"):
        asyncio.run(edit_sticker(_AuthorCtx(), guild, 777, name="x"))


def test_edit_sticker_refuses_a_non_guild_sticker():
    from core.ops import edit_sticker
    standard = _FakeSticker(stype=discord.StickerType.standard)
    guild = _StickerGuild(stickers=[standard])
    with pytest.raises(ValueError, match="not a guild sticker"):
        asyncio.run(edit_sticker(_AuthorCtx(), guild, 901, name="x"))
    assert standard.edit_kwargs is None


def test_delete_sticker_deletes_and_reports():
    from core.ops import delete_sticker
    sticker = _FakeSticker()
    guild = _StickerGuild(stickers=[sticker])
    info = asyncio.run(delete_sticker(_AuthorCtx(), guild, 901))
    assert sticker.deleted
    assert info == {"deleted": True, "id": 901, "name": "wave"}
    with pytest.raises(ValueError, match="list_stickers"):
        asyncio.run(delete_sticker(_AuthorCtx(), guild, 901234))


class _StickerSendChannel(_SendCaptureChannel):
    def __init__(self, guild):
        super().__init__()
        self.guild = guild


def test_send_message_attaches_a_guild_sticker():
    from core.ops import send_message
    sticker = _FakeSticker()
    chan = _StickerSendChannel(_StickerGuild(stickers=[sticker]))
    # Sticker-only send: empty content is allowed when a sticker rides along.
    asyncio.run(send_message(_NoActorCtx(), chan, "", sticker_id=901))
    assert chan.kwargs["stickers"] == [sticker]
    assert chan.kwargs["allowed_mentions"].everyone is False


def test_send_message_refuses_a_foreign_sticker_id():
    from core.ops import send_message
    chan = _StickerSendChannel(_StickerGuild(stickers=[_FakeSticker()]))
    with pytest.raises(ValueError, match="list_stickers"):
        asyncio.run(send_message(_NoActorCtx(), chan, "hi", sticker_id=777))
    assert chan.kwargs is None


def test_download_emoji_writes_the_asset_under_the_download_dir(
        tmp_path, monkeypatch):
    import core.ops as ops_module
    from core.ops import download_emoji
    monkeypatch.setattr(ops_module, "EMOJI_DOWNLOAD_DIR", tmp_path / "dl")

    class _Emoji:
        id = 42
        name = "blob"
        animated = False

        async def read(self):
            return b"emoji-bytes"

    guild = _StickerGuild(emojis=[_Emoji()])
    payload = asyncio.run(download_emoji(_NoActorCtx(), guild, 42))
    dest = tmp_path / "dl" / "blob_42.png"
    assert payload == {"file_path": str(dest), "bytes": 11, "name": "blob"}
    assert dest.read_bytes() == b"emoji-bytes"


def test_download_emoji_sticker_mode_uses_the_sticker_extension(
        tmp_path, monkeypatch):
    import core.ops as ops_module
    from core.ops import download_emoji
    monkeypatch.setattr(ops_module, "EMOJI_DOWNLOAD_DIR", tmp_path)
    sticker = _FakeSticker(fmt_name="lottie", fmt_ext="json")
    guild = _StickerGuild(stickers=[sticker])
    payload = asyncio.run(download_emoji(_NoActorCtx(), guild, 901,
                                         sticker=True))
    assert payload["file_path"].endswith("wave_901.json")
    assert payload["bytes"] == len(b"sticker-bytes")
    # An emoji id must not resolve in sticker mode and vice versa.
    with pytest.raises(ValueError, match="list_emojis"):
        asyncio.run(download_emoji(_NoActorCtx(), guild, 901))


class _VotersAnswer:
    def __init__(self, aid, text, user_ids):
        self.id = aid
        self.text = text
        self._user_ids = list(user_ids)
        self.seen_limit = None

    def voters(self, *, limit=None):
        self.seen_limit = limit
        ids = self._user_ids[:limit]

        async def gen():
            for uid in ids:
                yield type("U", (), {"id": uid, "name": f"u{uid}",
                                     "display_name": f"U{uid}"})()
        return gen()


def test_get_poll_voters_enumerates_one_answer():
    from core.ops import get_poll_voters
    answer = _VotersAnswer(2, "fruit", [11, 22])

    class _Poll:
        @staticmethod
        def get_answer(aid):
            return answer if aid == 2 else None

    class _Msg:
        poll = _Poll()

    payload = asyncio.run(get_poll_voters(_NoActorCtx(), _Msg(), 2))
    assert answer.seen_limit == 100
    assert payload == {
        "answer_id": 2, "text": "fruit",
        "voters": [{"id": 11, "name": "u11", "display_name": "U11"},
                   {"id": 22, "name": "u22", "display_name": "U22"}],
        "count": 2}

    with pytest.raises(ValueError, match="no answer"):
        asyncio.run(get_poll_voters(_NoActorCtx(), _Msg(), 9))


def test_get_poll_voters_refuses_a_poll_less_message():
    from core.ops import get_poll_voters

    class _Msg:
        poll = None

    with pytest.raises(ValueError, match="no poll"):
        asyncio.run(get_poll_voters(_NoActorCtx(), _Msg(), 1))


class _RestrictableEmoji:
    def __init__(self):
        self.id = 42
        self.name = "blob"
        self.animated = False
        self.managed = False
        self.url = "https://cdn/emojis/42.png"
        self.roles = []
        self.edit_kwargs = None

    def __str__(self):
        return "<:blob:42>"

    async def edit(self, *, reason=None, **kwargs):
        self.edit_kwargs = dict(kwargs)
        for key, value in kwargs.items():
            setattr(self, key, value)
        return self


def test_edit_emoji_applies_a_role_restriction_and_serializes_it():
    from core.ops import edit_emoji
    emoji = _RestrictableEmoji()
    role = type("R", (), {"id": 7, "name": "Mods"})()
    guild = _StickerGuild(emojis=[emoji])
    guild.id = 1
    guild.get_role = lambda rid: role if rid == 7 else None

    result = asyncio.run(edit_emoji(_AuthorCtx(), guild, 42, "blob",
                                    role_ids=["7"]))
    assert emoji.edit_kwargs == {"name": "blob", "roles": [role]}
    payload = registry.get("edit_emoji").serialize_result(result)
    assert payload["roles"] == [7]

    # Empty list clears the restriction (roles=[] on the API call).
    asyncio.run(edit_emoji(_AuthorCtx(), guild, 42, "blob", role_ids=[]))
    assert emoji.edit_kwargs == {"name": "blob", "roles": []}


def test_edit_emoji_refuses_an_unresolvable_role_id():
    from core.ops import ResolutionError, edit_emoji
    emoji = _RestrictableEmoji()
    guild = _StickerGuild(emojis=[emoji])
    guild.id = 1
    guild.get_role = lambda rid: None
    with pytest.raises(ResolutionError):
        asyncio.run(edit_emoji(_AuthorCtx(), guild, 42, "blob",
                               role_ids=["999"]))
    assert emoji.edit_kwargs is None


class _InviteChannel:
    def __init__(self, cid=10):
        self.id = cid
        self.seen_kwargs = None

    async def create_invite(self, **kwargs):
        import datetime as _dt
        self.seen_kwargs = dict(kwargs)
        return type("Inv", (), {
            "code": "fresh1",
            "url": "https://discord.gg/fresh1",
            "created_at": _dt.datetime(2026, 8, 20, 12, 0, 0),
            "expires_at": None,
        })()


def test_create_invite_defaults_to_expiring_and_computes_expiry():
    from core.ops import create_invite
    chan = _InviteChannel()
    payload = asyncio.run(create_invite(_AuthorCtx(), chan))
    assert chan.seen_kwargs["max_age"] == 86400
    assert chan.seen_kwargs["max_uses"] == 0
    assert chan.seen_kwargs["temporary"] is False
    assert chan.seen_kwargs["unique"] is True
    assert chan.seen_kwargs["reason"] == "create_invite op by tester (321)"
    assert payload["code"] == "fresh1"
    assert payload["url"] == "https://discord.gg/fresh1"
    assert payload["channel_id"] == 10
    assert payload["max_age"] == 86400
    # Discord's create response carries no expires_at; it is derived from
    # created_at + max_age rather than reported as null.
    assert payload["expires_at"] == "2026-08-21T12:00:00"


def test_create_invite_refuses_an_inviteless_channel():
    from core.ops import create_invite

    class _Category:
        id = 9
    with pytest.raises(ValueError, match="cannot carry invites"):
        asyncio.run(create_invite(_AuthorCtx(), _Category()))


class _RevocableInvite:
    def __init__(self, code, uses=3):
        self.code = code
        self.uses = uses
        self.deleted_reason = None

    async def delete(self, *, reason=None):
        self.deleted_reason = reason


class _InviteGuild:
    def __init__(self, invites):
        self.name = "Testland"
        self._invites = list(invites)

    async def invites(self):
        return list(self._invites)


def test_revoke_invite_revokes_a_verified_code():
    from core.ops import revoke_invite
    invite = _RevocableInvite("abc123", uses=5)
    guild = _InviteGuild([invite])
    payload = asyncio.run(revoke_invite(_AuthorCtx(), guild, "abc123"))
    assert invite.deleted_reason == "revoke_invite op by tester (321)"
    assert payload == {"revoked": True, "code": "abc123",
                       "uses_at_revoke": 5}


def test_revoke_invite_accepts_a_pasted_url():
    from core.ops import revoke_invite
    invite = _RevocableInvite("abc123")
    guild = _InviteGuild([invite])
    payload = asyncio.run(revoke_invite(
        _AuthorCtx(), guild, "https://discord.gg/abc123"))
    assert payload["code"] == "abc123"
    assert invite.deleted_reason is not None


def test_revoke_invite_refuses_a_code_not_in_this_guild():
    """The guard against blind Client.delete_invite: a code that isn't in
    THIS guild's own invite list is never deleted."""
    from core.ops import revoke_invite
    foreign = _RevocableInvite("other99")
    guild = _InviteGuild([foreign])
    with pytest.raises(ValueError, match="No active invite"):
        asyncio.run(revoke_invite(_AuthorCtx(), guild, "abc123"))
    assert foreign.deleted_reason is None


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
    assert payload == {"webhooks": [{
        "id": 1, "name": "gh-feed", "channel_id": 10, "type": "incoming",
        "creator_id": 42, "creator_name": "alice"}], "count": 1}
    # The credential must not appear ANYWHERE in the wire payload.
    import json
    flat = json.dumps(payload)
    assert "SECRET" not in flat and "TOKEN" not in flat


def test_list_webhooks_narrows_to_one_channel():
    from core.ops import list_webhooks

    class _Chan:
        id = 10

        async def webhooks(self):
            return [_webhook(2, "chan-hook", 10, 42, "alice")]

    class _Guild:
        async def webhooks(self):  # pragma: no cover - must not be called
            raise AssertionError("guild-wide path taken despite channel")

    payload = asyncio.run(list_webhooks(_NoActorCtx(), _Guild(), _Chan()))
    assert [w["id"] for w in payload["webhooks"]] == [2]

    class _Category:
        id = 9
    with pytest.raises(ValueError, match="cannot carry webhooks"):
        asyncio.run(list_webhooks(_NoActorCtx(), _Guild(), _Category()))


# --------------------------------------------------------------------------
# Voice, scheduled-event, and automod ops (2026-08 voice-domain gap pass).
# Wire shapes first (decisions worth locking), then impl behavior with
# fakes. Vocal-channel fakes SUBCLASS the real discord.py types without
# calling their __init__, because the ops' isinstance guards are part of
# the behavior under test (same pattern as _FakeThread above).
# --------------------------------------------------------------------------

def test_get_voice_state_shape():
    schema = registry.get("get_voice_state").to_json_schema()
    assert set(schema["properties"]) == {"guild_id", "user_id"}
    # guild_id is ambient-optional, same as add_role.
    assert schema["required"] == ["user_id"]


def test_list_voice_states_shape():
    schema = registry.get("list_voice_states").to_json_schema()
    assert set(schema["properties"]) == {"guild_id"}
    assert schema["required"] == []


def test_move_member_shape():
    schema = registry.get("move_member").to_json_schema()
    assert set(schema["properties"]) == {"guild_id", "user_id", "channel_id"}
    assert set(schema["required"]) == {"user_id", "channel_id"}


def test_disconnect_member_shape():
    schema = registry.get("disconnect_member").to_json_schema()
    assert set(schema["properties"]) == {"guild_id", "user_id"}
    assert schema["required"] == ["user_id"]


@pytest.mark.parametrize("name,flag", [
    ("set_voice_mute", "muted"),
    ("set_voice_deafen", "deafened"),
    ("set_stage_suppress", "suppressed"),
])
def test_voice_toggle_ops_take_a_required_boolean(name, flag):
    """One op with a boolean beats an op pair — the flag is REQUIRED so a
    caller always states the direction explicitly."""
    schema = registry.get(name).to_json_schema()
    assert set(schema["properties"]) == {"guild_id", "user_id", flag}
    assert set(schema["required"]) == {"user_id", flag}
    assert schema["properties"][flag]["type"] == "boolean"


def test_get_stage_instance_shape():
    schema = registry.get("get_stage_instance").to_json_schema()
    assert set(schema["properties"]) == {"channel_id"}
    assert schema["required"] == ["channel_id"]


def test_scheduled_event_read_shapes():
    listing = registry.get("list_scheduled_events").to_json_schema()
    assert set(listing["properties"]) == {"guild_id"}
    assert listing["required"] == []
    single = registry.get("get_scheduled_event").to_json_schema()
    assert set(single["properties"]) == {"guild_id", "event_id"}
    assert single["required"] == ["event_id"]
    assert single["properties"]["event_id"]["type"] == "string"
    users = registry.get("list_scheduled_event_users").to_json_schema()
    assert set(users["properties"]) == {"guild_id", "event_id", "limit"}
    assert users["required"] == ["event_id"]
    assert users["properties"]["limit"]["maximum"] == 1000


def test_create_scheduled_event_shape():
    schema = registry.get("create_scheduled_event").to_json_schema()
    assert set(schema["properties"]) == {
        "guild_id", "name", "start_time", "entity_type", "channel_id",
        "location", "end_time", "description"}
    assert set(schema["required"]) == {"name", "start_time", "entity_type"}


def test_edit_scheduled_event_shape():
    schema = registry.get("edit_scheduled_event").to_json_schema()
    assert set(schema["properties"]) == {
        "guild_id", "event_id", "name", "description", "channel_id",
        "location", "start_time", "end_time", "status"}
    assert schema["required"] == ["event_id"]


def test_list_automod_rules_shape():
    schema = registry.get("list_automod_rules").to_json_schema()
    assert set(schema["properties"]) == {"guild_id"}
    assert schema["required"] == []


@pytest.mark.parametrize("name,level", [
    # Reads any member gets in the client stay EVERYONE.
    ("get_voice_state", PermissionLevel.EVERYONE),
    ("list_voice_states", PermissionLevel.EVERYONE),
    ("get_stage_instance", PermissionLevel.EVERYONE),
    ("list_scheduled_events", PermissionLevel.EVERYONE),
    ("get_scheduled_event", PermissionLevel.EVERYONE),
    ("list_scheduled_event_users", PermissionLevel.EVERYONE),
    # Reversible client-parity voice writes and event writes are ADMIN.
    ("move_member", PermissionLevel.ADMIN),
    ("disconnect_member", PermissionLevel.ADMIN),
    ("set_voice_mute", PermissionLevel.ADMIN),
    ("set_voice_deafen", PermissionLevel.ADMIN),
    ("set_stage_suppress", PermissionLevel.ADMIN),
    ("create_scheduled_event", PermissionLevel.ADMIN),
    ("edit_scheduled_event", PermissionLevel.ADMIN),
    # ADMIN read: keyword rules expose the guild's filtered-word lists.
    ("list_automod_rules", PermissionLevel.ADMIN),
])
def test_voice_domain_permission_tiers(name, level):
    op_obj = registry.require(name)
    assert op_obj.permission == level
    assert op_obj.scope == OpScope.GUILD


def test_voice_and_event_groups_are_assigned():
    for name in ("get_voice_state", "list_voice_states", "move_member",
                 "disconnect_member", "set_voice_mute", "set_voice_deafen",
                 "set_stage_suppress", "get_stage_instance"):
        assert registry.require(name).group == "voice"
    for name in ("list_scheduled_events", "get_scheduled_event",
                 "list_scheduled_event_users", "create_scheduled_event",
                 "edit_scheduled_event"):
        assert registry.require(name).group == "events"
    assert registry.require("list_automod_rules").group == "moderation"


class _FakeVoiceState:
    def __init__(self, channel=None, **flags):
        self.channel = channel
        self.mute = flags.get("mute", False)
        self.deaf = flags.get("deaf", False)
        self.self_mute = flags.get("self_mute", False)
        self.self_deaf = flags.get("self_deaf", False)
        self.self_stream = flags.get("self_stream", False)
        self.self_video = flags.get("self_video", False)
        self.suppress = flags.get("suppress", False)
        self.requested_to_speak_at = None


class _DuckVoiceChannel:
    """Duck-typed vocal channel for the READ ops (no isinstance guards):
    permissions_for drives the actor-visibility policy."""

    def __init__(self, cid=30, name="General", perms=None, members=()):
        self.id = cid
        self.name = name
        self._perms = perms or _Perms(True, True)
        self.members = list(members)

    def permissions_for(self, member):
        return self._perms


class _VoiceMember:
    def __init__(self, mid=42, display_name="Alice", voice=None):
        self.id = mid
        self.display_name = display_name
        self.voice = voice
        self.moved_to = "unset"
        self.move_reason = None
        self.edit_kwargs = None
        self.edit_reason = None

    async def move_to(self, channel, *, reason=None):
        self.moved_to = channel
        self.move_reason = reason

    async def edit(self, **kwargs):
        self.edit_reason = kwargs.pop("reason", None)
        self.edit_kwargs = kwargs


def test_get_voice_state_reads_the_cached_state():
    from core.ops import get_voice_state
    chan = _DuckVoiceChannel(30, "General")
    vs = _FakeVoiceState(channel=chan, self_mute=True, self_stream=True)
    member = _VoiceMember(voice=vs)
    payload = asyncio.run(get_voice_state(_NoActorCtx(), member))
    assert payload["in_voice"] is True
    assert payload["channel_id"] == 30
    assert payload["channel_name"] == "General"
    assert payload["self_mute"] is True
    assert payload["streaming"] is True
    assert payload["mute"] is False
    assert payload["suppress"] is False


def test_get_voice_state_reports_not_in_voice():
    from core.ops import get_voice_state
    payload = asyncio.run(get_voice_state(_NoActorCtx(),
                                          _VoiceMember(voice=None)))
    assert payload == {"in_voice": False}


def test_get_voice_state_hides_a_channel_the_actor_cannot_see():
    """A real Member who can't read the target's voice channel gets
    in_voice=false — never a leak of WHERE the member is."""
    from core.ops import get_voice_state
    hidden = _DuckVoiceChannel(30, "staff-voice", perms=_Perms(False, False))
    member = _VoiceMember(voice=_FakeVoiceState(channel=hidden))
    ctx = OpContext(bot=None, author=_FakeMember(), guild=None)
    payload = asyncio.run(get_voice_state(ctx, member))
    assert payload == {"in_voice": False}


def test_list_voice_states_walks_voice_and_stage_channels():
    from core.ops import list_voice_states
    talker = _VoiceMember(1, "Alice",
                          _FakeVoiceState(self_mute=True))
    lurker = _VoiceMember(2, "Bob", _FakeVoiceState(deaf=True))
    voice = _DuckVoiceChannel(30, "General", members=[talker])
    stage = _DuckVoiceChannel(40, "Town hall", members=[lurker])
    empty = _DuckVoiceChannel(50, "AFK")

    class _Guild:
        voice_channels = [voice, empty]
        stage_channels = [stage]

    rows = asyncio.run(list_voice_states(_NoActorCtx(), guild=_Guild()))
    assert [r["channel_id"] for r in rows] == [30, 50, 40]
    assert [r["type"] for r in rows] == ["voice", "voice", "stage"]
    assert rows[0]["members"][0] == {
        "id": 1, "display_name": "Alice", "mute": False, "deaf": False,
        "self_mute": True, "self_deaf": False, "streaming": False,
        "video": False}
    assert rows[1]["members"] == []  # empty channels included, like the sidebar
    assert rows[2]["members"][0]["deaf"] is True
    payload = registry.get("list_voice_states").serialize_result(rows)
    assert payload == {"channels": rows, "count": 3}


def test_list_voice_states_drops_channels_the_actor_cannot_see():
    from core.ops import list_voice_states
    visible = _DuckVoiceChannel(30, "General")
    hidden = _DuckVoiceChannel(31, "staff-voice",
                               perms=_Perms(False, False))

    class _Guild:
        voice_channels = [visible, hidden]
        stage_channels = []

    ctx = OpContext(bot=None, author=_FakeMember(), guild=None)
    rows = asyncio.run(list_voice_states(ctx, guild=_Guild()))
    assert [r["channel_id"] for r in rows] == [30]


class _FakeVocalChannel(discord.VoiceChannel):
    """A real discord.VoiceChannel (isinstance guards must pass) with none
    of the gateway state — __init__ is deliberately not chained."""

    def __init__(self, cid=30, name="General", guild=None):
        self.id = cid
        self.name = name
        self.guild = guild


class _FakeStageChannelReal(discord.StageChannel):
    """Real discord.StageChannel for isinstance guards. The class attribute
    shadows the parent's `instance` property so tests can assign it."""

    instance = None

    def __init__(self, cid=40, name="Stage", guild=None, instance=None):
        self.id = cid
        self.name = name
        self.guild = guild
        self.instance = instance
        self.fetch_calls = 0

    async def fetch_instance(self):
        self.fetch_calls += 1
        response = type("R", (), {"status": 404, "reason": "Not Found"})()
        raise discord.NotFound(response, "no live stage")


def test_move_member_moves_with_an_audit_stamp():
    from core.ops import move_member
    dest = _FakeVocalChannel(30)
    member = _VoiceMember()
    payload = asyncio.run(move_member(_AuthorCtx(), member, dest))
    assert member.moved_to is dest
    assert member.move_reason == "move_member op by tester (321)"
    assert payload == {"moved": True, "channel_id": 30}


def test_move_member_refuses_a_non_vocal_channel():
    from core.ops import move_member

    class _TextChan:
        id = 10

    member = _VoiceMember()
    with pytest.raises(ValueError, match="not a voice or stage"):
        asyncio.run(move_member(_AuthorCtx(), member, _TextChan()))
    assert member.moved_to == "unset"


def test_disconnect_member_moves_to_none():
    from core.ops import disconnect_member
    member = _VoiceMember()
    assert asyncio.run(disconnect_member(_AuthorCtx(), member)) is True
    assert member.moved_to is None
    assert member.move_reason == "disconnect_member op by tester (321)"


@pytest.mark.parametrize("value", [True, False])
def test_set_voice_mute_toggles_both_ways(value):
    from core.ops import set_voice_mute
    member = _VoiceMember()
    payload = asyncio.run(set_voice_mute(_AuthorCtx(), member, value))
    assert member.edit_kwargs == {"mute": value}
    assert member.edit_reason == "set_voice_mute op by tester (321)"
    assert payload == {"member_id": 42, "muted": value}


@pytest.mark.parametrize("value", [True, False])
def test_set_voice_deafen_toggles_both_ways(value):
    from core.ops import set_voice_deafen
    member = _VoiceMember()
    payload = asyncio.run(set_voice_deafen(_AuthorCtx(), member, value))
    assert member.edit_kwargs == {"deafen": value}
    assert payload == {"member_id": 42, "deafened": value}


def test_set_stage_suppress_edits_a_stage_member():
    from core.ops import set_stage_suppress
    stage = _FakeStageChannelReal()
    member = _VoiceMember(voice=_FakeVoiceState(channel=stage))
    payload = asyncio.run(set_stage_suppress(_AuthorCtx(), member, True))
    assert member.edit_kwargs == {"suppress": True}
    assert payload == {"member_id": 42, "suppressed": True}


def test_set_stage_suppress_refuses_outside_a_stage_channel():
    """Suppress only exists on stages: a member in a plain voice channel
    (or not in voice at all) is refused locally, before any API call."""
    from core.ops import set_stage_suppress
    in_voice = _VoiceMember(
        voice=_FakeVoiceState(channel=_FakeVocalChannel()))
    with pytest.raises(ValueError, match="stage"):
        asyncio.run(set_stage_suppress(_AuthorCtx(), in_voice, True))
    assert in_voice.edit_kwargs is None
    not_connected = _VoiceMember(voice=None)
    with pytest.raises(ValueError, match="stage"):
        asyncio.run(set_stage_suppress(_AuthorCtx(), not_connected, False))


def test_get_stage_instance_reads_a_live_stage():
    from core.ops import get_stage_instance
    inst = type("I", (), {
        "topic": "AMA", "privacy_level": discord.PrivacyLevel.guild_only,
        "scheduled_event_id": 77})()
    stage = _FakeStageChannelReal(instance=inst)
    payload = asyncio.run(get_stage_instance(_NoActorCtx(), stage))
    assert payload == {"live": True, "topic": "AMA",
                       "privacy_level": "guild_only",
                       "scheduled_event_id": 77}
    assert stage.fetch_calls == 0  # cache hit, no REST call


def test_get_stage_instance_reports_no_live_stage():
    from core.ops import get_stage_instance
    stage = _FakeStageChannelReal(instance=None)
    payload = asyncio.run(get_stage_instance(_NoActorCtx(), stage))
    assert payload == {"live": False}
    assert stage.fetch_calls == 1  # cache miss fell through to fetch


def test_get_stage_instance_refuses_a_non_stage_channel():
    from core.ops import get_stage_instance
    with pytest.raises(ValueError, match="not a stage channel"):
        asyncio.run(get_stage_instance(_NoActorCtx(), _FakeVocalChannel()))


# -- scheduled events ------------------------------------------------------

class _FakeScheduledEvent:
    def __init__(self, eid=7, users=()):
        import datetime as _dt
        self.id = eid
        self.name = "Movie night"
        self.description = "bring popcorn"
        self.status = discord.EventStatus.scheduled
        self.entity_type = discord.EntityType.voice
        self.start_time = _dt.datetime(2026, 9, 1, 20, 0,
                                       tzinfo=_dt.timezone.utc)
        self.end_time = None
        self.channel_id = 30
        self.location = None
        self.creator_id = 42
        self.user_count = 5
        self.url = f"https://discord.com/events/1/{eid}"
        self.cover_image = None
        self._users = list(users)
        self.edit_kwargs = None
        self.edit_reason = None

    def users(self, *, limit=None):
        rows = self._users[:limit]

        async def gen():
            for u in rows:
                yield u
        return gen()

    async def edit(self, *, reason=None, **kwargs):
        self.edit_kwargs = dict(kwargs)
        self.edit_reason = reason
        return self


class _EventGuild:
    def __init__(self, events=()):
        self._events = {e.id: e for e in events}
        self.fetch_seen = None
        self.create_kwargs = None

    async def fetch_scheduled_events(self, *, with_counts=True):
        self.fetch_seen = with_counts
        return list(self._events.values())

    async def fetch_scheduled_event(self, event_id, *, with_counts=True):
        self.fetch_seen = (event_id, with_counts)
        return self._events[event_id]

    async def create_scheduled_event(self, **kwargs):
        self.create_kwargs = kwargs
        return _FakeScheduledEvent()


def test_list_scheduled_events_serializes_rows_with_counts():
    from core.ops import list_scheduled_events
    guild = _EventGuild([_FakeScheduledEvent()])
    rows = asyncio.run(list_scheduled_events(_NoActorCtx(), guild=guild))
    assert guild.fetch_seen is True  # with_counts requested
    assert rows == [{
        "id": 7, "name": "Movie night", "description": "bring popcorn",
        "status": "scheduled", "entity_type": "voice",
        "start_time": "2026-09-01T20:00:00+00:00", "end_time": None,
        "channel_id": 30, "location": None, "creator_id": 42,
        "user_count": 5}]
    payload = registry.get("list_scheduled_events").serialize_result(rows)
    assert payload == {"events": rows, "count": 1}


def test_get_scheduled_event_adds_url_and_image():
    from core.ops import get_scheduled_event
    guild = _EventGuild([_FakeScheduledEvent()])
    event = asyncio.run(get_scheduled_event(_NoActorCtx(), 7, guild=guild))
    assert guild.fetch_seen == (7, True)
    payload = registry.get("get_scheduled_event").serialize_result(event)
    assert payload["url"] == "https://discord.com/events/1/7"
    assert payload["image_url"] is None
    assert payload["name"] == "Movie night"


def test_list_scheduled_event_users_enumerates_rsvps():
    from core.ops import list_scheduled_event_users
    users = [type("U", (), {"id": 1, "display_name": "Alice"})(),
             type("U", (), {"id": 2, "display_name": "Bob"})()]
    guild = _EventGuild([_FakeScheduledEvent(users=users)])
    rows = asyncio.run(list_scheduled_event_users(
        _NoActorCtx(), 7, limit=1, guild=guild))
    assert rows == [{"id": 1, "display_name": "Alice"}]
    payload = registry.get(
        "list_scheduled_event_users").serialize_result(rows)
    assert payload == {"users": rows, "count": 1}


def test_create_scheduled_event_builds_a_voice_event():
    from core.ops import create_scheduled_event
    guild = _EventGuild()
    chan = _FakeVocalChannel(30)
    asyncio.run(create_scheduled_event(
        _AuthorCtx(), "Movie night", "2100-01-01T20:00:00+00:00", "voice",
        channel=chan, description="bring popcorn", guild=guild))
    kw = guild.create_kwargs
    assert kw["name"] == "Movie night"
    assert kw["entity_type"] is discord.EntityType.voice
    assert kw["privacy_level"] is discord.PrivacyLevel.guild_only
    assert kw["channel"] is chan
    assert kw["description"] == "bring popcorn"
    assert kw["start_time"].tzinfo is not None
    assert kw["reason"] == "create_scheduled_event op by tester (321)"
    assert "location" not in kw


def test_create_scheduled_event_naive_time_is_taken_as_utc():
    import datetime as _dt
    from core.ops import create_scheduled_event
    guild = _EventGuild()
    asyncio.run(create_scheduled_event(
        _AuthorCtx(), "X", "2100-01-01T20:00:00", "external",
        location="the park", end_time="2100-01-01T22:00:00", guild=guild))
    kw = guild.create_kwargs
    assert kw["start_time"].tzinfo == _dt.timezone.utc
    assert kw["end_time"].tzinfo == _dt.timezone.utc
    assert kw["location"] == "the park"
    assert "channel" not in kw


@pytest.mark.parametrize("kwargs,match", [
    # Unknown entity type.
    (dict(entity_type="concert"), "entity_type"),
    # Past start time.
    (dict(start_time="2001-01-01T00:00:00+00:00"), "future"),
    # Garbage timestamp.
    (dict(start_time="tomorrowish"), "ISO-8601"),
    # voice event without a channel.
    (dict(channel=None), "require a voice/stage channel"),
    # external event without a location / end_time.
    (dict(entity_type="external", end_time="2100-01-02T00:00:00"),
     "location"),
    (dict(entity_type="external", location="the park"), "end_time"),
])
def test_create_scheduled_event_refusals(kwargs, match):
    from core.ops import create_scheduled_event
    guild = _EventGuild()
    base = dict(name="X", start_time="2100-01-01T20:00:00+00:00",
                entity_type="voice", channel=_FakeVocalChannel(), guild=guild)
    base.update(kwargs)
    with pytest.raises(ValueError, match=match):
        asyncio.run(create_scheduled_event(_AuthorCtx(), **base))
    assert guild.create_kwargs is None


def test_edit_scheduled_event_applies_sparse_edits_with_status():
    from core.ops import edit_scheduled_event
    event = _FakeScheduledEvent()
    guild = _EventGuild([event])
    asyncio.run(edit_scheduled_event(
        _AuthorCtx(), 7, name="Movie afternoon", status="active",
        guild=guild))
    assert event.edit_kwargs == {"name": "Movie afternoon",
                                 "status": discord.EventStatus.active}
    assert event.edit_reason == "edit_scheduled_event op by tester (321)"


def test_edit_scheduled_event_refuses_cancellation_and_empty_edits():
    """'canceled' would destroy the RSVP list — the delete gap is an owner
    decision, so the status vocabulary is forward-only on purpose."""
    from core.ops import edit_scheduled_event
    event = _FakeScheduledEvent()
    guild = _EventGuild([event])
    with pytest.raises(ValueError, match="active.*completed"):
        asyncio.run(edit_scheduled_event(_AuthorCtx(), 7, status="canceled",
                                         guild=guild))
    with pytest.raises(ValueError, match="Nothing to edit"):
        asyncio.run(edit_scheduled_event(_AuthorCtx(), 7, guild=guild))
    assert event.edit_kwargs is None


# -- automod ---------------------------------------------------------------

def test_list_automod_rules_serializes_defensively():
    import datetime as _dt
    from core.ops import list_automod_rules

    _keyword_trigger = type("T", (), {
        "type": discord.AutoModRuleTriggerType.keyword,
        "keyword_filter": ["badword"], "regex_patterns": ["b[a4]d"],
        "allow_list": ["badminton"], "mention_limit": None,
        "presets": None})()
    _action = type("A", (), {
        "type": discord.AutoModRuleActionType.timeout,
        "channel_id": None,
        "duration": _dt.timedelta(minutes=1)})()
    _keyword_rule = type("R", (), {
        "id": 1, "name": "no swears", "enabled": True,
        "event_type": discord.AutoModRuleEventType.message_send,
        "trigger": _keyword_trigger, "actions": [_action],
        "exempt_role_ids": [2], "exempt_channel_ids": [], "creator_id": 42})()
    # A presets rule exercises the flags path and absent keyword facets.
    _presets_trigger = type("T", (), {
        "type": discord.AutoModRuleTriggerType.keyword_preset,
        "keyword_filter": None, "regex_patterns": None, "allow_list": None,
        "mention_limit": None,
        "presets": discord.AutoModPresets(profanity=True)})()
    _presets_rule = type("R", (), {
        "id": 2, "name": "presets", "enabled": False,
        "event_type": discord.AutoModRuleEventType.message_send,
        "trigger": _presets_trigger, "actions": [],
        "exempt_role_ids": [], "exempt_channel_ids": [9], "creator_id": 42})()

    class _Guild:
        async def fetch_automod_rules(self):
            return [_keyword_rule, _presets_rule]

    rows = asyncio.run(list_automod_rules(_NoActorCtx(), guild=_Guild()))
    assert rows[0] == {
        "id": 1, "name": "no swears", "enabled": True,
        "event_type": "message_send", "trigger_type": "keyword",
        "keyword_filter": ["badword"], "regex_patterns": ["b[a4]d"],
        "allow_list": ["badminton"], "mention_limit": None, "presets": [],
        "actions": [{"type": "timeout", "channel_id": None,
                     "duration_s": 60.0}],
        "exempt_role_ids": [2], "exempt_channel_ids": [], "creator_id": 42}
    assert rows[1]["trigger_type"] == "keyword_preset"
    assert rows[1]["presets"] == ["profanity"]
    assert rows[1]["keyword_filter"] == []
    payload = registry.get("list_automod_rules").serialize_result(rows)
    assert payload == {"rules": rows, "count": 2}


# ==========================================================================
# NEEDS_OWNER-tier destructive / privileged ops (2026-08 owner-tier pass).
#
# 36 ops filling the deliberately-omitted destructive half of each domain.
# These freeze the inventory (the exact 36 names), the permission FLOOR
# (ADMIN, with three SUPERADMIN), the derived 'admin' agent gate, the new
# group ids, and one impl behavior per op.
# ==========================================================================

OWNER_TIER_OPS = {
    # channels
    "create_channel", "delete_channel", "clone_channel", "move_channel",
    "set_channel_overwrite", "delete_channel_overwrite",
    # threads (destructive/membership)
    "delete_thread", "add_thread_member", "remove_thread_member",
    "list_private_archived_threads",
    # member moderation
    "kick_member", "ban_member", "unban_member", "bulk_ban", "prune_members",
    "edit_member_roles",
    # message moderation
    "bulk_delete_messages", "purge_messages", "publish_message", "send_tts",
    "send_sticker", "remove_reaction_other", "clear_reactions",
    # webhooks
    "create_webhook", "edit_webhook", "delete_webhook", "execute_webhook",
    # guild settings
    "edit_guild_settings",
    # automod CRUD
    "create_automod_rule", "edit_automod_rule", "delete_automod_rule",
    # events / stage
    "delete_scheduled_event", "create_stage", "edit_stage", "end_stage",
    # invites
    "delete_invite",
}

# The three with server-wide blast radius are SUPERADMIN; the rest ADMIN.
OWNER_TIER_SUPERADMIN = {"edit_guild_settings", "bulk_ban", "purge_messages"}

OWNER_TIER_GROUPS = {
    "create_channel": "channels", "delete_channel": "channels",
    "clone_channel": "channels", "move_channel": "channels",
    "set_channel_overwrite": "channels",
    "delete_channel_overwrite": "channels",
    "delete_thread": "threads", "add_thread_member": "threads",
    "remove_thread_member": "threads",
    "list_private_archived_threads": "threads",
    "kick_member": "members", "ban_member": "members",
    "unban_member": "members", "bulk_ban": "members",
    "prune_members": "members", "edit_member_roles": "members",
    "bulk_delete_messages": "message-mod", "purge_messages": "message-mod",
    "publish_message": "message-mod", "send_tts": "message-mod",
    "send_sticker": "message-mod", "remove_reaction_other": "message-mod",
    "clear_reactions": "message-mod",
    "create_webhook": "webhooks", "edit_webhook": "webhooks",
    "delete_webhook": "webhooks", "execute_webhook": "webhooks",
    "edit_guild_settings": "guild-info",
    "create_automod_rule": "automod", "edit_automod_rule": "automod",
    "delete_automod_rule": "automod",
    "delete_scheduled_event": "events",
    "create_stage": "voice", "edit_stage": "voice", "end_stage": "voice",
    "delete_invite": "invites",
}


def test_owner_tier_ops_all_registered():
    """The full 36-op inventory is present — a missing one silently drops the
    owner-approved destructive capability from every frontend."""
    for name in OWNER_TIER_OPS:
        assert registry.get(name) is not None, f"{name} not registered"


@pytest.mark.parametrize("name", sorted(OWNER_TIER_OPS))
def test_owner_tier_permission_floor(name):
    """Every owner-tier op is at least ADMIN (three are SUPERADMIN), guild
    scope, so its agent gate derives 'admin' — never 'everyone'."""
    o = registry.require(name)
    expected = (PermissionLevel.SUPERADMIN if name in OWNER_TIER_SUPERADMIN
                else PermissionLevel.ADMIN)
    assert o.permission == expected, f"{name} floor {o.permission}"
    assert o.scope == OpScope.GUILD
    assert o.default_gate() == "admin"  # derived from the ADMIN+ floor


@pytest.mark.parametrize("name,group", sorted(OWNER_TIER_GROUPS.items()))
def test_owner_tier_group_assignments(name, group):
    assert registry.require(name).group == group


def test_owner_tier_new_groups_exist_and_stay_under_cap():
    from cogs.optional.gpt import SELECT_MAX_OPTIONS
    for gid in ("channels", "message-mod", "webhooks", "automod"):
        assert gid in OP_GROUPS, f"new group {gid} missing from OP_GROUPS"
    # No group anywhere exceeds Discord's select cap after the additions.
    for _gid, _label, ops in registry.grouped():
        assert len(ops) <= SELECT_MAX_OPTIONS


# -- wire-shape freeze (required sets) --------------------------------------

@pytest.mark.parametrize("name,props,required", [
    # create_channel's `category` is a CHANNEL param → wire name channel_id.
    ("create_channel",
     {"guild_id", "name", "kind", "channel_id", "topic"},
     {"guild_id", "name"}),
    ("delete_channel", {"channel_id"}, {"channel_id"}),
    ("clone_channel", {"channel_id", "name"}, {"channel_id"}),
    # move_channel's category_id is a SNOWFLAKE (resolved in-impl) so it does
    # not collide with the moved channel's own channel_id wire slot.
    ("move_channel", {"channel_id", "position", "category_id"},
     {"channel_id"}),
    ("set_channel_overwrite",
     {"channel_id", "target_type", "target_id", "allow", "deny"},
     {"channel_id", "target_type", "target_id"}),
    ("delete_channel_overwrite",
     {"channel_id", "target_type", "target_id"},
     {"channel_id", "target_type", "target_id"}),
    ("delete_thread", {"channel_id"}, {"channel_id"}),
    ("add_thread_member", {"channel_id", "user_id"},
     {"channel_id", "user_id"}),
    ("remove_thread_member", {"channel_id", "user_id"},
     {"channel_id", "user_id"}),
    ("list_private_archived_threads", {"channel_id", "limit"},
     {"channel_id"}),
    ("kick_member", {"guild_id", "user_id", "reason"}, {"user_id"}),
    ("ban_member",
     {"guild_id", "user_id", "reason", "delete_message_seconds"},
     {"user_id"}),
    ("unban_member", {"guild_id", "user_id", "reason"}, {"user_id"}),
    ("bulk_ban",
     {"guild_id", "user_ids", "reason", "delete_message_seconds"},
     {"user_ids"}),
    ("prune_members", {"guild_id", "days", "reason"}, {"days"}),
    ("edit_member_roles",
     {"guild_id", "user_id", "add_role_ids", "remove_role_ids"},
     {"user_id"}),
    ("bulk_delete_messages", {"channel_id", "message_ids"},
     {"channel_id", "message_ids"}),
    ("purge_messages", {"channel_id", "limit", "user_id"},
     {"channel_id", "limit"}),
    ("publish_message", {"channel_id", "message_id"},
     {"channel_id", "message_id"}),
    ("send_tts", {"channel_id", "content"}, {"channel_id", "content"}),
    ("send_sticker", {"channel_id", "sticker_id"},
     {"channel_id", "sticker_id"}),
    ("remove_reaction_other",
     {"channel_id", "message_id", "user_id", "emoji"},
     {"channel_id", "message_id", "user_id", "emoji"}),
    ("clear_reactions", {"channel_id", "message_id", "emoji"},
     {"channel_id", "message_id"}),
    ("create_webhook", {"channel_id", "name"}, {"channel_id", "name"}),
    ("edit_webhook", {"guild_id", "webhook_id", "name", "channel_id"},
     {"guild_id", "webhook_id"}),
    ("delete_webhook", {"guild_id", "webhook_id"},
     {"guild_id", "webhook_id"}),
    ("execute_webhook",
     {"guild_id", "webhook_id", "content", "username", "avatar_url"},
     {"guild_id", "webhook_id", "content"}),
    ("edit_guild_settings",
     {"guild_id", "name", "description", "verification_level"},
     {"guild_id"}),
    ("create_automod_rule",
     {"guild_id", "name", "trigger_type", "keyword_filter",
      "regex_patterns", "mention_limit", "enabled"},
     {"guild_id", "name", "trigger_type"}),
    ("edit_automod_rule",
     {"guild_id", "rule_id", "name", "enabled", "keyword_filter",
      "regex_patterns"},
     {"guild_id", "rule_id"}),
    ("delete_automod_rule", {"guild_id", "rule_id"},
     {"guild_id", "rule_id"}),
    ("delete_scheduled_event", {"guild_id", "event_id"}, {"event_id"}),
    ("create_stage", {"channel_id", "topic", "send_notification"},
     {"channel_id", "topic"}),
    ("edit_stage", {"channel_id", "topic"}, {"channel_id", "topic"}),
    ("end_stage", {"channel_id"}, {"channel_id"}),
    ("delete_invite", {"guild_id", "code"}, {"guild_id", "code"}),
])
def test_owner_tier_wire_shapes(name, props, required):
    schema = registry.get(name).to_json_schema()
    assert set(schema["properties"]) == props, name
    assert set(schema["required"]) == required, name


# -- channel-op impls -------------------------------------------------------

class _CrudGuild:
    """Guild fake covering the channel-CRUD / member-mod / invite creators."""

    def __init__(self, gid=1, name="Guild", roles=None, members=None):
        self.id = gid
        self.name = name
        self._roles = {r.id: r for r in (roles or [])}
        self._members = {m.id: m for m in (members or [])}
        self.create_kwargs = None
        self.kicked = []
        self.banned = []
        self.unbanned = []
        self.pruned_kwargs = None
        self.edit_kwargs = None
        self.description = None
        self.verification_level = None

    def get_role(self, rid):
        return self._roles.get(rid)

    def get_member(self, uid):
        return self._members.get(uid)

    async def create_text_channel(self, **kw):
        self.create_kwargs = ("text", kw)
        return type("C", (), {"id": 90, "name": kw["name"],
                              "type": "text", "category": None})()

    async def create_voice_channel(self, **kw):
        self.create_kwargs = ("voice", kw)
        return type("C", (), {"id": 91, "name": kw["name"],
                              "type": "voice", "category": None})()

    async def create_category(self, **kw):
        self.create_kwargs = ("category", kw)
        return type("C", (), {"id": 92, "name": kw["name"],
                              "type": "category", "category": None})()

    async def kick(self, user, *, reason=None):
        self.kicked.append((user, reason))

    async def ban(self, user, *, reason=None, delete_message_seconds=0):
        self.banned.append((user, reason, delete_message_seconds))

    async def unban(self, user, *, reason=None):
        self.unbanned.append((user, reason))

    async def bulk_ban(self, users, *, reason=None,
                       delete_message_seconds=86400):
        ids = [u.id for u in users]
        return type("R", (), {
            "banned": [type("O", (), {"id": i})() for i in ids[:-1]],
            "failed": [type("O", (), {"id": i})() for i in ids[-1:]]})()

    async def prune_members(self, *, days, reason=None):
        self.pruned_kwargs = {"days": days, "reason": reason}
        return 7

    async def edit(self, *, reason=None, **kw):
        self.edit_kwargs = dict(kw)
        for k, v in kw.items():
            setattr(self, k, v)


def test_create_channel_builds_a_text_channel():
    from core.ops import create_channel
    guild = _CrudGuild()
    ch = asyncio.run(create_channel(_AuthorCtx(), guild, "new-chan",
                                    topic="hi"))
    kind, kw = guild.create_kwargs
    assert kind == "text"
    assert kw["name"] == "new-chan"
    assert kw["topic"] == "hi"
    assert kw["reason"] == "create_channel op by tester (321)"
    payload = registry.get("create_channel").serialize_result(ch)
    assert payload["id"] == 90


def test_create_channel_refuses_unknown_kind():
    from core.ops import create_channel
    with pytest.raises(ValueError, match="kind must be"):
        asyncio.run(create_channel(_AuthorCtx(), _CrudGuild(), "x",
                                   kind="wormhole"))


class _CrudChannel:
    def __init__(self, cid=10, name="general", guild=None):
        self.id = cid
        self.name = name
        self.guild = guild
        self.type = "text"
        self.category = None
        self.deleted_reason = None
        self.clone_kwargs = None
        self.edit_kwargs = None
        self.set_perms = []

    async def delete(self, *, reason=None):
        self.deleted_reason = reason

    async def clone(self, **kw):
        self.clone_kwargs = kw
        return _CrudChannel(cid=99, name=kw.get("name", self.name))

    async def edit(self, *, reason=None, **kw):
        self.edit_kwargs = dict(kw)

    async def move(self, **kw):
        self.edit_kwargs = dict(kw)

    async def set_permissions(self, target, *, overwrite=None, reason=None):
        self.set_perms.append((target, overwrite, reason))


def test_delete_channel_deletes_with_audit_reason():
    from core.ops import delete_channel
    chan = _CrudChannel()
    payload = asyncio.run(delete_channel(_AuthorCtx(), chan))
    assert chan.deleted_reason == "delete_channel op by tester (321)"
    assert payload == {"deleted_channel_id": 10, "name": "general"}


def test_clone_channel_copies_with_optional_name():
    from core.ops import clone_channel
    chan = _CrudChannel()
    cloned = asyncio.run(clone_channel(_AuthorCtx(), chan, name="copy"))
    assert chan.clone_kwargs["name"] == "copy"
    payload = registry.get("clone_channel").serialize_result(cloned)
    assert payload["id"] == 99


def test_move_channel_edits_position_only():
    from core.ops import move_channel
    chan = _CrudChannel()
    asyncio.run(move_channel(_AuthorCtx(), chan, position=2))
    assert chan.edit_kwargs["position"] == 2


def test_move_channel_requires_a_target():
    from core.ops import move_channel
    with pytest.raises(ValueError, match="Nothing to move"):
        asyncio.run(move_channel(_AuthorCtx(), _CrudChannel()))


def test_set_channel_overwrite_writes_allow_and_deny():
    from core.ops import set_channel_overwrite
    role = type("R", (), {"id": 7, "name": "Mods"})()
    guild = _CrudGuild(roles=[role])
    chan = _CrudChannel(guild=guild)
    payload = asyncio.run(set_channel_overwrite(
        _AuthorCtx(), chan, "role", 7,
        allow=["read_messages"], deny=["send_messages"]))
    target, overwrite, reason = chan.set_perms[0]
    assert target is role
    assert overwrite.read_messages is True
    assert overwrite.send_messages is False
    assert payload["target_id"] == 7


def test_set_channel_overwrite_rejects_unknown_permission():
    from core.ops import set_channel_overwrite
    role = type("R", (), {"id": 7, "name": "Mods"})()
    guild = _CrudGuild(roles=[role])
    with pytest.raises(ValueError, match="Unknown permission"):
        asyncio.run(set_channel_overwrite(
            _AuthorCtx(), _CrudChannel(guild=guild), "role", 7,
            allow=["fly"]))


def test_delete_channel_overwrite_clears_the_entry():
    from core.ops import delete_channel_overwrite
    member = type("M", (), {"id": 42})()
    guild = _CrudGuild(members=[member])
    chan = _CrudChannel(guild=guild)
    payload = asyncio.run(delete_channel_overwrite(
        _AuthorCtx(), chan, "member", 42))
    target, overwrite, _reason = chan.set_perms[0]
    assert target is member
    assert overwrite is None
    assert payload["removed"] is True


# -- thread destructive/membership impls ------------------------------------

class _ModThread(_FakeThread):
    def __init__(self, **attrs):
        super().__init__(**attrs)
        self.deleted = False
        self.added = []
        self.removed = []
        self._archived_private = []

    async def delete(self):
        self.deleted = True

    async def add_user(self, user):
        self.added.append(user)

    async def remove_user(self, user):
        self.removed.append(user)

    def archived_threads(self, *, private=False, limit=100):
        rows = self._archived_private if private else []

        async def gen():
            for t in rows[:limit]:
                yield t
        return gen()


def test_delete_thread_deletes():
    from core.ops import delete_thread
    thread = _ModThread()
    payload = asyncio.run(delete_thread(_NoActorCtx(), thread))
    assert thread.deleted
    assert payload == {"deleted_thread_id": 111, "name": "topic-drift"}


def test_add_and_remove_thread_member():
    from core.ops import add_thread_member, remove_thread_member
    member = type("M", (), {"id": 42})()
    thread = _ModThread()
    add = asyncio.run(add_thread_member(_NoActorCtx(), thread, member))
    assert thread.added == [member]
    assert add == {"thread_id": 111, "user_id": 42, "added": True}
    rem = asyncio.run(remove_thread_member(_NoActorCtx(), thread, member))
    assert thread.removed == [member]
    assert rem["removed"] is True


def test_list_private_archived_threads_enumerates_private_only():
    from core.ops import list_private_archived_threads
    hidden = _FakeThread(id=222, name="secret")
    parent = type("P", (), {})()
    holder = _ModThread()
    holder._archived_private = [hidden]
    # A channel that parents threads: reuse the _ModThread's iterator by
    # attaching it to a plain object.
    parent.archived_threads = holder.archived_threads
    payload = asyncio.run(
        list_private_archived_threads(_NoActorCtx(), parent))
    assert payload["count"] == 1
    assert payload["threads"][0]["id"] == 222


# -- member moderation impls ------------------------------------------------

def test_kick_member_kicks_with_reason():
    from core.ops import kick_member
    guild = _CrudGuild()
    member = type("M", (), {"id": 42, "guild": guild})()
    payload = asyncio.run(kick_member(_AuthorCtx(), member, reason="spam"))
    assert guild.kicked == [(member, "spam")]
    assert payload == {"member_id": 42, "kicked": True}


def test_ban_member_passes_delete_seconds():
    from core.ops import ban_member
    guild = _CrudGuild()
    member = type("M", (), {"id": 42, "guild": guild})()
    payload = asyncio.run(ban_member(_AuthorCtx(), member,
                                     delete_message_seconds=3600))
    user, _reason, secs = guild.banned[0]
    assert user is member
    assert secs == 3600
    assert payload["deleted_message_seconds"] == 3600


def test_unban_member_unbans():
    from core.ops import unban_member
    guild = _CrudGuild()
    user = type("U", (), {"id": 42})()
    ctx = _AuthorCtx()
    ctx.guild = guild
    payload = asyncio.run(unban_member(ctx, user))
    assert guild.unbanned[0][0] is user
    assert payload == {"user_id": 42, "unbanned": True}


def test_bulk_ban_caps_and_reports_results():
    from core.ops import bulk_ban
    guild = _CrudGuild()
    ctx = _AuthorCtx()
    ctx.guild = guild
    payload = asyncio.run(bulk_ban(ctx, ["1", "2", "3"]))
    # The _CrudGuild fake bans all but the last, fails the last.
    assert payload["banned_user_ids"] == [1, 2]
    assert payload["failed_user_ids"] == [3]
    assert payload["banned_count"] == 2


def test_bulk_ban_refuses_over_200():
    from core.ops import bulk_ban
    ctx = _AuthorCtx()
    ctx.guild = _CrudGuild()
    with pytest.raises(ValueError, match="at most 200"):
        asyncio.run(bulk_ban(ctx, [str(i) for i in range(201)]))


def test_prune_members_prunes():
    from core.ops import prune_members
    guild = _CrudGuild()
    ctx = _AuthorCtx()
    ctx.guild = guild
    payload = asyncio.run(prune_members(ctx, 14))
    assert guild.pruned_kwargs["days"] == 14
    assert payload == {"days": 14, "pruned_members": 7}


def test_edit_member_roles_adds_and_removes():
    from core.ops import edit_member_roles

    class _Role:
        def __init__(self, rid):
            self.id = rid
            self.managed = False

        def is_default(self):
            return False

    r_add, r_rem = _Role(10), _Role(20)
    guild = _CrudGuild(roles=[r_add, r_rem])

    class _Member:
        id = 42
        guild = None

        def __init__(self):
            self.added = None
            self.removed = None

        async def add_roles(self, *roles, reason=None):
            self.added = list(roles)

        async def remove_roles(self, *roles, reason=None):
            self.removed = list(roles)

    member = _Member()
    member.guild = guild
    payload = asyncio.run(edit_member_roles(
        _AuthorCtx(), member, add_role_ids=["10"], remove_role_ids=["20"]))
    assert member.added == [r_add]
    assert member.removed == [r_rem]
    assert payload == {"member_id": 42, "added_role_ids": [10],
                       "removed_role_ids": [20]}


def test_edit_member_roles_refuses_a_managed_role():
    from core.ops import edit_member_roles

    class _Managed:
        id = 10
        name = "Integration"
        managed = True

        def is_default(self):
            return False

    guild = _CrudGuild(roles=[_Managed()])
    member = type("M", (), {"id": 42, "guild": guild})()
    with pytest.raises(ValueError, match="managed"):
        asyncio.run(edit_member_roles(_AuthorCtx(), member,
                                      add_role_ids=["10"]))


# -- message moderation impls -----------------------------------------------

class _ModChannel:
    def __init__(self, cid=10):
        self.id = cid
        self.deleted = None
        self.purge_kwargs = None
        self.sent = None

    async def delete_messages(self, messages, *, reason=None):
        self.deleted = [m.id for m in messages]

    async def purge(self, **kw):
        self.purge_kwargs = kw
        return [object(), object(), object()]

    async def send(self, content=None, *, tts=False, stickers=None,
                   allowed_mentions=None):
        self.sent = {"content": content, "tts": tts, "stickers": stickers}
        return type("M", (), {"id": 555, "attachments": []})()


def test_bulk_delete_messages_deletes_by_id():
    from core.ops import bulk_delete_messages
    chan = _ModChannel()
    payload = asyncio.run(bulk_delete_messages(
        _AuthorCtx(), chan, ["1", "2", "3"]))
    assert chan.deleted == [1, 2, 3]
    assert payload == {"channel_id": 10, "deleted_count": 3}


def test_bulk_delete_messages_caps_at_100():
    from core.ops import bulk_delete_messages
    with pytest.raises(ValueError, match="at most 100"):
        asyncio.run(bulk_delete_messages(
            _AuthorCtx(), _ModChannel(), [str(i) for i in range(101)]))


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


def test_publish_message_publishes():
    from core.ops import publish_message

    class _Msg:
        id = 5

        def __init__(self):
            self.published = False

        async def publish(self):
            self.published = True

    msg = _Msg()
    result = asyncio.run(publish_message(_NoActorCtx(), msg))
    assert msg.published
    payload = registry.get("publish_message").serialize_result(result)
    assert payload == {"message_id": 5, "published": True}


def test_send_tts_sends_with_tts_flag():
    from core.ops import send_tts
    chan = _ModChannel()
    asyncio.run(send_tts(_NoActorCtx(), chan, "hello aloud"))
    assert chan.sent["tts"] is True
    assert chan.sent["content"] == "hello aloud"


def test_send_tts_refuses_empty():
    from core.ops import send_tts
    with pytest.raises(ValueError, match="non-empty"):
        asyncio.run(send_tts(_NoActorCtx(), _ModChannel(), "   "))


def test_send_sticker_requires_a_guild_sticker(monkeypatch):
    import core.ops as ops_module
    from core.ops import send_sticker
    sticker = object()
    monkeypatch.setattr(ops_module, "_require_guild_sticker",
                        lambda guild, sid: sticker)
    chan = _ModChannel()
    chan.guild = type("G", (), {"id": 1})()
    asyncio.run(send_sticker(_NoActorCtx(), chan, 77))
    assert chan.sent["stickers"] == [sticker]


class _ModMessage:
    def __init__(self, mid=5):
        self.id = mid
        self.removed = []
        self.cleared_emoji = []
        self.cleared_all = False

    async def remove_reaction(self, emoji, member):
        self.removed.append((emoji, member))

    async def clear_reaction(self, emoji):
        self.cleared_emoji.append(emoji)

    async def clear_reactions(self):
        self.cleared_all = True


def test_remove_reaction_other_targets_the_named_member():
    from core.ops import remove_reaction_other
    member = type("M", (), {"id": 42})()
    msg = _ModMessage()
    payload = asyncio.run(remove_reaction_other(
        _NoActorCtx(), msg, member, "👍"))
    assert msg.removed == [("👍", member)]
    assert payload == {"message_id": 5, "user_id": 42, "removed": True}


def test_clear_reactions_one_emoji_vs_all():
    from core.ops import clear_reactions
    msg = _ModMessage()
    one = asyncio.run(clear_reactions(_NoActorCtx(), msg, emoji="👍"))
    assert msg.cleared_emoji == ["👍"]
    assert one == {"message_id": 5, "cleared_emoji": "👍"}
    msg2 = _ModMessage()
    allp = asyncio.run(clear_reactions(_NoActorCtx(), msg2))
    assert msg2.cleared_all
    assert allp == {"message_id": 5, "cleared_all": True}


# -- webhook impls ----------------------------------------------------------

class _FakeWebhook:
    def __init__(self, wid=7, name="hook", channel_id=10):
        self.id = wid
        self.name = name
        self.channel_id = channel_id
        self.edit_kwargs = None
        self.deleted_reason = None
        self.sent = None

    async def edit(self, *, reason=None, **kw):
        self.edit_kwargs = dict(kw)
        for k, v in kw.items():
            if k == "name":
                self.name = v
        return self

    async def delete(self, *, reason=None):
        self.deleted_reason = reason

    async def send(self, content, *, wait=False, username=None,
                   avatar_url=None, allowed_mentions=None):
        self.sent = {"content": content, "username": username,
                     "avatar_url": avatar_url}
        return type("M", (), {"id": 900})()


class _WebhookChannel:
    def __init__(self, cid=10, guild=None):
        self.id = cid
        self.guild = guild
        self.created = None

    async def create_webhook(self, *, name, reason=None):
        self.created = name
        return _FakeWebhook(name=name, channel_id=self.id)


class _WebhookGuild:
    def __init__(self, gid=1, webhooks=()):
        self.id = gid
        self._webhooks = list(webhooks)

    async def webhooks(self):
        return list(self._webhooks)


def test_create_webhook_never_serializes_url_or_token():
    from core.ops import create_webhook
    chan = _WebhookChannel()
    hook = asyncio.run(create_webhook(_AuthorCtx(), chan, "poster"))
    assert chan.created == "poster"
    payload = registry.get("create_webhook").serialize_result(hook)
    assert set(payload) == {"id", "name", "channel_id"}
    assert "url" not in payload and "token" not in payload


def test_edit_webhook_resolves_in_guild_and_renames():
    from core.ops import edit_webhook
    hook = _FakeWebhook(wid=7, name="old")
    guild = _WebhookGuild(webhooks=[hook])
    asyncio.run(edit_webhook(_AuthorCtx(), guild, 7, name="new"))
    assert hook.edit_kwargs["name"] == "new"


def test_edit_webhook_refuses_unknown_id():
    from core.ops import edit_webhook
    guild = _WebhookGuild(webhooks=[])
    with pytest.raises(ValueError, match="No webhook"):
        asyncio.run(edit_webhook(_AuthorCtx(), guild, 999, name="x"))


def test_delete_webhook_deletes_in_guild():
    from core.ops import delete_webhook
    hook = _FakeWebhook(wid=7, name="doomed")
    guild = _WebhookGuild(webhooks=[hook])
    payload = asyncio.run(delete_webhook(_AuthorCtx(), guild, 7))
    assert hook.deleted_reason == "delete_webhook op by tester (321)"
    assert payload == {"deleted_webhook_id": 7, "name": "doomed"}


def test_execute_webhook_posts_under_custom_identity():
    from core.ops import execute_webhook
    hook = _FakeWebhook(wid=7)
    guild = _WebhookGuild(webhooks=[hook])
    payload = asyncio.run(execute_webhook(
        _AuthorCtx(), guild, 7, "hi", username="Ghost",
        avatar_url="https://cdn/a.png"))
    assert hook.sent == {"content": "hi", "username": "Ghost",
                         "avatar_url": "https://cdn/a.png"}
    assert payload == {"webhook_id": 7, "message_id": 900}


# -- guild settings impl ----------------------------------------------------

def test_edit_guild_settings_edits_name_and_verification():
    from core.ops import edit_guild_settings
    guild = _CrudGuild(name="Old")
    payload = asyncio.run(edit_guild_settings(
        _AuthorCtx(), guild, name="New", verification_level="high"))
    assert guild.edit_kwargs["name"] == "New"
    assert (guild.edit_kwargs["verification_level"]
            is discord.VerificationLevel.high)
    assert payload["name"] == "New"


def test_edit_guild_settings_rejects_bad_verification():
    from core.ops import edit_guild_settings
    with pytest.raises(ValueError, match="verification_level"):
        asyncio.run(edit_guild_settings(_AuthorCtx(), _CrudGuild(),
                                        verification_level="ultra"))


def test_edit_guild_settings_requires_a_field():
    from core.ops import edit_guild_settings
    with pytest.raises(ValueError, match="Nothing to edit"):
        asyncio.run(edit_guild_settings(_AuthorCtx(), _CrudGuild()))


# -- automod CRUD impls -----------------------------------------------------

class _AutomodGuild:
    def __init__(self, rules=None):
        self._rules = {r.id: r for r in (rules or [])}
        self.create_kwargs = None

    async def create_automod_rule(self, **kw):
        self.create_kwargs = kw
        return type("R", (), {"id": 5, "name": kw["name"], "enabled": True})()

    async def fetch_automod_rule(self, rid):
        return self._rules[rid]


class _FakeAutomodRule:
    def __init__(self, rid=5, name="rule", trigger=None):
        self.id = rid
        self.name = name
        self.trigger = trigger
        self.edit_kwargs = None
        self.deleted_reason = None

    async def edit(self, *, reason=None, **kw):
        self.edit_kwargs = dict(kw)
        return self

    async def delete(self, *, reason=None):
        self.deleted_reason = reason


def test_create_automod_rule_builds_a_keyword_rule():
    from core.ops import create_automod_rule
    guild = _AutomodGuild()
    asyncio.run(create_automod_rule(
        _AuthorCtx(), guild, "no swears", "keyword",
        keyword_filter=["badword"]))
    kw = guild.create_kwargs
    assert kw["name"] == "no swears"
    assert kw["trigger"].keyword_filter == ["badword"]
    assert kw["event_type"] is discord.AutoModRuleEventType.message_send
    assert kw["actions"][0].type is discord.AutoModRuleActionType.block_message


def test_create_automod_rule_keyword_needs_a_filter():
    from core.ops import create_automod_rule
    with pytest.raises(ValueError, match="keyword_filter"):
        asyncio.run(create_automod_rule(
            _AuthorCtx(), _AutomodGuild(), "x", "keyword"))


def test_create_automod_rule_rejects_unknown_trigger():
    from core.ops import create_automod_rule
    with pytest.raises(ValueError, match="trigger_type"):
        asyncio.run(create_automod_rule(
            _AuthorCtx(), _AutomodGuild(), "x", "telepathy"))


def test_edit_automod_rule_replaces_keyword_filter():
    from core.ops import edit_automod_rule
    trigger = type("T", (), {
        "type": discord.AutoModRuleTriggerType.keyword,
        "keyword_filter": ["old"], "regex_patterns": []})()
    rule = _FakeAutomodRule(trigger=trigger)
    guild = _AutomodGuild(rules=[rule])
    asyncio.run(edit_automod_rule(_AuthorCtx(), guild, 5,
                                  keyword_filter=["new"]))
    assert rule.edit_kwargs["trigger"].keyword_filter == ["new"]


def test_edit_automod_rule_requires_a_field():
    from core.ops import edit_automod_rule
    rule = _FakeAutomodRule()
    guild = _AutomodGuild(rules=[rule])
    with pytest.raises(ValueError, match="Nothing to edit"):
        asyncio.run(edit_automod_rule(_AuthorCtx(), guild, 5))


def test_delete_automod_rule_deletes():
    from core.ops import delete_automod_rule
    rule = _FakeAutomodRule(name="doomed")
    guild = _AutomodGuild(rules=[rule])
    payload = asyncio.run(delete_automod_rule(_AuthorCtx(), guild, 5))
    assert rule.deleted_reason == "delete_automod_rule op by tester (321)"
    assert payload == {"deleted_rule_id": 5, "name": "doomed"}


# -- scheduled-event delete + stage lifecycle impls -------------------------

def test_delete_scheduled_event_deletes():
    from core.ops import delete_scheduled_event

    class _Event:
        id = 7
        name = "Movie night"

        def __init__(self):
            self.deleted = False

        async def delete(self):
            self.deleted = True

    event = _Event()

    class _Guild:
        async def fetch_scheduled_event(self, eid):
            assert eid == 7
            return event

    payload = asyncio.run(delete_scheduled_event(
        _NoActorCtx(), 7, guild=_Guild()))
    assert event.deleted
    assert payload == {"deleted_event_id": 7, "name": "Movie night"}


class _StageInstance:
    def __init__(self):
        self.channel_id = 40
        self.topic = "AMA"
        self.privacy_level = discord.PrivacyLevel.guild_only
        self.edit_topic = None
        self.deleted_reason = None

    async def edit(self, *, topic=None, reason=None):
        self.edit_topic = topic

    async def delete(self, *, reason=None):
        self.deleted_reason = reason


class _LiveStageChannel(_FakeStageChannelReal):
    async def create_instance(self, *, topic, send_start_notification=False,
                              reason=None):
        inst = _StageInstance()
        inst.topic = topic
        self.created = (topic, send_start_notification)
        return inst


def test_create_stage_goes_live():
    from core.ops import create_stage
    stage = _LiveStageChannel()
    inst = asyncio.run(create_stage(_AuthorCtx(), stage, "Town hall"))
    assert stage.created == ("Town hall", False)
    payload = registry.get("create_stage").serialize_result(inst)
    assert payload["topic"] == "Town hall"


def test_edit_stage_edits_the_live_instance():
    from core.ops import edit_stage
    inst = _StageInstance()
    stage = _FakeStageChannelReal(instance=inst)
    asyncio.run(edit_stage(_AuthorCtx(), stage, "New topic"))
    assert inst.edit_topic == "New topic"


def test_edit_stage_refuses_when_no_live_instance():
    from core.ops import edit_stage
    stage = _FakeStageChannelReal(instance=None)
    with pytest.raises(ValueError, match="no live stage"):
        asyncio.run(edit_stage(_AuthorCtx(), stage, "x"))


def test_end_stage_deletes_the_live_instance():
    from core.ops import end_stage
    inst = _StageInstance()
    stage = _FakeStageChannelReal(instance=inst)
    payload = asyncio.run(end_stage(_AuthorCtx(), stage))
    assert inst.deleted_reason == "end_stage op by tester (321)"
    assert payload == {"channel_id": 40, "ended": True}


# -- invite delete impl -----------------------------------------------------

def test_delete_invite_deletes_a_guild_owned_code():
    from core.ops import delete_invite

    class _Invite:
        code = "abc123"

        def __init__(self):
            self.deleted_reason = None

        async def delete(self, *, reason=None):
            self.deleted_reason = reason

    invite = _Invite()

    class _Guild:
        name = "Guild"

        async def invites(self):
            return [invite]

    payload = asyncio.run(delete_invite(_AuthorCtx(), _Guild(), "abc123"))
    assert invite.deleted_reason == "delete_invite op by tester (321)"
    assert payload == {"deleted": True, "code": "abc123"}


def test_delete_invite_refuses_a_foreign_code():
    from core.ops import delete_invite

    class _Guild:
        name = "Guild"

        async def invites(self):
            return []

    with pytest.raises(ValueError, match="No active invite"):
        asyncio.run(delete_invite(_AuthorCtx(), _Guild(), "nope"))
