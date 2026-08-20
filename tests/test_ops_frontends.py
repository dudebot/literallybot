"""Frontend-facing invariants of the ops registry: panel rendering, the
stored-config/live-registry split, and MCP settings resolution.

tests/test_ops_registry.py owns the registry's own properties and
tests/test_cog_ops.py owns the shipped cogs' lifecycle. This file covers the
seams BETWEEN the registry and the things that read it, where the refactor's
decisions actually become breakable:

- Discord's 25-option select cap, asserted against every select the panel can
  really render — both tab universes, after grouping AND chunking, with cog
  ops loaded (the case that can push a group over the cap in production but
  never in a core-only test run);
- the doctrine that stored config is a WISH and the live registry is the
  AUTHORITY: a name whose cog is unloaded drops out of the effective set and
  stays in stored config, surviving the round trip;
- token/port resolution order for the MCP server (config > env > default),
  including the generate-and-persist branch.

No test here hand-maintains a list of op names. Universes are derived from
the live registry, and the synthetic cogs exist to prove behaviour for ops
that do not exist yet — which is the whole point of killing the `agent=True`
flag.
"""

import pytest

from core.ops import (
    ORIGIN_COG,
    ORIGIN_CORE,
    OP_GROUPS,
    OpScope,
    PermissionLevel,
    op,
    registry,
)
from core.agent_loop import agent_ops, resolve_bot_tools
from cogs.optional.gpt import (
    SELECT_MAX_OPTIONS,
    AiSettingsView,
    _grouped_tool_sections,
)
import core.mcp_server as mcp_server


# --------------------------------------------------------------------------
# Panel universes, derived — never a literal op list.
# --------------------------------------------------------------------------

def _sections(scope=None):
    """The exact section structure AiSettingsView builds: core primitives and
    cog-provided ops as visibly separate territory, each grouped."""
    return [
        ("**Core tools**", registry.grouped(scope=scope, origin=ORIGIN_CORE)),
        ("**Cog tools**", registry.grouped(scope=scope, origin=ORIGIN_COG)),
    ]


# The two tabs' universes: the server tab is guild-scoped only, the MCP tab is
# the whole registry. Expressed as scopes, so a new op joins the right tab by
# declaring its scope rather than by being added here.
PANEL_SCOPES = [
    pytest.param(OpScope.GUILD, id="server-tab"),
    pytest.param(None, id="mcp-tab"),
]


def _rendered_selects(scope, current=()):
    return [payload for kind, payload
            in _grouped_tool_sections(_sections(scope), list(current), None)
            if kind == "select"]


class _ManyOpCog:
    """A synthetic cog crowding ONE group, to prove the cap holds for group
    sizes the shipped registry has not reached yet. Ops are declared at class
    creation time by a loop because the point is the COUNT, not the names."""


for _i in range(SELECT_MAX_OPTIONS + 7):
    def _make(idx):
        @op(f"crowd_op_{idx:02d}", "Synthetic crowding op.",
            PermissionLevel.EVERYONE, group="messaging")
        async def _impl(self, ctx):
            return idx
        return _impl
    setattr(_ManyOpCog, f"crowd_{_i:02d}", _make(_i))


@pytest.fixture
def crowded_registry(monkeypatch):
    """The shared registry with one group deliberately pushed past the select
    cap, restored afterwards. The panel helpers read the module-level
    registry, so this must patch in place rather than pass a registry in."""
    cog = _ManyOpCog()
    registry.register_cog_ops(cog)
    try:
        yield cog
    finally:
        registry.unregister_owner(cog)


# --------------------------------------------------------------------------
# 1. The select cap, on every select the panel can actually render.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("scope", PANEL_SCOPES)
def test_every_rendered_select_fits_discords_cap(scope):
    """Discord rejects a select with >25 options at HTTP 400 when the panel
    OPENS, and discord.py does not validate it client-side — so an over-long
    select ships green and breaks in a user's face. This asserts the cap on
    what `_grouped_tool_sections` really yields, not on a flat pre-chunk
    list."""
    selects = _rendered_selects(scope)
    assert selects, "the panel rendered no selects at all"
    for sel in selects:
        assert len(sel.options) <= SELECT_MAX_OPTIONS, (
            f"a rendered select carries {len(sel.options)} options; Discord's "
            f"cap is {SELECT_MAX_OPTIONS} and discord.py will NOT catch it")


@pytest.mark.parametrize("scope", PANEL_SCOPES)
def test_oversized_group_is_chunked_not_truncated(scope, crowded_registry):
    """A group that outgrows the cap must SPLIT across selects. Truncating
    would hide ops from an allowlist editor: an op nobody can see is an op
    nobody can enable, and the cross-select merge would then drop it."""
    selects = _rendered_selects(scope)
    for sel in selects:
        assert len(sel.options) <= SELECT_MAX_OPTIONS

    rendered = [o.value for sel in selects for o in sel.options]
    expected = registry.op_names(scope=scope)
    assert sorted(rendered) == sorted(expected), \
        "chunking an oversized group dropped or duplicated ops"
    assert len(rendered) == len(set(rendered))


def test_crowding_actually_exceeded_the_cap(crowded_registry):
    """Guards the test above from silently passing on a group that never got
    big enough — if this fails, the fixture stopped exercising the split."""
    sizes = {gid: len(ops) for gid, _label, ops in registry.grouped()}
    assert max(sizes.values()) > SELECT_MAX_OPTIONS


def test_registry_has_a_plausible_floor_of_ops():
    """A floor, not an inventory: catches a registry that failed to populate
    (import error swallowed, decorator no-oping) without failing every time
    someone adds an op."""
    assert len(registry.names()) >= 26


# --------------------------------------------------------------------------
# 2. Scope doctrine at the frontend boundary.
# --------------------------------------------------------------------------

def _all_on():
    """A whitelist enabling every registered op — the ceiling wide open, so
    tests that predate the two-tier model still exercise the guild-scope
    universe rule (the whitelist ceiling has its own dedicated tests)."""
    return {n: True for n in registry.names()}


def test_guild_agent_universe_is_exactly_guild_scope_including_cog_ops(
        crowded_registry):
    """The live-query doctrine: guild-scoped ops registered by a COG join the
    agent universe automatically, with no code-level subset to update. With
    the whitelist fully open, the universe is exactly the guild-scoped set."""
    universe = set(agent_ops(_all_on(), None))
    assert universe == set(registry.op_names(scope=OpScope.GUILD))
    assert {n for n in universe if n.startswith("crowd_op_")}, \
        "cog-registered guild ops must join the agent universe live"


@pytest.mark.parametrize("scope", [OpScope.DM, OpScope.GLOBAL])
def test_non_guild_scopes_never_reach_the_guild_agent_surface(scope):
    """A guild-confined, user-actored loop must not be handed an op that
    reaches outside the guild it is confined to. Asserted by SCOPE, so a new
    DM or global op is covered the day it is written."""
    off_limits = set(registry.op_names(scope=scope))
    assert off_limits, f"no {scope.value}-scoped ops — is the assignment gone?"
    assert not off_limits & set(agent_ops(_all_on(), None))

    server_tab = {o.value for sel in _rendered_selects(OpScope.GUILD)
                  for o in sel.options}
    assert not off_limits & server_tab, \
        "a guild admin must not be able to enable an out-of-guild op"


def test_mcp_tab_universe_is_the_whole_registry():
    """The MCP frontend is a host-side operator surface (superadmin-gated),
    so unlike the server tab it spans every scope."""
    rendered = {o.value for sel in _rendered_selects(None) for o in sel.options}
    assert rendered == set(registry.names())
    assert rendered > set(agent_ops(_all_on(), None)), \
        "the MCP tab must be strictly wider than the guild agent universe"


@pytest.mark.parametrize("scope", PANEL_SCOPES)
def test_sections_split_by_origin_and_label_every_group(scope, crowded_registry):
    """Core primitives and cog ops render as separate sections, and every
    group carries a human label — an unlabelled group id would surface as a
    kebab-case slug in the placeholder."""
    kinds = list(_grouped_tool_sections(_sections(scope), [], None))
    headings = [payload for kind, payload in kinds if kind == "heading"]
    assert headings == ["**Core tools**", "**Cog tools**"]

    for gid, _label, ops in registry.grouped(scope=scope):
        assert gid in OP_GROUPS
        assert ops


# --------------------------------------------------------------------------
# 3. Stored config is a wish; the live registry is the authority.
# --------------------------------------------------------------------------

class _GhostCog:
    @op("ghost_tool", "Vanishes when its cog unloads.",
        PermissionLevel.EVERYONE, group="messaging")
    async def ghost(self, ctx):
        return "boo"


class _FakeGlobalConfig:
    """Global-scope stand-in for core.config, recording writes so a test can
    prove config was NOT rewritten."""

    def __init__(self, values=None):
        self.values = dict(values or {})
        self.writes = []

    def get_global(self, key, default=None):
        return self.values.get(key, default)

    def set_global(self, key, value):
        self.values[key] = value
        self.writes.append((key, value))


def test_unregistered_name_drops_from_effective_set_but_not_from_config():
    """The live-registry doctrine, in the two-tier model: a whitelist that
    enables a cog's op keeps that choice while the cog is unloaded, and the
    op simply isn't offered to the agent until it comes back. The whitelist
    dict is never mutated by resolution."""
    whitelist = {"search_history": True, "ghost_tool": True}

    # Cog unloaded: ghost_tool is not live, so it is not offered to the agent.
    assert resolve_bot_tools(whitelist, None) == ["search_history"]
    assert whitelist == {"search_history": True, "ghost_tool": True}, \
        "resolution must not mutate the whitelist"

    # Cog loaded: the whitelisted choice takes effect with no config write.
    cog = _GhostCog()
    registry.register_cog_ops(cog)
    try:
        assert set(resolve_bot_tools(whitelist, None)) == {"search_history", "ghost_tool"}
    finally:
        registry.unregister_owner(cog)

    # Unloaded again: back to the effective subset, whitelist still intact.
    assert resolve_bot_tools(whitelist, None) == ["search_history"]
    assert whitelist == {"search_history": True, "ghost_tool": True}


def test_mcp_effective_set_follows_the_same_rule():
    config = _FakeGlobalConfig({"mcp_tools_enabled": ["list_guilds", "ghost_tool"]})

    assert mcp_server.resolve_mcp_tools(config) == ["list_guilds"]

    cog = _GhostCog()
    registry.register_cog_ops(cog)
    try:
        assert mcp_server.resolve_mcp_tools(config) == ["list_guilds", "ghost_tool"]
    finally:
        registry.unregister_owner(cog)

    assert config.values["mcp_tools_enabled"] == ["list_guilds", "ghost_tool"]
    assert config.writes == [], "resolving an effective set must never write"


def test_a_panel_save_while_the_cog_is_unloaded_preserves_the_ghost():
    """The destructive case, now on the super-admin whitelist: a super-admin
    opens the Agent Ops tab while a cog is down and toggles something
    unrelated. The unrenderable whitelisted name must survive the save, or the
    ceiling is silently and permanently destroyed. (This property moved off
    the per-guild bot_tools_enabled list — which no longer exists — onto
    agent_ops_whitelist; the whitelist universe is the whole registry.)"""
    stored = ["ghost_tool", "search_history"]
    merged = AiSettingsView._merge_stored(
        stored, ["list_channels"], registry.names())
    assert "ghost_tool" in merged
    assert set(merged) == {"ghost_tool", "list_channels"}

    # And once the cog returns, the preserved whitelisted name resolves live
    # again for the agent (resolve_bot_tools takes the {name: True} whitelist).
    whitelist = {n: True for n in merged}
    cog = _GhostCog()
    registry.register_cog_ops(cog)
    try:
        assert "ghost_tool" in resolve_bot_tools(whitelist, None)
    finally:
        registry.unregister_owner(cog)


def test_unregister_owner_is_idempotent_against_the_shared_registry():
    """Teardown may run twice (failed setup, then eject); the second call must
    be a no-op rather than an error or a cross-owner eviction."""
    before = set(registry.names())
    cog = _GhostCog()
    registry.register_cog_ops(cog)
    assert registry.unregister_owner(cog) == ["ghost_tool"]
    assert registry.unregister_owner(cog) == []
    assert set(registry.names()) == before


def test_a_failed_batch_leaves_the_shared_registry_untouched():
    """Batch atomicity where it matters most — the real registry the running
    bot reads. A half-registered cog would leave ops with no live owner."""
    class _HalfBadCog:
        @op("good_tool", "Fine.", PermissionLevel.EVERYONE, group="messaging")
        async def good(self, ctx):
            return 1

        @op("search_history", "Collides with a core op.",
            PermissionLevel.EVERYONE, group="messaging")
        async def clash(self, ctx):
            return 2

    before = set(registry.names())
    with pytest.raises(ValueError, match="already registered"):
        registry.register_cog_ops(_HalfBadCog())
    assert set(registry.names()) == before
    assert registry.get("good_tool") is None, \
        "a rejected batch must not leave its earlier ops behind"


# --------------------------------------------------------------------------
# 3b. The two-tier agent permission panel: the Server tab's per-op gate and
#     the super-admin Agent Ops whitelist tab. These exercise the save PATHS
#     end to end against a recording config, so the panel and the resolver
#     agree on what config the model reads.
# --------------------------------------------------------------------------

class _TwoTierConfig:
    """Records guild-scoped and global config writes so the panel's save
    paths can be asserted. Guild scope is keyed by the bare guild id the
    panel passes as ctx (see AiSettingsView._cfg_ctx)."""

    def __init__(self, guild_values=None, global_values=None):
        self.guild = dict(guild_values or {})
        self.globals = dict(global_values or {})
        self.guild_writes = []
        self.global_writes = []

    def get(self, ctx, key, default=None, scope="guild"):
        if scope == "global":
            return self.globals.get(key, default)
        return self.guild.get(key, default)

    def set(self, ctx, key, value, scope="guild"):
        self.guild[key] = value
        self.guild_writes.append((key, value))

    def get_global(self, key, default=None):
        return self.globals.get(key, default)

    def set_global(self, key, value):
        self.globals[key] = value
        self.global_writes.append((key, value))


def _panel(config, *, is_super):
    """A bare AiSettingsView with just the fields its save paths touch.

    `is_super` is a LIVE property (reads is_superadmin from config), so we drive
    it by seeding the invoker into the global superadmins list rather than by
    assigning the attribute — the same path production uses, which also keeps
    the "demoted superadmin loses the tab live" behavior under test."""
    view = AiSettingsView.__new__(AiSettingsView)

    class _Bot:
        pass
    bot = _Bot()
    bot.config = config
    view.bot = bot
    view.invoker_id = 77
    view.guild = type("G", (), {"id": 4242})()
    if is_super:
        config.set_global("superadmins", [77])
    view._flash = None
    view.flash = lambda text: setattr(view, "_flash", text)

    async def fake_rerender(interaction):
        pass
    view.rerender = fake_rerender
    return view


class _OkInteraction:
    """Interaction whose response.send_message fails the test if the panel
    tries to reject the actor — used when the gate is expected to PASS."""

    class response:
        @staticmethod
        async def send_message(*a, **kw):
            raise AssertionError("permission gate rejected an allowed actor")


class _DenyRecordingInteraction:
    """Records the ephemeral rejection a gate sends when it refuses."""

    def __init__(self):
        self.denied = []
        outer = self

        class _Resp:
            @staticmethod
            async def send_message(msg, *a, **kw):
                outer.denied.append(msg)
        self.response = _Resp()


def _patch_gates(monkeypatch, *, admin, superadmin):
    import cogs.optional.gpt as gpt_mod
    monkeypatch.setattr(gpt_mod, "is_admin", lambda *a, **k: admin)
    # is_superadmin is called both as (ctx) and (config, user_id) in the cog —
    # accept any args so both the panel's live is_super property and the gate
    # helpers resolve to the intended value.
    monkeypatch.setattr(gpt_mod, "is_superadmin", lambda *a, **k: superadmin)


def test_server_gate_roundtrips_through_guild_config(monkeypatch):
    """Setting a whitelisted op's gate to off/admin/everyone on the Server tab
    writes agent_ops_gate for THIS guild, and each value survives the round
    trip back through the gate resolver the agent loop uses."""
    from core.agent_gate import GATE_KEY, guild_gate

    # search_history is a real registered guild-scoped op; whitelist it so the
    # Server tab will accept a gate write for it.
    config = _TwoTierConfig(global_values={"agent_ops_whitelist": {"search_history": True}})
    view = _panel(config, is_super=False)
    _patch_gates(monkeypatch, admin=True, superadmin=False)

    op = registry.require("search_history")
    import asyncio
    for state in ("off", "admin", "everyone"):
        asyncio.run(view._save_op_gate(_OkInteraction(), "search_history", state))
        stored = config.guild[GATE_KEY]
        assert stored["search_history"] == state
        # The resolver the loop consults reads back the same decision.
        assert guild_gate(op, stored) == state


def test_server_gate_refuses_a_non_whitelisted_op(monkeypatch):
    """The Server tab must never write a gate for an op the super-admin
    whitelist does not enable — that op is disabled everywhere and should not
    be reachable even from a stale panel."""
    from core.agent_gate import GATE_KEY

    config = _TwoTierConfig(global_values={"agent_ops_whitelist": {}})
    view = _panel(config, is_super=False)
    _patch_gates(monkeypatch, admin=True, superadmin=False)

    import asyncio
    asyncio.run(view._save_op_gate(_OkInteraction(), "search_history", "everyone"))
    assert GATE_KEY not in config.guild, "no gate should be written"
    assert "not an enabled agent op" in (view._flash or "")


def test_server_gate_requires_admin(monkeypatch):
    """A non-admin cannot write the guild's agent gate."""
    from core.agent_gate import GATE_KEY

    config = _TwoTierConfig(global_values={"agent_ops_whitelist": {"search_history": True}})
    view = _panel(config, is_super=False)
    _patch_gates(monkeypatch, admin=False, superadmin=False)

    interaction = _DenyRecordingInteraction()
    import asyncio
    asyncio.run(view._save_op_gate(interaction, "search_history", "everyone"))
    assert GATE_KEY not in config.guild
    assert interaction.denied and "admin" in interaction.denied[0].lower()


def test_whitelist_tab_writes_global_config(monkeypatch):
    """The super-admin Agent Ops tab writes agent_ops_whitelist GLOBALLY, as a
    {name: True} map, and a saved op then resolves live for the agent."""
    from core.agent_gate import WHITELIST_KEY

    config = _TwoTierConfig()
    view = _panel(config, is_super=True)
    _patch_gates(monkeypatch, admin=True, superadmin=True)

    import asyncio
    # Save with search_history selected; universe is the whole registry.
    asyncio.run(view._save_whitelist(
        _OkInteraction(), ["search_history"], registry.names()))
    assert config.globals[WHITELIST_KEY] == {"search_history": True}
    assert ("agent_ops_whitelist", {"search_history": True}) in config.global_writes
    # The loop's resolver now offers it.
    assert "search_history" in resolve_bot_tools(config.globals[WHITELIST_KEY], None)


def test_whitelist_save_preserves_offline_names(monkeypatch):
    """A whitelisted op whose cog is unloaded stays whitelisted across a save
    made while it is invisible to every select — the ceiling is not silently
    destroyed. (The ghost invariant, exercised through the real save path.)"""
    from core.agent_gate import WHITELIST_KEY

    config = _TwoTierConfig(
        global_values={WHITELIST_KEY: {"search_history": True, "ghost_tool": True}})
    view = _panel(config, is_super=True)
    _patch_gates(monkeypatch, admin=True, superadmin=True)

    import asyncio
    # The panel can only render live ops; it re-selects the live one and the
    # offline ghost_tool is invisible — it must still survive.
    asyncio.run(view._save_whitelist(
        _OkInteraction(), ["search_history"], registry.names()))
    assert config.globals[WHITELIST_KEY] == {
        "search_history": True, "ghost_tool": True}


def test_whitelist_tab_requires_superadmin(monkeypatch):
    """A non-superadmin (even a guild admin) cannot save the global whitelist.
    The tab is also omitted from their panel render — asserted separately via
    is_super gating in _build — but the save path must fail closed regardless
    of what a crafted interaction claims."""
    from core.agent_gate import WHITELIST_KEY

    config = _TwoTierConfig()
    view = _panel(config, is_super=False)
    _patch_gates(monkeypatch, admin=True, superadmin=False)

    interaction = _DenyRecordingInteraction()
    import asyncio
    asyncio.run(view._save_whitelist(
        interaction, ["search_history"], registry.names()))
    assert WHITELIST_KEY not in config.globals
    assert interaction.denied and "superadmin" in interaction.denied[0].lower()


def test_non_superadmin_panel_omits_the_agent_ops_tab(monkeypatch):
    """The Agent Ops (whitelist) tab is a global-config surface: a guild admin
    who is not a bot super-admin must not even see it, and navigating to it
    (e.g. a stale button) redirects to the Server tab rather than rendering
    the whitelist editor. Driven through the real _build so the redirect guard
    and the tab-bar construction are both exercised."""
    import discord
    _patch_gates(monkeypatch, admin=True, superadmin=False)

    config = _TwoTierConfig(
        global_values={"agent_ops_whitelist": {"search_history": True}})
    view = _build_server_view(monkeypatch, config, is_super=False, page="agentops")
    # A non-super was redirected off the whitelist page.
    assert view.page == "server"
    # No tab button advertises the whitelist editor.
    labels = _tab_button_labels(view)
    assert labels, "tab bar rendered no buttons"
    assert not any("Agent Ops" in l for l in labels)


def test_superadmin_panel_shows_the_agent_ops_tab(monkeypatch):
    """The counterpart: a bot super-admin DOES get the Agent Ops tab button."""
    _patch_gates(monkeypatch, admin=True, superadmin=True)
    config = _TwoTierConfig()
    view = _build_server_view(monkeypatch, config, is_super=True, page="server")
    labels = _tab_button_labels(view)
    assert any("Agent Ops" in l for l in labels)


def _build_server_view(monkeypatch, config, *, is_super, page):
    """Construct an AiSettingsView far enough to run _build() for the tab-bar
    assertions, without a live gpt cog. LayoutView.__init__ needs a running
    loop, so the init + build run inside asyncio.run."""
    import asyncio
    import discord

    async def _make():
        view = _panel(config, is_super=is_super)
        discord.ui.LayoutView.__init__(view, timeout=1)
        view.page = page
        view.provider = "openai"
        view.model = "gpt"
        view._whitelist = lambda: config.globals.get("agent_ops_whitelist", {})
        view._gate_cfg = lambda: {}
        view._bot_tools = lambda: []
        view._whitelist_names = lambda: [
            n for n, on in view._whitelist().items() if on]
        monkeypatch.setattr(AiSettingsView, "_text", lambda self: "text")
        monkeypatch.setattr(AiSettingsView, "_add_gate_sections", lambda self: None)
        monkeypatch.setattr(
            AiSettingsView, "_add_tool_sections",
            lambda self, *a, **k: None)
        stub = lambda *a, **k: discord.ui.Button(label="x")
        import cogs.optional.gpt as gpt_mod
        monkeypatch.setattr(gpt_mod, "_ProviderSelect", stub)
        monkeypatch.setattr(gpt_mod, "_ModelSelect", stub)
        monkeypatch.setattr(AiSettingsView, "_ai_toggle_button", stub)
        monkeypatch.setattr(AiSettingsView, "_personality_button", stub)
        monkeypatch.setattr(AiSettingsView, "_nickname_button", stub)
        monkeypatch.setattr(AiSettingsView, "_preset_button", stub)
        view._build()
        return view

    return asyncio.run(_make())


def _tab_button_labels(view):
    import discord
    labels = []
    for child in view.children:
        if isinstance(child, discord.ui.ActionRow):
            for item in child.children:
                if isinstance(item, discord.ui.Button) and item.label:
                    labels.append(item.label)
    return labels


# The `agent=True` flag that this seed snapshots, read off the pre-refactor
# registry at 58526e2 (the commit before the flag was deleted). Written here
# as the historical FACT it is, so the assertions below can state how the
# shipped seed relates to it rather than restating the seed itself.
# --------------------------------------------------------------------------
# 4. MCP settings: config first, env fallback, generate as a last resort.
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_mcp_env(monkeypatch):
    """A real MCP_OPS_* export on a dev box would mask the branch under
    test, so every test in this module starts from an unset environment."""
    for var in (mcp_server.TOKEN_ENV_VAR, mcp_server.PORT_ENV_VAR,
                mcp_server.HOST_ENV_VAR):
        monkeypatch.delenv(var, raising=False)


def test_token_prefers_config_over_env(monkeypatch):
    monkeypatch.setenv(mcp_server.TOKEN_ENV_VAR, "from-env")
    config = _FakeGlobalConfig({mcp_server.TOKEN_CONFIG_KEY: "from-config"})
    assert mcp_server.load_token(config) == "from-config"
    assert config.writes == [], "a configured token must not be rewritten"


def test_token_falls_back_to_env_without_persisting(monkeypatch):
    """Env stays authoritative for env-configured deployments: copying it into
    config would fork the source of truth on the next env change."""
    monkeypatch.setenv(mcp_server.TOKEN_ENV_VAR, "from-env")
    config = _FakeGlobalConfig()
    assert mcp_server.load_token(config) == "from-env"
    assert config.writes == []


def test_token_is_generated_and_persisted_when_nowhere_configured():
    """An operator who flipped mcp_ops_enabled has no UI to set a secret, so
    the server generates one rather than stranding them — still fail-closed,
    since the server is never unauthenticated."""
    config = _FakeGlobalConfig()
    token = mcp_server.load_token(config)
    assert len(token) >= 32
    assert config.values[mcp_server.TOKEN_CONFIG_KEY] == token
    assert config.writes == [(mcp_server.TOKEN_CONFIG_KEY, token)]
    # Stable across reads: a fresh token each call would lock out live clients.
    assert mcp_server.load_token(config) == token


def test_generated_tokens_are_unique_per_deployment():
    first = _FakeGlobalConfig()
    second = _FakeGlobalConfig()
    assert mcp_server.load_token(first) != mcp_server.load_token(second)


def test_a_blank_configured_token_does_not_count_as_configured(monkeypatch):
    """Whitespace in configs/global.json must not produce an empty bearer
    token that authenticates nothing."""
    monkeypatch.setenv(mcp_server.TOKEN_ENV_VAR, "from-env")
    config = _FakeGlobalConfig({mcp_server.TOKEN_CONFIG_KEY: "   "})
    assert mcp_server.load_token(config) == "from-env"


def test_the_token_value_is_never_logged(caplog):
    config = _FakeGlobalConfig()
    with caplog.at_level("WARNING"):
        token = mcp_server.load_token(config)
    assert caplog.text, "generating a token must say so"
    assert token not in caplog.text, "the token value must never be logged"
    assert mcp_server.TOKEN_CONFIG_KEY in caplog.text, \
        "the log must say where to find the token"


def test_port_resolution_order(monkeypatch):
    monkeypatch.setenv(mcp_server.PORT_ENV_VAR, "9000")
    assert mcp_server.load_port(
        _FakeGlobalConfig({mcp_server.PORT_CONFIG_KEY: 9100})) == 9100
    assert mcp_server.load_port(_FakeGlobalConfig()) == 9000
    monkeypatch.delenv(mcp_server.PORT_ENV_VAR)
    assert mcp_server.load_port(_FakeGlobalConfig()) == mcp_server.DEFAULT_PORT


def test_port_accepts_a_numeric_string_from_either_source(monkeypatch):
    """JSON config may hold "8770" as a string and env is always a string."""
    assert mcp_server.load_port(
        _FakeGlobalConfig({mcp_server.PORT_CONFIG_KEY: "8770"})) == 8770
    monkeypatch.setenv(mcp_server.PORT_ENV_VAR, "8771")
    assert mcp_server.load_port(_FakeGlobalConfig()) == 8771


@pytest.mark.parametrize("bad", ["not-a-port", 0, 70000, -1])
def test_port_fails_closed_on_an_unusable_value(bad):
    """Refuse to start rather than bind somewhere surprising."""
    with pytest.raises(RuntimeError):
        mcp_server.load_port(_FakeGlobalConfig({mcp_server.PORT_CONFIG_KEY: bad}))


def test_blank_config_port_falls_through_to_env(monkeypatch):
    monkeypatch.setenv(mcp_server.PORT_ENV_VAR, "8772")
    assert mcp_server.load_port(
        _FakeGlobalConfig({mcp_server.PORT_CONFIG_KEY: "  "})) == 8772


def test_enabled_flag_is_config_only_and_defaults_off():
    """No env var may turn the server on — the switch lives in global config
    so it is operable from the panel and auditable in one place."""
    assert mcp_server.is_enabled(_FakeGlobalConfig()) is False
    assert mcp_server.is_enabled(
        _FakeGlobalConfig({mcp_server.ENABLE_CONFIG_KEY: True})) is True


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", ""])
def test_loopback_host_env_is_accepted(monkeypatch, host):
    monkeypatch.setenv(mcp_server.HOST_ENV_VAR, host)
    mcp_server._check_host_env()


def test_non_loopback_host_env_is_refused(monkeypatch):
    """The legacy var no longer rebinds anything. Refusing loudly beats
    letting an operator believe they exposed the server deliberately."""
    monkeypatch.setenv(mcp_server.HOST_ENV_VAR, "0.0.0.0")
    with pytest.raises(RuntimeError, match="loopback"):
        mcp_server._check_host_env()


def test_settings_load_end_to_end_with_an_empty_config(monkeypatch):
    monkeypatch.setenv(mcp_server.PORT_ENV_VAR, "8999")
    config = _FakeGlobalConfig()
    token, port = mcp_server._load_settings(config)
    assert port == 8999
    assert token == config.values[mcp_server.TOKEN_CONFIG_KEY]


def test_settings_load_refuses_before_generating_a_token(monkeypatch):
    """Host validation runs FIRST: a refused start must not leave a freshly
    generated secret written to config as a side effect."""
    monkeypatch.setenv(mcp_server.HOST_ENV_VAR, "0.0.0.0")
    config = _FakeGlobalConfig()
    with pytest.raises(RuntimeError, match="loopback"):
        mcp_server._load_settings(config)
    assert config.writes == []


# --------------------------------------------------------------------------
# 5. Op-identity races (Codex review, 2026-08-11): a tool built from one Op
#    object must fail CLOSED when that name is later re-registered against a
#    different Op, and a panel rendered before a cog registers must not
#    delete stored names on save.
#
#    Since #86 the cog set is fixed at boot, so nothing in production
#    re-registers a name mid-run. These stay as the regression lock on the
#    fail-closed belt itself: name dispatch is only sound because identity
#    is re-checked, and that must survive any future reintroduction of
#    dynamism.
# --------------------------------------------------------------------------

import asyncio as _asyncio
import logging as _logging

from core.agent_loop import _make_agent_tool
from core.ops import Op as _Op
from cogs.optional.gpt import _ToolSelect


class _RetargetCogV1:
    @op("retarget_probe", "First registration.", PermissionLevel.EVERYONE,
        group="messaging")
    async def probe(self, ctx):
        return "v1"


class _RetargetCogV2:
    @op("retarget_probe", "Same name, different op object.",
        PermissionLevel.EVERYONE, group="messaging")
    async def probe(self, ctx):
        return "v2"


class _FakeCtx:
    class _Author:
        id = 1

    class _Guild:
        id = 1

    author = _Author()
    guild = _Guild()


def test_agent_tool_refuses_when_its_op_is_reregistered_mid_run():
    """The tool closure captures the Op (schema, serializer, SCOPE) but
    dispatches by name. Re-registering that name must refuse the stale tool
    rather than silently retarget it — the replacement could declare a
    different scope than the one the guild agent's universe was built
    from."""
    v1, v2 = _RetargetCogV1(), _RetargetCogV2()
    registry.register_cog_ops(v1)
    try:
        tool_fn = None
        captured = registry.require("retarget_probe")
        # Build the tool from the original op, as build_agent_tools would.
        from core import agent_loop as _al
        budget = {"used": 0, "cap": 8}
        tool = _make_agent_tool(captured, _FakeCtx(), frozenset({1}),
                                _logging.getLogger("test"), budget)
        tool_fn = tool.function

        # Same name, new op object.
        registry.unregister_owner(v1)
        registry.register_cog_ops(v2)
        try:
            result = _asyncio.run(tool_fn())
            assert result["ok"] is False
            assert "changed" in result["error"]
        finally:
            registry.unregister_owner(v2)
    finally:
        registry.unregister_owner(v1)
        registry.unregister_owner(v2)


def test_agent_gate_is_evaluated_live_at_dispatch_not_snapshotted():
    """Codex review 2026-08-20: an admin tightening an op to 'admin only'
    mid-run must bind on the NEXT call. The gate is a callback re-read at
    dispatch, not a boolean captured at build time — so flipping the policy
    between calls changes the outcome for the same non-admin invoker."""
    v1 = _RetargetCogV1()
    registry.register_cog_ops(v1)
    try:
        captured = registry.require("retarget_probe")
        # A mutable policy the gate_check closes over — stands in for live config.
        policy = {"admin_only": False}
        def gate_check(_op_name):
            return policy["admin_only"]   # non-admin invoker; admin-only ⇒ refuse

        budget = {"used": 0, "cap": 8}
        tool = _make_agent_tool(captured, _FakeCtx(), frozenset({1}),
                                _logging.getLogger("test"), budget,
                                gate_check=gate_check)
        tool_fn = tool.function

        # The gate is consulted FRESH on every dispatch: flip the policy to
        # "admin only" and the call is refused before any budget is spent or
        # any Discord work happens — the refusal short-circuits at the top of
        # tool_fn. (A snapshotted boolean would have frozen the build-time
        # value and let this through.)
        policy["admin_only"] = True
        refused = _asyncio.run(tool_fn())
        assert refused["ok"] is False
        assert "admin-only" in refused["error"].lower()
        assert budget["used"] == 0, \
            "a gate refusal must short-circuit before spending budget"

        # And with the policy relaxed, the SAME tool no longer refuses AT THE
        # GATE — it proceeds past the gate into dispatch (which then fails on
        # this bare fake ctx, proving only that the gate let it through). A
        # snapshotted boolean would have frozen the tightened value and kept
        # refusing here.
        policy["admin_only"] = False
        with pytest.raises(AttributeError):
            _asyncio.run(tool_fn())   # got past the gate, died in real dispatch
    finally:
        registry.unregister_owner(v1)


def test_mcp_tool_refuses_when_its_op_is_reregistered_after_build():
    """Same race on the MCP side: the surface is restart-bound, so a stale
    tool must raise loudly instead of serving the old schema against the new
    op's implementation."""
    v1, v2 = _RetargetCogV1(), _RetargetCogV2()
    registry.register_cog_ops(v1)
    try:
        captured = registry.require("retarget_probe")
        tool_fn = mcp_server._make_mcp_tool(None, captured)

        registry.unregister_owner(v1)
        registry.register_cog_ops(v2)
        try:
            with pytest.raises(mcp_server.BotUnavailableError,
                               match="re-registered"):
                _asyncio.run(tool_fn(actor_id="1"))
        finally:
            registry.unregister_owner(v2)
    finally:
        registry.unregister_owner(v1)
        registry.unregister_owner(v2)


def test_select_hands_the_saver_its_render_time_universe():
    """The stale-panel save race: a name whose cog loads AFTER the panel
    rendered is live at save time but was never rendered, so merging against
    the LIVE universe would delete it from stored config. The select must
    hand the saver the universe it was BUILT with; _merge_stored then
    preserves the newly-live name as an offline one."""
    seen = {}

    async def spy_on_save(interaction, selected, universe):
        seen["selected"] = selected
        seen["universe"] = universe

    select = _ToolSelect(["a", "b"], ["b", "c"], spy_on_save,
                         universe=["a", "b", "c"])
    select._values = ["a"]  # what the user picked in THIS select
    _asyncio.run(select.callback(interaction=None))

    assert seen["universe"] == ["a", "b", "c"], \
        "saver must receive the render-time capture, not query live"
    # 'c' (enabled in another select of this panel) is carried through.
    assert set(seen["selected"]) == {"a", "c"}

    # End to end through the merge: ghost_tool's cog loaded after render, so
    # it is OUTSIDE the captured universe and must survive even though a
    # live-universe merge would classify it as selectable-but-unselected.
    stored = ["ghost_tool", "b"]
    merged = AiSettingsView._merge_stored(stored, seen["selected"],
                                          seen["universe"])
    assert "ghost_tool" in merged
    assert set(merged) == {"ghost_tool", "a", "c"}


def test_mcp_tool_refuses_a_swap_during_context_resolution(monkeypatch):
    """The TOCTOU variant: the entry check passes, then the re-registration
    lands WHILE resolve_context_guild is awaited. The pre-dispatch re-check
    must catch it — the entry check alone cannot."""
    v1, v2 = _RetargetCogV1(), _RetargetCogV2()
    registry.register_cog_ops(v1)
    try:
        captured = registry.require("retarget_probe")

        class _FakeUser:
            id = 1

        class _FakeBot:
            user = _FakeUser()

            def get_guild(self, gid):
                return None

        async def swap_during_resolution(bot, raw, guild_id):
            # The re-registration interleaves exactly here, mid-await.
            registry.unregister_owner(v1)
            registry.register_cog_ops(v2)
            return None

        monkeypatch.setattr(mcp_server, "resolve_context_guild",
                            swap_during_resolution)
        tool_fn = mcp_server._make_mcp_tool(_FakeBot(), captured)
        try:
            with pytest.raises(mcp_server.BotUnavailableError,
                               match="re-registered"):
                _asyncio.run(tool_fn(actor_id="1"))
        finally:
            registry.unregister_owner(v2)
    finally:
        registry.unregister_owner(v1)
        registry.unregister_owner(v2)
