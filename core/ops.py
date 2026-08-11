"""Decorator-based tool/ops registry wrapping atomic Discord actions.

The "world pattern": one ops layer, many frontends. Every atomic Discord
action (send a message, add a role, ...) is registered here exactly once,
with its permission requirement declared alongside it. Any frontend — an
in-bot agent loop, a slash-command cog, an MCP server — calls into the same
`registry.call(op_name, ctx, **kwargs)` and gets the same permission
enforcement and the same error shape back. No frontend re-implements
Discord call plumbing or permission checks.

Ops are self-describing: each op declares its parameters as typed
`OpParam`s (Discord entities are ids on the wire — channel_id, message_id,
user_id, role_id, guild_id — plain JSON types otherwise). From that single
declaration the registry mechanically generates:

- a JSON schema per op (`Op.to_json_schema()`), consumable by any
  tool-calling frontend (MCP tool listing, pydantic-ai tool spec, ...);
- id-based invocation (`registry.call_ids(op, ctx, **raw)`) with a shared
  cache-then-fetch resolver (get_channel -> fetch_channel,
  guild.get_member -> fetch_member, message via its channel) and guild
  confinement — every id-resolved target must belong to an allowed guild;
- a JSON-safe result shape (`Op.serialize_result(value)`) so every
  frontend returns identical payloads for the same op.

Object-based callers keep using `registry.call(...)` with live discord.py
objects and pay no re-fetches; id-based frontends use `call_ids(...)`.

Each op also carries the metadata frontends need to DERIVE their surfaces
instead of hand-listing them: `scope` (GUILD/DM/GLOBAL — the in-guild agent
universe is exactly the guild-scoped ops), `group` (which section a panel
renders it under), and `origin` ('core' for the registrations below, 'cog'
for ops a cog contributed). Origin is stamped by the registration PATH and
is never a decorator argument, so a cog cannot claim to be core.

There is deliberately no code-level "agent" subset flag. Frontends query
the registry LIVE (`guild_agent_names()`, `ops(...)`, `grouped(...)`) rather
than freezing a module-level tuple at import: cog ops come and go with cog
load/unload, so any import-time snapshot is stale after the first reload.

This module is frontend-agnostic on purpose: it does not import
`discord.ext.commands`, and it does not get wired into `bot.py`. Cogs are
known only as opaque owner objects — a cog declares ops with the
module-level `op(...)` decorator, and `bot.py` calls
`registry.register_cog_ops(cog)` / `unregister_owner(cog)` around the
discord.py cog lifecycle. It only knows how to run an op against an
`OpContext`.

Permission gates route through `core.utils.is_admin` / `is_superadmin`,
the same helpers `cogs/optional/cleanup.py` and `cogs/optional/setrole.py`
already use via `@commands.check(...)`. Those helpers expect a duck-typed
ctx with `.author`, `.guild`, and `.bot.config` — `OpContext` below mirrors
that shape so the existing helpers work unmodified.

Frontends: core/mcp_server.py (MCP) and core/agent_loop.py (in-bot agent
loop) both generate their tool surfaces from this registry.

`python3 -m core.ops` prints the offline JSON schemas for every registered
op (no bot, no Discord connection). The registry's invariants are covered
by tests/test_ops_registry.py.
"""
from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import discord

from core.dm_log import load_dms, log_dm, row_from_message
from core.utils import is_admin, is_superadmin

# Shared history-scan cap: search_history never scans more than this many
# messages regardless of the requested limit (silently clamped, matching
# the original MCP frontend's behavior).
HISTORY_LIMIT_MAX = 200

# Discord upload hard cap (bytes). Free-tier bots are fine under 10MB for
# typical image drops; reject above 25MB so we fail closed before the API.
ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024
ATTACHMENT_EXTENSIONS = frozenset({
    ".gif", ".png", ".jpg", ".jpeg", ".webp", ".mp4", ".webm",
    ".mov", ".pdf", ".txt", ".opus", ".ogg", ".mp3", ".wav",
})


class PermissionLevel(IntEnum):
    """Ordered so a numeric comparison also makes sense, but permission
    checks below dispatch by exact level rather than relying on ordering."""
    EVERYONE = 0
    ADMIN = 1
    SUPERADMIN = 2


class OpScope(str, Enum):
    """WHERE an op acts, which is what makes a frontend's op universe
    derivable instead of hand-listed.

    GUILD  — acts on/inside a guild (channels, members, roles, emoji). The
             in-guild agent surface is exactly this set, queried LIVE.
    DM     — acts on a one-to-one DM conversation; no guild involved.
    GLOBAL — acts on the bot itself across guilds (e.g. list_guilds).
    """
    GUILD = "guild"
    DM = "dm"
    GLOBAL = "global"


# Op groups: a stable kebab-case id -> human display label. The id is what
# code and (eventually) stored config speak; the label is presentation only,
# so relabeling never breaks a lookup. Frontends render one select/section
# per group, so each group must stay under Discord's 25-option select cap.
OP_GROUPS: Dict[str, str] = {
    "messaging": "Messaging",
    "roles": "Roles",
    "emojis": "Emojis",
    "guild-info": "Guild info",
    "dm": "Direct messages",
    "guild": "Guild",
    # Cog-provided groups. Declared here (not invented ad hoc by the cog) so
    # a panel section keeps a stable order and display label whether or not
    # the owning cog happens to be loaded.
    "role-automation": "Role automation",
    "integrations": "Integrations",
    "auto-response": "Auto-responses",   # auto_response.py
    "media": "Media library",            # media.py
}

# Where an op came from. Assigned by the REGISTRATION PATH, never accepted
# as a decorator argument — a cog cannot claim to be core.
ORIGIN_CORE = "core"
ORIGIN_COG = "cog"


class ParamKind(str, Enum):
    """What an op parameter IS, driving both schema generation and id
    resolution. Discord entities travel as ids on the wire and are resolved
    to live objects before the op impl runs; scalars pass through."""
    CHANNEL = "channel"    # wire: channel_id (str) -> discord channel object
    CHANNEL_LIST = "channel_list"  # wire: channel_ids (str array) -> list of channels
    STRING_LIST = "string_list"    # wire: <name> (str array) -> list of strings
    MESSAGE = "message"    # wire: channel_id + message_id (str) -> discord.Message
    MEMBER = "member"      # wire: user_id (str) -> discord.Member (of ctx.guild)
    USER = "user"          # wire: user_id (str) -> discord.User (guild-independent)
    ROLE = "role"          # wire: role_id (str) -> discord.Role (of ctx.guild)
    GUILD = "guild"        # wire: guild_id (str) -> discord.Guild
    STRING = "string"
    INTEGER = "integer"
    SNOWFLAKE = "snowflake"  # a Discord id used as a scalar (filter/reference),
                             # NOT resolved to an object; wire type is string
                             # for the same 2**53 reason as entity ids.
    BOOLEAN = "boolean"
    INTERNAL = "internal"  # never on the wire; frontends may pass a live object


# Discord snowflakes are 64-bit and routinely exceed 2**53, the largest
# integer a JSON/JavaScript double represents exactly. Declared as "integer"
# they silently round in transit (1208839321801465886 -> ...900) and resolve
# to the wrong entity or fail outright, so they travel as decimal STRINGS.
# Every consumer already funnels ids through _as_int(), which accepts both —
# do not "simplify" this back to "integer". Genuine scalar ints (limit,
# position) stay ParamKind.INTEGER: they're small and unaffected.
_SNOWFLAKE_JSON_TYPE = "string"

# JSON-schema type for each scalar kind.
_SCALAR_JSON_TYPES = {
    ParamKind.STRING: "string",
    ParamKind.INTEGER: "integer",
    ParamKind.SNOWFLAKE: _SNOWFLAKE_JSON_TYPE,
    ParamKind.BOOLEAN: "boolean",
}

# Wire name for each entity kind (MESSAGE also implies channel_id; see
# Op.wire_params()).
_ENTITY_WIRE_NAMES = {
    ParamKind.CHANNEL: "channel_id",
    ParamKind.MESSAGE: "message_id",
    ParamKind.MEMBER: "user_id",
    ParamKind.USER: "user_id",
    ParamKind.ROLE: "role_id",
    ParamKind.GUILD: "guild_id",
}


@dataclass(frozen=True)
class OpParam:
    """Typed declaration of one op parameter.

    `name` is the impl-side keyword (what the `async def impl(ctx, ...)`
    receives); the wire-side name is derived from `kind` for Discord
    entities (channel_id/message_id/user_id/role_id/guild_id) and equals
    `name` for scalars. `minimum`/`maximum` clamp integer values silently.
    """
    name: str
    kind: ParamKind
    description: str = ""
    required: bool = True
    default: Any = None
    minimum: Optional[int] = None
    maximum: Optional[int] = None


@dataclass
class WireParam:
    """One generated wire-level (JSON) parameter."""
    name: str
    json_type: str
    description: str
    required: bool
    default: Any = None
    minimum: Optional[int] = None
    maximum: Optional[int] = None


class ResolutionError(RuntimeError):
    """An id-based call could not be resolved to live Discord objects
    (missing/unknown id, bad value, or a target outside the allowed
    guilds). Frontends surface this as a tool error, not a crash."""


class GuildNotAllowedError(ResolutionError):
    """The resolved target violates the caller's guild confinement (a
    frontend-supplied allowed_guild_ids set), or is a guild-less DM target
    on an id-based call."""


@dataclass
class OpContext:
    """Minimal actor/target context an op needs to run.

    Duck-types the subset of `discord.ext.commands.Context` that
    `core.utils.is_admin` / `is_superadmin` and the op implementations
    below actually touch: `.bot` (with `.config`), `.author`, and
    `.guild`. A real `commands.Context` satisfies this directly —
    pass it straight through. A non-cog frontend (agent loop, MCP server)
    builds one of these from whatever ids/objects it has on hand.
    """
    bot: Any
    author: Any
    guild: Optional[Any] = None


@dataclass
class OpResult:
    """Uniform result shape every op returns — frontends branch on `.ok`
    rather than catching exceptions, so a failed permission check and a
    failed Discord API call look the same to a caller."""
    ok: bool
    value: Any = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Shared resolvers: cache-then-fetch, with guild confinement. Lifted out of
# core/mcp_server.py so every id-based frontend resolves identically.
# ---------------------------------------------------------------------------

def check_guild_allowed(guild: Any, allowed_guild_ids: Optional[frozenset],
                        what: str) -> None:
    """Guild confinement is CALLER policy: `allowed_guild_ids=None` means no
    confinement — the target may live in any guild the bot is in (the MCP
    frontend: primitives, access control upstream). A frozenset confines to
    those guilds (the in-bot agent loop passes exactly {ctx.guild.id})."""
    if guild is None:
        raise GuildNotAllowedError(
            f"{what} has no guild (DMs are not allowed through id-based calls)."
        )
    if allowed_guild_ids is not None and guild.id not in allowed_guild_ids:
        raise GuildNotAllowedError(
            f"{what} belongs to guild {guild.id}, which is not in the "
            f"caller's allowed guild set."
        )


async def resolve_channel(bot: Any, channel_id: int,
                          allowed_guild_ids: Optional[frozenset]) -> Any:
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller as a tool error
            raise ResolutionError(f"Could not resolve channel {channel_id}: {exc}") from exc
    check_guild_allowed(getattr(channel, "guild", None), allowed_guild_ids,
                        f"Channel {channel_id}")
    return channel


async def fetch_message_in(channel: Any, message_id: int) -> Any:
    try:
        return await channel.fetch_message(message_id)
    except Exception as exc:  # noqa: BLE001
        raise ResolutionError(
            f"Could not resolve message {message_id} in channel "
            f"{getattr(channel, 'id', '?')}: {exc}"
        ) from exc


async def resolve_user(bot: Any, user_id: int) -> Any:
    """Resolve a user id to a discord.User, guild-independent (cache then
    GET /users/{user_id}). Used by the DM ops: DMs are user-keyed at the
    API level, and Discord itself refuses bot DMs to users who share no
    guild — no membership pre-check needed here."""
    user = bot.get_user(user_id)
    if user is None:
        try:
            user = await bot.fetch_user(user_id)
        except Exception as exc:  # noqa: BLE001
            raise ResolutionError(
                f"Could not resolve user {user_id}: {exc}") from exc
    return user


async def resolve_member(guild: Any, user_id: int) -> Any:
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except Exception as exc:  # noqa: BLE001
            raise ResolutionError(
                f"Could not resolve member {user_id} in guild {guild.id}: {exc}"
            ) from exc
    return member


def resolve_role(guild: Any, role_id: int) -> Any:
    role = guild.get_role(role_id)
    if role is None:
        raise ResolutionError(f"Could not resolve role {role_id} in guild {guild.id}.")
    return role


def resolve_guild(bot: Any, guild_id: int,
                  allowed_guild_ids: Optional[frozenset]) -> Any:
    guild = bot.get_guild(guild_id)
    if guild is None:
        raise ResolutionError(f"Could not resolve guild {guild_id}.")
    check_guild_allowed(guild, allowed_guild_ids, f"Guild {guild_id}")
    return guild


async def resolve_context_guild(bot: Any, raw: Dict[str, Any],
                                allowed_guild_ids: Optional[frozenset],
                                ) -> Optional[Any]:
    """Resolve the guild an id-based call targets, from its raw wire params,
    BEFORE building an OpContext (frontends that construct their actor from
    the target guild — e.g. the MCP server — need this first). Returns None
    for ops with no guild-bound target (e.g. list_guilds)."""
    if raw.get("channel_id") is not None:
        channel = await resolve_channel(bot, _as_int(raw["channel_id"], "channel_id"),
                                        allowed_guild_ids)
        return channel.guild
    channel_ids = raw.get("channel_ids")
    if channel_ids:
        first = channel_ids[0] if isinstance(channel_ids, (list, tuple)) else channel_ids
        channel = await resolve_channel(bot, _as_int(first, "channel_ids"),
                                        allowed_guild_ids)
        return channel.guild
    if raw.get("guild_id") is not None:
        return resolve_guild(bot, _as_int(raw["guild_id"], "guild_id"), allowed_guild_ids)
    return None


def _as_int(value: Any, wire_name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ResolutionError(f"Parameter '{wire_name}' must be an integer id, got {value!r}.") from exc


# ---------------------------------------------------------------------------
# Shared result serializers — one JSON shape per Discord object, used by
# every frontend so identical ops return identical payloads.
# ---------------------------------------------------------------------------

def serialize_message(message: Any) -> Dict[str, Any]:
    return {
        "id": message.id,
        "channel_id": message.channel.id,
        "author_id": message.author.id,
        "content": message.content,
        "created_at": message.created_at.isoformat() if getattr(message, "created_at", None) else None,
    }


@dataclass
class Op:
    name: str
    description: str
    permission: PermissionLevel
    impl: Callable[..., Any]
    params: List[OpParam] = field(default_factory=list)
    serialize: Optional[Callable[[Any], Dict[str, Any]]] = None
    # WHERE this op acts. The in-guild agent universe is derived from this
    # (scope == GUILD), queried live — there is deliberately NO code-level
    # "agent" subset flag, because every such flag drifted from the surface
    # it claimed to describe.
    scope: OpScope = OpScope.GUILD
    # Which group this op renders under in a frontend's grouped listing.
    # Kebab-case key into OP_GROUPS.
    group: str = "messaging"
    # 'core' for ops registered inline in this module, 'cog' for ops a cog
    # contributed via register_cog_ops. Stamped by the registration path,
    # never passed in by the op author.
    origin: str = ORIGIN_CORE
    # The cog instance that owns a 'cog'-origin op, so unregister_owner can
    # remove exactly that cog's batch on unload. None for core ops.
    owner: Any = None
    # Behavioral guidance injected into the agent system prompt when this op
    # is on a guild's enabled-tool list (see gpt.py build_agentic_guidance).
    # Lives here, not in the prompt builder, so guidance travels with the op
    # and can't drift out of sync with the enabled-tool set. Distinct from
    # `description`, which rides inside the function schema itself.
    agent_guidance: Optional[str] = None

    async def __call__(self, ctx: OpContext, **kwargs) -> OpResult:
        allowed, reason = _check_permission(ctx, self.permission)
        if not allowed:
            return OpResult(ok=False, error=reason)
        vis_ok, vis_reason = _check_channel_visibility(ctx, kwargs)
        if not vis_ok:
            return OpResult(ok=False, error=vis_reason)
        try:
            value = await self.impl(ctx, **kwargs)
        except Exception as exc:  # noqa: BLE001 - ops surface failure, not raise
            return OpResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        return OpResult(ok=True, value=value)

    # -- schema generation ------------------------------------------------

    def wire_params(self) -> List[WireParam]:
        """Expand the typed param declarations into flat wire-level (JSON)
        parameters. MESSAGE params imply a channel_id; if the op also
        declares a CHANNEL param the two share one channel_id."""
        wire: List[WireParam] = []
        seen = set()

        def add(wp: WireParam):
            if wp.name in seen:
                return
            seen.add(wp.name)
            wire.append(wp)

        for p in self.params:
            if p.kind == ParamKind.INTERNAL:
                continue
            if p.kind == ParamKind.MESSAGE:
                add(WireParam("channel_id", _SNOWFLAKE_JSON_TYPE,
                              f"Discord channel id containing the {p.name}.", True))
                add(WireParam("message_id", _SNOWFLAKE_JSON_TYPE,
                              p.description or f"Discord message id of the {p.name}.",
                              p.required, p.default))
            elif p.kind == ParamKind.CHANNEL_LIST:
                add(WireParam("channel_ids", "array",
                              p.description or "List of Discord channel ids.",
                              p.required, p.default))
            elif p.kind == ParamKind.STRING_LIST:
                add(WireParam(p.name, "array", p.description,
                              p.required, p.default))
            elif p.kind in _ENTITY_WIRE_NAMES:
                add(WireParam(_ENTITY_WIRE_NAMES[p.kind], _SNOWFLAKE_JSON_TYPE,
                              p.description or f"Discord {p.kind.value} id.",
                              p.required, p.default))
            else:
                add(WireParam(p.name, _SCALAR_JSON_TYPES[p.kind], p.description,
                              p.required, p.default, p.minimum, p.maximum))
        return wire

    def to_json_schema(self) -> Dict[str, Any]:
        """JSON schema for this op's wire params — the mechanical source of
        both MCP tool schemas and pydantic-ai tool signatures."""
        properties: Dict[str, Any] = {}
        required: List[str] = []
        for wp in self.wire_params():
            prop: Dict[str, Any] = {"type": wp.json_type}
            if wp.json_type == "array":
                # Both array wire kinds (CHANNEL_LIST snowflake ids, plain
                # STRING_LIST) carry string items, so one items type serves —
                # and core/mcp_server.py's _JSON_TYPE_TO_PY "array" -> List[str]
                # stays a single mapping. An array kind with NON-string items
                # must grow an items-type facet on WireParam.
                prop["items"] = {"type": "string"}
            if wp.description:
                prop["description"] = wp.description
            if wp.default is not None:
                prop["default"] = wp.default
            if wp.minimum is not None:
                prop["minimum"] = wp.minimum
            if wp.maximum is not None:
                prop["maximum"] = wp.maximum
            properties[wp.name] = prop
            if wp.required:
                required.append(wp.name)
        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }

    def to_schema(self) -> Dict[str, Any]:
        """A frontend-agnostic description of this op — enough for an MCP
        tool listing or an agent-loop tool spec without importing discord.py."""
        return {
            "name": self.name,
            "description": self.description,
            "permission": self.permission.name,
            "scope": self.scope.value,
            "group": self.group,
            "origin": self.origin,
            "params": self.to_json_schema(),
        }

    # -- id resolution ----------------------------------------------------

    async def resolve_kwargs(self, bot: Any, guild: Optional[Any], raw: Dict[str, Any],
                             allowed_guild_ids: Optional[frozenset]) -> Dict[str, Any]:
        """Resolve raw wire params (ids + scalars) into impl kwargs.
        Raises ResolutionError on any missing/unknown/out-of-guild target."""
        raw = dict(raw)
        kwargs: Dict[str, Any] = {}
        resolved_channel: Any = None

        # Consume in declaration order so CHANNEL resolves before MESSAGE.
        for p in self.params:
            if p.kind == ParamKind.INTERNAL:
                if p.name in raw:
                    kwargs[p.name] = raw.pop(p.name)
                continue

            if p.kind == ParamKind.CHANNEL:
                channel_id = raw.pop("channel_id", None)
                if channel_id is None:
                    if p.required:
                        raise ResolutionError(f"Missing required parameter 'channel_id' for op '{self.name}'.")
                    kwargs[p.name] = None
                    continue
                resolved_channel = await resolve_channel(
                    bot, _as_int(channel_id, "channel_id"), allowed_guild_ids)
                kwargs[p.name] = resolved_channel

            elif p.kind == ParamKind.CHANNEL_LIST:
                channel_ids = raw.pop("channel_ids", None)
                if channel_ids is None:
                    if p.required:
                        raise ResolutionError(f"Missing required parameter 'channel_ids' for op '{self.name}'.")
                    kwargs[p.name] = None
                    continue
                if not isinstance(channel_ids, (list, tuple)):
                    channel_ids = [channel_ids]
                kwargs[p.name] = [
                    await resolve_channel(bot, _as_int(cid, "channel_ids"),
                                          allowed_guild_ids)
                    for cid in channel_ids
                ]

            elif p.kind == ParamKind.STRING_LIST:
                values = raw.pop(p.name, None)
                if values is None:
                    if p.required:
                        raise ResolutionError(f"Missing required parameter '{p.name}' for op '{self.name}'.")
                    kwargs[p.name] = None
                    continue
                if not isinstance(values, (list, tuple)):
                    values = [values]
                kwargs[p.name] = [str(v) for v in values]

            elif p.kind == ParamKind.MESSAGE:
                message_id = raw.pop("message_id", None)
                channel_id = raw.pop("channel_id", None)
                if message_id is None:
                    if p.required:
                        raise ResolutionError(f"Missing required parameter 'message_id' for op '{self.name}'.")
                    continue
                channel = resolved_channel
                if channel is None:
                    if channel_id is None:
                        raise ResolutionError(f"Missing required parameter 'channel_id' for op '{self.name}'.")
                    channel = await resolve_channel(
                        bot, _as_int(channel_id, "channel_id"), allowed_guild_ids)
                kwargs[p.name] = await fetch_message_in(channel, _as_int(message_id, "message_id"))

            elif p.kind == ParamKind.MEMBER:
                user_id = raw.pop("user_id", None)
                if user_id is None:
                    if p.required:
                        raise ResolutionError(f"Missing required parameter 'user_id' for op '{self.name}'.")
                    continue
                if guild is None:
                    raise ResolutionError(f"Op '{self.name}' requires a guild context to resolve members.")
                kwargs[p.name] = await resolve_member(guild, _as_int(user_id, "user_id"))

            elif p.kind == ParamKind.USER:
                user_id = raw.pop("user_id", None)
                if user_id is None:
                    if p.required:
                        raise ResolutionError(f"Missing required parameter 'user_id' for op '{self.name}'.")
                    kwargs[p.name] = None
                    continue
                kwargs[p.name] = await resolve_user(bot, _as_int(user_id, "user_id"))

            elif p.kind == ParamKind.ROLE:
                role_id = raw.pop("role_id", None)
                if role_id is None:
                    if p.required:
                        raise ResolutionError(f"Missing required parameter 'role_id' for op '{self.name}'.")
                    continue
                if guild is None:
                    raise ResolutionError(f"Op '{self.name}' requires a guild context to resolve roles.")
                kwargs[p.name] = resolve_role(guild, _as_int(role_id, "role_id"))

            elif p.kind == ParamKind.GUILD:
                guild_id = raw.pop("guild_id", None)
                if guild_id is None:
                    if p.required:
                        raise ResolutionError(f"Missing required parameter 'guild_id' for op '{self.name}'.")
                    kwargs[p.name] = None
                    continue
                kwargs[p.name] = resolve_guild(bot, _as_int(guild_id, "guild_id"), allowed_guild_ids)
                # An explicit guild_id also becomes the resolution context
                # for MEMBER/ROLE params declared after it — the API calls
                # behind those ops are guild-keyed, so the wire guild wins
                # over any ambient ctx.guild.
                guild = kwargs[p.name]

            else:  # scalar
                if p.name not in raw:
                    if p.required and p.default is None:
                        raise ResolutionError(f"Missing required parameter '{p.name}' for op '{self.name}'.")
                    if p.default is not None:
                        kwargs[p.name] = p.default
                    continue
                value = raw.pop(p.name)
                if value is None:
                    continue
                if p.kind in (ParamKind.INTEGER, ParamKind.SNOWFLAKE):
                    # SNOWFLAKE arrives as a decimal string (see
                    # _SNOWFLAKE_JSON_TYPE); both become real ints here, so
                    # op impls receive an int either way and never have to
                    # remember to coerce their own id scalars. Clamping
                    # applies to INTEGER only — ids have no meaningful range.
                    value = _as_int(value, p.name)
                    if p.kind == ParamKind.INTEGER:
                        if p.minimum is not None:
                            value = max(p.minimum, value)
                        if p.maximum is not None:
                            value = min(p.maximum, value)
                kwargs[p.name] = value

        if raw:
            raise ResolutionError(
                f"Unexpected parameter(s) for op '{self.name}': {sorted(raw)}. "
                f"Expected: {[wp.name for wp in self.wire_params()]}."
            )
        return kwargs

    # -- result serialization ----------------------------------------------

    def serialize_result(self, value: Any) -> Dict[str, Any]:
        """JSON-safe payload for an op's return value; identical across
        frontends. Ops without a registered serializer return {}."""
        if self.serialize is None:
            return {}
        return self.serialize(value)

    def result_payload(self, result: OpResult) -> Dict[str, Any]:
        """The uniform {"ok": ...} wire envelope every tool-calling frontend
        returns for this op — one place, so payload shape can't drift."""
        if not result.ok:
            return {"ok": False, "error": result.error}
        return {"ok": True, **self.serialize_result(result.value)}


def _check_channel_visibility(ctx: OpContext, kwargs: Dict[str, Any]) -> "tuple[bool, Optional[str]]":
    """When the actor is a real guild Member, refuse ops whose resolved target
    channel the actor cannot read — otherwise the bot (which sees more channels
    than any single user) would leak history/members/presence from channels the
    caller can't see, or post into them. A bare id-holder actor (MCP fallback)
    has no reliable permissions to check and is governed by the frontend's own
    trust boundary (the documented localhost accepted-risk), so it is skipped.
    """
    actor = getattr(ctx, "author", None)
    # Only real Members carry channel-level permissions_for; skip otherwise.
    if actor is None or not hasattr(actor, "guild_permissions"):
        return True, None
    for value in kwargs.values():
        # CHANNEL_LIST params resolve to lists — gate every element.
        items = value if isinstance(value, (list, tuple)) else [value]
        for item in items:
            channel = item if _is_guild_channel(item) else getattr(item, "channel", None)
            if channel is None or not hasattr(channel, "permissions_for"):
                continue
            try:
                perms = channel.permissions_for(actor)
            except Exception:  # noqa: BLE001 - be permissive on odd channel types
                continue
            if not getattr(perms, "read_messages", True):
                return False, f"Actor cannot access channel {getattr(channel, 'id', '?')}."
    return True, None


def _is_guild_channel(value: Any) -> bool:
    """True for a resolved Discord guild channel/thread (has permissions_for
    and a guild), distinguishing it from messages/members/roles."""
    return (
        hasattr(value, "permissions_for")
        and hasattr(value, "guild")
        and not hasattr(value, "content")  # exclude Message
    )


def _check_permission(ctx: OpContext, level: PermissionLevel) -> "tuple[bool, Optional[str]]":
    """Route to core.utils.is_admin / is_superadmin, the same gates the
    existing cogs use via @commands.check(...). Those helpers read
    ctx.bot.config, ctx.author, and ctx.guild — see OpContext's docstring."""
    if level == PermissionLevel.EVERYONE:
        return True, None
    if ctx is None or ctx.author is None:
        return False, "No actor on context; cannot check permissions."
    if level == PermissionLevel.SUPERADMIN:
        if is_superadmin(ctx):
            return True, None
        return False, "Requires superadmin."
    if level == PermissionLevel.ADMIN:
        if is_admin(ctx):
            return True, None
        return False, "Requires admin."
    return False, f"Unknown permission level: {level!r}"


@dataclass(frozen=True)
class OpSpec:
    """An op declaration attached to a cog method by the module-level
    `op(...)` decorator, waiting to be registered.

    Deliberately inert: attaching a spec at import time must NOT touch the
    registry, or a module import would leak ops that no live cog backs (and
    a re-import would collide with itself). Registration happens per cog
    INSTANCE in `register_cog_ops`, and is undone in `unregister_owner`.
    """
    name: str
    description: str
    permission: PermissionLevel
    params: Tuple[OpParam, ...] = ()
    serialize: Optional[Callable[[Any], Dict[str, Any]]] = None
    agent_guidance: Optional[str] = None
    scope: OpScope = OpScope.GUILD
    group: str = "messaging"


# Attribute an OpSpec rides on. Mirrors how discord.py's CogMeta finds
# commands: decorate the method, let the cog machinery collect them on the
# instance.
OP_SPEC_ATTR = "__op_spec__"


def op(name: str, description: str, permission: PermissionLevel,
       params: Optional[List[OpParam]] = None,
       serialize: Optional[Callable[[Any], Dict[str, Any]]] = None,
       agent_guidance: Optional[str] = None,
       scope: OpScope = OpScope.GUILD,
       group: str = "messaging"):
    """Declare a cog method as an op, WITHOUT registering it.

        class MyCog(commands.Cog):
            @op("do_thing", "Does the thing.", PermissionLevel.ADMIN)
            async def do_thing(self, ctx, ...): ...

    The spec is attached to the function; `registry.register_cog_ops(cog)`
    (called from LiterallyBot.add_cog) registers the whole batch against the
    live cog instance, and `registry.unregister_owner(cog)` removes it on
    unload. There is no `origin` parameter — this path always stamps 'cog'.
    """
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        if not inspect.iscoroutinefunction(func):
            raise TypeError(f"Op '{name}' implementation must be an async function.")
        if getattr(func, OP_SPEC_ATTR, None) is not None:
            raise TypeError(f"Function already declares op "
                            f"'{getattr(func, OP_SPEC_ATTR).name}'; one op per function.")
        setattr(func, OP_SPEC_ATTR, OpSpec(
            name=name, description=description, permission=permission,
            params=tuple(params or []), serialize=serialize,
            agent_guidance=agent_guidance, scope=scope, group=group,
        ))
        return func
    return decorator


def _build_op(*, name: str, description: str, permission: PermissionLevel,
              impl: Callable[..., Any], params: Optional[List[OpParam]],
              serialize: Optional[Callable[[Any], Dict[str, Any]]],
              agent_guidance: Optional[str], scope: OpScope, group: str,
              origin: str, owner: Any) -> Op:
    """Validate and construct an Op. Shared by both registration paths so a
    cog op and a core op are held to exactly the same rules."""
    if not inspect.iscoroutinefunction(impl):
        raise TypeError(f"Op '{name}' implementation must be an async function.")
    if not isinstance(scope, OpScope):
        raise TypeError(f"Op '{name}' scope must be an OpScope, got {scope!r}.")
    if not group or not isinstance(group, str):
        raise ValueError(f"Op '{name}' must declare a non-empty group id.")
    return Op(
        name=name, description=description, permission=permission,
        impl=impl, params=list(params or []), serialize=serialize,
        agent_guidance=agent_guidance, scope=scope, group=group,
        origin=origin, owner=owner,
    )


class OpsRegistry:
    """Registry of ops, shared by any frontend (in-bot agent loop, MCP
    server, ...). Import the module-level `registry` instance below rather
    than constructing your own, unless you're writing an isolated test."""

    def __init__(self):
        self._ops: Dict[str, Op] = {}

    def op(self, name: str, description: str, permission: PermissionLevel,
           params: Optional[List[OpParam]] = None,
           serialize: Optional[Callable[[Any], Dict[str, Any]]] = None,
           agent_guidance: Optional[str] = None,
           scope: OpScope = OpScope.GUILD,
           group: str = "messaging"):
        """Decorator: `@registry.op("name", "...", PermissionLevel.ADMIN)`
        registers an `async def impl(ctx, **kwargs)` under `name`.

        This is the CORE registration path — ops registered through it are
        stamped origin='core'. Cog-provided ops go through the module-level
        `op(...)` decorator plus `register_cog_ops`, which stamps 'cog'.
        Origin is never a decorator argument on either path.
        """
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self._insert(_build_op(
                name=name, description=description, permission=permission,
                impl=func, params=params, serialize=serialize,
                agent_guidance=agent_guidance, scope=scope, group=group,
                origin=ORIGIN_CORE, owner=None,
            ))
            return func
        return decorator

    def _insert(self, op: Op) -> None:
        """Add a fully-built Op, refusing a duplicate name. The single
        mutation point for both registration paths."""
        if op.name in self._ops:
            raise ValueError(f"Op '{op.name}' is already registered.")
        self._ops[op.name] = op

    def get(self, name: str) -> Optional[Op]:
        return self._ops.get(name)

    def require(self, name: str) -> Op:
        """Get an op or raise — for frontends generating their tool surface
        from a static op-name list, where a miss means registry drift and
        should fail loudly at build time."""
        op = self._ops.get(name)
        if op is None:
            raise ValueError(f"Op '{name}' not found in the ops registry.")
        return op

    def list_tools(self) -> List[Dict[str, Any]]:
        return [op.to_schema() for op in self._ops.values()]

    def names(self) -> List[str]:
        return list(self._ops.keys())

    # -- live queries -------------------------------------------------------
    #
    # Every one of these reads self._ops at CALL time. Frontends must query
    # them per use and must not freeze the result into a module-level tuple:
    # cog ops appear and disappear with cog load/unload, so an import-time
    # snapshot is wrong the moment any cog is reloaded.

    def ops(self, *, scope: Optional[OpScope] = None,
            origin: Optional[str] = None,
            group: Optional[str] = None) -> List[Op]:
        """Live, filtered view of registered ops, in registration order."""
        return [
            op for op in self._ops.values()
            if (scope is None or op.scope == scope)
            and (origin is None or op.origin == origin)
            and (group is None or op.group == group)
        ]

    def op_names(self, *, scope: Optional[OpScope] = None,
                 origin: Optional[str] = None,
                 group: Optional[str] = None) -> List[str]:
        """Names of the ops `ops()` would return, same filters."""
        return [op.name for op in self.ops(scope=scope, origin=origin,
                                           group=group)]

    def guild_agent_names(self) -> List[str]:
        """The in-guild agent tool UNIVERSE: every registered guild-scoped
        op, queried live.

        This replaces the former `agent=True` flag. The doctrine is that
        there is no code-level op subset — an op is available to the
        guild-confined agent loop exactly when it acts on a guild, and
        WHICH of those a given guild enables is per-guild config
        (`bot_tools_enabled`), not a constant in this file."""
        return self.op_names(scope=OpScope.GUILD)

    def grouped(self, *, scope: Optional[OpScope] = None,
                origin: Optional[str] = None,
                ) -> List[Tuple[str, str, List[Op]]]:
        """Live listing partitioned by group, as (group_id, label, ops).

        Ordered by OP_GROUPS declaration order so a panel's sections keep a
        stable order across reloads; empty groups are omitted. Any group id
        not in OP_GROUPS (a cog inventing its own) sorts after the known
        ones and falls back to its raw id as the label."""
        selected = self.ops(scope=scope, origin=origin)
        order = list(OP_GROUPS)
        seen: List[str] = []
        for op in selected:
            if op.group not in seen:
                seen.append(op.group)
        seen.sort(key=lambda g: (order.index(g) if g in order else len(order),
                                 g))
        return [
            (gid, OP_GROUPS.get(gid, gid),
             [op for op in selected if op.group == gid])
            for gid in seen
        ]

    # -- cog op lifecycle ---------------------------------------------------

    def register_cog_ops(self, cog: Any) -> List[str]:
        """Register every `@op(...)`-decorated method of a cog instance,
        ALL-OR-NONE.

        The whole batch is built and checked for duplicates (against the
        registry AND within the batch itself) BEFORE anything is inserted,
        so a cog with one bad op registers zero ops rather than half of
        them — a half-registered cog is unremovable by name and leaves the
        agent surface lying about what actually works.

        Bound methods are collected, so the op impl receives `self` and can
        use cog state. Returns the registered names (empty if the cog
        declares no ops).
        """
        batch: List[Op] = []
        claimed: Dict[str, str] = {}
        # dir() over the CLASS, getattr on the INSTANCE: gives bound methods
        # while avoiding triggering instance-level descriptors/properties.
        for attr in dir(type(cog)):
            member = getattr(type(cog), attr, None)
            spec = getattr(member, OP_SPEC_ATTR, None)
            if spec is None:
                continue
            bound = getattr(cog, attr)
            if spec.name in claimed:
                raise ValueError(
                    f"Cog {type(cog).__name__} declares op '{spec.name}' twice "
                    f"({claimed[spec.name]} and {attr}); no ops were registered."
                )
            if spec.name in self._ops:
                raise ValueError(
                    f"Cog {type(cog).__name__}.{attr} declares op '{spec.name}', "
                    f"which is already registered; no ops were registered."
                )
            claimed[spec.name] = attr
            batch.append(_build_op(
                name=spec.name, description=spec.description,
                permission=spec.permission, impl=bound,
                params=list(spec.params), serialize=spec.serialize,
                agent_guidance=spec.agent_guidance, scope=spec.scope,
                group=spec.group, origin=ORIGIN_COG, owner=cog,
            ))
        # Preflight passed — commit.
        for built in batch:
            self._insert(built)
        return [built.name for built in batch]

    def unregister_owner(self, owner: Any) -> List[str]:
        """Remove every op owned by `owner` (a cog instance).

        Identity-based (`is`), not name-based: two cogs of the same class
        must not evict each other's ops. Never raises for an owner that
        registered nothing — cog teardown can run after a partially failed
        setup, and unregistration must be safe to call unconditionally.
        Returns the removed names.
        """
        removed = [name for name, op in self._ops.items() if op.owner is owner]
        for name in removed:
            del self._ops[name]
        return removed

    async def call(self, op_name: str, ctx: OpContext, **kwargs) -> OpResult:
        op = self._ops.get(op_name)
        if op is None:
            return OpResult(ok=False, error=f"Unknown op: {op_name}")
        return await op(ctx, **kwargs)

    async def call_ids(self, op_name: str, ctx: OpContext,
                       allowed_guild_ids: Optional[frozenset] = None,
                       **raw) -> OpResult:
        """Id-based invocation: resolve wire params (channel_id, message_id,
        user_id, role_id, guild_id + scalars) to live objects, then run the
        op with the same permission gates as `call()`.

        Guild confinement is CALLER policy: pass `allowed_guild_ids` to
        confine id-resolved targets to those guilds (the in-bot agent loop
        passes exactly {ctx.guild.id}); the default None means NO
        confinement — targets resolve anywhere the bot is (the MCP
        frontend: primitives, access control upstream). Resolution failures
        come back as OpResult(ok=False), never as raised exceptions.
        """
        op = self._ops.get(op_name)
        if op is None:
            return OpResult(ok=False, error=f"Unknown op: {op_name}")
        if allowed_guild_ids is not None:
            allowed_guild_ids = frozenset(allowed_guild_ids)
        # Gate BEFORE resolution: an unauthorized caller must not be able to
        # trigger Discord fetches (channel/message/member lookups) as a side
        # effect of a call it was never allowed to make. Op.__call__ checks
        # again for the object-based `call()` path — cheap belt-and-suspenders.
        allowed, reason = _check_permission(ctx, op.permission)
        if not allowed:
            return OpResult(ok=False, error=reason)
        try:
            kwargs = await op.resolve_kwargs(ctx.bot, getattr(ctx, "guild", None),
                                             raw, allowed_guild_ids)
        except ResolutionError as exc:
            return OpResult(ok=False, error=str(exc))
        return await op(ctx, **kwargs)


# ---------------------------------------------------------------------------
# The shared registry instance. Frontends import this, not the class.
# ---------------------------------------------------------------------------

registry = OpsRegistry()


def load_discord_attachments(
    file_paths: Optional[List[str]] = None,
) -> List[discord.File]:
    """Open local files as discord.File for send_message / send_dm.

    Validates ALL paths (existence, regular-file, extension allowlist,
    size) before opening ANY, so a rejected path never leaves earlier
    files dangling open. Files are constructed from paths so discord.py
    owns the handles — its HTTP layer closes them after the send attempt;
    a caller that fails BEFORE sending must close them itself.

    Paths are resolved (symlink-followed). Raises ValueError on rejection.
    """
    paths: List[str] = []
    for raw in (file_paths or []):
        s = str(raw).strip()
        if s and s not in paths:
            paths.append(s)
    if not paths:
        return []
    if len(paths) > 10:
        raise ValueError("Discord allows at most 10 attachments per message")

    resolved: List[Path] = []
    for raw in paths:
        path = Path(raw).expanduser()
        try:
            path = path.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ValueError(f"attachment not found: {raw}") from exc
        if not path.is_file():
            raise ValueError(f"attachment is not a file: {path}")
        ext = path.suffix.lower()
        if ext not in ATTACHMENT_EXTENSIONS:
            raise ValueError(
                f"attachment extension not allowed: {ext or '(none)'} "
                f"(allowed: {', '.join(sorted(ATTACHMENT_EXTENSIONS))})"
            )
        size = path.stat().st_size
        if size <= 0:
            raise ValueError(f"attachment is empty: {path}")
        if size > ATTACHMENT_MAX_BYTES:
            raise ValueError(
                f"attachment too large ({size} bytes > {ATTACHMENT_MAX_BYTES}): {path}"
            )
        resolved.append(path)
    return [discord.File(path, filename=path.name) for path in resolved]


def _require_admin_for_attachments(ctx: OpContext, file_paths) -> None:
    """Attachments read the HOST filesystem, so they are admin-gated even on
    ops whose text path is open to everyone — otherwise any user with the
    agentic chat tools could exfiltrate server files into a channel."""
    if not file_paths:
        return
    allowed, reason = _check_permission(ctx, PermissionLevel.ADMIN)
    if not allowed:
        raise ValueError(f"file attachments require admin: {reason}")


def _serialize_sent_message(m) -> Dict[str, Any]:
    return {
        "message_id": m.id,
        "attachments": [
            {"filename": a.filename, "url": a.url, "size": a.size}
            for a in (m.attachments or [])
        ],
    }


@registry.op(
    "send_message",
    "Send a text message to a channel, optionally as a reply to an existing "
    "message in that channel. Optional local file attachment(s) via "
    "file_paths (server filesystem paths the bot can read; admin-only).",
    PermissionLevel.EVERYONE,
    params=[
        OpParam("channel", ParamKind.CHANNEL,
                "Discord channel id to send into."),
        OpParam("content", ParamKind.STRING,
                "Message text to send (may be empty if attaching a file).",
                required=False, default=""),
        OpParam("reference_message_id", ParamKind.SNOWFLAKE,
                "Optional message id in the same channel to reply to.",
                required=False),
        OpParam("file_paths", ParamKind.STRING_LIST,
                "Optional attachments: absolute server-side file paths "
                "(gif/png/jpg/webp/mp4/…), max 10. Requires admin.",
                required=False),
        OpParam("allowed_mentions", ParamKind.INTERNAL),
    ],
    serialize=_serialize_sent_message,
    agent_guidance=(
        "send_message returns the new message's message_id; reuse it for "
        "follow-up edits or reactions, and use reference_message_id to reply "
        "to a message. Your final text reply is posted to the current channel "
        "automatically — never duplicate it with send_message."),
    scope=OpScope.GUILD,
    group="messaging",
)
async def send_message(ctx: OpContext, channel, content: str = "",
                       reference_message_id: Optional[int] = None,
                       file_paths: Optional[List[str]] = None,
                       allowed_mentions=None):
    # Never-ping by default: model/tool-originated sends must not be able
    # to ping anyone. An object-based caller that WANTS pings must pass an
    # explicit allowed_mentions. (Policy hoisted here from the agent-loop
    # and MCP frontends so no frontend can forget it.)
    _require_admin_for_attachments(ctx, file_paths)
    text = content if content is not None else ""
    if not str(text).strip() and not file_paths:
        raise ValueError("send_message requires non-empty content and/or a file attachment")
    files = load_discord_attachments(file_paths)
    kwargs: Dict[str, Any] = {
        "allowed_mentions": allowed_mentions
        if allowed_mentions is not None else discord.AllowedMentions.none(),
    }
    if files:
        kwargs["files"] = files
    try:
        if reference_message_id is not None:
            # Reply to a message in the same channel. mention_author is governed
            # by allowed_mentions (the frontends pass none), so a reply never pings.
            ref = await fetch_message_in(channel, int(reference_message_id))
            kwargs["reference"] = ref
        return await channel.send(text if str(text).strip() else None, **kwargs)
    except BaseException:
        # discord.py closes files it was handed once a send is ATTEMPTED;
        # failures before that point (reference fetch, arg validation)
        # leave the handles ours to close.
        for f in files:
            f.close()
        raise


@registry.op(
    "edit_message",
    "Edit the content of a message the bot previously sent.",
    PermissionLevel.EVERYONE,
    params=[
        OpParam("message", ParamKind.MESSAGE,
                "Discord message id to edit (must be authored by the bot)."),
        OpParam("content", ParamKind.STRING, "Replacement message text."),
    ],
    serialize=lambda m: {"message_id": m.id},
    scope=OpScope.GUILD,
    group="messaging",
)
async def edit_message(ctx: OpContext, message, content: str):
    return await message.edit(content=content)


@registry.op(
    "delete_message",
    "Delete a message. Requires admin — mirrors cogs/optional/cleanup.py's "
    "bulk-delete gate, which restricts message deletion to superadmin/admin.",
    PermissionLevel.ADMIN,
    params=[OpParam("message", ParamKind.MESSAGE, "Discord message id to delete.")],
    agent_guidance=(
        "delete_message requires the invoking user to be a bot admin; if the "
        "tool returns a permission error, relay that plainly."),
    scope=OpScope.GUILD,
    group="messaging",
)
async def delete_message(ctx: OpContext, message):
    await message.delete()
    return True


@registry.op(
    "add_reaction",
    "Add an emoji reaction to a message.",
    PermissionLevel.EVERYONE,
    params=[
        OpParam("message", ParamKind.MESSAGE, "Discord message id to react to."),
        OpParam("emoji", ParamKind.STRING,
                "Emoji to react with (unicode emoji or `name:id` custom emoji)."),
    ],
    agent_guidance=(
        "add_reaction needs a literal unicode emoji character (💩, 💨, ❤️) or "
        "name:id for custom emoji — never a word or description. ('fart' and "
        "'-' are invalid; the fart/dash emoji is 💨.)"),
    scope=OpScope.GUILD,
    group="messaging",
)
async def add_reaction(ctx: OpContext, message, emoji: str):
    await message.add_reaction(emoji)
    return True


@registry.op(
    "remove_reaction",
    "Remove the bot's own emoji reaction from a message. Only reactions "
    "the bot itself added can be removed — other users' reactions are "
    "untouchable by design.",
    PermissionLevel.EVERYONE,
    params=[
        OpParam("message", ParamKind.MESSAGE,
                "Discord message id to remove the bot's reaction from."),
        OpParam("emoji", ParamKind.STRING,
                "Emoji to remove (unicode emoji or `name:id` custom emoji)."),
    ],
    agent_guidance=(
        "remove_reaction only removes reactions the bot itself added, and "
        "takes the same literal-emoji form as add_reaction."),
    scope=OpScope.GUILD,
    group="messaging",
)
async def remove_reaction(ctx: OpContext, message, emoji: str):
    await message.remove_reaction(emoji, ctx.bot.user)
    return True


async def _index_search(bot, guild_id, channel_ids, limit, author_id, contains):
    """Query Discord's guild message-search index, newest first — guild-wide
    when channel_ids is empty, else scoped to those channels (the endpoint
    accepts repeated channel_id params and unions them; verified live
    2026-07-21). Documented endpoint (GET /guilds/{guild.id}/messages/search;
    needs READ_MESSAGE_HISTORY + the MESSAGE_CONTENT intent) — full-history
    keyword search. include_nsfw is always sent because actor visibility is
    enforced by our own gates (and this endpoint excludes age-restricted
    channels by default, which silently hides most of an NSFW-flagged
    guild). The API pages 25 at a time; raises on any error so the caller
    can fall back / degrade.
    """
    from discord.http import Route
    hits = []
    total = None
    while len(hits) < limit:
        # List-of-tuples so channel_id can repeat; str values for aiohttp.
        params = [
            ("include_nsfw", "true"),
            ("sort_by", "timestamp"),
            ("sort_order", "desc"),
            ("limit", str(min(25, limit - len(hits)))),
            ("offset", str(len(hits))),
        ]
        params.extend(("channel_id", str(cid)) for cid in channel_ids)
        if author_id is not None:
            params.append(("author_id", str(author_id)))
        if contains is not None:
            params.append(("content", contains))
        data = await bot.http.request(
            Route("GET", "/guilds/{guild_id}/messages/search",
                  guild_id=guild_id),
            params=params)
        total = data.get("total_results")
        # Each result is a group of messages with the actual match flagged
        # `hit`; the rest is surrounding context we don't want.
        page = [next((m for m in group if m.get("hit")), group[0])
                for group in data.get("messages", []) if group]
        if not page:
            break
        # Same row shape as serialize_message — the fallback scan path uses
        # it, and consumers must not see two shapes for one op.
        hits.extend({
            "id": int(m["id"]),
            "channel_id": int(m["channel_id"]),
            "author_id": int(m["author"]["id"]),
            "content": m.get("content", ""),
            "created_at": m.get("timestamp"),
        } for m in page)
        if total is not None and len(hits) >= int(total):
            break
    return hits[:limit], total


def _drop_hits_actor_cannot_see(ctx: OpContext, guild, hits):
    """Guild-wide search can surface channels the invoking user can't read;
    apply the same actor-visibility policy as _check_channel_visibility
    (real Members are filtered, bare id-holder actors are the MCP frontend's
    documented accepted risk and pass through). Hits in channels the bot no
    longer resolves are dropped as unverifiable."""
    actor = getattr(ctx, "author", None)
    if actor is None or not hasattr(actor, "guild_permissions"):
        return hits
    visible = []
    for hit in hits:
        channel = guild.get_channel(hit["channel_id"]) if guild else None
        if channel is None or not hasattr(channel, "permissions_for"):
            continue
        try:
            if channel.permissions_for(actor).read_messages:
                visible.append(hit)
        except Exception:  # noqa: BLE001 - odd channel types err to hidden
            continue
    return visible


@registry.op(
    "search_history",
    "Search FULL message history via Discord's search index (keyword "
    "matching like the Discord search bar) — the whole server at once, or "
    "one channel — optionally filtered by author id and/or a content keyword.",
    PermissionLevel.EVERYONE,
    params=[
        OpParam("guild", ParamKind.GUILD,
                "Discord guild id to search server-wide. Optional when the "
                "invoking context already carries a guild (in-guild "
                "commands); pass it explicitly for a server-wide search "
                "when channel_ids is omitted.",
                required=False),
        OpParam("channels", ParamKind.CHANNEL_LIST,
                "Discord channel ids to search, unioned in one call — "
                "[id] for a single channel, [id1, id2, ...] for a subset. "
                "OMIT entirely to search every channel in the server.",
                required=False),
        OpParam("limit", ParamKind.INTEGER,
                f"Max number of matching messages to return, most recent "
                f"first (clamped to {HISTORY_LIMIT_MAX}).",
                required=False, default=100, minimum=1, maximum=HISTORY_LIMIT_MAX),
        OpParam("author_id", ParamKind.SNOWFLAKE,
                "Optional filter — only messages from this user id.",
                required=False),
        OpParam("contains", ParamKind.STRING,
                "Optional filter — keyword(s) to match in message content "
                "(whole-word matching, like Discord's search bar; also "
                "matches text inside embeds/links).",
                required=False),
    ],
    serialize=lambda payload: payload,
    agent_guidance=(
        "search_history searches FULL history via Discord's search index, "
        "in ONE call however it's scoped: OMIT channel_ids for server-wide "
        "questions ('has anyone ever...', 'how many times did X get said in "
        "this server'); channel_ids: [id] for one channel; channel_ids: "
        "[id1, id2] for a subset. Filter with `contains` (keyword) and/or "
        "`author_id`. Results come back to YOU as data, newest first, each "
        "hit tagged with its channel_id; `total_matches` is how many exist "
        "in all — report that number honestly when it exceeds what was "
        "returned. Read the results and answer in plain text — never paste "
        "them raw. If the result carries a `note` about a fallback scan, "
        "only the most recent messages were checked — say so instead of "
        "claiming 'never'."),
    scope=OpScope.GUILD,
    group="messaging",
)
async def search_history(ctx: OpContext, guild=None, channels=None,
                          limit: int = 100,
                          author_id: Optional[int] = None,
                          contains: Optional[str] = None):
    scoped = list(channels or [])
    guild = scoped[0].guild if scoped else (guild or ctx.guild)
    if guild is None:
        raise ValueError("search_history needs a channel or a guild context.")
    # Snowflake scalars arrive as strings on the wire; compare as ints.
    if author_id is not None:
        author_id = _as_int(author_id, "author_id")
    try:
        hits, total = await _index_search(
            ctx.bot, guild.id, [c.id for c in scoped],
            limit, author_id, contains)
        hits = _drop_hits_actor_cannot_see(ctx, guild, hits)
        return {"messages": hits, "count": len(hits), "total_matches": total}
    except Exception as exc:
        # Index cold, endpoint withdrawn, or intent missing — degrade rather
        # than failing the tool outright, and SAY so in the payload so the
        # model doesn't overclaim "never said".
        logger = getattr(ctx.bot, "logger", None)
        if logger:
            logger.warning(
                f"search_history index query failed ({type(exc).__name__}: "
                f"{exc}); falling back to recent-window scan")
        if len(scoped) != 1:
            return {"messages": [], "count": 0,
                    "note": ("search index unavailable; guild-wide and "
                             "multi-channel search require it — retry with "
                             "channel_ids: [one channel id] to scan that "
                             "channel's recent messages")}
        results = []
        async for message in scoped[0].history(limit=limit):
            if author_id is not None and message.author.id != author_id:
                continue
            if contains is not None and contains.lower() not in message.content.lower():
                continue
            results.append(serialize_message(message))
        return {"messages": results, "count": len(results),
                "note": (f"search index unavailable; scanned only the "
                         f"{limit} most recent messages")}


@registry.op(
    "add_role",
    "Add a role to a member. Requires admin — self-service role assignment "
    "is the reaction-role system's job (cogs/optional/setrole.py), not this op's.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD,
                "Discord guild id the member and role belong to (the API "
                "call is guild-keyed). Optional when the invoking context "
                "already carries a guild (in-guild commands); required "
                "over guild-less frontends like MCP.",
                required=False),
        OpParam("member", ParamKind.MEMBER, "Discord user id to grant the role to."),
        OpParam("role", ParamKind.ROLE, "Discord role id to grant."),
    ],
    scope=OpScope.GUILD,
    group="roles",
)
async def add_role(ctx: OpContext, member, role, guild=None):
    await member.add_roles(role)
    return True


@registry.op(
    "remove_role",
    "Remove a role from a member. Requires admin, mirroring add_role.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD,
                "Discord guild id the member and role belong to (the API "
                "call is guild-keyed). Optional when the invoking context "
                "already carries a guild (in-guild commands); required "
                "over guild-less frontends like MCP.",
                required=False),
        OpParam("member", ParamKind.MEMBER, "Discord user id to remove the role from."),
        OpParam("role", ParamKind.ROLE, "Discord role id to remove."),
    ],
    scope=OpScope.GUILD,
    group="roles",
)
async def remove_role(ctx: OpContext, member, role, guild=None):
    await member.remove_roles(role)
    return True


@registry.op(
    "pin_message",
    "Pin a message in its channel. Requires admin (pins are surfaced to "
    "the whole channel and Discord itself caps pins at 50 per channel).",
    PermissionLevel.ADMIN,
    params=[OpParam("message", ParamKind.MESSAGE, "Discord message id to pin.")],
    scope=OpScope.GUILD,
    group="messaging",
)
async def pin_message(ctx: OpContext, message):
    await message.pin()
    return True


@registry.op(
    "create_thread",
    "Create a thread, either attached to an existing message or standalone "
    "on a channel.",
    PermissionLevel.EVERYONE,
    params=[
        OpParam("channel", ParamKind.CHANNEL,
                "Discord channel id to create the thread in."),
        OpParam("name", ParamKind.STRING, "Thread name."),
        OpParam("message", ParamKind.MESSAGE,
                "Optional message id to attach the thread to.",
                required=False),
    ],
    serialize=lambda t: {"thread_id": t.id, "name": t.name},
    scope=OpScope.GUILD,
    group="messaging",
)
async def create_thread(ctx: OpContext, channel, name: str, message=None):
    if message is not None:
        return await message.create_thread(name=name)
    return await channel.create_thread(name=name)


@registry.op(
    "list_guilds",
    "List the guilds the bot is a member of (id and name).",
    PermissionLevel.EVERYONE,
    params=[],
    serialize=lambda gs: {"guilds": gs, "count": len(gs)},
    scope=OpScope.GLOBAL,
    group="guild",
)
async def list_guilds(ctx: OpContext):
    return [{"id": g.id, "name": g.name} for g in ctx.bot.guilds]


@registry.op(
    "list_channels",
    "List a guild's channels the bot can see (id, name, type).",
    PermissionLevel.EVERYONE,
    params=[OpParam("guild", ParamKind.GUILD, "Discord guild id to enumerate.")],
    serialize=lambda cs: {"channels": cs, "count": len(cs)},
    agent_guidance=(
        "Channel ids must come from list_channels or the visible context — "
        "NEVER guess or invent an id. When the user names channels (e.g. "
        "'check #memes'), call list_channels first to resolve names to ids."),
    scope=OpScope.GUILD,
    group="guild-info",
)
async def list_channels(ctx: OpContext, guild):
    return [
        {"id": c.id, "name": c.name, "type": str(c.type)}
        for c in guild.channels
    ]


@registry.op(
    "list_members",
    "List members who can see a channel and their online status "
    "(online/idle/dnd/offline). Same visibility as the Discord member "
    "sidebar for that channel.",
    PermissionLevel.EVERYONE,
    params=[
        OpParam("channel", ParamKind.CHANNEL,
                "Discord channel id whose members to list."),
        OpParam("status", ParamKind.STRING,
                "Optional filter — only members with this status "
                "(online/idle/dnd/offline).",
                required=False),
        OpParam("include_bots", ParamKind.BOOLEAN,
                "Include bot accounts (default false).",
                required=False, default=False),
        OpParam("limit", ParamKind.INTEGER,
                "Max members to return (default 100, clamped to 1000).",
                required=False, default=100, minimum=1, maximum=1000),
    ],
    serialize=lambda ms: {"members": ms, "count": len(ms)},
    scope=OpScope.GUILD,
    group="guild-info",
)
async def list_members(ctx: OpContext, channel, status: Optional[str] = None,
                       include_bots: bool = False, limit: int = 100):
    want = status.lower() if status else None
    results = []
    for m in getattr(channel, "members", []):
        if not include_bots and getattr(m, "bot", False):
            continue
        member_status = str(getattr(m, "status", "offline"))
        if want is not None and member_status != want:
            continue
        results.append({
            "id": m.id,
            "display_name": m.display_name,
            "status": member_status,
        })
        if len(results) >= limit:
            break
    return results


# ---------------------------------------------------------------------------
# Direct messages.
#
# DM channels belong to no guild, so they cannot travel through the CHANNEL
# param kind (which rejects guild-less targets by design). These ops resolve
# a USER (guild-independent, one-to-one with the API: POST /users/@me/channels
# takes only the recipient id) and open the DM channel from that user. A raw
# DM channel id is never accepted from the wire.
#
# The former GUILD+MEMBER shape (guild_id required on the wire) was removed
# 2026-08 by owner decision — one-to-one primitives; no invented params. The
# membership confinement it provided was redundant: Discord itself refuses
# bot DMs to users who share no guild. Consequence for the ADMIN gate: over
# guild-less frontends (MCP) there is no guild admin list to consult, so DM
# ops are effectively superadmin-only there; in-bot, ambient ctx.guild keeps
# the per-guild admin check.
#
# CONSENT IS DELIBERATELY NOT ENFORCED HERE. Whether a given user has opted in
# is a per-deployment convention (one guild's opt-in role id is meaningless in
# another, and some deployments use none), so authorization belongs to the
# caller — typically by calling list_role_members for whatever role that
# deployment treats as consent, and DMing only that set. Do not add a role
# check to this op; it would be wrong for every other deployment.
# ---------------------------------------------------------------------------

@registry.op(
    "send_dm",
    "Send a direct message to a user. Never pings. Optional local file "
    "attachment(s) via file_paths. Discord only delivers bot DMs to users "
    "sharing at least one guild with the bot. NOTE: this op does not check "
    "whether the user consented to DMs — the caller owns that decision.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("user", ParamKind.USER, "Discord user id to DM."),
        OpParam("content", ParamKind.STRING,
                "Message text to send (may be empty if attaching a file).",
                required=False, default=""),
        OpParam("file_paths", ParamKind.STRING_LIST,
                "Optional attachments: absolute server-side file paths "
                "(gif/png/jpg/webp/mp4/…), max 10.",
                required=False),
        OpParam("allowed_mentions", ParamKind.INTERNAL),
    ],
    serialize=_serialize_sent_message,
    agent_guidance=(
        "send_dm messages one member privately and does NOT check whether "
        "they opted in — establish that first (list_role_members over the "
        "role this server treats as consent) before DMing anyone. A DM "
        "cannot be seen by the channel, so never use it to answer a question "
        "asked in public."),
    scope=OpScope.DM,
    group="dm",
)
async def send_dm(ctx: OpContext, user, content: str = "",
                  file_paths: Optional[List[str]] = None,
                  allowed_mentions=None):
    # Same never-ping default as send_message; a DM notifies on its own.
    text = content if content is not None else ""
    if not str(text).strip() and not file_paths:
        raise ValueError("send_dm requires non-empty content and/or a file attachment")
    files = load_discord_attachments(file_paths)
    mentions = (allowed_mentions if allowed_mentions is not None
                else discord.AllowedMentions.none())
    kwargs: Dict[str, Any] = {"allowed_mentions": mentions}
    if files:
        kwargs["files"] = files
    try:
        message = await user.send(text if str(text).strip() else None, **kwargs)
    except BaseException:
        # Same pre-send handle ownership rule as send_message.
        for f in files:
            f.close()
        raise
    # Store our own side too, so read_dms returns a conversation rather than
    # a one-sided mailbox.
    try:
        log_dm(user.id, row_from_message(message, user.id))
    except Exception:  # noqa: BLE001 - a storage failure must not undo a sent DM
        logger = getattr(ctx.bot, "logger", None)
        if logger:
            logger.warning("send_dm: failed to persist outbound DM", exc_info=True)
    return message


@registry.op(
    "read_dms",
    "Read the stored DM transcript with a user, oldest first. Covers DMs "
    "exchanged since transcript storage was enabled — use fetch_dms for "
    "older history straight from Discord.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("user", ParamKind.USER, "Discord user id whose DMs to read."),
        OpParam("since", ParamKind.STRING,
                "Optional ISO timestamp — only rows strictly after it. "
                "Coarse filter; can drop a message sharing the cursor row's "
                "timestamp. Prefer after_message_id for polling.",
                required=False),
        OpParam("after_message_id", ParamKind.SNOWFLAKE,
                "Optional poll cursor: only rows with a strictly greater "
                "message id. Monotonic and tie-proof.",
                required=False),
        OpParam("limit", ParamKind.INTEGER,
                "Max rows to return (default 50). With after_message_id the "
                "OLDEST matching rows are kept (lossless forward paging); "
                "otherwise the most recent.",
                required=False, default=50, minimum=1, maximum=500),
    ],
    serialize=lambda rows: {"messages": rows, "count": len(rows)},
    agent_guidance=(
        "read_dms returns only DMs recorded since transcript storage was "
        "enabled, each tagged direction 'in' (from the user) or 'out' (from "
        "the bot) — an empty result means nothing was recorded, not that "
        "nothing was ever said (fetch_dms reads real Discord history). To "
        "poll for new replies, pass the last row's message_id as "
        "after_message_id and repeat while pages come back full — it never "
        "skips or repeats a message."),
    scope=OpScope.DM,
    group="dm",
)
async def read_dms(ctx: OpContext, user, since: Optional[str] = None,
                   after_message_id: Optional[int] = None, limit: int = 50):
    # File I/O off the event loop: a large transcript must not stall the bot.
    return await asyncio.to_thread(load_dms, user.id, limit=limit,
                                   since=since, after_id=after_message_id)


@registry.op(
    "fetch_dms",
    "Fetch DM history with a user directly from Discord, oldest first. "
    "Unlike read_dms this covers the full conversation history, including "
    "messages that predate transcript storage. Paginate backwards with "
    "before_message_id.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("user", ParamKind.USER,
                "Discord user id whose DM history to fetch."),
        OpParam("limit", ParamKind.INTEGER,
                "Max messages to fetch (default 50, clamped to 100 — one "
                "Discord history page).",
                required=False, default=50, minimum=1, maximum=100),
        OpParam("before_message_id", ParamKind.SNOWFLAKE,
                "Optional pagination cursor: only messages older than this "
                "id. Pass the previous page's oldest message_id to walk "
                "further back.",
                required=False),
    ],
    serialize=lambda rows: {"messages": rows, "count": len(rows)},
    agent_guidance=(
        "fetch_dms reads live Discord DM history (rows match read_dms: "
        "direction in/out, attachments with filename+url). Results are "
        "oldest-first within the page; to page backwards through history, "
        "call again with before_message_id set to the first row's "
        "message_id, until a page comes back empty."),
    scope=OpScope.DM,
    group="dm",
)
async def fetch_dms(ctx: OpContext, user, limit: int = 50,
                    before_message_id: Optional[int] = None):
    channel = user.dm_channel or await user.create_dm()
    before = (discord.Object(id=before_message_id)
              if before_message_id is not None else None)
    rows = []
    async for msg in channel.history(limit=limit, before=before):
        rows.append(row_from_message(msg, user.id))
    rows.reverse()  # history() yields newest-first; present oldest-first
    return rows


def _parse_color(color: str):
    value = color.lstrip("#")
    try:
        return discord.Colour(int(value, 16))
    except ValueError as exc:
        raise ValueError(f"Color must be a hex string like '#5865F2', got {color!r}.") from exc


def serialize_role(role: Any) -> Dict[str, Any]:
    return {
        "id": role.id,
        "name": role.name,
        "color": f"#{role.colour.value:06X}",
        "position": role.position,
        "hoist": role.hoist,
        "mentionable": role.mentionable,
        "managed": role.managed,
        "member_count": len(role.members),
        # Guild-level grants only; channel ACLs live in list_channel_overwrites.
        "permissions": [name for name, value in role.permissions if value],
    }


def _guard_editable(role: Any):
    """Managed (integration) roles and @everyone are not editable/deletable."""
    if role.managed:
        raise ValueError(f"Role '{role.name}' is managed by an integration and cannot be modified.")
    if role.is_default():
        raise ValueError("The @everyone role cannot be modified.")


@registry.op(
    "list_roles",
    "List a guild's roles (id, name, color, position, member count), "
    "top of the hierarchy first.",
    PermissionLevel.EVERYONE,
    params=[OpParam("guild", ParamKind.GUILD, "Discord guild id to enumerate.")],
    serialize=lambda rs: {"roles": rs, "count": len(rs)},
    scope=OpScope.GUILD,
    group="roles",
)
async def list_roles(ctx: OpContext, guild):
    return [serialize_role(r) for r in
            sorted(guild.roles, key=lambda r: r.position, reverse=True)]


@registry.op(
    "list_role_members",
    "List the members who hold a given role. Use list_roles first to find "
    "the role id. Bots are excluded unless include_bots is set.",
    PermissionLevel.EVERYONE,
    params=[
        OpParam("guild", ParamKind.GUILD, "Discord guild id the role belongs to."),
        OpParam("role", ParamKind.ROLE, "Discord role id whose holders to list."),
        OpParam("include_bots", ParamKind.BOOLEAN,
                "Include bot accounts (default false).",
                required=False, default=False),
        OpParam("limit", ParamKind.INTEGER,
                "Max members to return (default 100, clamped to 1000).",
                required=False, default=100, minimum=1, maximum=1000),
    ],
    serialize=lambda ms: {"members": ms, "count": len(ms)},
    agent_guidance=(
        "Role ids for list_role_members must come from list_roles — never "
        "guess one. It answers 'who has role X'; list_members answers 'who "
        "can see channel Y'. The two are not interchangeable."),
    scope=OpScope.GUILD,
    group="roles",
)
async def list_role_members(ctx: OpContext, guild, role,
                            include_bots: bool = False, limit: int = 100):
    # role.members reads the member CACHE — silently short on a guild that
    # hasn't finished chunking. This op is the documented way to select who
    # an agent may contact, so under-reporting means silently skipping
    # people: chunk first if the cache is incomplete.
    if not guild.chunked:
        try:
            await guild.chunk()
        except discord.ClientException:
            pass  # members intent unavailable; cache is the best we have
    results = []
    for m in role.members:
        if not include_bots and getattr(m, "bot", False):
            continue
        results.append({
            "id": m.id,
            "display_name": m.display_name,
            "status": str(getattr(m, "status", "offline")),
        })
        if len(results) >= limit:
            break
    return results


@registry.op(
    "create_role",
    "Create a new role in a guild. Requires admin. The role is created "
    "unassigned; use add_role to grant it to members.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD, "Discord guild id to create the role in."),
        OpParam("name", ParamKind.STRING, "Role name."),
        OpParam("color", ParamKind.STRING,
                "Optional hex color like '#5865F2'.", required=False),
        OpParam("hoist", ParamKind.BOOLEAN,
                "Show members separately in the sidebar (default false).",
                required=False, default=False),
        OpParam("mentionable", ParamKind.BOOLEAN,
                "Allow anyone to @mention the role (default false).",
                required=False, default=False),
    ],
    serialize=serialize_role,
    agent_guidance=(
        "create_role returns the new role's id; reuse it for add_role or "
        "edit_role instead of calling list_roles again. Check list_roles "
        "first rather than creating near-duplicate names."),
    scope=OpScope.GUILD,
    group="roles",
)
async def create_role(ctx: OpContext, guild, name: str, color: Optional[str] = None,
                      hoist: bool = False, mentionable: bool = False):
    kwargs = {"name": name, "hoist": hoist, "mentionable": mentionable}
    if color is not None:
        kwargs["colour"] = _parse_color(color)
    return await guild.create_role(**kwargs)


@registry.op(
    "edit_role",
    "Edit a role's name, color, hoist/mentionable flags, or hierarchy "
    "position. Requires admin. Managed roles and @everyone are refused.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD, "Discord guild id the role belongs to."),
        OpParam("role", ParamKind.ROLE, "Discord role id to edit."),
        OpParam("name", ParamKind.STRING, "New role name.", required=False),
        OpParam("color", ParamKind.STRING,
                "New hex color like '#5865F2'.", required=False),
        OpParam("hoist", ParamKind.BOOLEAN,
                "Show members separately in the sidebar.", required=False),
        OpParam("mentionable", ParamKind.BOOLEAN,
                "Allow anyone to @mention the role.", required=False),
        OpParam("position", ParamKind.INTEGER,
                "New hierarchy position (1 = just above @everyone; higher = "
                "higher in the list; other roles shift around it). The bot "
                "cannot move a role above its own top role.",
                required=False, minimum=1),
    ],
    serialize=serialize_role,
    agent_guidance=(
        "edit_role position values shift the whole hierarchy — after a batch "
        "of moves, call list_roles once to see the settled order instead of "
        "assuming each move landed exactly where requested."),
    scope=OpScope.GUILD,
    group="roles",
)
async def edit_role(ctx: OpContext, guild, role, name: Optional[str] = None,
                    color: Optional[str] = None, hoist: Optional[bool] = None,
                    mentionable: Optional[bool] = None,
                    position: Optional[int] = None):
    _guard_editable(role)
    kwargs = {}
    if name is not None:
        kwargs["name"] = name
    if color is not None:
        kwargs["colour"] = _parse_color(color)
    if hoist is not None:
        kwargs["hoist"] = hoist
    if mentionable is not None:
        kwargs["mentionable"] = mentionable
    if position is not None:
        kwargs["position"] = position
    if not kwargs:
        raise ValueError("Nothing to edit: pass at least one of name/color/hoist/mentionable/position.")
    await role.edit(**kwargs)
    return role


@registry.op(
    "delete_role",
    "Delete a role from a guild. Requires admin. Managed roles and "
    "@everyone are refused.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD, "Discord guild id the role belongs to."),
        OpParam("role", ParamKind.ROLE, "Discord role id to delete."),
    ],
    serialize=lambda info: info,
    agent_guidance=(
        "delete_role is irreversible and detaches the role from every member "
        "holding it — confirm intent before calling it on a role with a "
        "nonzero member_count."),
    scope=OpScope.GUILD,
    group="roles",
)
async def delete_role(ctx: OpContext, guild, role):
    _guard_editable(role)
    info = {"deleted_role_id": role.id, "name": role.name}
    await role.delete()
    return info


# Discord rejects custom emoji images above 256KB. Checked here so an
# oversized file fails with a clear message instead of a raw HTTP 400, and
# so we never read a huge file into memory to hand to the API.
EMOJI_MAX_BYTES = 256 * 1024
EMOJI_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})


def serialize_emoji(emoji: Any) -> Dict[str, Any]:
    return {
        "id": emoji.id,
        "name": emoji.name,
        "animated": emoji.animated,
        "managed": emoji.managed,
        "url": str(emoji.url),
        # The literal form add_reaction/message content needs. Getting this
        # wrong is the single most common custom-emoji mistake, so the op
        # hands back the exact string rather than making callers build it.
        "mention": str(emoji),
        "reaction_form": f"{emoji.name}:{emoji.id}",
    }


def _require_guild_emoji(guild, emoji_id: int):
    """Resolve an emoji id against THIS guild. Emoji are not resolved by the
    shared id resolver (no ParamKind), so guild confinement is enforced here:
    an id from another guild must not be editable/deletable through an op
    scoped to this one."""
    for e in guild.emojis:
        if e.id == emoji_id:
            return e
    raise ValueError(
        f"No custom emoji with id {emoji_id} in guild '{guild.name}'. "
        f"Call list_emojis to see valid ids."
    )


def _guard_emoji_editable(emoji: Any):
    if emoji.managed:
        raise ValueError(
            f"Emoji '{emoji.name}' is managed by an integration (e.g. Twitch) "
            f"and cannot be modified."
        )


def load_emoji_image(file_path: str) -> bytes:
    """Read a local image as bytes for create_custom_emoji.

    Unlike send_message attachments (which discord.py streams from a path),
    the emoji API takes raw bytes, so this validates BEFORE reading: a
    2GB file must not be slurped into memory just to be rejected.
    """
    path = Path(str(file_path).strip()).expanduser()
    try:
        path = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError(f"emoji image not found: {file_path}") from exc
    if not path.is_file():
        raise ValueError(f"emoji image is not a file: {path}")
    ext = path.suffix.lower()
    if ext not in EMOJI_EXTENSIONS:
        raise ValueError(
            f"emoji image extension not allowed: {ext or '(none)'} "
            f"(allowed: {', '.join(sorted(EMOJI_EXTENSIONS))})"
        )
    size = path.stat().st_size
    if size <= 0:
        raise ValueError(f"emoji image is empty: {path}")
    if size > EMOJI_MAX_BYTES:
        raise ValueError(
            f"emoji image too large ({size} bytes > {EMOJI_MAX_BYTES}). "
            f"Discord's custom-emoji limit is 256KB — resize or re-encode it."
        )
    return path.read_bytes()


@registry.op(
    "list_emojis",
    "List a guild's custom emoji (id, name, animated, and the exact string "
    "to use in a message or reaction).",
    PermissionLevel.EVERYONE,
    params=[OpParam("guild", ParamKind.GUILD, "Discord guild id to enumerate.")],
    serialize=lambda es: {"emojis": es, "count": len(es)},
    agent_guidance=(
        "Use list_emojis to get a custom emoji's exact id before add_reaction "
        "or edit/delete — never guess an id or assume a name is unique. Pass "
        "`reaction_form` (name:id) to add_reaction, and `mention` when writing "
        "the emoji into message content."),
    scope=OpScope.GUILD,
    group="emojis",
)
async def list_emojis(ctx: OpContext, guild):
    return [serialize_emoji(e) for e in guild.emojis]


@registry.op(
    "create_emoji",
    "Upload a new custom emoji to a guild from a local image file. Requires "
    "admin. Image must be png/jpg/gif/webp and under 256KB.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD, "Discord guild id to add the emoji to."),
        OpParam("name", ParamKind.STRING,
                "Emoji name (2-32 chars, letters/numbers/underscores only; "
                "this is what users type between colons)."),
        OpParam("file_path", ParamKind.STRING,
                "Absolute server-side path to the image (png/jpg/gif/webp, "
                "max 256KB). Animated gifs create an animated emoji."),
    ],
    serialize=serialize_emoji,
    agent_guidance=(
        "create_emoji returns the new emoji's id plus `reaction_form` and "
        "`mention` — reuse those directly instead of calling list_emojis "
        "again. Guilds have a hard emoji slot limit; if creation fails for a "
        "full guild, say so rather than retrying."),
    scope=OpScope.GUILD,
    group="emojis",
)
async def create_emoji(ctx: OpContext, guild, name: str, file_path: str):
    # Reads the HOST filesystem, so it carries the same admin gate as
    # send_message attachments even though the op is already ADMIN — keeping
    # the check explicit means the rule survives a future tier change.
    _require_admin_for_attachments(ctx, [file_path])
    image = load_emoji_image(file_path)
    return await guild.create_custom_emoji(
        name=name, image=image,
        reason=f"create_emoji op by {ctx.author} ({ctx.author.id})",
    )


@registry.op(
    "edit_emoji",
    "Rename an existing custom emoji. Requires admin. Managed "
    "(integration-owned) emoji are refused.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD, "Discord guild id the emoji belongs to."),
        OpParam("emoji_id", ParamKind.SNOWFLAKE,
                "Custom emoji id to edit (from list_emojis)."),
        OpParam("name", ParamKind.STRING, "New emoji name."),
    ],
    serialize=serialize_emoji,
    agent_guidance=(
        "Renaming an emoji changes the :name: users type but keeps its id, so "
        "existing reactions and messages keep working."),
    scope=OpScope.GUILD,
    group="emojis",
)
async def edit_emoji(ctx: OpContext, guild, emoji_id: int, name: str):
    emoji = _require_guild_emoji(guild, _as_int(emoji_id, "emoji_id"))
    _guard_emoji_editable(emoji)
    await emoji.edit(
        name=name,
        reason=f"edit_emoji op by {ctx.author} ({ctx.author.id})",
    )
    return emoji


@registry.op(
    "delete_emoji",
    "Delete a custom emoji from a guild. Requires admin. Managed "
    "(integration-owned) emoji are refused.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD, "Discord guild id the emoji belongs to."),
        OpParam("emoji_id", ParamKind.SNOWFLAKE,
                "Custom emoji id to delete (from list_emojis)."),
    ],
    serialize=lambda info: info,
    agent_guidance=(
        "delete_emoji is irreversible and breaks every existing message and "
        "reaction using that emoji — confirm intent before calling it."),
    scope=OpScope.GUILD,
    group="emojis",
)
async def delete_emoji(ctx: OpContext, guild, emoji_id: int):
    emoji = _require_guild_emoji(guild, _as_int(emoji_id, "emoji_id"))
    _guard_emoji_editable(emoji)
    info = {"deleted_emoji_id": emoji.id, "name": emoji.name}
    await emoji.delete(
        reason=f"delete_emoji op by {ctx.author} ({ctx.author.id})",
    )
    return info


@registry.op(
    "list_channel_overwrites",
    "List permission overwrites (channel ACLs): which roles/members are "
    "explicitly allowed or denied what, per channel. Requires admin — this "
    "enumerates ACLs of channels hidden from ordinary members.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD, "Discord guild id to inspect."),
        OpParam("channel", ParamKind.CHANNEL,
                "Optional channel id to restrict to one channel.",
                required=False),
        OpParam("role", ParamKind.ROLE,
                "Optional role id to restrict to one role's overwrites.",
                required=False),
    ],
    serialize=lambda os: {"overwrites": os, "count": len(os)},
    agent_guidance=(
        "list_channel_overwrites returns explicit per-channel ACL entries "
        "only — a role with no overwrites simply grants its guild-level "
        "permissions. Filter by role_id when auditing what one role "
        "unlocks; the unfiltered guild-wide dump can be large."),
    scope=OpScope.GUILD,
    group="guild-info",
)
async def list_channel_overwrites(ctx: OpContext, guild, channel=None, role=None):
    channels = [channel] if channel is not None else guild.channels
    out = []
    for ch in channels:
        for target, overwrite in ch.overwrites.items():
            if role is not None and target.id != role.id:
                continue
            allow, deny = overwrite.pair()
            out.append({
                "channel_id": ch.id,
                "channel_name": ch.name,
                "channel_type": str(ch.type),
                "target_type": "role" if isinstance(target, discord.Role) else "member",
                "target_id": target.id,
                "target_name": getattr(target, "name", str(target)),
                "allow": [name for name, value in allow if value],
                "deny": [name for name, value in deny if value],
            })
    return out



# ---------------------------------------------------------------------------
# Offline schema dump — instantiates the module-level registry and prints
# every op's generated wire schema WITHOUT a live bot/Discord connection.
# Documented in the README. Run directly:
#     python3 -m core.ops
#
# This is a VIEWER, not a test: the registry's invariants live in
# tests/test_ops_registry.py (run with `python -m pytest tests/`). Keeping
# assertions out of here is deliberate — the previous version imported
# cogs.optional.gpt to assert on panel chunking, inverting the dependency
# so core depended on a cog. That check now lives in the test file, where
# importing a cog is fine.
# ---------------------------------------------------------------------------

def _print_schemas() -> None:
    tools = registry.list_tools()
    print(f"core.ops — {len(tools)} ops registered")
    for gid, label, ops in registry.grouped():
        print(f"\n{label} ({gid}) — {len(ops)} ops")
        for op_ in ops:
            schema = op_.to_json_schema()
            wire = ", ".join(schema["properties"]) or "-"
            print(f"  [{op_.permission.name:>10}] [{op_.scope.value:>6}] "
                  f"{op_.name:<24} wire: {wire}")


if __name__ == "__main__":
    _print_schemas()
