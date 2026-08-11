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
    AGENT_OPS_DEFAULT_ON,
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

def test_guild_agent_universe_is_exactly_guild_scope_including_cog_ops(
        crowded_registry):
    """The live-query doctrine: guild-scoped ops registered by a COG join the
    agent universe automatically, with no code-level subset to update."""
    universe = set(agent_ops())
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
    assert not off_limits & set(agent_ops())

    server_tab = {o.value for sel in _rendered_selects(OpScope.GUILD)
                  for o in sel.options}
    assert not off_limits & server_tab, \
        "a guild admin must not be able to enable an out-of-guild op"


def test_mcp_tab_universe_is_the_whole_registry():
    """The MCP frontend is a host-side operator surface (superadmin-gated),
    so unlike the server tab it spans every scope."""
    rendered = {o.value for sel in _rendered_selects(None) for o in sel.options}
    assert rendered == set(registry.names())
    assert rendered > set(agent_ops()), \
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
    """The whole point of resolving through the live registry: a guild that
    enabled a cog's op keeps that choice in config while the cog is unloaded,
    and simply doesn't get the tool until it comes back."""
    stored = ["search_history", "ghost_tool"]

    # Cog unloaded: ghost_tool is not live, so it is not offered to the agent.
    assert resolve_bot_tools(stored) == ["search_history"]
    assert stored == ["search_history", "ghost_tool"], \
        "resolution must not mutate the stored list"

    # Cog loaded: the stored choice takes effect with no config write.
    cog = _GhostCog()
    registry.register_cog_ops(cog)
    try:
        assert resolve_bot_tools(stored) == ["search_history", "ghost_tool"]
    finally:
        registry.unregister_owner(cog)

    # Unloaded again: back to the effective subset, config still intact.
    assert resolve_bot_tools(stored) == ["search_history"]
    assert stored == ["search_history", "ghost_tool"]


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
    """The destructive case: an admin opens the panel while a cog is down and
    toggles something unrelated. The unrenderable name must survive, or the
    guild's choice is silently and permanently destroyed."""
    stored = ["ghost_tool", "search_history"]
    merged = AiSettingsView._merge_stored(stored, ["list_channels"], agent_ops())
    assert "ghost_tool" in merged
    assert set(merged) == {"ghost_tool", "list_channels"}

    # And once the cog returns, the preserved name resolves live again.
    cog = _GhostCog()
    registry.register_cog_ops(cog)
    try:
        assert "ghost_tool" in resolve_bot_tools(merged)
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


# The `agent=True` flag that this seed snapshots, read off the pre-refactor
# registry at 58526e2 (the commit before the flag was deleted). Written here
# as the historical FACT it is, so the assertions below can state how the
# shipped seed relates to it rather than restating the seed itself.
HISTORICAL_AGENT_OPS = frozenset({
    "add_reaction", "delete_message", "edit_message", "list_channels",
    "list_members", "remove_reaction", "search_history", "send_message",
})


def test_migration_seed_is_a_frozen_historical_snapshot():
    """AGENT_OPS_DEFAULT_ON migrates pre-panel guilds to an explicit
    allowlist. It must stay a literal: recomputing it from the live registry
    would retroactively change what past guilds were migrated to, and every
    op added later would silently become default-on."""
    assert isinstance(AGENT_OPS_DEFAULT_ON, tuple)
    # No duplicates — a repeated name would double-write the migrated list.
    assert len(set(AGENT_OPS_DEFAULT_ON)) == len(AGENT_OPS_DEFAULT_ON)
    # The snapshot is historical, but it may not name an op that never
    # existed — that would migrate guilds to a permanently dead entry.
    assert set(AGENT_OPS_DEFAULT_ON) <= set(registry.names())
    assert set(AGENT_OPS_DEFAULT_ON) <= set(agent_ops()), \
        "every migrated name must be guild-scoped"


def test_migration_seed_is_the_historical_set_minus_send_message():
    """The seed is DELIBERATELY narrower than the old `agent=True` set: it
    withholds send_message, which can post into arbitrary channels and was
    never default-on for the in-bot agent (see the comment above the literal
    in gpt.py). Pinned because the difference is a safety decision — a future
    edit "restoring the historical set" would silently grant every migrated
    guild the ability to post anywhere."""
    assert set(AGENT_OPS_DEFAULT_ON) == HISTORICAL_AGENT_OPS - {"send_message"}
    assert "send_message" not in AGENT_OPS_DEFAULT_ON


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
