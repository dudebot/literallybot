"""MCP frontend over the bot's ops registry ("world pattern").

One ops layer (core/ops.py), two frontends: the in-bot agent loop (not built
yet) and this MCP server. Neither frontend re-implements Discord call
plumbing or permission checks; both go through
`registry.call(op_name, ctx, **kwargs)`.

This module does NOT start anything on import — call build_server() to get a
configured FastMCP instance, or serve() to run it over authenticated
streamable HTTP. bot.py only starts it when MCP_OPS_ENABLED=1.

Guardrails (per the Codex review of the original spike, issue #58):
- Shared-token bearer auth is mandatory (see mcp_ops/auth.py) — the serve()
  helper refuses to run without a token.
- Binds to loopback ONLY. serve() hard-codes 127.0.0.1; there is no host
  parameter on purpose.
- Server-side guild allowlist: every tool call resolves its channel and then
  verifies the channel belongs to an allowlisted guild. DM channels and
  channels in other guilds are refused. An empty allowlist refuses to serve
  (fail closed).
- send_message always sends with allowed_mentions=none — no pings, ever.
- search_history clamps `limit` to MAX_HISTORY_LIMIT.
- ACCEPTED RISK: `actor_id` is caller-supplied and not credential-bound, so a
  client that already holds the bearer token can act as any user id for
  permission purposes. Acceptable for localhost self-use only; do not expose
  this server beyond loopback without adding real actor authentication.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

import discord
from mcp.server.fastmcp import FastMCP

from core.ops import OpContext, registry

logger = logging.getLogger("mcp_ops.server")

MAX_HISTORY_LIMIT = 200


class BotUnavailableError(RuntimeError):
    """Raised when the MCP server needs a live discord.py bot/channel/message
    and doesn't have one (e.g. bot not passed in, or the id doesn't resolve)."""


class GuildNotAllowedError(RuntimeError):
    """Raised when a tool call targets a channel/guild outside the
    server-side guild allowlist."""


def _require_bot(bot: Any) -> Any:
    if bot is None:
        raise BotUnavailableError(
            "No live discord.py bot attached to this MCP server instance."
        )
    return bot


def _check_guild_allowed(guild: Any, allowed_guild_ids: frozenset,
                         what: str) -> None:
    if guild is None:
        raise GuildNotAllowedError(
            f"{what} has no guild (DMs are not allowed through this server)."
        )
    if guild.id not in allowed_guild_ids:
        raise GuildNotAllowedError(
            f"{what} belongs to guild {guild.id}, which is not in this "
            f"server's guild allowlist."
        )


async def _resolve_channel(bot: Any, channel_id: int,
                           allowed_guild_ids: frozenset) -> Any:
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception as exc:  # noqa: BLE001 - surfaced to the MCP caller as a tool error
            raise BotUnavailableError(f"Could not resolve channel {channel_id}: {exc}") from exc
    _check_guild_allowed(getattr(channel, "guild", None), allowed_guild_ids,
                         f"Channel {channel_id}")
    return channel


async def _resolve_message(bot: Any, channel_id: int, message_id: int,
                           allowed_guild_ids: frozenset) -> Any:
    channel = await _resolve_channel(bot, channel_id, allowed_guild_ids)
    try:
        return await channel.fetch_message(message_id)
    except Exception as exc:  # noqa: BLE001
        raise BotUnavailableError(
            f"Could not resolve message {message_id} in channel {channel_id}: {exc}"
        ) from exc


def _build_context(bot: Any, actor_id: int, guild: Any) -> OpContext:
    """Build an OpContext from ids supplied over MCP (no discord.py Context
    exists on this frontend — that's the point of OpContext's duck-typed
    shape). core.ops routes permission gates through core.utils.is_admin /
    is_superadmin, which read `ctx.bot.config`, `ctx.author.id`, and
    `ctx.guild` — so prefer the real guild Member (correct roles/permissions)
    and fall back to a bare id-holder, which those helpers treat as an
    ordinary non-admin user unless the id is in the config admin lists.
    """
    author: Any = None
    if guild is not None:
        author = guild.get_member(actor_id)
    if author is None:
        class _Author:
            def __init__(self, user_id: int):
                self.id = user_id
        author = _Author(actor_id)
    return OpContext(bot=bot, author=author, guild=guild)


def parse_guild_allowlist(raw: Optional[str]) -> frozenset:
    """Parse a comma-separated guild-id allowlist string into ids.
    Empty/invalid input yields an empty set — callers must fail closed."""
    ids = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            logger.error("Ignoring non-numeric guild id in allowlist: %r", part)
    return frozenset(ids)


def build_server(bot: Any = None, *, allowed_guild_ids: Iterable[int],
                 name: str = "literallybot-ops") -> FastMCP:
    """Construct a FastMCP server exposing three ops-registry tools.

    `bot` should be a live discord.py Bot/Client instance (with `.config`
    attached, as bot.py does) so the tools can resolve channel/message ids
    and permission checks read the real config. If `bot` is None (schema-only
    smoke test), the tools raise BotUnavailableError when invoked.

    `allowed_guild_ids` is the server-side guild allowlist; every call is
    refused unless its resolved channel belongs to one of these guilds.
    """
    allowed = frozenset(int(g) for g in allowed_guild_ids)
    if not allowed:
        raise ValueError(
            "build_server requires a non-empty guild allowlist "
            "(set MCP_OPS_GUILD_ALLOWLIST); refusing to build an "
            "unrestricted server."
        )

    mcp = FastMCP(name=name, instructions=(
        "Ops-registry bridge for literallybot. Exposes a subset of the "
        "bot's ops registry as MCP tools: send_message, edit_message, "
        "search_history, add_reaction, list_guilds, list_channels. Every "
        "call is permission-checked the same way an in-bot command would "
        "be, via the shared ops registry, and is restricted to a "
        "server-side guild allowlist."
    ))

    @mcp.tool(
        name="send_message",
        description=registry.get("send_message").description,
    )
    async def send_message(channel_id: int, content: str, actor_id: int) -> dict:
        """Send a text message to a channel (mentions are always suppressed).

        Args:
            channel_id: Discord channel id to send into (must be in an
                allowlisted guild).
            content: Message text to send.
            actor_id: Discord user id on whose behalf this call is made
                (used for permission checks — send_message is EVERYONE-level).
        """
        live_bot = _require_bot(bot)
        channel = await _resolve_channel(live_bot, channel_id, allowed)
        ctx = _build_context(live_bot, actor_id, channel.guild)
        result = await registry.call(
            "send_message", ctx, channel=channel, content=content,
            allowed_mentions=discord.AllowedMentions.none(),
        )
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
                             contains: Optional[str] = None) -> dict:
        """Search a channel's message history.

        Args:
            channel_id: Discord channel id to search (must be in an
                allowlisted guild).
            actor_id: Discord user id on whose behalf this call is made.
            limit: Max number of messages to scan, most recent first
                (clamped to 200).
            author_id: Optional filter — only messages from this user id.
            contains: Optional filter — substring match on message content.
        """
        live_bot = _require_bot(bot)
        channel = await _resolve_channel(live_bot, channel_id, allowed)
        ctx = _build_context(live_bot, actor_id, channel.guild)
        limit = max(1, min(int(limit), MAX_HISTORY_LIMIT))
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
                           actor_id: int) -> dict:
        """Add an emoji reaction to a message.

        Args:
            channel_id: Discord channel id containing the message (must be
                in an allowlisted guild).
            message_id: Discord message id to react to.
            emoji: Emoji to react with (unicode emoji or `name:id` custom emoji).
            actor_id: Discord user id on whose behalf this call is made.
        """
        live_bot = _require_bot(bot)
        message = await _resolve_message(live_bot, channel_id, message_id, allowed)
        ctx = _build_context(live_bot, actor_id, message.guild)
        result = await registry.call("add_reaction", ctx, message=message, emoji=emoji)
        if not result.ok:
            return {"ok": False, "error": result.error}
        return {"ok": True}

    @mcp.tool(
        name="edit_message",
        description=registry.get("edit_message").description,
    )
    async def edit_message(channel_id: int, message_id: int, content: str,
                           actor_id: int) -> dict:
        """Edit the content of a message. Discord only permits bots to edit
        their OWN messages — editing anyone else's returns a 403 error.

        Args:
            channel_id: Discord channel id containing the message (must be
                in an allowlisted guild).
            message_id: Discord message id to edit (must be authored by the bot).
            content: Replacement message text.
            actor_id: Discord user id on whose behalf this call is made.
        """
        live_bot = _require_bot(bot)
        message = await _resolve_message(live_bot, channel_id, message_id, allowed)
        ctx = _build_context(live_bot, actor_id, message.guild)
        result = await registry.call("edit_message", ctx, message=message, content=content)
        if not result.ok:
            return {"ok": False, "error": result.error}
        return {"ok": True, "message_id": message.id}

    @mcp.tool(
        name="list_guilds",
        description=registry.get("list_guilds").description,
    )
    async def list_guilds(actor_id: int) -> dict:
        """List guilds the bot is in, restricted to this server's allowlist
        (guilds outside the allowlist are not disclosed).

        Args:
            actor_id: Discord user id on whose behalf this call is made.
        """
        live_bot = _require_bot(bot)
        ctx = _build_context(live_bot, actor_id, None)
        result = await registry.call("list_guilds", ctx)
        if not result.ok:
            return {"ok": False, "error": result.error}
        guilds = [g for g in result.value if g["id"] in allowed]
        return {"ok": True, "guilds": guilds, "count": len(guilds)}

    @mcp.tool(
        name="list_channels",
        description=registry.get("list_channels").description,
    )
    async def list_channels(guild_id: int, actor_id: int) -> dict:
        """List a guild's channels (id, name, type). The guild must be in
        this server's allowlist.

        Args:
            guild_id: Discord guild id to enumerate.
            actor_id: Discord user id on whose behalf this call is made.
        """
        live_bot = _require_bot(bot)
        guild = live_bot.get_guild(guild_id)
        if guild is None:
            raise BotUnavailableError(f"Could not resolve guild {guild_id}.")
        _check_guild_allowed(guild, allowed, f"Guild {guild_id}")
        ctx = _build_context(live_bot, actor_id, guild)
        result = await registry.call("list_channels", ctx, guild=guild)
        if not result.ok:
            return {"ok": False, "error": result.error}
        return {"ok": True, "channels": result.value, "count": len(result.value)}

    return mcp


async def serve(bot: Any, *, port: int, token: str,
                allowed_guild_ids: Iterable[int],
                name: str = "literallybot-ops") -> None:
    """Serve the ops MCP server over authenticated streamable HTTP, bound to
    127.0.0.1 ONLY (no host parameter on purpose — do not add one).

    Runs until cancelled. Callers: mcp_ops/run_mcp_server.py (standalone
    process) and bot.py (in-process, gated on MCP_OPS_ENABLED=1).
    """
    import contextlib

    import uvicorn

    from mcp_ops.auth import wrap_with_auth

    if not token:
        raise ValueError("serve() requires a non-empty auth token (MCP_OPS_TOKEN).")

    class _NoSignalCaptureServer(uvicorn.Server):
        """uvicorn.Server.serve() normally takes over SIGINT/SIGTERM in the
        main thread; when embedded in the bot process that would fight
        discord.py's own shutdown, so signal handling is left to the host."""

        @contextlib.contextmanager
        def capture_signals(self):
            yield

    mcp = build_server(bot=bot, allowed_guild_ids=allowed_guild_ids, name=name)
    app = wrap_with_auth(mcp.streamable_http_app(), token)

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="info")
    server = _NoSignalCaptureServer(config)
    logger.warning(
        "Starting MCP ops server on 127.0.0.1:%s — auth REQUIRED (Bearer "
        "token), loopback-only bind, guild allowlist %s. Every tool call "
        "runs as a live, authenticated Discord bot action.",
        port, sorted(set(int(g) for g in allowed_guild_ids)),
    )
    await server.serve()
