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

import discord
from pydantic_ai import Tool

from core.ops import Op, registry

# Tool surface for the v1 loop. Roles/pins/threads/delete stay out until
# there's a concrete ask (YAGNI) — adding one later is one string here.
AGENT_OPS = (
    "send_message",
    "edit_message",
    "add_reaction",
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
    tools = []
    for op_name in AGENT_OPS:
        op = registry.get(op_name)
        if op is None:  # registry drift — fail loudly at build time
            raise ValueError(f"Op '{op_name}' not found in the ops registry.")
        tools.append(_make_agent_tool(op, ctx, allowed, logger))
    return tools


def _make_agent_tool(op: Op, ctx: Any, allowed: frozenset,
                     logger: logging.Logger) -> Tool:
    async def tool_fn(**raw) -> dict:
        extra = {}
        if op.name == "send_message":
            # Loop policy: never ping. Mentions are always suppressed.
            extra["allowed_mentions"] = discord.AllowedMentions.none()

        result = await registry.call_ids(op.name, ctx, allowed_guild_ids=allowed,
                                         **raw, **extra)
        logger.info(
            "agent-op %s actor=%s params=%s -> %s",
            op.name, ctx.author.id, raw,
            "ok" if result.ok else f"error: {result.error}",
        )
        if not result.ok:
            return {"ok": False, "error": result.error}
        return {"ok": True, **op.serialize_result(result.value)}

    return Tool.from_schema(
        tool_fn,
        name=op.name,
        description=op.description,
        json_schema=op.to_json_schema(),
    )
