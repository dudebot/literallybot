"""In-bot agent-loop frontend over the ops registry ("world pattern").

The second frontend next to core/mcp_server.py: generates pydantic-ai
`Tool`s mechanically from the registry's typed op declarations so a
tool-calling model invoked via `!gpt` can ACTUALLY perform Discord actions
instead of narrating them. All resolution/permission/serialization comes
from core/ops.py; the only policy here is the loop's:

- ACTOR: the invoking user's Member (the real commands.Context passes
  straight through as the OpContext) — never the bot, never guild.me.
  Ops the invoking user can't pass permission gates for fail closed and
  the gate's error is returned INTO the loop as a tool error.
- IN-GUILD CONFINEMENT: allowed_guild_ids is exactly {ctx.guild.id};
  id-resolved targets outside the invoking guild are refused.
- send_message always uses allowed_mentions=none.
- Every executed op is logged at INFO (op, params, actor, ok/error).

Wired up by cogs/optional/gpt.py when a guild's resolved agent universe (the
super-admin `agent_ops_whitelist` ceiling narrowed by the per-guild
`agent_ops_gate`, see core/agent_gate) is non-empty; the empty default routes
to plain chat, and nothing imports this module otherwise.
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional

from pydantic_ai import Tool

from core.ops import Op, registry

def agent_ops(whitelist=None, gate_cfg=None, *, is_superadmin_actor=False) -> List[str]:
    """The bot agent's tool UNIVERSE for a guild, live from the registry.

    Governed by the two-tier model (core/agent_gate, #92 follow-up): an op is
    in the universe only if the super-admin `agent_ops_whitelist` enables it
    AND its per-guild gate is not "off". A super-admin invoking the agent gets
    every whitelisted op of any scope (incl. cross-guild). Called with no args
    it returns [] (nothing whitelisted by default) — callers pass the resolved
    config. delete_message and the other ADMIN-floor ops still enforce their
    hardcoded floor at call time; the gate only widens/narrows the AGENT tier.
    """
    from core.agent_gate import agent_universe
    return agent_universe(whitelist, gate_cfg,
                          is_superadmin_actor=is_superadmin_actor)


def resolve_bot_tools(whitelist=None, gate_cfg=None, *, is_superadmin_actor=False) -> List[str]:
    """The guild's effective agent tool list. Since the two-tier model landed,
    this IS agent_ops(...) — the whitelist + per-guild gate fully determine the
    set, so there is no separate hand-picked `bot_tools_enabled` allowlist to
    intersect any more. Kept as the named entry point gpt.py and the panel
    resolve through, so what the panel renders is what the loop gets."""
    return agent_ops(whitelist, gate_cfg, is_superadmin_actor=is_superadmin_actor)


# Soft tool budget per agentic run, enforced HERE (not via pydantic-ai's
# UsageLimits) so exhaustion degrades into a model-authored answer instead
# of an exception. pydantic-ai's limiter is preemptive about parallel
# batches — a model that answers "check every channel" with 8 parallel
# search calls would blow a hard tool_calls_limit before a single call ran
# (observed live 2026-07-21) and the user gets a canned failure. Instead:
# calls past the budget are REFUSED with an answer-now error, and the last
# few results carry a countdown so the model lands before the cliff.
AGENT_TOOL_BUDGET = 8
# Results start carrying `tool_calls_remaining` when this many are left.
BUDGET_COUNTDOWN_AT = 3

LAST_CALL_NOTE = (
    "That was your LAST tool call. Your next response MUST be your final "
    "text answer — further tool calls will be refused."
)
BUDGET_EXHAUSTED_ERROR = (
    "Tool budget exhausted — no more tool calls will run. Give your FINAL "
    "text answer NOW using the results you already have, and be honest "
    "about anything you could not check."
)


def build_agent_tools(ctx: Any, logger: logging.Logger,
                      op_names: List[str],
                      tool_budget: int = AGENT_TOOL_BUDGET,
                      admin_gated: Optional[frozenset] = None,
                      is_admin_actor: bool = True,
                      gate_check=None) -> List[Tool]:
    """Build the pydantic-ai tool list for one agentic `!gpt` run.

    `ctx` is the live commands.Context of the invoking user — it IS the
    OpContext (duck-typed), so permission gates evaluate the invoking
    user's real Member, in their real guild.

    `op_names` is the guild's resolved agent tool set (from agent_ops()).
    An empty list yields no tools — callers should route those runs through
    the plain-chat path instead (see gpt.py process_askgpt).

    `admin_gated` is the subset of `op_names` whose per-guild gate is "admin"
    (as opposed to "everyone"); combined with `is_admin_actor`, it lets a
    non-admin user's agent run REFUSE those tools at call time without hiding
    the op from admins in the same guild. When the invoking user is an admin
    the set is a no-op. This is the per-guild admin/everyone distinction; the
    op's own hardcoded permission floor still applies independently.

    All tools from one call share a `tool_budget` counter (see the
    AGENT_TOOL_BUDGET comment above for why enforcement lives here).
    """
    if ctx.guild is None:
        raise ValueError("The agent loop only runs inside a guild.")
    allowed = frozenset({ctx.guild.id})
    # `gate_check` re-evaluates the per-guild admin gate at DISPATCH from live
    # policy, not from a boolean snapshotted at build time (codex review
    # 2026-08-20): an admin tightening an op to "admin only" mid-run must bind
    # on the next call, and a stale captured boolean would let a plain user's
    # in-flight run keep calling it. Default (None) preserves the old behavior
    # for callers that don't pass one.
    admin_gated = admin_gated or frozenset()
    if gate_check is None:
        def gate_check(op_name):
            return (op_name in admin_gated) and not is_admin_actor
    budget = {"used": 0, "cap": tool_budget}
    return [_make_agent_tool(registry.require(op_name), ctx, allowed, logger,
                             budget, gate_check=gate_check)
            for op_name in op_names]


def _make_agent_tool(op: Op, ctx: Any, allowed: frozenset,
                     logger: logging.Logger, budget: dict,
                     gate_check=None) -> Tool:
    async def tool_fn(**raw) -> dict:
        # Per-guild admin gate, re-evaluated LIVE at dispatch (not a snapshot):
        # a server admin set this op to "admin only" for agent use and the
        # invoking user is not an admin. Refuse before spending budget or
        # touching Discord. (Admins and "everyone"-gated ops pass; the op's own
        # hardcoded floor is still enforced by registry.call_ids below.)
        if gate_check is not None and gate_check(op.name):
            logger.info(
                "agent-op %s actor=%s REFUSED (guild gate: admin only)",
                op.name, ctx.author.id,
            )
            return {"ok": False,
                    "error": f"'{op.name}' is set to admin-only for the agent in "
                             "this server; a server admin must ask for it."}
        budget["used"] += 1
        remaining = budget["cap"] - budget["used"]
        if remaining < 0:
            logger.info(
                "agent-op %s actor=%s REFUSED (tool budget %s exhausted)",
                op.name, ctx.author.id, budget["cap"],
            )
            return {"ok": False, "error": BUDGET_EXHAUSTED_ERROR}
        # Fail closed if the op changed under this run: the tool's schema,
        # serializer and (crucially) SCOPE were captured from `op` when the
        # run started, but dispatch resolves by NAME — anything that
        # re-registered that name with a different declaration would
        # otherwise let a guild-scoped tool silently retarget to (say) a DM
        # op the guild agent must never reach. Since #86 the cog set is
        # fixed at boot, so this guard should never fire; it is deliberately
        # kept as the structural belt against dynamism being reintroduced —
        # identity, not liveness, is what makes name dispatch safe.
        if registry.get(op.name) is not op:
            logger.info(
                "agent-op %s actor=%s REFUSED (op re-registered mid-run)",
                op.name, ctx.author.id,
            )
            return {"ok": False,
                    "error": f"Tool '{op.name}' changed while this run was in "
                             "flight (it was re-registered). Call refused; "
                             "ask again to use the updated tool."}
        # send_message never pings: enforced by the op itself (see
        # core/ops.py send_message — never-ping is the registry default).
        result = await registry.call_ids(op.name, ctx, allowed_guild_ids=allowed,
                                         **raw)
        logger.info(
            "agent-op %s actor=%s params=%s -> %s",
            op.name, ctx.author.id, raw,
            "ok" if result.ok else f"error: {result.error}",
        )
        payload = op.result_payload(result)
        if remaining <= BUDGET_COUNTDOWN_AT:
            payload["tool_calls_remaining"] = remaining
            if remaining == 0:
                payload["budget_note"] = LAST_CALL_NOTE
        return payload

    return Tool.from_schema(
        tool_fn,
        name=op.name,
        description=op.description,
        json_schema=op.to_json_schema(),
    )
