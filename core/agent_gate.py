"""The two-tier agent permission model (#92 follow-up).

Governs what the in-chat AI AGENT will do on a user's behalf. Two gates sit on
top of each op's hardcoded `permission` floor:

1. **Super-admin whitelist** — global, `agent_ops_whitelist` = {op_name: bool}.
   An op absent or false is disabled *everywhere*: it never reaches a guild's
   agent tool universe and never renders in any settings panel. This is the
   context-bloat control — every exposed op costs prompt tokens, so the owner
   curates which ops the agent may ever see.

2. **Per-guild gate** — `agent_ops_gate` = {op_name: "off"|"admin"|"everyone"},
   set by a server admin. Missing entry falls back to the op's `default_gate()`
   (always `"off"` — a whitelist tick does not expose the op to members until
   a guild admin picks Admin or Everyone). "off" hides the op from that
   guild's agent and panel.

Resolution order for a guild + op: not whitelisted → OFF; else guild override if
present; else the op's default. A super-admin invoking the agent bypasses the
per-guild gate, but the universe is still GUILD-scoped — DM and GLOBAL ops
are MCP-only. SELF has a separate invoker-only policy below; its switches
and gates never have a privileged bypass.

This module is the SINGLE authority: gpt.py's tool builder, the /aisettings
panels, and the call-time check all resolve through here so the panel shows
exactly what the loop gets. It touches NO Discord objects — pure config →
decision — so it is trivially testable. The MCP surface is deliberately NOT
governed here (a host-side operator sees the full registry; see mcp_server.py).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.ops import OpScope, registry

WHITELIST_KEY = "agent_ops_whitelist"   # GLOBAL config: {op_name: bool}
GATE_KEY = "agent_ops_gate"             # per-GUILD config: {op_name: "off"|"admin"|"everyone"}

GATE_OFF = "off"
GATE_ADMIN = "admin"
GATE_EVERYONE = "everyone"
_VALID_GATES = (GATE_OFF, GATE_ADMIN, GATE_EVERYONE)


def is_whitelisted(op_name: str, whitelist: Optional[Dict[str, bool]]) -> bool:
    """An op is available to the agent tier only if the super-admin whitelist
    has it explicitly true. Absent or false → globally disabled.

    A None/empty whitelist means "nothing enabled yet" — the safe default is
    that the owner opts ops in, not that everything is live before they look.
    """
    if not whitelist:
        return False
    return bool(whitelist.get(op_name, False))


def guild_gate(op: Any, gate_cfg: Optional[Dict[str, str]]) -> str:
    """The effective Off/Admin/Everyone gate for one op in one guild, BEFORE
    the whitelist is consulted. Guild override if present and valid, else the
    op's own default."""
    if gate_cfg:
        v = gate_cfg.get(op.name)
        if v in _VALID_GATES:
            return v
    return op.default_gate()


def effective_gate(op: Any, whitelist: Optional[Dict[str, bool]],
                   gate_cfg: Optional[Dict[str, str]]) -> str:
    """Full resolution for a guild-scoped op: OFF if not whitelisted, else the
    guild gate (override or default)."""
    if not is_whitelisted(op.name, whitelist):
        return GATE_OFF
    return guild_gate(op, gate_cfg)


def agent_universe(whitelist: Optional[Dict[str, bool]],
                   gate_cfg: Optional[Dict[str, str]],
                   *, is_superadmin_actor: bool = False) -> List[str]:
    """The op names the in-chat agent may use in this guild, live from the
    registry. Always guild-scoped: DM and GLOBAL ops belong on the MCP
    surface, not in a guild-confined loop.

    - super-admin actor: every whitelisted GUILD op, ignoring the per-guild
      off/admin/everyone gate — the owner running the agent still cannot
      reach outside the guild from in-chat.
    - everyone else: whitelisted guild-scoped ops whose effective gate is not
      OFF. The per-user admin/everyone distinction is enforced at CALL time
      (call_gate below), not by hiding admin ops from the universe — the tool
      still exists; a non-admin just gets a permission error if they invoke it,
      exactly like the hardcoded ADMIN floor behaves today.
    """
    names: List[str] = []
    for op in registry.ops():
        if not is_whitelisted(op.name, whitelist):
            continue
        if op.scope != OpScope.GUILD:
            continue
        if is_superadmin_actor:
            names.append(op.name)
            continue
        if guild_gate(op, gate_cfg) == GATE_OFF:
            continue
        names.append(op.name)
    return names


def call_requires_admin(op: Any, whitelist: Optional[Dict[str, bool]],
                        gate_cfg: Optional[Dict[str, str]]) -> bool:
    """Whether an AGENT invocation of this op requires the invoking user to be
    an admin, per the guild gate. "admin" → yes; "everyone" → no (still subject
    to the op's own hardcoded floor); "off"/not-whitelisted → treated as admin
    (defensive: an un-exposed op should never be reachable by a plain user, and
    the universe filter already hides it).

    The op's hardcoded `permission` remains an independent floor enforced by
    Op.__call__ — an "everyone" gate can widen the AGENT surface down to plain
    users but never below what the op itself demands.
    """
    gate = effective_gate(op, whitelist, gate_cfg)
    return gate != GATE_EVERYONE

SELF_TOOLS_KEY = "ai_self_tools_enabled"


def self_tools_enabled(ctx) -> bool:
    """Global master defaults on; public guild participation defaults off."""
    config = ctx.bot.config
    if not config.get_global(SELF_TOOLS_KEY, True):
        return False
    guild = getattr(ctx, "guild", None)
    return guild is None or bool(config.get(guild.id, SELF_TOOLS_KEY, False))


def resolve_agent_tools(ctx, scopes=None) -> List[str]:
    """Resolve one trusted surface's tool policy, also used as its cache key."""
    from core.permissions import is_superadmin

    guild = getattr(ctx, "guild", None)
    scopes = set(scopes if scopes is not None else
                 ({OpScope.GUILD, OpScope.SELF} if guild else {OpScope.SELF}))
    config = ctx.bot.config
    wl = config.get_global(WHITELIST_KEY)
    gates = config.get(guild.id, GATE_KEY) if guild else None
    names = (agent_universe(wl, gates, is_superadmin_actor=is_superadmin(ctx))
             if guild and OpScope.GUILD in scopes else [])
    if OpScope.SELF in scopes and self_tools_enabled(ctx):
        for op in registry.ops(scope=OpScope.SELF):
            if op.self_call_error(ctx, {}) or not is_whitelisted(op.name, wl):
                continue
            if guild and guild_gate(op, gates) == GATE_OFF:
                continue
            names.append(op.name)
    return names


def self_call_allowed(ctx, op) -> bool:
    """No privileged bypass: SELF policy is always re-read at dispatch."""
    from core.permissions import is_admin
    if op.name not in resolve_agent_tools(ctx, {OpScope.SELF}):
        return False
    if getattr(ctx, "guild", None):
        gate = guild_gate(op, ctx.bot.config.get(ctx.guild.id, GATE_KEY))
        return gate == GATE_EVERYONE or (gate == GATE_ADMIN and is_admin(ctx))
    return True


def guild_call_requires_admin(ctx, name) -> bool:
    """Preserve the existing GUILD admin bypass for already-built tools."""
    from core.permissions import is_admin, is_superadmin
    if is_admin(ctx) or is_superadmin(ctx):
        return False
    op = registry.get(name)
    return op is None or call_requires_admin(
        op, ctx.bot.config.get_global(WHITELIST_KEY),
        ctx.bot.config.get(ctx, GATE_KEY))
