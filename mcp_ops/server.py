"""SPIKE: MCP frontend over the bot's ops registry ("world pattern" demo).

One ops layer (core/ops.py — here core/ops_stub.py, see DEPENDENCY NOTE below),
two frontends: the in-bot agent loop (not built in this spike) and this MCP
server. Neither frontend re-implements Discord call plumbing or permission
checks; both go through `registry.call(op_name, ctx, **kwargs)`.

DEPENDENCY NOTE: this should import the real `core.ops` registry built on
`feat/ops-registry`. That branch's `core/ops.py` existed only as an
uncommitted file in this shared worktree and was lost to a concurrent branch
switch before this spike could build against it (see mcp_ops/README section
in the top-level README for the full story). This module currently imports
`core.ops_stub`, a minimal hand-rolled stand-in covering send_message,
search_history, and add_reaction. Swap the import below for `core.ops` once
that branch's registry is committed for real — the call sites don't change,
since ops_stub mirrors core.ops's OpContext/OpsRegistry/PermissionLevel shape.

This module does NOT start anything on import — call build_server() to get a
configured FastMCP instance. Nothing here is imported by bot.py.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from core.ops_stub import OpContext, registry  # see DEPENDENCY NOTE above

logger = logging.getLogger("mcp_ops.server")


class BotUnavailableError(RuntimeError):
    """Raised when the MCP server needs a live discord.py bot/channel/message
    and doesn't have one (e.g. bot not passed in, or the id doesn't resolve)."""


def _require_bot(bot: Any) -> Any:
    if bot is None:
        raise BotUnavailableError(
            "No live discord.py bot attached to this MCP server instance."
        )
    return bot


async def _resolve_channel(bot: Any, channel_id: int) -> Any:
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception as exc:  # noqa: BLE001 - surfaced to the MCP caller as a tool error
            raise BotUnavailableError(f"Could not resolve channel {channel_id}: {exc}") from exc
    return channel


async def _resolve_message(bot: Any, channel_id: int, message_id: int) -> Any:
    channel = await _resolve_channel(bot, channel_id)
    try:
        return await channel.fetch_message(message_id)
    except Exception as exc:  # noqa: BLE001
        raise BotUnavailableError(
            f"Could not resolve message {message_id} in channel {channel_id}: {exc}"
        ) from exc


def _build_context(bot: Any, actor_id: int, guild_id: Optional[int]) -> OpContext:
    """Build an OpContext from ids supplied over MCP (no discord.py Context
    exists on this frontend — that's the point of OpContext's duck-typed
    shape). The stub registry's permission gate reads `.is_admin` /
    `.is_superadmin` truthy attributes off a lightweight author stand-in;
    the real core.ops registry instead calls core.utils.is_admin/is_superadmin
    against the live bot config — swap this out together with the ops_stub
    import above.
    """
    class _Author:
        def __init__(self, user_id: int):
            self.id = user_id
            # Stub-only flags; the real registry derives these from bot.config.
            self.is_admin = False
            self.is_superadmin = False

    guild = bot.get_guild(guild_id) if (bot is not None and guild_id) else None
    return OpContext(bot=bot, author=_Author(actor_id), guild=guild)


def build_server(bot: Any = None, *, name: str = "literallybot-ops") -> FastMCP:
    """Construct a FastMCP server exposing three ops-registry tools.

    `bot` should be a live discord.py Bot/Client instance so the tools can
    resolve channel/message ids to real objects and actually perform the
    Discord action. If `bot` is None (e.g. quick schema-only smoke test),
    the tools raise BotUnavailableError when invoked — they still register
    and describe themselves correctly.
    """
    mcp = FastMCP(name=name, instructions=(
        "Ops-registry bridge for literallybot (SPIKE). Exposes a subset of "
        "the bot's ops registry as MCP tools: send_message, search_history, "
        "add_reaction. Every call is permission-checked the same way an "
        "in-bot command would be, via the shared ops registry."
    ))

    @mcp.tool(
        name="send_message",
        description=registry.get("send_message").description,
    )
    async def send_message(channel_id: int, content: str, actor_id: int,
                            guild_id: Optional[int] = None) -> dict:
        """Send a text message to a channel.

        Args:
            channel_id: Discord channel id to send into.
            content: Message text to send.
            actor_id: Discord user id on whose behalf this call is made
                (used for permission checks — send_message is EVERYONE-level).
            guild_id: Optional guild id, for permission context.
        """
        live_bot = _require_bot(bot)
        channel = await _resolve_channel(live_bot, channel_id)
        ctx = _build_context(live_bot, actor_id, guild_id)
        result = await registry.call("send_message", ctx, channel=channel, content=content)
        if not result.ok:
            return {"ok": False, "error": result.error}
        return {"ok": True, "message_id": getattr(result.value, "id", None)}

    @mcp.tool(
        name="search_history",
        description=registry.get("search_history").description,
    )
    async def search_history(channel_id: int, actor_id: int,
                              limit: int = 100,
                              author_id: Optional[int] = None,
                              contains: Optional[str] = None,
                              guild_id: Optional[int] = None) -> dict:
        """Search a channel's message history.

        Args:
            channel_id: Discord channel id to search.
            actor_id: Discord user id on whose behalf this call is made.
            limit: Max number of messages to scan (most recent first).
            author_id: Optional filter — only messages from this user id.
            contains: Optional filter — substring match on message content.
            guild_id: Optional guild id, for permission context.
        """
        live_bot = _require_bot(bot)
        channel = await _resolve_channel(live_bot, channel_id)
        ctx = _build_context(live_bot, actor_id, guild_id)
        result = await registry.call(
            "search_history", ctx, channel=channel, limit=limit,
            author_id=author_id, contains=contains,
        )
        if not result.ok:
            return {"ok": False, "error": result.error}
        messages = [
            {
                "id": m.id,
                "author_id": m.author.id,
                "content": m.content,
                "created_at": m.created_at.isoformat() if getattr(m, "created_at", None) else None,
            }
            for m in result.value
        ]
        return {"ok": True, "messages": messages, "count": len(messages)}

    @mcp.tool(
        name="add_reaction",
        description=registry.get("add_reaction").description,
    )
    async def add_reaction(channel_id: int, message_id: int, emoji: str,
                            actor_id: int, guild_id: Optional[int] = None) -> dict:
        """Add an emoji reaction to a message.

        Args:
            channel_id: Discord channel id containing the message.
            message_id: Discord message id to react to.
            emoji: Emoji to react with (unicode emoji or `name:id` custom emoji).
            actor_id: Discord user id on whose behalf this call is made.
            guild_id: Optional guild id, for permission context.
        """
        live_bot = _require_bot(bot)
        message = await _resolve_message(live_bot, channel_id, message_id)
        ctx = _build_context(live_bot, actor_id, guild_id)
        result = await registry.call("add_reaction", ctx, message=message, emoji=emoji)
        if not result.ok:
            return {"ok": False, "error": result.error}
        return {"ok": True}

    return mcp
