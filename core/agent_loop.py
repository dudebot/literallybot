"""In-bot agent-loop frontend over the ops registry ("world pattern").

The second frontend next to mcp_ops/server.py: generates pydantic-ai
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

Wired up by cogs/dynamic/gpt.py when the guild config flag
`gpt_agentic_enabled` is true; nothing imports this module otherwise.
"""
from __future__ import annotations

import logging
from typing import Any, List

from pydantic_ai import Tool

from core.ops import Op, registry

# Tool surface for the loop. Roles/pins/threads stay out until there's a
# concrete ask (YAGNI) — adding one later is one string here. delete_message
# is ADMIN-gated in the registry, so only invoking users who pass is_admin
# can actually use it (everyone else gets a tool error back in the loop).
AGENT_OPS = (
    "send_message",
    "edit_message",
    "delete_message",
    "add_reaction",
    "remove_reaction",
    "search_history",
    "list_channels",
    "list_members",
)


def build_agent_tools(ctx: Any, logger: logging.Logger) -> List[Tool]:
    """Build the pydantic-ai tool list for one agentic `!gpt` run.

    `ctx` is the live commands.Context of the invoking user — it IS the
    OpContext (duck-typed), so permission gates evaluate the invoking
    user's real Member, in their real guild.
    """
    if ctx.guild is None:
        raise ValueError("The agent loop only runs inside a guild.")
    allowed = frozenset({ctx.guild.id})
    return [_make_agent_tool(registry.require(op_name), ctx, allowed, logger)
            for op_name in AGENT_OPS]


def _make_agent_tool(op: Op, ctx: Any, allowed: frozenset,
                     logger: logging.Logger) -> Tool:
    async def tool_fn(**raw) -> dict:
        # send_message never pings: enforced by the op itself (see
        # core/ops.py send_message — never-ping is the registry default).
        result = await registry.call_ids(op.name, ctx, allowed_guild_ids=allowed,
                                         **raw)
        logger.info(
            "agent-op %s actor=%s params=%s -> %s",
            op.name, ctx.author.id, raw,
            "ok" if result.ok else f"error: {result.error}",
        )
        return op.result_payload(result)

    return Tool.from_schema(
        tool_fn,
        name=op.name,
        description=op.description,
        json_schema=op.to_json_schema(),
    )
