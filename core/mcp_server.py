"""MCP frontend over the bot's ops registry ("world pattern").

One ops layer (core/ops.py), two frontends: the in-bot agent loop
(core/agent_loop.py) and this MCP server. Neither frontend re-implements
Discord call plumbing, id resolution, result serialization, or permission
checks — all of that lives in the registry. This module GENERATES its MCP
tools mechanically from each op's typed param declarations
(`Op.wire_params()`): the only hand-written pieces here are frontend
policy (actor construction from `actor_id`, forced allowed_mentions=none
on sends) and the auth/serving shell at the bottom of the file.

This module does NOT start anything on import — call build_server() to get a
configured FastMCP instance, or serve() to run it over authenticated
streamable HTTP. There is only ONE way to run it: in-process with the live
bot, via maybe_start_in_bot(bot), gated on the `mcp_ops_enabled` global
config boolean. (The former standalone runner logged a SECOND Discord
client into the same bot account and could not see cog-registered ops at
all; it was deleted in the ops-registry refactor rather than fixed.)

RESTART-BOUND SURFACE: the tool list is built once, when the server starts,
from a live registry query. FastMCP on mcp 1.x cannot reliably broadcast
`notifications/tools/list_changed` to already-connected streamable-HTTP
sessions, so growing/shrinking the surface mid-flight would leave clients
holding a stale tool list with no way to learn better. Ops registered later
by a cog load, and edits to the `mcp_tools_enabled` allowlist, therefore
take effect on the next bot restart — deliberately, not incidentally.

Guardrails (per the Codex review of the original spike, issue #58):
- Shared-token bearer auth is mandatory — serve() refuses to run without a
  token, and BearerTokenMiddleware refuses to construct without one.
- Binds to loopback ONLY. serve() hard-codes 127.0.0.1; there is no host
  parameter on purpose, and a non-loopback legacy MCP_OPS_HOST is a refusal
  to start, not a silent rebind.
- Guild reach is UNRESTRICTED by design (owner decision 2026-08): tools
  act as raw primitives across every guild the bot account is in; access
  control belongs upstream in the caller. The only guild confinement in
  the system is the in-bot !gpt agent loop's own {ctx.guild.id} policy.
  DM channels are still refused on id-based calls — DMs flow only through
  the user-keyed DM ops (send_dm/read_dms/fetch_dms/delete_dm/edit_dm/
  add_dm_reaction/remove_dm_reaction/list_dm_pins/list_dm_conversations),
  one-to-one with the DM API (keyed by user_id, never a channel id; Discord
  itself refuses bot DMs to users sharing no guild).
- send_message always sends with allowed_mentions=none — no pings, ever.
- search_history clamps `limit` to core.ops.HISTORY_LIMIT_MAX (200),
  declared on the op itself.
- ACCEPTED RISK: `actor_id` is caller-supplied and not credential-bound, so a
  client that already holds the bearer token can act as any user id for
  permission purposes. Acceptable for localhost self-use only; do not expose
  this server beyond loopback without adding real actor authentication.
"""
from __future__ import annotations

import asyncio
import hmac
import inspect
import logging
import os
import secrets
from typing import Annotated, Any, List, Optional

from pydantic import Field
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from core.ops import (
    Op,
    OpContext,
    ResolutionError,
    _as_int,
    registry,
    resolve_context_guild,
)

logger = logging.getLogger("core.mcp_server")

ENABLE_CONFIG_KEY = "mcp_ops_enabled"   # global config boolean, NOT env
TOKEN_CONFIG_KEY = "mcp_ops_token"      # global config string; env is fallback
PORT_CONFIG_KEY = "mcp_ops_port"        # global config int; env is fallback

TOKEN_ENV_VAR = "MCP_OPS_TOKEN"
PORT_ENV_VAR = "MCP_OPS_PORT"
HOST_ENV_VAR = "MCP_OPS_HOST"  # legacy; only loopback values are accepted

DEFAULT_PORT = 8765
_LOOPBACK_HOSTS = {"", "127.0.0.1", "localhost", "::1"}

# Fallback FastMCP server name when there's no live bot user to name it after
# (schema-only builds). The live name is derived from the bot account at build
# time — see _server_name() — so a downstream copy of this file identifies
# itself as ITS deployment, not as whichever bot it was written for.
DEFAULT_SERVER_NAME = "discord-ops"

# "array" always carries STRING items: both array wire kinds (CHANNEL_LIST
# snowflake ids, plain STRING_LIST) are string-itemed, so one mapping serves
# (see Op.to_json_schema). An array kind with NON-string items must grow an
# items-type facet on WireParam — and split this mapping with it.
_JSON_TYPE_TO_PY = {"integer": int, "string": str, "boolean": bool,
                    "array": List[str]}


def exposed_ops() -> List[str]:
    """The MCP surface's UNIVERSE — the ceiling of what `mcp_tools_enabled`
    may contain.

    The WHOLE ops registry, queried LIVE (never an import-time snapshot, so
    cog-registered ops are visible the moment their cog is loaded). Ops of
    every scope are exposed: an MCP caller is a host-side operator, not a
    guild member. ADMIN-tier ops (delete_message) are safe to expose because
    registry.call_ids checks the permission gate BEFORE resolving any ids,
    so permission failures never trigger target lookups.

    Caveat: THIS frontend pre-resolves the context guild/channel (tool_fn
    must build the actor Member from the target guild before the gate can
    run), so guild/channel id EXISTENCE is observable to any token-holder —
    accepted under the loopback + bearer-token trust model, same as
    caller-supplied actor_id.

    Gate what a deployment actually serves via the `mcp_tools_enabled`
    allowlist, not by editing this."""
    return registry.names()


def resolve_mcp_tools(config) -> List[str]:
    """Effective MCP tool set from global config (`mcp_tools_enabled`):
    unset => the full exposed_ops() universe (back-compat default); an
    explicit [] exposes nothing; names outside the universe are dropped —
    the MCP surface never grows past what the live registry holds. config=None
    (schema-only builds with no bot attached) => full universe. THE one
    owner of this rule — the server build and the !aisettings panel both
    resolve through it, so what the panel shows is what gets served.

    Dropping here is an effective-set filter only: a name stays in stored
    config even while its op is unregistered (its cog disabled), so
    re-enabling the cog restores the choice instead of silently losing it."""
    universe = exposed_ops()
    if config is None:
        return universe
    configured = config.get_global("mcp_tools_enabled")
    if configured is None:
        return universe
    live = set(universe)
    return [n for n in configured if n in live]


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


def _make_mcp_tool(bot: Any, op: Op):
    """Generate one MCP tool function from an op's typed declaration.

    The returned coroutine has an explicit `__signature__` built from the
    op's wire params (plus the MCP-frontend `actor_id`), so FastMCP derives
    the same JSON schema the registry declares.
    """

    async def tool_fn(**raw) -> dict:
        # Identity belt: dispatch resolves by NAME but this tool's schema was
        # built from `op` at server start; inert while the cog set is fixed
        # at boot (#86).
        if registry.get(op.name) is not op:
            raise BotUnavailableError(
                f"Op '{op.name}' was re-registered after this MCP surface "
                "was built. Restart the bot to rebuild the tool surface.")
        live_bot = _require_bot(bot)
        # Snowflake-as-string on the wire (see _SNOWFLAKE_JSON_TYPE in
        # core/ops.py); coerce before it reaches guild.get_member().
        actor_id = _as_int(raw.pop("actor_id"), "actor_id")

        # Resolve the target guild first (raises on unknown ids — surfaced
        # as MCP tool errors) so the actor can be built as a real Member of
        # that guild. Guild-less calls (e.g. list_guilds) stand on the op's
        # own behavior; ops that need a guild say so in their error.
        try:
            guild = await resolve_context_guild(live_bot, raw, None)
        except ResolutionError as exc:
            raise BotUnavailableError(str(exc)) from exc

        ctx = _build_context(live_bot, actor_id, guild)

        # send_message never pings: enforced by the op itself (see
        # core/ops.py send_message — never-ping is the registry default).
        # allowed_guild_ids stays at its None default: this frontend is
        # unconfined primitives; access control is the caller's job.
        result = await registry.call_ids(op.name, ctx, **raw)

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

    actor_annotation = Annotated[str, Field(
        description="Discord user id on whose behalf this call is made "
                    "(used for permission checks). Decimal string."
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


def _server_name(bot: Any) -> str:
    """Name the FastMCP server after the live bot account.

    Deployment-neutral on purpose: this file gets copied downstream, and a
    hard-coded product name would then be a lie in the client's server list.
    Derived from `bot.user` when there is one (the bot is logged in by the
    time maybe_start_in_bot runs), else DEFAULT_SERVER_NAME."""
    user = getattr(bot, "user", None) if bot is not None else None
    if user is None:
        return DEFAULT_SERVER_NAME
    raw = getattr(user, "name", None) or str(user)
    slug = "".join(c if (c.isalnum() or c in "-_") else "-"
                   for c in raw.strip().lower()).strip("-")
    return f"{slug}-ops" if slug else DEFAULT_SERVER_NAME


def build_server(bot: Any = None, *, name: Optional[str] = None) -> FastMCP:
    """Construct a FastMCP server whose tools are generated from the ops
    registry. Tools act as raw primitives across every guild the bot account
    is in — host-side MCP callers have full control.

    `bot` should be a live discord.py Bot/Client instance (with `.config`
    attached, as bot.py does) so the tools can resolve channel/message ids
    and permission checks read the real config. If `bot` is None (schema-only
    smoke test), the tools raise BotUnavailableError when invoked.

    The universe is queried LIVE here, but only HERE: see the module
    docstring on why the built surface is restart-bound afterwards.
    """
    # Which ops to expose is a global-config list edited live from the
    # !aisettings panel (MCP tab). Read once at build time — changes take
    # effect on the next server (bot) restart.
    op_names = resolve_mcp_tools(
        getattr(bot, "config", None) if bot is not None else None)

    server_name = name or _server_name(bot)
    mcp = FastMCP(name=server_name, instructions=(
        f"Ops-registry bridge for the Discord bot '{server_name}'. Exposes a "
        "subset of the bot's ops registry as MCP tools: "
        + ", ".join(op_names) + ". "
        "Every call is permission-checked the same way an in-bot command "
        "would be, via the shared ops registry. Tools are raw primitives: "
        "every guild the bot is in is reachable."
    ))

    for op_name in op_names:
        op = registry.require(op_name)  # raises on registry drift
        mcp.add_tool(_make_mcp_tool(bot, op),
                     name=op.name, description=op.description)

    return mcp


# --------------------------------------------------------------------------
# Auth
#
# Deliberately NOT the MCP SDK's full OAuth `AuthSettings` / `TokenVerifier`
# machinery (that expects a standing OAuth issuer, which is overkill for a
# single shared secret on loopback). Instead: a small ASGI middleware that
# checks a static bearer token before any request reaches the FastMCP app.
#
# - OFF by default: maybe_start_in_bot refuses to start unless the
#   `mcp_ops_enabled` global config bool is set.
# - Auth required: every request must carry `Authorization: Bearer <token>`.
#   No token anywhere => the server refuses to start (fail closed).
# - No token comparison shortcuts: hmac.compare_digest, so the comparison
#   leaks no timing side-channel.
# --------------------------------------------------------------------------


class BearerTokenMiddleware(BaseHTTPMiddleware):
    """Rejects any request that doesn't carry the configured bearer token."""

    def __init__(self, app, token: str):
        super().__init__(app)
        if not token:
            raise ValueError(
                f"BearerTokenMiddleware requires a non-empty token "
                f"(global config '{TOKEN_CONFIG_KEY}' or {TOKEN_ENV_VAR})."
            )
        self._token = token

    async def dispatch(self, request: Request, call_next):
        header = request.headers.get("authorization", "")
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer" or not presented:
            return JSONResponse(
                {"error": "missing or malformed Authorization header; expected 'Bearer <token>'"},
                status_code=401,
            )
        if not hmac.compare_digest(presented, self._token):
            return JSONResponse({"error": "invalid token"}, status_code=401)
        return await call_next(request)


def wrap_with_auth(app: Starlette, token: str) -> Starlette:
    """Wrap a Starlette app so every request must present the bearer token."""
    app.add_middleware(BearerTokenMiddleware, token=token)
    return app


# --------------------------------------------------------------------------
# Settings + startup
# --------------------------------------------------------------------------


def is_enabled(config: Any) -> bool:
    """The on/off switch lives in global config (surfaced in !aisettings ->
    MCP tab) so it's operable without shell access. Read at startup; a
    toggle takes effect on the next bot restart."""
    return bool(config.get_global(ENABLE_CONFIG_KEY, False))


def load_token(config: Any) -> str:
    """Resolve the bearer token, config-first with env fallback (the same
    pattern as the `<PROVIDER>_API_KEY` settings): global config
    `mcp_ops_token`, else the MCP_OPS_TOKEN env var.

    If neither exists, GENERATE one (secrets.token_urlsafe(32)) and persist
    it to global config. Rationale: the server is already gated OFF by
    `mcp_ops_enabled`, so an operator who turned it on has asked for a
    running server — failing closed on a missing secret they have no UI to
    set just strands them. Generating is fail-closed too: the server is
    never unauthenticated, it simply has a secret only the host filesystem
    knows. The token VALUE is never logged; only where to find it."""
    token = (config.get_global(TOKEN_CONFIG_KEY) or "").strip()
    if token:
        return token
    token = os.environ.get(TOKEN_ENV_VAR, "").strip()
    if token:
        return token
    token = secrets.token_urlsafe(32)
    config.set_global(TOKEN_CONFIG_KEY, token)
    logger.warning(
        "No MCP ops auth token was configured; generated one and stored it "
        "in global config under '%s' (configs/global.json). Read it from "
        "there to connect a client. The value is deliberately not logged.",
        TOKEN_CONFIG_KEY,
    )
    return token


def load_port(config: Any) -> int:
    """Port, config-first with env fallback: global config `mcp_ops_port`,
    else MCP_OPS_PORT, else DEFAULT_PORT. Raises RuntimeError on a value
    that isn't a usable port number (fail closed rather than bind somewhere
    surprising)."""
    raw = config.get_global(PORT_CONFIG_KEY)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raw = os.environ.get(PORT_ENV_VAR, "").strip() or DEFAULT_PORT
    try:
        port = int(raw)
    except (TypeError, ValueError):
        raise RuntimeError(
            f"MCP ops port {raw!r} is not an integer (global config "
            f"'{PORT_CONFIG_KEY}' / {PORT_ENV_VAR})."
        ) from None
    if not (1 <= port <= 65535):
        raise RuntimeError(f"MCP ops port {port} is out of range (1-65535).")
    return port


def _check_host_env() -> None:
    """This server binds to 127.0.0.1 ONLY. If the legacy MCP_OPS_HOST var
    is set to anything non-loopback, refuse to start rather than let an
    operator believe they rebound it."""
    host = os.environ.get(HOST_ENV_VAR, "").strip()
    if host not in _LOOPBACK_HOSTS:
        raise RuntimeError(
            f"{HOST_ENV_VAR}={host!r} is not loopback. This server binds to "
            f"127.0.0.1 ONLY; unset {HOST_ENV_VAR}."
        )


def _load_settings(config: Any) -> "tuple[str, int]":
    """Resolve and validate token/port. Raises RuntimeError on any invalid
    gate (fail closed)."""
    _check_host_env()
    return load_token(config), load_port(config)


async def serve(bot: Any, *, port: int, token: str,
                name: Optional[str] = None) -> None:
    """Serve the ops MCP server over authenticated streamable HTTP, bound to
    127.0.0.1 ONLY (no host parameter on purpose — do not add one).

    Runs until cancelled. Sole caller: maybe_start_in_bot (in-process, gated
    on the `mcp_ops_enabled` global config boolean).
    """
    import contextlib

    import uvicorn

    if not token:
        raise ValueError(
            f"serve() requires a non-empty auth token (global config "
            f"'{TOKEN_CONFIG_KEY}' or {TOKEN_ENV_VAR})."
        )

    class _NoSignalCaptureServer(uvicorn.Server):
        """uvicorn.Server.serve() normally takes over SIGINT/SIGTERM in the
        main thread; when embedded in the bot process that would fight
        discord.py's own shutdown, so signal handling is left to the host."""

        @contextlib.contextmanager
        def capture_signals(self):
            yield

    mcp = build_server(bot=bot, name=name)
    app = wrap_with_auth(mcp.streamable_http_app(), token)

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="info")
    server = _NoSignalCaptureServer(config)
    logger.warning(
        "Starting MCP ops server on 127.0.0.1:%s — auth REQUIRED (Bearer "
        "token), loopback-only bind, unrestricted guild reach. Every tool "
        "call runs as a live, authenticated Discord bot action.",
        port,
    )
    await server.serve()


def maybe_start_in_bot(bot: Any) -> Optional[asyncio.Task]:
    """Called by bot.py once the bot is ready. Starts the MCP ops server as
    a background task on the bot's event loop IF the `mcp_ops_enabled`
    global config boolean is set and all fail-closed gates pass; returns
    None (and changes nothing) otherwise.
    """
    if not is_enabled(bot.config):
        return None
    try:
        token, port = _load_settings(bot.config)
    except RuntimeError as exc:
        logger.error("MCP ops server NOT started: %s", exc)
        return None
    task = asyncio.get_running_loop().create_task(
        serve(bot, port=port, token=token),
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
