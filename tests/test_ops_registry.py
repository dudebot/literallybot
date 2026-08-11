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
    for name in ("send_dm", "read_dms", "fetch_dms", "list_guilds"):
        assert name not in universe


def test_scope_assignments():
    assert {o.name for o in registry.ops(scope=OpScope.DM)} == {
        "send_dm", "read_dms", "fetch_dms"}
    assert {o.name for o in registry.ops(scope=OpScope.GLOBAL)} == {"list_guilds"}


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
        "channel_id", "content", "reference_message_id", "file_paths"}
    # content is optional: a message may be attachment-only.
    assert send["required"] == ["channel_id"]
    assert send["properties"]["file_paths"]["type"] == "array"


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


@pytest.mark.parametrize("name", ["send_dm", "read_dms", "fetch_dms"])
def test_dm_ops_take_a_user_id_and_nothing_guild_shaped(name):
    """DM ops resolve a guild-independent USER, one-to-one with the DM API.
    A raw DM channel id is never accepted, and the removed guild_id must not
    creep back in (see the DM section comment in core/ops.py)."""
    schema = registry.get(name).to_json_schema()
    assert "user_id" in schema["properties"]
    assert schema["properties"]["user_id"]["type"] == "string"
    assert "channel_id" not in schema["properties"]
    assert "guild_id" not in schema["properties"]
    assert set(schema["required"]) == {"user_id"}


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


def test_reload_cycle_restores_the_batch(reg):
    """discord.py's extension reload is atomic: a failed reload restores the
    old cog, which must re-register its old batch cleanly."""
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


def test_server_tab_universe_is_exactly_guild_scope():
    """The server tab is the in-guild agent surface: guild-scoped ops only.
    A DM or GLOBAL op leaking in would let a guild admin enable an op that
    reaches outside their guild."""
    from cogs.optional.gpt import _grouped_tool_sections

    rendered = {
        o.value
        for kind, payload in _grouped_tool_sections(
            _panel_sections(OpScope.GUILD), [], None)
        if kind == "select"
        for o in payload.options
    }
    assert rendered == set(agent_ops())
    assert not rendered & set(registry.op_names(scope=OpScope.DM))
    assert not rendered & set(registry.op_names(scope=OpScope.GLOBAL))


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
    """A name stored while its cog was loaded must survive a panel save made
    while that cog is unloaded — the select cannot render it, so a naive
    filter would silently and permanently destroy the guild's choice."""
    from cogs.optional.gpt import AiSettingsView

    merged = AiSettingsView._merge_stored(
        ["ghost_op", "search_history"], ["list_channels"], agent_ops())
    assert "ghost_op" in merged
    assert merged.count("ghost_op") == 1
    assert set(merged) == {"ghost_op", "list_channels"}

    # An explicit "clear all" still persists as empty (not "unset") — but only
    # for names the universe can render. An OFFLINE name survives a merge of
    # [], which is exactly why the panel's "Clear all" button must not route
    # through _merge_stored (see the test below).
    assert AiSettingsView._merge_stored(
        ["search_history"], [], agent_ops()) == []
    assert AiSettingsView._merge_stored(
        ["ghost_op"], [], agent_ops()) == ["ghost_op"]


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
