#!/usr/bin/env python3
"""Standalone entrypoint: run the ops-registry MCP server in its own process.

    python3 -m mcp_ops.run_mcp_server

The MCP server can run two ways, sharing the same mcp_ops.server.serve()
guardrails (loopback-only bind, mandatory bearer auth, guild allowlist):

  1. In-process with the bot: bot.py starts it automatically when
     MCP_OPS_ENABLED=1 (see maybe_start_in_bot below). This is the normal
     path for a dev instance — the MCP tools then act through the live bot.
  2. Standalone via this entrypoint: logs into Discord with the same
     DISCORD_TOKEN as the bot but registers NO cogs, NO command prefix
     handling, and NO event handlers beyond becoming ready.

Security model (fail-closed, all gates independently required):
  - Off by default: refuses to start unless MCP_OPS_ENABLED=1.
  - Auth required: refuses to start unless MCP_OPS_TOKEN is a non-empty
    shared secret; every MCP request must present it as
    `Authorization: Bearer <token>`.
  - Guild allowlist required: refuses to start unless
    MCP_OPS_GUILD_ALLOWLIST names at least one guild id; tool calls
    targeting channels outside those guilds are refused.
  - Binds to 127.0.0.1 ONLY. There is no host override; if the legacy
    MCP_OPS_HOST var is set to anything non-loopback, startup is refused
    rather than silently rebinding.

See README.md's "MCP Ops Server" section for the run/connect walkthrough.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any, Optional

import discord
from dotenv import load_dotenv

from mcp_ops.auth import load_token_from_env
from mcp_ops.server import parse_guild_allowlist, serve

logger = logging.getLogger("mcp_ops.run_mcp_server")

ENABLE_ENV_VAR = "MCP_OPS_ENABLED"
HOST_ENV_VAR = "MCP_OPS_HOST"  # legacy; only loopback values are accepted
PORT_ENV_VAR = "MCP_OPS_PORT"
ALLOWLIST_ENV_VAR = "MCP_OPS_GUILD_ALLOWLIST"

_LOOPBACK_HOSTS = {"", "127.0.0.1", "localhost", "::1"}


def is_enabled() -> bool:
    return os.environ.get(ENABLE_ENV_VAR, "").strip() == "1"


def _load_settings() -> "tuple[str, int, frozenset]":
    """Read and validate token/port/allowlist from the environment.
    Raises RuntimeError on any missing/invalid gate (fail closed)."""
    token = load_token_from_env()  # raises RuntimeError if unset

    host = os.environ.get(HOST_ENV_VAR, "").strip()
    if host not in _LOOPBACK_HOSTS:
        raise RuntimeError(
            f"{HOST_ENV_VAR}={host!r} is not loopback. This server binds to "
            f"127.0.0.1 ONLY; unset {HOST_ENV_VAR}."
        )

    allowlist = parse_guild_allowlist(os.environ.get(ALLOWLIST_ENV_VAR))
    if not allowlist:
        raise RuntimeError(
            f"{ALLOWLIST_ENV_VAR} is not set (comma-separated guild ids). "
            f"The MCP ops server requires an explicit guild allowlist and "
            f"refuses to start without one."
        )

    port = int(os.environ.get(PORT_ENV_VAR, "8765"))
    return token, port, allowlist


def maybe_start_in_bot(bot: Any) -> Optional[asyncio.Task]:
    """Called by bot.py once the bot is ready. Starts the MCP ops server as
    a background task on the bot's event loop IF MCP_OPS_ENABLED=1 and all
    fail-closed gates pass; returns None (and changes nothing) otherwise.
    """
    if not is_enabled():
        return None
    try:
        token, port, allowlist = _load_settings()
    except RuntimeError as exc:
        logger.error("MCP ops server NOT started: %s", exc)
        return None
    task = asyncio.get_running_loop().create_task(
        serve(bot, port=port, token=token, allowed_guild_ids=allowlist),
        name="mcp-ops-server",
    )

    # A background task's exception is otherwise only reported at GC time,
    # if ever — a schema-generation bug once killed the server with zero log
    # output. Fail LOUDLY instead.
    def _report_death(t: asyncio.Task) -> None:
        if not t.cancelled() and t.exception() is not None:
            logger.error("MCP ops server task DIED: %r", t.exception(),
                         exc_info=t.exception())

    task.add_done_callback(_report_death)
    return task


async def _make_discord_client(token: str) -> discord.Client:
    """A minimal discord.py client — just enough to resolve channels/messages
    and perform ops-registry actions. No cogs, no command framework."""
    intents = discord.Intents.default()
    intents.message_content = True
    intents.guilds = True
    intents.members = True
    client = discord.Client(intents=intents)

    # core.utils.is_admin / is_superadmin read permissions from
    # `ctx.bot.config` — give the standalone client the same JSON config
    # store the bot uses so the permission gates are truthful here too.
    from core.config import Config
    client.config = Config()

    ready = asyncio.Event()

    @client.event
    async def on_ready():
        logger.info("MCP ops server: Discord client ready as %s", client.user)
        ready.set()

    start_task = asyncio.create_task(client.start(token))

    # Race readiness against the login/connect task itself so a bad token
    # (or any other startup failure) surfaces immediately instead of hanging
    # forever on ready.wait().
    ready_task = asyncio.create_task(ready.wait())
    done, pending = await asyncio.wait(
        {start_task, ready_task}, return_when=asyncio.FIRST_COMPLETED
    )
    if start_task in done and not ready.is_set():
        ready_task.cancel()
        # Re-raise whatever killed client.start() (e.g. discord.LoginFailure).
        start_task.result()
        raise RuntimeError("Discord client.start() finished without becoming ready.")
    return client


async def _run() -> None:
    load_dotenv()

    if not is_enabled():
        logger.error(
            "%s is not set to '1'. This MCP server is OFF by default and will "
            "not start. Set %s=1 in the environment to run it explicitly.",
            ENABLE_ENV_VAR, ENABLE_ENV_VAR,
        )
        sys.exit(1)

    token, port, allowlist = _load_settings()

    discord_token = os.getenv("DISCORD_TOKEN")
    if not discord_token:
        logger.error("DISCORD_TOKEN is not set; cannot log the ops client into Discord.")
        sys.exit(1)

    client = await _make_discord_client(discord_token)
    try:
        await serve(client, port=port, token=token, allowed_guild_ids=allowlist)
    finally:
        await client.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s:%(name)s: %(message)s",
    )
    try:
        asyncio.run(_run())
    except (RuntimeError, discord.LoginFailure) as exc:
        # e.g. missing MCP_OPS_TOKEN, or a bad DISCORD_TOKEN — fail closed
        # with a clear message, not a stack trace.
        logger.error(str(exc))
        sys.exit(1)


if __name__ == "__main__":
    main()
