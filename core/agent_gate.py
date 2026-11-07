"""Configured in-chat tool access, shared by settings and dispatch.

The global whitelist selects available capabilities. Each guild's saved
Off/Admin/Everyone choice authorizes calls; absent or invalid settings mean
Off. Op declarations carry no additional permission tier. Privileged actors
follow the same configured Off state. MCP has its own authenticated operator
surface and enabled-tool list.
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
                   gate_cfg: Optional[Dict[str, str]]) -> List[str]:
    """Offer configured guild ops; enforce Admin/Everyone at dispatch."""
    return [op.name for op in registry.ops(scope=OpScope.GUILD)
            if effective_gate(op, whitelist, gate_cfg) != GATE_OFF]


SELF_TOOLS_KEY = "ai_self_tools_enabled"


def self_tools_enabled(ctx) -> bool:
    """Bot-wide master switch for SELF tools; absent means on. In a guild each
    SELF op still needs its per-op gate, which defaults off."""
    return bool(ctx.bot.config.get_global(SELF_TOOLS_KEY, True))


def resolve_agent_tools(ctx, scopes=None) -> List[str]:
    """Resolve one trusted surface's tool policy, also used as its cache key."""
    guild = getattr(ctx, "guild", None)
    scopes = set(scopes if scopes is not None else
                 ({OpScope.GUILD, OpScope.SELF} if guild else {OpScope.SELF}))
    config = ctx.bot.config
    wl = config.get_global(WHITELIST_KEY)
    gates = config.get(guild.id, GATE_KEY) if guild else None
    names = (agent_universe(wl, gates)
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


def guild_call_denied(ctx, name) -> bool:
    """Re-read whitelist and guild access before resolving any Discord IDs."""
    from core.permissions import is_admin
    op = registry.get(name)
    if op is None or op.scope != OpScope.GUILD or not getattr(ctx, "guild", None):
        return True
    config = ctx.bot.config
    gate = effective_gate(op, config.get_global(WHITELIST_KEY),
                          config.get(ctx.guild.id, GATE_KEY))
    return not (gate == GATE_EVERYONE or (gate == GATE_ADMIN and is_admin(ctx)))
