#!/usr/bin/env python3
"""SPIKE entrypoint: run the ops-registry MCP server standalone.

THIS IS NOT STARTED BY bot.py. It is a separate process you launch explicitly:

    python3 -m mcp_ops.run_mcp_server

Security model (fail-closed, both gates independently required):
  1. Off by default: refuses to start unless MCP_OPS_ENABLED=1 is set.
  2. Auth required: refuses to start unless MCP_OPS_TOKEN is set to a
     non-empty shared secret; every MCP request must present it as
     `Authorization: Bearer <token>`.

It logs into Discord using the same DISCORD_TOKEN as the normal bot (so ops
can actually call the Discord API), but registers NO cogs, NO command
prefix handling, and NO event handlers beyond what's needed to become ready
and serve MCP tool calls. It is intentionally a minimal, separate process —
not an alternate mode of bot.py — so it can be run (or not) independently of
the main bot's lifecycle.

See README.md's "MCP Ops Server (spike)" section for the full run/connect
walkthrough.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

import discord
import uvicorn
from dotenv import load_dotenv

from mcp_ops.auth import load_token_from_env, wrap_with_auth
from mcp_ops.server import build_server

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s:%(name)s: %(message)s",
)
logger = logging.getLogger("mcp_ops.run_mcp_server")

ENABLE_ENV_VAR = "MCP_OPS_ENABLED"
HOST_ENV_VAR = "MCP_OPS_HOST"
PORT_ENV_VAR = "MCP_OPS_PORT"


def _check_enabled() -> None:
    """Off-by-default gate. Refuses to start unless explicitly enabled."""
    if os.environ.get(ENABLE_ENV_VAR, "").strip() != "1":
        logger.error(
            "%s is not set to '1'. This MCP server is OFF by default and will "
            "not start. Set %s=1 in the environment to run it explicitly.",
            ENABLE_ENV_VAR, ENABLE_ENV_VAR,
        )
        sys.exit(1)


async def _make_discord_client(token: str) -> discord.Client:
    """A minimal discord.py client — just enough to resolve channels/messages
    and perform ops-registry actions. No cogs, no command framework."""
    intents = discord.Intents.default()
    intents.message_content = True
    intents.guilds = True
    client = discord.Client(intents=intents)

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
    _check_enabled()
    token = load_token_from_env()  # raises RuntimeError (fail closed) if unset

    load_dotenv()
    discord_token = os.getenv("DISCORD_TOKEN")
    if not discord_token:
        logger.error("DISCORD_TOKEN is not set; cannot log the ops client into Discord.")
        sys.exit(1)

    host = os.environ.get(HOST_ENV_VAR, "127.0.0.1")
    port = int(os.environ.get(PORT_ENV_VAR, "8765"))

    logger.warning(
        "Starting MCP ops server on %s:%s — auth REQUIRED (Bearer token), "
        "bind host defaults to loopback only. Do not expose this port "
        "publicly without understanding the blast radius: every tool call "
        "runs as a live, authenticated Discord bot action.",
        host, port,
    )

    client = await _make_discord_client(discord_token)
    mcp = build_server(bot=client)

    app = mcp.streamable_http_app()
    app = wrap_with_auth(app, token)

    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        await client.close()


def main() -> None:
    try:
        asyncio.run(_run())
    except (RuntimeError, discord.LoginFailure) as exc:
        # e.g. missing MCP_OPS_TOKEN, or a bad DISCORD_TOKEN — fail closed
        # with a clear message, not a stack trace.
        logger.error(str(exc))
        sys.exit(1)


if __name__ == "__main__":
    main()
