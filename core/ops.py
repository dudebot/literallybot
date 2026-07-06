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

This module is frontend-agnostic on purpose: it does not import
`discord.ext.commands`, does not know about cogs, and does not get wired
into `bot.py`. It only knows how to run an op against an `OpContext`.

Permission gates route through `core.utils.is_admin` / `is_superadmin`,
the same helpers `cogs/dynamic/cleanup.py` and `cogs/dynamic/setrole.py`
already use via `@commands.check(...)`. Those helpers expect a duck-typed
ctx with `.author`, `.guild`, and `.bot.config` — `OpContext` below mirrors
that shape so the existing helpers work unmodified.

Frontends: mcp_ops/server.py (MCP) and core/agent_loop.py (in-bot agent
loop) both generate their tool surfaces from this registry. See the
bottom-of-file smoke test for a no-bot-required sanity check.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any, Callable, Dict, List, Optional, Tuple

import discord

from core.utils import is_admin, is_superadmin

# Shared history-scan cap: search_history never scans more than this many
# messages regardless of the requested limit (silently clamped, matching
# the original MCP frontend's behavior).
HISTORY_LIMIT_MAX = 200


class PermissionLevel(IntEnum):
    """Ordered so a numeric comparison also makes sense, but permission
    checks below dispatch by exact level rather than relying on ordering."""
    EVERYONE = 0
    ADMIN = 1
    SUPERADMIN = 2


class ParamKind(str, Enum):
    """What an op parameter IS, driving both schema generation and id
    resolution. Discord entities travel as ids on the wire and are resolved
    to live objects before the op impl runs; scalars pass through."""
    CHANNEL = "channel"    # wire: channel_id (int) -> discord channel object
    MESSAGE = "message"    # wire: channel_id + message_id (int) -> discord.Message
    MEMBER = "member"      # wire: user_id (int) -> discord.Member (of ctx.guild)
    ROLE = "role"          # wire: role_id (int) -> discord.Role (of ctx.guild)
    GUILD = "guild"        # wire: guild_id (int) -> discord.Guild
    STRING = "string"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    INTERNAL = "internal"  # never on the wire; frontends may pass a live object


# JSON-schema type for each scalar kind.
_SCALAR_JSON_TYPES = {
    ParamKind.STRING: "string",
    ParamKind.INTEGER: "integer",
    ParamKind.BOOLEAN: "boolean",
}

# Wire name for each entity kind (MESSAGE also implies channel_id; see
# Op.wire_params()).
_ENTITY_WIRE_NAMES = {
    ParamKind.CHANNEL: "channel_id",
    ParamKind.MESSAGE: "message_id",
    ParamKind.MEMBER: "user_id",
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
    """The resolved target belongs to a guild outside the caller's allowed
    guild set (guild confinement / allowlist violation)."""


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
# mcp_ops/server.py so every id-based frontend resolves identically.
# ---------------------------------------------------------------------------

def check_guild_allowed(guild: Any, allowed_guild_ids: frozenset, what: str) -> None:
    if guild is None:
        raise GuildNotAllowedError(
            f"{what} has no guild (DMs are not allowed through id-based calls)."
        )
    if guild.id not in allowed_guild_ids:
        raise GuildNotAllowedError(
            f"{what} belongs to guild {guild.id}, which is not in the "
            f"caller's allowed guild set."
        )


async def resolve_channel(bot: Any, channel_id: int, allowed_guild_ids: frozenset) -> Any:
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


def resolve_guild(bot: Any, guild_id: int, allowed_guild_ids: frozenset) -> Any:
    guild = bot.get_guild(guild_id)
    if guild is None:
        raise ResolutionError(f"Could not resolve guild {guild_id}.")
    check_guild_allowed(guild, allowed_guild_ids, f"Guild {guild_id}")
    return guild


async def resolve_context_guild(bot: Any, raw: Dict[str, Any],
                                allowed_guild_ids: frozenset) -> Optional[Any]:
    """Resolve the guild an id-based call targets, from its raw wire params,
    BEFORE building an OpContext (frontends that construct their actor from
    the target guild — e.g. the MCP server — need this first). Returns None
    for ops with no guild-bound target (e.g. list_guilds)."""
    if raw.get("channel_id") is not None:
        channel = await resolve_channel(bot, _as_int(raw["channel_id"], "channel_id"),
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
                add(WireParam("channel_id", "integer",
                              f"Discord channel id containing the {p.name}.", True))
                add(WireParam("message_id", "integer",
                              p.description or f"Discord message id of the {p.name}.",
                              p.required, p.default))
            elif p.kind in _ENTITY_WIRE_NAMES:
                add(WireParam(_ENTITY_WIRE_NAMES[p.kind], "integer",
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
            "params": self.to_json_schema(),
        }

    # -- id resolution ----------------------------------------------------

    async def resolve_kwargs(self, bot: Any, guild: Optional[Any], raw: Dict[str, Any],
                             allowed_guild_ids: frozenset) -> Dict[str, Any]:
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
                    raise ResolutionError(f"Missing required parameter 'channel_id' for op '{self.name}'.")
                resolved_channel = await resolve_channel(
                    bot, _as_int(channel_id, "channel_id"), allowed_guild_ids)
                kwargs[p.name] = resolved_channel

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
                    raise ResolutionError(f"Missing required parameter 'guild_id' for op '{self.name}'.")
                kwargs[p.name] = resolve_guild(bot, _as_int(guild_id, "guild_id"), allowed_guild_ids)

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
                if p.kind == ParamKind.INTEGER:
                    value = _as_int(value, p.name)
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
        channel = value if _is_guild_channel(value) else getattr(value, "channel", None)
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


class OpsRegistry:
    """Registry of ops, shared by any frontend (in-bot agent loop, MCP
    server, ...). Import the module-level `registry` instance below rather
    than constructing your own, unless you're writing an isolated test."""

    def __init__(self):
        self._ops: Dict[str, Op] = {}

    def op(self, name: str, description: str, permission: PermissionLevel,
           params: Optional[List[OpParam]] = None,
           serialize: Optional[Callable[[Any], Dict[str, Any]]] = None):
        """Decorator: `@registry.op("name", "...", PermissionLevel.ADMIN)`
        registers an `async def impl(ctx, **kwargs)` under `name`."""
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            if name in self._ops:
                raise ValueError(f"Op '{name}' is already registered.")
            if not inspect.iscoroutinefunction(func):
                raise TypeError(f"Op '{name}' implementation must be an async function.")
            self._ops[name] = Op(
                name=name, description=description, permission=permission,
                impl=func, params=params or [], serialize=serialize,
            )
            return func
        return decorator

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

        Guild confinement: every id-resolved target must belong to
        `allowed_guild_ids` (default: exactly ctx.guild — an id-based call
        cannot reach into other guilds the bot is in). Resolution failures
        come back as OpResult(ok=False), never as raised exceptions.
        """
        op = self._ops.get(op_name)
        if op is None:
            return OpResult(ok=False, error=f"Unknown op: {op_name}")
        if allowed_guild_ids is None:
            allowed_guild_ids = (
                frozenset({ctx.guild.id}) if getattr(ctx, "guild", None) is not None
                else frozenset()
            )
        # Gate BEFORE resolution: an unauthorized caller must not be able to
        # trigger Discord fetches (channel/message/member lookups) as a side
        # effect of a call it was never allowed to make. Op.__call__ checks
        # again for the object-based `call()` path — cheap belt-and-suspenders.
        allowed, reason = _check_permission(ctx, op.permission)
        if not allowed:
            return OpResult(ok=False, error=reason)
        try:
            kwargs = await op.resolve_kwargs(ctx.bot, getattr(ctx, "guild", None),
                                             raw, frozenset(allowed_guild_ids))
        except ResolutionError as exc:
            return OpResult(ok=False, error=str(exc))
        return await op(ctx, **kwargs)


# ---------------------------------------------------------------------------
# The shared registry instance. Frontends import this, not the class.
# ---------------------------------------------------------------------------

registry = OpsRegistry()


@registry.op(
    "send_message",
    "Send a text message to a channel, optionally as a reply to an existing "
    "message in that channel.",
    PermissionLevel.EVERYONE,
    params=[
        OpParam("channel", ParamKind.CHANNEL,
                "Discord channel id to send into."),
        OpParam("content", ParamKind.STRING, "Message text to send."),
        OpParam("reference_message_id", ParamKind.INTEGER,
                "Optional message id in the same channel to reply to.",
                required=False),
        OpParam("allowed_mentions", ParamKind.INTERNAL),
    ],
    serialize=lambda m: {"message_id": m.id},
)
async def send_message(ctx: OpContext, channel, content: str,
                       reference_message_id: Optional[int] = None,
                       allowed_mentions=None):
    # Never-ping by default: model/tool-originated sends must not be able
    # to ping anyone. An object-based caller that WANTS pings must pass an
    # explicit allowed_mentions. (Policy hoisted here from the agent-loop
    # and MCP frontends so no frontend can forget it.)
    kwargs = {"allowed_mentions": allowed_mentions
              if allowed_mentions is not None else discord.AllowedMentions.none()}
    if reference_message_id is not None:
        # Reply to a message in the same channel. mention_author is governed
        # by allowed_mentions (the frontends pass none), so a reply never pings.
        ref = await fetch_message_in(channel, int(reference_message_id))
        kwargs["reference"] = ref
    return await channel.send(content, **kwargs)


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
)
async def edit_message(ctx: OpContext, message, content: str):
    return await message.edit(content=content)


@registry.op(
    "delete_message",
    "Delete a message. Requires admin — mirrors cogs/dynamic/cleanup.py's "
    "bulk-delete gate, which restricts message deletion to superadmin/admin.",
    PermissionLevel.ADMIN,
    params=[OpParam("message", ParamKind.MESSAGE, "Discord message id to delete.")],
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
)
async def remove_reaction(ctx: OpContext, message, emoji: str):
    await message.remove_reaction(emoji, ctx.bot.user)
    return True


@registry.op(
    "search_history",
    "Search a channel's message history, optionally filtered by author id "
    "and/or a substring match on content.",
    PermissionLevel.EVERYONE,
    params=[
        OpParam("channel", ParamKind.CHANNEL, "Discord channel id to search."),
        OpParam("limit", ParamKind.INTEGER,
                f"Max number of messages to scan, most recent first "
                f"(clamped to {HISTORY_LIMIT_MAX}).",
                required=False, default=100, minimum=1, maximum=HISTORY_LIMIT_MAX),
        OpParam("author_id", ParamKind.INTEGER,
                "Optional filter — only messages from this user id.",
                required=False),
        OpParam("contains", ParamKind.STRING,
                "Optional filter — substring match on message content.",
                required=False),
    ],
    serialize=lambda msgs: {
        "messages": [serialize_message(m) for m in msgs],
        "count": len(msgs),
    },
)
async def search_history(ctx: OpContext, channel, limit: int = 100,
                          author_id: Optional[int] = None,
                          contains: Optional[str] = None):
    results = []
    async for message in channel.history(limit=limit):
        if author_id is not None and message.author.id != author_id:
            continue
        if contains is not None and contains.lower() not in message.content.lower():
            continue
        results.append(message)
    return results


@registry.op(
    "add_role",
    "Add a role to a member. Requires admin when targeting someone other "
    "than the invoking actor — mirrors cogs/dynamic/setrole.py's own-vs-"
    "other-member permission split. The registry-level gate here is a "
    "simpler admin-only rule; callers that need the whitelist-role / "
    "self-service behavior of !setrole should keep using that cog for now.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("member", ParamKind.MEMBER, "Discord user id to grant the role to."),
        OpParam("role", ParamKind.ROLE, "Discord role id to grant."),
    ],
)
async def add_role(ctx: OpContext, member, role):
    await member.add_roles(role)
    return True


@registry.op(
    "remove_role",
    "Remove a role from a member. Requires admin, mirroring add_role.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("member", ParamKind.MEMBER, "Discord user id to remove the role from."),
        OpParam("role", ParamKind.ROLE, "Discord role id to remove."),
    ],
)
async def remove_role(ctx: OpContext, member, role):
    await member.remove_roles(role)
    return True


@registry.op(
    "pin_message",
    "Pin a message in its channel. Requires admin (pins are surfaced to "
    "the whole channel and Discord itself caps pins at 50 per channel).",
    PermissionLevel.ADMIN,
    params=[OpParam("message", ParamKind.MESSAGE, "Discord message id to pin.")],
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
)
async def list_guilds(ctx: OpContext):
    return [{"id": g.id, "name": g.name} for g in ctx.bot.guilds]


@registry.op(
    "list_channels",
    "List a guild's channels the bot can see (id, name, type).",
    PermissionLevel.EVERYONE,
    params=[OpParam("guild", ParamKind.GUILD, "Discord guild id to enumerate.")],
    serialize=lambda cs: {"channels": cs, "count": len(cs)},
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
# In-file smoke test — instantiates the module-level registry and lists
# tools WITHOUT a live bot/Discord connection. Run directly:
#     python3 -m core.ops
# ---------------------------------------------------------------------------

def _smoke_test() -> None:
    expected = {
        "send_message", "edit_message", "delete_message", "add_reaction",
        "remove_reaction", "search_history", "add_role", "remove_role", "pin_message",
        "create_thread", "list_guilds", "list_channels", "list_members",
    }
    names = set(registry.names())
    missing = expected - names
    assert not missing, f"Registry is missing expected ops: {missing}"

    tools = registry.list_tools()
    assert len(tools) == len(names), "list_tools() count should match names() count"

    for tool in tools:
        assert tool["name"] in expected
        assert tool["permission"] in {"EVERYONE", "ADMIN", "SUPERADMIN"}
        assert isinstance(tool["description"], str) and tool["description"]
        schema = tool["params"]
        assert schema["type"] == "object"
        assert isinstance(schema["properties"], dict)
        assert isinstance(schema["required"], list)

    # Generated wire schemas: entity params travel as ids; MESSAGE implies
    # channel_id; INTERNAL params (allowed_mentions) never hit the wire.
    send = registry.get("send_message").to_json_schema()
    assert set(send["properties"]) == {"channel_id", "content", "reference_message_id"}, send
    assert send["required"] == ["channel_id", "content"]  # reference is optional

    edit = registry.get("edit_message").to_json_schema()
    assert set(edit["properties"]) == {"channel_id", "message_id", "content"}, edit

    search = registry.get("search_history").to_json_schema()
    assert set(search["properties"]) == {"channel_id", "limit", "author_id", "contains"}
    assert search["required"] == ["channel_id"]
    assert search["properties"]["limit"]["maximum"] == HISTORY_LIMIT_MAX

    thread = registry.get("create_thread").to_json_schema()
    assert set(thread["properties"]) == {"channel_id", "name", "message_id"}
    assert set(thread["required"]) == {"channel_id", "name"}

    roles = registry.get("add_role").to_json_schema()
    assert set(roles["properties"]) == {"user_id", "role_id"}

    # A no-actor context must fail closed on anything above EVERYONE, and
    # must never raise — permission failures surface as OpResult.error.
    empty_ctx = OpContext(bot=None, author=None, guild=None)
    import asyncio

    result = asyncio.run(registry.call("delete_message", empty_ctx, message=None))
    assert result.ok is False
    assert "actor" in (result.error or "").lower()

    unknown = asyncio.run(registry.call("not_a_real_op", empty_ctx))
    assert unknown.ok is False
    assert "Unknown op" in (unknown.error or "")

    # call_ids with no guild on ctx and no allowlist fails closed on
    # guild-bound targets, and rejects unknown params by name.
    class _FakeBot:
        def get_channel(self, cid):
            return None
        async def fetch_channel(self, cid):
            raise RuntimeError("no gateway in smoke test")

    no_guild_ctx = OpContext(bot=_FakeBot(), author=None, guild=None)
    res = asyncio.run(registry.call_ids("send_message", no_guild_ctx,
                                        channel_id=123, content="hi"))
    assert res.ok is False and "Could not resolve channel" in res.error

    res = asyncio.run(registry.call_ids("list_guilds", no_guild_ctx, bogus=1))
    assert res.ok is False and "Unexpected parameter" in res.error

    print(f"core.ops smoke test OK — {len(names)} ops registered:")
    for tool in tools:
        wire = ", ".join(tool["params"]["properties"].keys()) or "-"
        print(f"  [{tool['permission']:>10}] {tool['name']:<16} wire: {wire}")


if __name__ == "__main__":
    _smoke_test()
