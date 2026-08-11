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

Wired up by cogs/optional/gpt.py when a guild has at least one tool enabled in
its `bot_tools_enabled` allowlist (the empty default routes to plain chat);
nothing imports this module otherwise.
"""
from __future__ import annotations

import logging
from typing import Any, List

from pydantic_ai import Tool

from core.ops import Op, registry

def agent_ops() -> List[str]:
    """The bot agent's tool UNIVERSE — the ceiling of what a guild's
    `bot_tools_enabled` allowlist may contain (per-guild subsets are chosen
    in the /aisettings panel).

    Queried LIVE from the registry: it is every guild-scoped op, because a
    guild-confined, user-actored loop is exactly what guild-scoped ops are
    safe for. There is no `agent=True` flag any more, and this must never
    be frozen into a module-level tuple — cog-provided ops appear and
    disappear with cog load/unload. delete_message is ADMIN-gated in the
    registry, so only invoking users who pass is_admin can actually use it
    (everyone else gets a tool error back in the loop)."""
    return registry.guild_agent_names()


def resolve_bot_tools(raw) -> List[str]:
    """Effective bot-agent tool allowlist from the raw per-guild config
    value (`bot_tools_enabled`): None/empty means no tools (the plain-chat
    path); names whose op is not currently registered are dropped by
    intersecting with the LIVE universe. THE one owner of this rule —
    gpt.py and the /aisettings panel both resolve through it, so what the
    panel shows is what the loop gets.

    Dropping here is an effective-set filter only: a name stays in stored
    config even while its op is unregistered (a cog unloaded), so reloading
    the cog restores the guild's choice instead of silently losing it."""
    universe = set(agent_ops())
    return [n for n in (raw or []) if n in universe]


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
                      tool_budget: int = AGENT_TOOL_BUDGET) -> List[Tool]:
    """Build the pydantic-ai tool list for one agentic `!gpt` run.

    `ctx` is the live commands.Context of the invoking user — it IS the
    OpContext (duck-typed), so permission gates evaluate the invoking
    user's real Member, in their real guild.

    `op_names` is the guild's resolved bot-tool allowlist (a subset of
    agent_ops()). An empty list yields no tools — callers should route those
    runs through the plain-chat path instead (see gpt.py process_askgpt).

    All tools from one call share a `tool_budget` counter (see the
    AGENT_TOOL_BUDGET comment above for why enforcement lives here).
    """
    if ctx.guild is None:
        raise ValueError("The agent loop only runs inside a guild.")
    allowed = frozenset({ctx.guild.id})
    budget = {"used": 0, "cap": tool_budget}
    return [_make_agent_tool(registry.require(op_name), ctx, allowed, logger,
                             budget)
            for op_name in op_names]


def _make_agent_tool(op: Op, ctx: Any, allowed: frozenset,
                     logger: logging.Logger, budget: dict) -> Tool:
    async def tool_fn(**raw) -> dict:
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
        # run started, but dispatch resolves by name — a cog reload that
        # re-registers the same name with a different declaration would
        # otherwise let a guild-scoped tool silently retarget to (say) a DM
        # op the guild agent must never reach.
        if registry.get(op.name) is not op:
            logger.info(
                "agent-op %s actor=%s REFUSED (op re-registered mid-run)",
                op.name, ctx.author.id,
            )
            return {"ok": False,
                    "error": f"Tool '{op.name}' changed while this run was in "
                             "flight (its cog was reloaded). Call refused; "
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
