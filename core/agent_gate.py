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
   ("would a regular member reasonably ask for this?"). "off" hides the op from
   that guild's agent and panel.

Resolution order for a guild + op: not whitelisted → OFF; else guild override if
present; else the op's default. A super-admin invoking the agent bypasses the
per-guild gate entirely (they get the whole whitelisted universe, including
non-guild scopes).

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
    """The op names the agent may use in this guild, live from the registry.

    - super-admin actor: every whitelisted op of ANY scope (incl. DM/GLOBAL
      cross-guild ops) — the owner running the agent is the host operator.
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
        if is_superadmin_actor:
            names.append(op.name)
            continue
        if op.scope != OpScope.GUILD:
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
