"""MCP frontend over the bot's ops registry ("world pattern").

One ops layer (core/ops.py), two frontends: the in-bot agent loop
(core/agent_loop.py) and this MCP server. Neither frontend re-implements
Discord call plumbing, id resolution, result serialization, or permission
checks — all of that lives in the registry. This module GENERATES its MCP
tools mechanically from each op's typed param declarations
(`Op.wire_params()`): the only hand-written pieces here are frontend
policy (actor construction from `actor_id`, the guild allowlist, forced
allowed_mentions=none on sends, allowlist-filtered list_guilds).

This module does NOT start anything on import — call build_server() to get a
configured FastMCP instance, or serve() to run it over authenticated
streamable HTTP. bot.py only starts it when MCP_OPS_ENABLED=1.

Guardrails (per the Codex review of the original spike, issue #58):
- Shared-token bearer auth is mandatory (see mcp_ops/auth.py) — the serve()
  helper refuses to run without a token.
- Binds to loopback ONLY. serve() hard-codes 127.0.0.1; there is no host
  parameter on purpose.
- Server-side guild allowlist: every id-resolved target is verified to
  belong to an allowlisted guild (enforced by the registry's shared
  resolver via `allowed_guild_ids`). DM channels and channels in other
  guilds are refused. An empty allowlist refuses to serve (fail closed).
- send_message always sends with allowed_mentions=none — no pings, ever.
- search_history clamps `limit` to core.ops.HISTORY_LIMIT_MAX (200),
  declared on the op itself.
- ACCEPTED RISK: `actor_id` is caller-supplied and not credential-bound, so a
  client that already holds the bearer token can act as any user id for
  permission purposes. Acceptable for localhost self-use only; do not expose
  this server beyond loopback without adding real actor authentication.
"""
from __future__ import annotations

import inspect
import logging
from typing import Annotated, Any, Iterable, Optional

from pydantic import Field
from mcp.server.fastmcp import FastMCP

from core.ops import (
    Op,
    OpContext,
    OpResult,
    ResolutionError,
    registry,
    resolve_context_guild,
)

logger = logging.getLogger("mcp_ops.server")

# Ops exposed over MCP. pin/role/thread ops stay unexposed until a concrete
# need shows up. ADMIN-tier ops (delete_message) are safe to expose:
# registry.call_ids checks the permission gate BEFORE resolving any ids, so
# a non-admin caller gets "Requires admin." without ever triggering Discord
# lookups (no id-probing oracle).
_EXPOSED_OPS = (
    "send_message",
    "search_history",
    "add_reaction",
    "remove_reaction",
    "edit_message",
    "delete_message",
    "list_guilds",
    "list_channels",
    "list_members",
)

_JSON_TYPE_TO_PY = {"integer": int, "string": str, "boolean": bool}


class BotUnavailableError(RuntimeError):
    """Raised when the MCP server needs a live discord.py bot/channel/message
    and doesn't have one (e.g. bot not passed in, or the id doesn't resolve)."""


def _require_bot(bot: Any) -> Any:
    if bot is None:
        raise BotUnavailableError(
            "No live discord.py bot attached to this MCP server instance."
        )
    return bot


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


def _make_mcp_tool(bot: Any, op: Op, allowed: frozenset):
    """Generate one MCP tool function from an op's typed declaration.

    The returned coroutine has an explicit `__signature__` built from the
    op's wire params (plus the MCP-frontend `actor_id`), so FastMCP derives
    the same JSON schema the registry declares.
    """

    async def tool_fn(**raw) -> dict:
        live_bot = _require_bot(bot)
        actor_id = raw.pop("actor_id")

        # Resolve the target guild first (raises on unknown ids and on
        # allowlist violations — surfaced as MCP tool errors) so the actor
        # can be built as a real Member of that guild.
        try:
            guild = await resolve_context_guild(live_bot, raw, allowed)
        except ResolutionError as exc:
            raise BotUnavailableError(str(exc)) from exc

        ctx = _build_context(live_bot, actor_id, guild)

        # send_message never pings: enforced by the op itself (see
        # core/ops.py send_message — never-ping is the registry default).
        result = await registry.call_ids(op.name, ctx, allowed_guild_ids=allowed,
                                         **raw)
        if result.ok and op.name == "list_guilds":
            # MCP policy: guilds outside the allowlist are not disclosed.
            result = OpResult(ok=True,
                              value=[g for g in result.value if g["id"] in allowed])

        return op.result_payload(result)

    # Build the explicit signature FastMCP introspects.
    parameters = []
    annotations = {}
    for wp in op.wire_params():
        py_type: Any = _JSON_TYPE_TO_PY[wp.json_type]
        if not wp.required and wp.default is None:
            py_type = Optional[py_type]
        annotation = (
            Annotated[py_type, Field(description=wp.description)]
            if wp.description else py_type
        )
        default = (
            inspect.Parameter.empty if wp.required and wp.default is None
            else wp.default
        )
        parameters.append(inspect.Parameter(
            wp.name, inspect.Parameter.KEYWORD_ONLY,
            annotation=annotation, default=default,
        ))
        annotations[wp.name] = annotation

    actor_annotation = Annotated[int, Field(
        description="Discord user id on whose behalf this call is made "
                    "(used for permission checks)."
    )]
    parameters.append(inspect.Parameter(
        "actor_id", inspect.Parameter.KEYWORD_ONLY, annotation=actor_annotation,
    ))
    annotations["actor_id"] = actor_annotation

    # Required params (no default) must precede optional ones in a Signature.
    parameters.sort(key=lambda p: p.default is not inspect.Parameter.empty)

    tool_fn.__name__ = op.name
    tool_fn.__doc__ = op.description
    tool_fn.__signature__ = inspect.Signature(parameters)  # type: ignore[attr-defined]
    tool_fn.__annotations__ = annotations
    return tool_fn


def build_server(bot: Any = None, *, allowed_guild_ids: Iterable[int],
                 name: str = "literallybot-ops") -> FastMCP:
    """Construct a FastMCP server whose tools are generated from the ops
    registry (`_EXPOSED_OPS`).

    `bot` should be a live discord.py Bot/Client instance (with `.config`
    attached, as bot.py does) so the tools can resolve channel/message ids
    and permission checks read the real config. If `bot` is None (schema-only
    smoke test), the tools raise BotUnavailableError when invoked.

    `allowed_guild_ids` is the server-side guild allowlist; every call is
    refused unless its resolved target belongs to one of these guilds.
    """
    allowed = frozenset(int(g) for g in allowed_guild_ids)
    if not allowed:
        raise ValueError(
            "build_server requires a non-empty guild allowlist "
            "(set MCP_OPS_GUILD_ALLOWLIST); refusing to build an "
            "unrestricted server."
        )

    # Which ops to expose is a global-config allowlist edited live from the
    # /ai settings panel (MCP tab). Unset => the full _EXPOSED_OPS universe
    # (back-compat default); an explicit [] exposes nothing. Read once at
    # build time — like MCP_OPS_GUILD_ALLOWLIST, changes take effect on the
    # next server (bot) restart. Names outside the _EXPOSED_OPS universe are
    # dropped: the MCP surface never grows past what this module vets.
    op_names = list(_EXPOSED_OPS)
    if bot is not None and getattr(bot, "config", None) is not None:
        configured = bot.config.get_global("mcp_tools_enabled")
        if configured is not None:
            op_names = [n for n in configured if n in _EXPOSED_OPS]

    mcp = FastMCP(name=name, instructions=(
        "Ops-registry bridge for literallybot. Exposes a subset of the "
        "bot's ops registry as MCP tools: " + ", ".join(op_names) + ". "
        "Every call is permission-checked the same way an in-bot command "
        "would be, via the shared ops registry, and is restricted to a "
        "server-side guild allowlist."
    ))

    for op_name in op_names:
        op = registry.require(op_name)  # raises on registry drift
        mcp.add_tool(_make_mcp_tool(bot, op, allowed),
                     name=op.name, description=op.description)

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
