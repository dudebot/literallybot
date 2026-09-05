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
than freezing a module-level tuple at import: cog ops arrive with their cogs
during startup and leave at shutdown, and WHICH cogs load is a runtime config
decision (`disabled_cogs`) — so an import-time snapshot is wrong before the
bot has even finished booting.

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
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum, IntEnum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import discord

from core.dm_log import list_dm_users, load_dms, log_dm, row_from_message
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
    # Thread-family ops live in their own group, NOT "messaging": each group
    # must fit one 25-option Discord select (see
    # test_each_group_fits_in_one_select), and messaging is the group closest
    # to that cap — the thread ops would have pushed it over.
    "threads": "Threads",
    "roles": "Roles",
    # Stickers joined this group in the 2026-08 expressive-domain pass —
    # relabeled rather than minting a new group id (labels are presentation
    # only; the id is what code and stored config speak).
    "emojis": "Emojis & stickers",
    "guild-info": "Guild info",
    # Member-state writes (nickname, timeout) live apart from "roles" and
    # "guild-info": the reads stay browsable in guild-info, while this group
    # collects the ADMIN member-moderation write surface.
    "members": "Members",
    # Moderation reads (ban list, automod rules). The ban/kick/prune WRITES
    # live in the NEEDS_OWNER-tier "members" surface below (ADMIN+ floors).
    "moderation": "Moderation",
    # Voice-state reads and the reversible client-parity voice writes
    # (move/disconnect/server-mute/deafen/stage suppress) from the 2026-08
    # voice-domain pass. Bot voice PRESENCE (connect/play audio) is out of
    # scope — a stateful gateway session, not an atomic op.
    "voice": "Voice",
    # Guild scheduled events (2026-08 voice-domain pass): reads for everyone,
    # create/edit for admins. Delete/cancel lives in the NEEDS_OWNER tier
    # (delete_scheduled_event) — it destroys the accrued RSVP list.
    "events": "Scheduled events",
    # Invite lifecycle (2026-08 expressive-domain pass): admin list/create/
    # revoke. Webhook CRUD + execute live in the NEEDS_OWNER-tier "webhooks"
    # group below — a webhook URL is a persistent unauthenticated posting
    # credential.
    "invites": "Invites",
    # NEEDS_OWNER-tier destructive/privileged surfaces (2026-08 owner-tier
    # pass). Each fills the deliberately-omitted destructive half of a domain
    # above, kept in its own group so a server admin sees the blast radius
    # grouped rather than mixed into the reversible reads/writes.
    "channels": "Channels",             # channel create/delete/clone/move + overwrite writes
    "message-mod": "Message moderation",  # bulk delete/purge/publish/tts/sticker/reaction moderation
    "webhooks": "Webhooks",             # webhook CRUD + execute (impersonation surface)
    "automod": "AutoMod",               # automod rule create/edit/delete
    "dm": "Direct messages",
    "guild": "Guild",
    # Shared by core webhook primitives AND the danbooru cog. The id stays
    # here because core ops use it; the cog's group_label matches.
    "integrations": "Integrations",
}
# Cog and util ops declare their own group id (+ optional group_label) on
# `@op`. The live registry learns those groups at registration; they are
# NOT listed here. This dict is the catalog of CORE primitive groups only
# — a fork cog must never need a line in this file.

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

def serialize_embed(embed: Any) -> Dict[str, Any]:
    """JSON shape for one Discord embed, from a discord.py Embed OR the
    raw dict the search-index HTTP path returns. Title/description/fields
    are the body — #log (and most bot status posts) have empty `content`
    and live entirely in embeds."""
    if isinstance(embed, dict):
        footer = embed.get("footer") or {}
        author = embed.get("author") or {}
        raw_fields = embed.get("fields") or []
        return {
            "title": embed.get("title"),
            "description": embed.get("description"),
            "url": embed.get("url"),
            "fields": [
                {"name": f.get("name"), "value": f.get("value"),
                 "inline": bool(f.get("inline"))}
                for f in raw_fields
            ],
            "footer": footer.get("text") if isinstance(footer, dict) else None,
            "author": author.get("name") if isinstance(author, dict) else None,
        }
    footer = getattr(embed, "footer", None)
    author = getattr(embed, "author", None)
    return {
        "title": getattr(embed, "title", None),
        "description": getattr(embed, "description", None),
        "url": getattr(embed, "url", None),
        "fields": [
            {"name": getattr(f, "name", None),
             "value": getattr(f, "value", None),
             "inline": bool(getattr(f, "inline", False))}
            for f in (getattr(embed, "fields", None) or [])
        ],
        "footer": getattr(footer, "text", None) if footer is not None else None,
        "author": getattr(author, "name", None) if author is not None else None,
    }


def serialize_embeds(embeds: Any) -> list:
    return [serialize_embed(e) for e in (embeds or [])]


def serialize_message(message: Any) -> Dict[str, Any]:
    created = getattr(message, "created_at", None)
    return {
        "id": message.id,
        "channel_id": message.channel.id,
        "author_id": message.author.id,
        "content": message.content,
        "created_at": created.isoformat() if created else None,
        "embeds": serialize_embeds(getattr(message, "embeds", None)),
    }


def _message_search_text(message: Any) -> str:
    """Content plus embed title/description/fields/footer.

    Discord's search index matches text inside embeds. The fallback scan
    used to search `content` only, so an embed-only channel (#log) was
    invisible to `contains` whenever the index was cold.
    """
    parts = [getattr(message, "content", None) or ""]
    for e in serialize_embeds(getattr(message, "embeds", None)):
        parts.append(e.get("title") or "")
        parts.append(e.get("description") or "")
        for f in e.get("fields") or []:
            parts.append(f.get("name") or "")
            parts.append(f.get("value") or "")
        parts.append(e.get("footer") or "")
        parts.append(e.get("author") or "")
    return "\n".join(parts)


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
    # Core primitives use an OP_GROUPS key; cog/util ops pass any kebab-case
    # id and an optional group_label (the registry learns the label live).
    group: str = "messaging"
    group_label: Optional[str] = None
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

    def default_gate(self) -> str:
        """The Off/Admin/Everyone default for this op's agent exposure.

        Always off: an op the super-admin has whitelisted still does not
        reach a guild's members until a guild admin picks Admin or Everyone
        in /aisettings. PermissionLevel is the invoke floor, not the agent
        default — mapping EVERYONE-floor ops to gate "everyone" would make
        a single whitelist tick expose send_message (and friends) to every
        member."""
        return "off"

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
        frontends. Ops without a registered serializer pass a dict through
        unchanged and return {} for anything else (void actions)."""
        if self.serialize is None:
            return value if isinstance(value, dict) else {}
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
    group_label: Optional[str] = None


# Attribute an OpSpec rides on. Mirrors how discord.py's CogMeta finds
# commands: decorate the method, let the cog machinery collect them on the
# instance.
OP_SPEC_ATTR = "__op_spec__"


def op(name: str, description: str, permission: PermissionLevel,
       params: Optional[List[OpParam]] = None,
       serialize: Optional[Callable[[Any], Dict[str, Any]]] = None,
       agent_guidance: Optional[str] = None,
       scope: OpScope = OpScope.GUILD,
       group: str = "messaging",
       group_label: Optional[str] = None):
    """Declare a function as an op, WITHOUT registering it.

    Used on cog methods (registered in add_cog) and on util functions
    (registered via register_module_ops). The spec is inert at import.

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
            group_label=group_label,
        ))
        return func
    return decorator


def _build_op(*, name: str, description: str, permission: PermissionLevel,
              impl: Callable[..., Any], params: Optional[List[OpParam]],
              serialize: Optional[Callable[[Any], Dict[str, Any]]],
              agent_guidance: Optional[str], scope: OpScope, group: str,
              origin: str, owner: Any,
              group_label: Optional[str] = None) -> Op:
    """Validate and construct an Op. Shared by both registration paths so a
    cog op and a core op are held to exactly the same rules."""
    if not inspect.iscoroutinefunction(impl):
        raise TypeError(f"Op '{name}' implementation must be an async function.")
    if not isinstance(scope, OpScope):
        raise TypeError(f"Op '{name}' scope must be an OpScope, got {scope!r}.")
    if not group or not isinstance(group, str):
        raise ValueError(f"Op '{name}' must declare a non-empty group id.")
    # Wire names collapse per kind (MEMBER and USER both user_id). Two
    # params that share a wire name would silently drop the second in
    # wire_params(); CHANNEL + MESSAGE sharing channel_id is the one
    # legal overlap. A second id of the same wire name must travel as a
    # SNOWFLAKE (see forward_message).
    def _wire_names_for(kind: ParamKind):
        if kind == ParamKind.MESSAGE:
            return ("channel_id", "message_id")
        if kind == ParamKind.CHANNEL_LIST:
            return ("channel_ids",)
        if kind in _ENTITY_WIRE_NAMES:
            return (_ENTITY_WIRE_NAMES[kind],)
        return ()

    seen_wire = set()
    for p in (params or []):
        for wn in _wire_names_for(p.kind):
            if wn in seen_wire:
                if wn == "channel_id":
                    continue
                raise ValueError(
                    f"Op '{name}' declares two parameters that wire as "
                    f"{wn}; they would share a name. Use a SNOWFLAKE "
                    f"scalar for the second id (see forward_message)."
                )
            seen_wire.add(wn)
    return Op(
        name=name, description=description, permission=permission,
        impl=impl, params=list(params or []), serialize=serialize,
        agent_guidance=agent_guidance, scope=scope, group=group,
        group_label=group_label, origin=origin, owner=owner,
    )


class OpsRegistry:
    """Registry of ops, shared by any frontend (in-bot agent loop, MCP
    server, ...). Import the module-level `registry` instance below rather
    than constructing your own, unless you're writing an isolated test."""

    def __init__(self):
        self._ops: Dict[str, Op] = {}
        # gid -> label for groups that are NOT in OP_GROUPS (cog/util ops).
        # Stamped at registration, pruned when the last op in that group
        # unregisters. Core primitive groups stay in OP_GROUPS.
        self._extra_groups: Dict[str, str] = {}

    def label_for(self, gid: str) -> str:
        """Display label for a group id: core catalog, then live cog/util
        labels, then the raw id."""
        return OP_GROUPS.get(gid) or self._extra_groups.get(gid) or gid

    def _note_group(self, gid: str, label: Optional[str]) -> None:
        if gid in OP_GROUPS:
            return
        if label:
            self._extra_groups.setdefault(gid, label)
        else:
            self._extra_groups.setdefault(gid, gid)

    def _prune_extra_groups(self) -> None:
        used = {op.group for op in self._ops.values()}
        self._extra_groups = {g: lab for g, lab in self._extra_groups.items()
                              if g in used}

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

    def names(self) -> List[str]:
        return list(self._ops.keys())

    # -- live queries -------------------------------------------------------
    #
    # Every one of these reads self._ops at CALL time. Frontends must query
    # them per use and must not freeze the result into a module-level tuple:
    # cog ops appear as their cogs load during startup, so an import-time
    # snapshot is wrong before the bot has finished booting.

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
        WHICH of those a given guild enables is config (the global
        `agent_ops_whitelist` x the per-guild `agent_ops_gate`, see
        core/agent_gate.py), not a constant in this file."""
        return self.op_names(scope=OpScope.GUILD)

    def grouped(self, *, scope: Optional[OpScope] = None,
                origin: Optional[str] = None,
                ) -> List[Tuple[str, str, List[Op]]]:
        """Live listing partitioned by group, as (group_id, label, ops).

        Ordered by OP_GROUPS (core primitives) then alphabetically for
        cog/util groups learned at registration. Empty groups are omitted."""
        selected = self.ops(scope=scope, origin=origin)
        order = list(OP_GROUPS)
        seen: List[str] = []
        for op in selected:
            if op.group not in seen:
                seen.append(op.group)
        seen.sort(key=lambda g: (order.index(g) if g in order else len(order),
                                 g))
        return [
            (gid, self.label_for(gid),
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
                group=spec.group, group_label=spec.group_label,
                origin=ORIGIN_COG, owner=cog,
            ))
        return self._commit_batch(batch)

    def register_module_ops(self, module: Any) -> List[str]:
        """Register every `@op(...)`-decorated function on a module.

        Same all-or-none batch as register_cog_ops. Origin is still 'cog'
        (behavioral, not an API primitive); owner is the module object so
        these outlive any one cog — used by utils.points, whose store
        exists whether the Points UI cog is loaded.
        """
        batch: List[Op] = []
        claimed: Dict[str, str] = {}
        for attr in dir(module):
            member = getattr(module, attr, None)
            spec = getattr(member, OP_SPEC_ATTR, None)
            if spec is None or inspect.isclass(member):
                continue
            if spec.name in claimed:
                raise ValueError(
                    f"Module {getattr(module, '__name__', module)} declares "
                    f"op '{spec.name}' twice; no ops were registered."
                )
            if spec.name in self._ops:
                raise ValueError(
                    f"Module {getattr(module, '__name__', module)}.{attr} "
                    f"declares op '{spec.name}', which is already registered; "
                    f"no ops were registered."
                )
            claimed[spec.name] = attr
            batch.append(_build_op(
                name=spec.name, description=spec.description,
                permission=spec.permission, impl=member,
                params=list(spec.params), serialize=spec.serialize,
                agent_guidance=spec.agent_guidance, scope=spec.scope,
                group=spec.group, group_label=spec.group_label,
                origin=ORIGIN_COG, owner=module,
            ))
        return self._commit_batch(batch)

    def _commit_batch(self, batch: List[Op]) -> List[str]:
        for built in batch:
            self._insert(built)
            self._note_group(built.group, built.group_label)
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
        self._prune_extra_groups()
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
    "file_paths (server filesystem paths the bot can read; admin-only), and "
    "an optional custom sticker from this guild via sticker_id.",
    PermissionLevel.EVERYONE,
    params=[
        OpParam("channel", ParamKind.CHANNEL,
                "Discord channel id to send into."),
        OpParam("content", ParamKind.STRING,
                "Message text to send (may be empty if attaching a file "
                "or a sticker).",
                required=False, default=""),
        OpParam("reference_message_id", ParamKind.SNOWFLAKE,
                "Optional message id in the same channel to reply to.",
                required=False),
        OpParam("file_paths", ParamKind.STRING_LIST,
                "Optional attachments: absolute server-side file paths "
                "(gif/png/jpg/webp/mp4/…), max 10. Requires admin.",
                required=False),
        OpParam("sticker_id", ParamKind.SNOWFLAKE,
                "Optional custom sticker id to attach — must belong to "
                "THIS guild (see list_stickers); foreign ids are refused.",
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
                       sticker_id: Optional[int] = None,
                       allowed_mentions=None):
    # Never-ping by default: model/tool-originated sends must not be able
    # to ping anyone. An object-based caller that WANTS pings must pass an
    # explicit allowed_mentions. (Policy hoisted here from the agent-loop
    # and MCP frontends so no frontend can forget it.)
    _require_admin_for_attachments(ctx, file_paths)
    text = content if content is not None else ""
    if not str(text).strip() and not file_paths and sticker_id is None:
        raise ValueError("send_message requires non-empty content, a file "
                         "attachment, and/or a sticker")
    stickers = None
    if sticker_id is not None:
        # Stickers resolve against the DESTINATION channel's own guild —
        # bots can only send a guild's stickers inside that guild, and the
        # in-guild lookup doubles as the guild-confinement refusal for
        # foreign sticker ids.
        guild = getattr(channel, "guild", None)
        if guild is None:
            raise ValueError("sticker_id requires a guild channel.")
        stickers = [_require_guild_sticker(
            guild, _as_int(sticker_id, "sticker_id"))]
    files = load_discord_attachments(file_paths)
    kwargs: Dict[str, Any] = {
        "allowed_mentions": allowed_mentions
        if allowed_mentions is not None else discord.AllowedMentions.none(),
    }
    if files:
        kwargs["files"] = files
    if stickers:
        kwargs["stickers"] = stickers
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
            "embeds": serialize_embeds(m.get("embeds")),
        } for m in page)
        if total is not None and len(hits) >= int(total):
            break
    return hits[:limit], total


def _drop_hits_actor_cannot_see(ctx: OpContext, guild, hits):
    """Guild-wide search can surface channels the invoking user can't read;
    apply the actor-visibility policy of _check_channel_visibility plus the
    history-specific Read Message History requirement — this is a HISTORY
    read, so read_messages alone is not enough (the generic gate stays
    read_messages-only because it also guards non-history ops like
    send_message). Real Members are filtered; bare id-holder actors are the
    MCP frontend's documented accepted risk and pass through. Hits in
    channels the bot no longer resolves are dropped as unverifiable."""
    actor = getattr(ctx, "author", None)
    if actor is None or not hasattr(actor, "guild_permissions"):
        return hits
    visible = []
    for hit in hits:
        channel = guild.get_channel(hit["channel_id"]) if guild else None
        if channel is None or not hasattr(channel, "permissions_for"):
            continue
        try:
            p = channel.permissions_for(actor)
            # History reads need BOTH: View Channel and Read Message History.
            # Discord treats them as distinct permissions (announcements and
            # ticket channels commonly grant the first and deny the second),
            # and search_history runs on the BOT's perms, so the actor's own
            # history permission must be enforced here.
            if p.read_messages and p.read_message_history:
                visible.append(hit)
        except Exception:  # noqa: BLE001 - odd channel types err to hidden
            continue
    return visible


def _require_actor_history_perm(ctx: OpContext, channel: Any) -> None:
    """History-class reads (chronological scans, pin listings) need Read
    Message History, which the generic visibility gate deliberately does NOT
    check (it stays read_messages-only because it also guards non-history
    ops like send_message). Same policy as search_history's fallback branch
    (#71): real Members are enforced; bare id-holder actors are the MCP
    frontend's documented accepted risk and pass through. Raises ValueError,
    which Op.__call__ surfaces as OpResult(ok=False)."""
    actor = getattr(ctx, "author", None)
    if actor is None or not hasattr(actor, "guild_permissions"):
        return
    try:
        allowed = bool(channel.permissions_for(actor).read_message_history)
    except Exception:  # noqa: BLE001 - odd channel types err to hidden
        allowed = False
    if not allowed:
        raise ValueError("actor lacks Read Message History in this channel")


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
                "matches text inside embeds/links). The recent-window "
                "fallback approximates this with a whole-word match on "
                "message text only.",
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
        "hit tagged with its channel_id; when present, `total_matches` is "
        "how many exist in all — report that number honestly when it "
        "exceeds what was returned. When `total_matches` is absent, some "
        "matches were hidden from the invoking user (see the `note`) — do "
        "NOT state or estimate a server-wide total. Read the results and "
        "answer in plain text — never paste "
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
        pre = len(hits)
        hits = _drop_hits_actor_cannot_see(ctx, guild, hits)
        payload = {"messages": hits, "count": len(hits)}
        if len(hits) < pre:
            # Some matches sit in channels the invoking user cannot read.
            # The index's total counts THOSE too, so returning it would
            # disclose activity in hidden channels — omit it and say why.
            payload["note"] = ("some matches were in channels the invoking "
                               "user cannot read and were dropped; "
                               "total_matches omitted")
        else:
            payload["total_matches"] = total
        return payload
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
        # The fallback bypasses _drop_hits_actor_cannot_see (it reads with
        # the BOT's perms), so enforce the actor's Read Message History
        # here; same real-Member-only policy as the generic gate.
        actor = getattr(ctx, "author", None)
        if actor is not None and hasattr(actor, "guild_permissions"):
            try:
                if not scoped[0].permissions_for(actor).read_message_history:
                    return {"messages": [], "count": 0,
                            "note": ("actor lacks Read Message History in "
                                     "this channel")}
            except Exception:  # noqa: BLE001 - odd channel types err to hidden
                return {"messages": [], "count": 0,
                        "note": ("could not verify actor's history "
                                 "permission in this channel")}
        results = []
        # Whole-word regex approximates the index path's whole-word
        # semantics — a bare substring test would silently CHANGE what
        # counts as a match between the two paths.
        word = (re.compile(rf"\b{re.escape(contains)}\b", re.IGNORECASE)
                if contains is not None else None)
        async for message in scoped[0].history(limit=limit):
            if author_id is not None and message.author.id != author_id:
                continue
            if word is not None and not word.search(_message_search_text(message)):
                continue
            results.append(serialize_message(message))
        note = (f"search index unavailable; scanned only the "
                f"{limit} most recent messages")
        if contains is not None:
            note += "; `contains` matched via whole-word approximation"
        return {"messages": results, "count": len(results), "note": note}


def _serialize_full_message(message: Any) -> Dict[str, Any]:
    """serialize_message plus the inspection fields get_message promises:
    attachments (filename+url), pinned flag, jump_url. Embeds ride on
    serialize_message (bodies, not a count) so read_history / search_history
    / get_message share one shape."""
    payload = serialize_message(message)
    payload["attachments"] = [
        {"filename": a.filename, "url": a.url}
        for a in (message.attachments or [])
    ]
    payload["pinned"] = bool(getattr(message, "pinned", False))
    payload["jump_url"] = getattr(message, "jump_url", None)
    return payload


@registry.op(
    "get_message",
    "Read a single message by id: content, author, attachments (filename "
    "and url), embeds (title/description/fields), pinned flag, and jump_url. "
    "Pure read.",
    PermissionLevel.EVERYONE,
    params=[OpParam("message", ParamKind.MESSAGE, "Discord message id to read.")],
    serialize=_serialize_full_message,
    agent_guidance=(
        "get_message reads one specific message (e.g. a linked or referenced "
        "one) — use it instead of search_history when you already have the "
        "message id."),
    scope=OpScope.GUILD,
    group="messaging",
)
async def get_message(ctx: OpContext, message):
    # The MESSAGE resolver already fetched the message and the generic
    # channel-visibility gate already ran; this impl is a bare pass-through
    # to the serializer.
    return message


@registry.op(
    "read_history",
    "Read a channel's message history chronologically (no keyword filter), "
    "oldest first. Defaults to the most recent messages; page backwards "
    "with before_message_id, or forwards from a known point with "
    "after_message_id. For keyword or author-filtered search use "
    "search_history instead.",
    PermissionLevel.EVERYONE,
    params=[
        OpParam("channel", ParamKind.CHANNEL, "Channel to read."),
        OpParam("limit", ParamKind.INTEGER,
                f"Max messages to return (default 50, clamped to "
                f"{HISTORY_LIMIT_MAX}).",
                required=False, default=50, minimum=1, maximum=HISTORY_LIMIT_MAX),
        OpParam("before_message_id", ParamKind.SNOWFLAKE,
                "Optional pagination cursor: only messages older than this "
                "id. Pass the previous page's oldest message id to walk "
                "further back.",
                required=False),
        OpParam("after_message_id", ParamKind.SNOWFLAKE,
                "Optional cursor: only messages newer than this id.",
                required=False),
    ],
    serialize=lambda payload: payload,
    agent_guidance=(
        "read_history returns messages oldest-first within the page. To page "
        "backwards through history, call again with before_message_id set to "
        "the first row's id until a page comes back empty. It reads only — "
        "for keyword questions use search_history."),
    scope=OpScope.GUILD,
    group="messaging",
)
async def read_history(ctx: OpContext, channel, limit: int = 50,
                       before_message_id: Optional[int] = None,
                       after_message_id: Optional[int] = None):
    # The generic gate checks read_messages only; a chronological scan is a
    # HISTORY read and must also enforce the actor's Read Message History
    # (same #71 policy as search_history's fallback branch).
    _require_actor_history_perm(ctx, channel)
    before = (discord.Object(id=before_message_id)
              if before_message_id is not None else None)
    after = (discord.Object(id=after_message_id)
             if after_message_id is not None else None)
    rows = []
    async for message in channel.history(limit=limit, before=before,
                                         after=after):
        rows.append(serialize_message(message))
    # history() iteration order depends on the cursors (newest-first by
    # default, oldest-first when `after` is set); snowflakes are monotonic,
    # so sorting by id presents oldest-first regardless.
    rows.sort(key=lambda r: r["id"])
    return {"messages": rows, "count": len(rows)}


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
    "unpin_message",
    "Unpin a message in its channel. Requires admin (mirror of pin_message; "
    "also the only way to manage Discord's 50-pin-per-channel cap).",
    PermissionLevel.ADMIN,
    params=[OpParam("message", ParamKind.MESSAGE, "Discord message id to unpin.")],
    scope=OpScope.GUILD,
    group="messaging",
)
async def unpin_message(ctx: OpContext, message):
    await message.unpin()
    return True


@registry.op(
    "list_pins",
    "List a channel's pinned messages (newest pin first), each with its "
    "pinned_at timestamp. Read-only — use it before pin_message/"
    "unpin_message to manage the 50-pin cap.",
    PermissionLevel.EVERYONE,
    params=[
        OpParam("channel", ParamKind.CHANNEL, "Channel whose pins to list."),
        OpParam("limit", ParamKind.INTEGER,
                "Max pinned messages to return (default 50, clamped to 50 — "
                "Discord's per-channel pin cap).",
                required=False, default=50, minimum=1, maximum=50),
    ],
    serialize=lambda payload: payload,
    scope=OpScope.GUILD,
    group="messaging",
)
async def list_pins(ctx: OpContext, channel, limit: int = 50):
    # Discord gates the pins endpoint itself on Read Message History, so the
    # actor must hold it too (#71 policy; the generic gate checks only
    # read_messages).
    _require_actor_history_perm(ctx, channel)
    rows = []
    async for message in channel.pins(limit=limit):
        row = serialize_message(message)
        pinned_at = getattr(message, "pinned_at", None)
        row["pinned_at"] = pinned_at.isoformat() if pinned_at else None
        rows.append(row)
    return {"messages": rows, "count": len(rows)}


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


def _reaction_matches(reaction: Any, emoji: str) -> bool:
    """Match a wire emoji string against a live Reaction: the literal form
    (unicode char or '<:name:id>') or the name:id reaction_form add_reaction
    accepts — both, so callers can echo back whatever form they hold."""
    if str(reaction.emoji) == emoji:
        return True
    em = reaction.emoji
    name = getattr(em, "name", None)
    eid = getattr(em, "id", None)
    return name is not None and eid is not None and f"{name}:{eid}" == emoji


@registry.op(
    "list_reactions",
    "List the reactions on a message (emoji, count, whether the bot "
    "reacted). Pass emoji to also get the user ids who reacted with that "
    "emoji. Read-only.",
    PermissionLevel.EVERYONE,
    params=[
        OpParam("message", ParamKind.MESSAGE,
                "Message whose reactions to list."),
        OpParam("emoji", ParamKind.STRING,
                "Optional: only this emoji, including its reactor user ids "
                "(unicode emoji or `name:id` custom emoji).",
                required=False),
        OpParam("limit", ParamKind.INTEGER,
                "Max reactor user ids to return when emoji is given "
                "(default 100, clamped to 100).",
                required=False, default=100, minimum=1, maximum=100),
    ],
    serialize=lambda payload: payload,
    agent_guidance=(
        "list_reactions answers 'who voted / who reacted': call it without "
        "emoji to see the tallies, then again with emoji to enumerate the "
        "reactors of one option. Emoji take the same literal form as "
        "add_reaction (unicode char or name:id)."),
    scope=OpScope.GUILD,
    group="messaging",
)
async def list_reactions(ctx: OpContext, message, emoji: Optional[str] = None,
                         limit: int = 100):
    reactions = [
        {"emoji": str(r.emoji), "count": r.count, "me": bool(r.me)}
        for r in (message.reactions or [])
    ]
    payload: Dict[str, Any] = {"reactions": reactions}
    if emoji is not None:
        target = next((r for r in (message.reactions or [])
                       if _reaction_matches(r, emoji)), None)
        users: List[int] = []
        if target is not None:
            async for u in target.users(limit=limit):
                users.append(u.id)
        payload["users"] = users
    return payload


@registry.op(
    "trigger_typing",
    "Show the bot's typing indicator in a channel for ~10 seconds (it "
    "self-expires; sending a message also clears it). Cosmetic only.",
    PermissionLevel.EVERYONE,
    params=[
        OpParam("channel", ParamKind.CHANNEL,
                "Channel to show the typing indicator in."),
    ],
    scope=OpScope.GUILD,
    group="messaging",
)
async def trigger_typing(ctx: OpContext, channel):
    # Awaiting the Typing object fires the one-shot ~10s indicator (2.6).
    await channel.typing()
    return True


@registry.op(
    "forward_message",
    "Forward a message to another channel in the SAME guild (Discord's "
    "forward feature; the forward arrives as a new bot-owned message, "
    "deletable via delete_message). Cross-guild destinations are refused.",
    PermissionLevel.EVERYONE,
    params=[
        OpParam("message", ParamKind.MESSAGE, "Message to forward."),
        # A SNOWFLAKE scalar on purpose, NOT a second ParamKind.CHANNEL: the
        # MESSAGE param already claims the channel_id wire name for the
        # SOURCE channel, so the destination must travel under its own name
        # (same dodge delete_dm documents for its message_id).
        OpParam("destination_channel_id", ParamKind.SNOWFLAKE,
                "Channel id to forward into (must be in the same guild)."),
    ],
    serialize=lambda m: {"message_id": m.id, "channel_id": m.channel.id},
    agent_guidance=(
        "forward_message posts a forward-embed of the source message into "
        "the destination channel and returns the new message's id — use "
        "delete_message on that id to undo. The destination must be in the "
        "same guild as the source."),
    scope=OpScope.GUILD,
    group="messaging",
)
async def forward_message(ctx: OpContext, message, destination_channel_id: int):
    # The destination arrives as a bare snowflake, so the shared resolver's
    # guild confinement must be applied here in-impl: confine it to the
    # SOURCE message's guild, which both keeps the op honestly GUILD-scoped
    # and blocks cross-guild exfiltration regardless of frontend policy.
    source_guild = getattr(message, "guild", None) or getattr(
        getattr(message, "channel", None), "guild", None)
    if source_guild is None:
        raise ValueError("forward_message requires a guild message as its source.")
    destination = await resolve_channel(
        ctx.bot, _as_int(destination_channel_id, "destination_channel_id"),
        frozenset({source_guild.id}))
    # The generic gate already covered the SOURCE channel (via the resolved
    # message); the destination resolved after gating, so check it here.
    vis_ok, vis_reason = _check_channel_visibility(
        ctx, {"destination": destination})
    if not vis_ok:
        raise ValueError(vis_reason)
    return await message.forward(destination)


@registry.op(
    "suppress_embeds",
    "Hide (or restore) the embeds on a message — the link-preview boxes. "
    "Requires admin: unlike edit_message it works on ANY message, not just "
    "the bot's own. Fully reversible with suppress=false.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("message", ParamKind.MESSAGE,
                "Message whose embeds to hide/show."),
        OpParam("suppress", ParamKind.BOOLEAN,
                "True hides embeds, False restores them.",
                required=False, default=True),
    ],
    scope=OpScope.GUILD,
    group="messaging",
)
async def suppress_embeds(ctx: OpContext, message, suppress: bool = True):
    await message.edit(suppress=suppress)
    return True


@registry.op(
    "send_embed",
    "Send a rich embed to a channel: title, description, link url, color, "
    "image, footer — all optional, but at least one of title/description/"
    "image_url is required. Never pings. Reversible via delete_message.",
    PermissionLevel.EVERYONE,
    params=[
        OpParam("channel", ParamKind.CHANNEL, "Channel to send into."),
        OpParam("title", ParamKind.STRING,
                "Embed title (max 256 chars).", required=False),
        OpParam("description", ParamKind.STRING,
                "Embed body text (max 4096 chars).", required=False),
        OpParam("url", ParamKind.STRING,
                "Link the title points at.", required=False),
        OpParam("color", ParamKind.STRING,
                "Accent color, hex like '#5865F2'.", required=False),
        OpParam("image_url", ParamKind.STRING,
                "Image to display in the embed body.", required=False),
        OpParam("footer", ParamKind.STRING,
                "Footer text.", required=False),
        OpParam("allowed_mentions", ParamKind.INTERNAL),
    ],
    serialize=_serialize_sent_message,
    agent_guidance=(
        "send_embed is send_message with rich formatting — use it for "
        "announcement-style output, not for ordinary replies. It returns the "
        "new message's message_id; reuse it for edits or reactions."),
    scope=OpScope.GUILD,
    group="messaging",
)
async def send_embed(ctx: OpContext, channel, title: Optional[str] = None,
                     description: Optional[str] = None,
                     url: Optional[str] = None, color: Optional[str] = None,
                     image_url: Optional[str] = None,
                     footer: Optional[str] = None, allowed_mentions=None):
    if not any((title, description, image_url)):
        raise ValueError(
            "send_embed requires at least one of title/description/image_url")
    embed = discord.Embed(title=title, description=description, url=url)
    if color is not None:
        embed.colour = _parse_color(color)
    if image_url is not None:
        embed.set_image(url=image_url)
    if footer is not None:
        embed.set_footer(text=footer)
    # Same never-ping default as send_message (embed text can't ping, but
    # the policy travels with every send-class op so no caller can forget).
    return await channel.send(
        embed=embed,
        allowed_mentions=allowed_mentions
        if allowed_mentions is not None else discord.AllowedMentions.none())


@registry.op(
    "send_poll",
    "Post a native Discord poll to a channel: a question and 2-10 answer "
    "options, open for duration_hours. The poll rides an ordinary message, "
    "so delete_message removes it.",
    PermissionLevel.EVERYONE,
    params=[
        OpParam("channel", ParamKind.CHANNEL,
                "Channel to post the poll in."),
        OpParam("question", ParamKind.STRING,
                "Poll question (max 300 chars)."),
        OpParam("answers", ParamKind.STRING_LIST,
                "Answer options, 2-10 entries."),
        OpParam("duration_hours", ParamKind.INTEGER,
                "How long the poll stays open, in hours (default 24, max "
                "168 = one week).",
                required=False, default=24, minimum=1, maximum=168),
        OpParam("multiselect", ParamKind.BOOLEAN,
                "Allow voters to pick multiple answers (default false).",
                required=False, default=False),
    ],
    serialize=_serialize_sent_message,
    agent_guidance=(
        "send_poll returns the poll message's message_id — reuse it with "
        "get_poll_results to read the tallies, and end_poll to close it "
        "early. Votes are NOT reactions; list_reactions won't see them."),
    scope=OpScope.GUILD,
    group="messaging",
)
async def send_poll(ctx: OpContext, channel, question: str, answers: List[str],
                    duration_hours: int = 24, multiselect: bool = False):
    if len(str(question)) > 300:
        raise ValueError("Poll questions are capped at 300 characters.")
    options = [str(a) for a in (answers or []) if str(a).strip()]
    if not 2 <= len(options) <= 10:
        raise ValueError(
            f"Polls need 2-10 answer options, got {len(options)}.")
    poll = discord.Poll(question=question,
                        duration=timedelta(hours=duration_hours),
                        multiple=multiselect)
    for text in options:
        poll.add_answer(text=text)
    return await channel.send(poll=poll,
                              allowed_mentions=discord.AllowedMentions.none())


def _require_message_poll(message: Any):
    poll = getattr(message, "poll", None)
    if poll is None:
        raise ValueError("That message has no poll.")
    return poll


@registry.op(
    "get_poll_results",
    "Read a poll's current tallies from its message: question, per-answer "
    "vote counts, expiry time, and whether it has finalized. Read-only.",
    PermissionLevel.EVERYONE,
    params=[
        OpParam("message", ParamKind.MESSAGE,
                "Message carrying the poll to read."),
    ],
    serialize=lambda payload: payload,
    agent_guidance=(
        "get_poll_results reads the live tallies at call time; finalized: "
        "false means voting is still open and the counts can still move."),
    scope=OpScope.GUILD,
    group="messaging",
)
async def get_poll_results(ctx: OpContext, message):
    poll = _require_message_poll(message)
    expires_at = poll.expires_at
    return {
        "question": poll.question,
        "answers": [{"text": a.text, "count": a.vote_count}
                    for a in poll.answers],
        "expires_at": expires_at.isoformat() if expires_at else None,
        "finalized": bool(poll.is_finalized()),
    }


@registry.op(
    "end_poll",
    "End one of the BOT's own polls immediately, finalizing the results. "
    "Refuses polls on messages the bot did not author — mirroring "
    "edit_message's own-message discipline.",
    PermissionLevel.EVERYONE,
    params=[
        OpParam("message", ParamKind.MESSAGE,
                "Bot-authored message carrying the poll to end."),
    ],
    serialize=lambda m: {"message_id": m.id},
    scope=OpScope.GUILD,
    group="messaging",
)
async def end_poll(ctx: OpContext, message):
    _require_message_poll(message)
    if message.author.id != ctx.bot.user.id:
        raise ValueError(
            "end_poll can only end polls on the bot's own messages.")
    return await message.end_poll()


@registry.op(
    "get_poll_voters",
    "List who voted for one poll answer (Discord polls are non-anonymous — "
    "the client shows this list to every channel member on click). Get the "
    "answer_id from get_poll_results. Read-only.",
    PermissionLevel.EVERYONE,
    params=[
        OpParam("message", ParamKind.MESSAGE,
                "Message carrying the poll."),
        # Poll answer ids are small per-poll ordinals, not snowflakes, but
        # every *_id wire param travels as a string by registry convention
        # (see _SNOWFLAKE_JSON_TYPE) — SNOWFLAKE kind gives the string wire
        # type and central int coercion.
        OpParam("answer_id", ParamKind.SNOWFLAKE,
                "Answer id to enumerate voters for (from "
                "get_poll_results)."),
        OpParam("limit", ParamKind.INTEGER,
                "Max voters to return (default 100, clamped to 100).",
                required=False, default=100, minimum=1, maximum=100),
    ],
    serialize=lambda payload: payload,
    agent_guidance=(
        "get_poll_voters answers 'who voted for X': get_poll_results first "
        "for the answer ids and tallies, then this per answer. Poll votes "
        "are NOT reactions — list_reactions cannot see them."),
    scope=OpScope.GUILD,
    group="messaging",
)
async def get_poll_voters(ctx: OpContext, message, answer_id: int,
                          limit: int = 100):
    poll = _require_message_poll(message)
    answer = poll.get_answer(_as_int(answer_id, "answer_id"))
    if answer is None:
        raise ValueError(
            f"Poll has no answer with id {answer_id} — see "
            f"get_poll_results for valid answer ids.")
    voters = []
    async for u in answer.voters(limit=limit):
        voters.append({
            "id": u.id,
            "name": getattr(u, "name", None),
            "display_name": getattr(u, "display_name", None),
        })
    return {
        "answer_id": answer.id,
        "text": getattr(answer, "text", None),
        "voters": voters,
        "count": len(voters),
    }


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
# DM ops take a user id and nothing else — no membership check is needed,
# because Discord itself refuses bot DMs to users who share no guild.
# Consequence for the ADMIN gate: over guild-less frontends (MCP) there is
# no guild admin list to consult, so DM ops are effectively superadmin-only
# there; in-bot, ambient ctx.guild keeps the per-guild admin check.
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


@registry.op(
    "delete_dm",
    "Delete a DM message the bot itself sent to a user — the retract "
    "button for a mistaken send_dm. Refuses messages the bot did not "
    "author: Discord allows no way to delete the other participant's "
    "side of a DM.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("user", ParamKind.USER,
                "Discord user id whose DM conversation holds the message."),
        # A SNOWFLAKE scalar on purpose, NOT ParamKind.MESSAGE: MESSAGE
        # implies a required channel_id and resolves through the
        # guild-refusing channel resolver — exactly what DM ops must avoid
        # (see the section comment above).
        OpParam("message_id", ParamKind.SNOWFLAKE,
                "Discord message id of the bot-authored DM to delete."),
    ],
    agent_guidance=(
        "delete_dm retracts one of the BOT's own DM messages; it can never "
        "delete anything the user wrote. The stored transcript row is kept, "
        "so read_dms still shows what was sent and later retracted."),
    scope=OpScope.DM,
    group="dm",
)
async def delete_dm(ctx: OpContext, user, message_id: int):
    channel = user.dm_channel or await user.create_dm()
    message = await channel.fetch_message(message_id)
    if message.author.id != ctx.bot.user.id:
        raise ValueError(
            "delete_dm can only retract the bot's own messages — Discord "
            "does not allow deleting the other participant's DMs.")
    await message.delete()
    # The logs/dms/ transcript row is deliberately untouched: it is the
    # audit record that the DM existed and was retracted.
    return True


@registry.op(
    "edit_dm",
    "Edit a DM message the bot itself sent to a user — the fix-the-typo "
    "sibling of delete_dm. Refuses messages the bot did not author: "
    "Discord allows no way to edit the other participant's side of a DM.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("user", ParamKind.USER,
                "Discord user id whose DM conversation holds the message."),
        # Same SNOWFLAKE-not-MESSAGE dodge as delete_dm: MESSAGE implies a
        # required channel_id and the guild-refusing channel resolver.
        OpParam("message_id", ParamKind.SNOWFLAKE,
                "Discord message id of the bot-authored DM to edit."),
        OpParam("content", ParamKind.STRING, "Replacement text."),
    ],
    serialize=lambda m: {"message_id": m.id},
    agent_guidance=(
        "edit_dm rewrites one of the BOT's own DM messages in place; it can "
        "never touch anything the user wrote. The stored transcript keeps "
        "the original row and gains an edited:true row, so read_dms shows "
        "both what was first sent and the correction."),
    scope=OpScope.DM,
    group="dm",
)
async def edit_dm(ctx: OpContext, user, message_id: int, content: str):
    if not str(content).strip():
        raise ValueError("edit_dm requires non-empty replacement content")
    channel = user.dm_channel or await user.create_dm()
    message = await channel.fetch_message(message_id)
    if message.author.id != ctx.bot.user.id:
        raise ValueError(
            "edit_dm can only edit the bot's own messages — Discord does "
            "not allow editing the other participant's DMs.")
    edited = await message.edit(content=content)
    # Append an update note to the transcript (the original row is kept as
    # the audit record of what was first sent), same failure policy as
    # send_dm: a storage failure must not undo a successful edit.
    try:
        row = row_from_message(edited, user.id)
        row["edited"] = True
        log_dm(user.id, row)
    except Exception:  # noqa: BLE001
        logger = getattr(ctx.bot, "logger", None)
        if logger:
            logger.warning("edit_dm: failed to persist DM edit note",
                           exc_info=True)
    return edited


def _dm_conversation_rows(limit: int) -> List[Dict[str, Any]]:
    """Blocking half of list_dm_conversations (runs via asyncio.to_thread,
    same as read_dms' file I/O): enumerate stored transcripts and read each
    one's newest row for a last_message_at timestamp."""
    rows = []
    for uid in list_dm_users()[:limit]:
        tail = load_dms(uid, limit=1)
        rows.append({
            "user_id": uid,
            "last_message_at": tail[-1]["timestamp"] if tail else None,
        })
    return rows


@registry.op(
    "list_dm_conversations",
    "List the users who have a stored DM transcript (the entry point for "
    "read_dms when the user id is not already known): user_id, cached "
    "user_name (null when the user is not in the bot's cache), and the "
    "timestamp of the newest stored row. Reads local transcript storage "
    "only — same privacy class as read_dms.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("limit", ParamKind.INTEGER,
                "Max conversations to return (default 100, clamped to 1000).",
                required=False, default=100, minimum=1, maximum=1000),
    ],
    serialize=lambda payload: payload,
    agent_guidance=(
        "list_dm_conversations covers only conversations recorded since "
        "transcript storage was enabled — a user missing here may still "
        "have real DM history (fetch_dms reads it once you have their id). "
        "user_name is a cache convenience and comes back null for users "
        "the bot cannot currently see."),
    scope=OpScope.DM,
    group="dm",
)
async def list_dm_conversations(ctx: OpContext, limit: int = 100):
    # File I/O off the event loop, same as read_dms.
    rows = await asyncio.to_thread(_dm_conversation_rows, limit)
    for row in rows:
        cached = ctx.bot.get_user(row["user_id"])
        row["user_name"] = cached.name if cached else None
    return {"conversations": rows, "count": len(rows)}


@registry.op(
    "add_dm_reaction",
    "Add the bot's emoji reaction to a message in a DM conversation — a "
    "lightweight acknowledgement of a user's DM without sending text. The "
    "DM mirror of add_reaction (whose MESSAGE param structurally cannot "
    "reach DM channels). Fully reversible via remove_dm_reaction.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("user", ParamKind.USER,
                "Discord user id whose DM conversation holds the message."),
        # Same SNOWFLAKE-not-MESSAGE dodge as delete_dm: MESSAGE implies a
        # required channel_id and the guild-refusing channel resolver.
        OpParam("message_id", ParamKind.SNOWFLAKE,
                "DM message id to react to."),
        OpParam("emoji", ParamKind.STRING,
                "Emoji to react with (unicode emoji or `name:id` custom "
                "emoji)."),
    ],
    agent_guidance=(
        "add_dm_reaction takes the same literal-emoji form as add_reaction "
        "(a unicode character or name:id — never a word or description) and "
        "reacts inside a private DM, not in any channel."),
    scope=OpScope.DM,
    group="dm",
)
async def add_dm_reaction(ctx: OpContext, user, message_id: int, emoji: str):
    channel = user.dm_channel or await user.create_dm()
    message = await channel.fetch_message(message_id)
    await message.add_reaction(emoji)
    return True


@registry.op(
    "remove_dm_reaction",
    "Remove the bot's own emoji reaction from a message in a DM "
    "conversation. Only reactions the bot itself added can be removed — "
    "the other participant's reactions are untouchable by design, "
    "mirroring the guild remove_reaction guarantee.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("user", ParamKind.USER,
                "Discord user id whose DM conversation holds the message."),
        OpParam("message_id", ParamKind.SNOWFLAKE,
                "DM message id to remove the bot's reaction from."),
        OpParam("emoji", ParamKind.STRING,
                "Emoji to remove (unicode emoji or `name:id` custom emoji)."),
    ],
    agent_guidance=(
        "remove_dm_reaction only removes reactions the bot itself added, "
        "and takes the same literal-emoji form as add_dm_reaction."),
    scope=OpScope.DM,
    group="dm",
)
async def remove_dm_reaction(ctx: OpContext, user, message_id: int,
                             emoji: str):
    channel = user.dm_channel or await user.create_dm()
    message = await channel.fetch_message(message_id)
    # Passing the bot's own user makes this structurally self-scoped: the
    # API call can only ever remove the bot's reaction.
    await message.remove_reaction(emoji, ctx.bot.user)
    return True


@registry.op(
    "list_dm_pins",
    "List the pinned messages in a DM conversation, same row shape as "
    "read_dms/fetch_dms (direction in/out, attachments with filename+url). "
    "Read-only.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("user", ParamKind.USER,
                "Discord user id whose DM conversation to read pins from."),
    ],
    serialize=lambda rows: {"messages": rows, "count": len(rows)},
    scope=OpScope.DM,
    group="dm",
)
async def list_dm_pins(ctx: OpContext, user):
    channel = user.dm_channel or await user.create_dm()
    rows = []
    async for message in channel.pins():
        rows.append(row_from_message(message, user.id))
    return rows


def _serialize_user_profile(u: Any) -> Dict[str, Any]:
    flags = getattr(u, "public_flags", None)
    accent = getattr(u, "accent_colour", None)
    avatar = getattr(u, "display_avatar", None)
    banner = getattr(u, "banner", None)
    return {
        "id": u.id,
        "name": u.name,
        "global_name": getattr(u, "global_name", None),
        "display_name": getattr(u, "display_name", None),
        "bot": bool(getattr(u, "bot", False)),
        "system": bool(getattr(u, "system", False)),
        "created_at": _iso(getattr(u, "created_at", None)),
        "avatar_url": str(avatar) if avatar else None,
        "banner_url": str(banner) if banner else None,
        "accent_color": (f"#{accent.value:06X}"
                         if accent is not None else None),
        "public_flags": ([f.name for f in flags.all()]
                         if flags is not None else []),
        "mutual_guilds": [
            {"id": g.id, "name": g.name}
            for g in (getattr(u, "mutual_guilds", None) or [])
        ],
    }


@registry.op(
    "get_user",
    "Look up any Discord user globally by id: name, global name, account "
    "creation date, bot/system flags, public profile badges, avatar/banner "
    "urls, and which guilds they share with the bot. Read-only public data "
    "— exactly what any Discord client renders for any user id. Guild-"
    "independent: works for DM correspondents who share no channel with "
    "the caller (use get_member for guild-specific facts like roles).",
    PermissionLevel.ADMIN,
    params=[
        OpParam("user", ParamKind.USER, "Discord user id to look up."),
    ],
    serialize=_serialize_user_profile,
    agent_guidance=(
        "get_user reads a user's global public profile; mutual_guilds lists "
        "the guilds the user shares with the BOT, not with the caller. For "
        "roles/nick/presence inside a guild, use get_member instead."),
    scope=OpScope.GLOBAL,
    group="guild",
)
async def get_user(ctx: OpContext, user):
    # The cache-then-fetch resolver may hand back a gateway-cached User,
    # which never carries banner/accent_colour (Discord only serves those
    # on GET /users/{id}); re-fetch for the full profile and fall back to
    # the resolved user if the fetch fails (the public-cache facts still
    # answer most of the question).
    try:
        return await ctx.bot.fetch_user(user.id)
    except Exception:  # noqa: BLE001
        return user


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
        # Role restriction: empty means everyone may use the emoji.
        "roles": [r.id for r in (getattr(emoji, "roles", None) or [])],
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
    "Rename an existing custom emoji, and/or restrict it to specific roles "
    "(role_ids; an empty list clears the restriction). Requires admin. "
    "Managed (integration-owned) emoji are refused.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD, "Discord guild id the emoji belongs to."),
        OpParam("emoji_id", ParamKind.SNOWFLAKE,
                "Custom emoji id to edit (from list_emojis)."),
        OpParam("name", ParamKind.STRING, "New emoji name."),
        OpParam("role_ids", ParamKind.STRING_LIST,
                "Optional role restriction: only these role ids may use the "
                "emoji. Pass an empty list to clear the restriction (back "
                "to everyone). Omit to leave roles unchanged.",
                required=False),
    ],
    serialize=serialize_emoji,
    agent_guidance=(
        "Renaming an emoji changes the :name: users type but keeps its id, so "
        "existing reactions and messages keep working. role_ids must come "
        "from list_roles — never guess one."),
    scope=OpScope.GUILD,
    group="emojis",
)
async def edit_emoji(ctx: OpContext, guild, emoji_id: int, name: str,
                     role_ids: Optional[List[str]] = None):
    emoji = _require_guild_emoji(guild, _as_int(emoji_id, "emoji_id"))
    _guard_emoji_editable(emoji)
    kwargs: Dict[str, Any] = {"name": name}
    if role_ids is not None:
        # Same in-guild refusal as ROLE params: an id from another guild
        # must not restrict (or unlock) an emoji through this op.
        kwargs["roles"] = [resolve_role(guild, _as_int(rid, "role_ids"))
                           for rid in role_ids]
    edited = await emoji.edit(
        reason=f"edit_emoji op by {ctx.author} ({ctx.author.id})", **kwargs,
    )
    return edited if edited is not None else emoji


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


# ---------------------------------------------------------------------------
# Stickers (2026-08 expressive-domain gap pass) — the exact sibling surface
# of the emoji ops above, sharing the "emojis" group (relabeled "Emojis &
# stickers"). Discord's sticker upload limit differs from the emoji one:
# 512KB, and lottie (.json) is a valid format alongside png/apng/gif.
# ---------------------------------------------------------------------------

STICKER_MAX_BYTES = 512 * 1024
STICKER_EXTENSIONS = frozenset({".png", ".apng", ".gif", ".json"})

# Where download_emoji drops fetched images: <repo>/media/tmp. A module
# constant (not inlined) so tests can point it at a tmp dir.
EMOJI_DOWNLOAD_DIR = Path(__file__).resolve().parent.parent / "media" / "tmp"


def serialize_sticker(sticker: Any) -> Dict[str, Any]:
    fmt = getattr(sticker, "format", None)
    return {
        "id": sticker.id,
        "name": sticker.name,
        "description": getattr(sticker, "description", None),
        # The related unicode emoji Discord requires on every sticker.
        "emoji": getattr(sticker, "emoji", None),
        "format": getattr(fmt, "name", str(fmt) if fmt is not None else None),
        "url": str(sticker.url),
    }


def _require_guild_sticker(guild, sticker_id: int):
    """Resolve a sticker id against THIS guild — same in-impl guild
    confinement as _require_guild_emoji (stickers have no ParamKind, so the
    shared resolver never sees them)."""
    for s in getattr(guild, "stickers", ()) or ():
        if s.id == sticker_id:
            return s
    raise ValueError(
        f"No custom sticker with id {sticker_id} in guild '{guild.name}'. "
        f"Call list_stickers to see valid ids."
    )


def _guard_sticker_editable(sticker: Any):
    """Only guild-owned stickers are editable/deletable; a standard
    (Discord-pack) sticker id must be refused, mirroring the managed-emoji
    guard."""
    stype = getattr(sticker, "type", None)
    if stype is not None and stype != discord.StickerType.guild:
        raise ValueError(
            f"Sticker '{sticker.name}' is not a guild sticker "
            f"(type {getattr(stype, 'name', stype)}) and cannot be modified."
        )


def load_sticker_file(file_path: str) -> Path:
    """Validate a local sticker file BEFORE any upload: existence,
    regular-file, extension allowlist, and Discord's 512KB sticker cap.
    Returns the resolved path (create_sticker hands discord.File the path,
    unlike the emoji API's raw bytes)."""
    path = Path(str(file_path).strip()).expanduser()
    try:
        path = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError(f"sticker file not found: {file_path}") from exc
    if not path.is_file():
        raise ValueError(f"sticker file is not a file: {path}")
    ext = path.suffix.lower()
    if ext not in STICKER_EXTENSIONS:
        raise ValueError(
            f"sticker file extension not allowed: {ext or '(none)'} "
            f"(allowed: {', '.join(sorted(STICKER_EXTENSIONS))})"
        )
    size = path.stat().st_size
    if size <= 0:
        raise ValueError(f"sticker file is empty: {path}")
    if size > STICKER_MAX_BYTES:
        raise ValueError(
            f"sticker file too large ({size} bytes > {STICKER_MAX_BYTES}). "
            f"Discord's sticker limit is 512KB — resize or re-encode it."
        )
    return path


@registry.op(
    "list_stickers",
    "List a guild's custom stickers (id, name, description, related emoji, "
    "format, image url). Use the id with send_message's sticker_id to send "
    "one.",
    PermissionLevel.EVERYONE,
    params=[OpParam("guild", ParamKind.GUILD, "Discord guild id to enumerate.")],
    serialize=lambda ss: {"stickers": ss, "count": len(ss)},
    agent_guidance=(
        "Use list_stickers to get a sticker's exact id before sending "
        "(send_message sticker_id) or edit/delete — never guess an id. "
        "Stickers are not emoji: they cannot be used in reactions."),
    scope=OpScope.GUILD,
    group="emojis",
)
async def list_stickers(ctx: OpContext, guild):
    return [serialize_sticker(s) for s in guild.stickers]


@registry.op(
    "create_sticker",
    "Upload a new custom sticker to a guild from a local file. Requires "
    "admin. File must be png/apng/gif/json(lottie) and under 512KB; Discord "
    "also requires a description and a related unicode emoji.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD,
                "Discord guild id to add the sticker to."),
        OpParam("name", ParamKind.STRING,
                "Sticker name (2-30 characters)."),
        OpParam("description", ParamKind.STRING,
                "Sticker description shown in the picker."),
        OpParam("emoji", ParamKind.STRING,
                "The related unicode emoji Discord requires (a literal "
                "emoji character, e.g. 😀)."),
        OpParam("file_path", ParamKind.STRING,
                "Absolute server-side path to the image "
                "(png/apng/gif/json, max 512KB)."),
    ],
    serialize=lambda s: {"id": s.id, "name": s.name, "url": str(s.url)},
    agent_guidance=(
        "create_sticker returns the new sticker's id — reuse it directly "
        "with send_message's sticker_id instead of calling list_stickers "
        "again. Guilds have a boost-tier sticker slot limit; if creation "
        "fails for a full guild, say so rather than retrying."),
    scope=OpScope.GUILD,
    group="emojis",
)
async def create_sticker(ctx: OpContext, guild, name: str, description: str,
                         emoji: str, file_path: str):
    # Reads the HOST filesystem — same explicit admin gate as create_emoji,
    # kept even though the op is already ADMIN so the rule survives a
    # future tier change.
    _require_admin_for_attachments(ctx, [file_path])
    if not 2 <= len(str(name)) <= 30:
        raise ValueError(
            f"Sticker names must be 2-30 characters, got {len(str(name))}.")
    path = load_sticker_file(file_path)
    file = discord.File(path, filename=path.name)
    try:
        return await guild.create_sticker(
            name=name, description=description, emoji=emoji, file=file,
            reason=f"create_sticker op by {ctx.author} ({ctx.author.id})",
        )
    finally:
        # discord.py closes files it was handed once the request runs;
        # File.close() is safe to repeat and covers pre-request failures.
        file.close()


@registry.op(
    "edit_sticker",
    "Edit a custom sticker's name, description, or related emoji. Requires "
    "admin. Reversible metadata edit; standard (Discord-pack) stickers are "
    "refused.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD,
                "Discord guild id the sticker belongs to."),
        OpParam("sticker_id", ParamKind.SNOWFLAKE,
                "Custom sticker id to edit (from list_stickers)."),
        OpParam("name", ParamKind.STRING,
                "New sticker name (2-30 characters).", required=False),
        OpParam("description", ParamKind.STRING,
                "New description.", required=False),
        OpParam("emoji", ParamKind.STRING,
                "New related unicode emoji.", required=False),
    ],
    serialize=lambda s: {"id": s.id, "name": s.name,
                         "description": getattr(s, "description", None)},
    scope=OpScope.GUILD,
    group="emojis",
)
async def edit_sticker(ctx: OpContext, guild, sticker_id: int,
                       name: Optional[str] = None,
                       description: Optional[str] = None,
                       emoji: Optional[str] = None):
    sticker = _require_guild_sticker(guild, _as_int(sticker_id, "sticker_id"))
    _guard_sticker_editable(sticker)
    kwargs: Dict[str, Any] = {}
    if name is not None:
        kwargs["name"] = name
    if description is not None:
        kwargs["description"] = description
    if emoji is not None:
        kwargs["emoji"] = emoji
    if not kwargs:
        raise ValueError(
            "Nothing to edit: pass at least one of name/description/emoji.")
    edited = await sticker.edit(
        reason=f"edit_sticker op by {ctx.author} ({ctx.author.id})", **kwargs,
    )
    return edited if edited is not None else sticker


@registry.op(
    "delete_sticker",
    "Delete a custom sticker from a guild. Requires admin. Standard "
    "(Discord-pack) stickers are refused.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD,
                "Discord guild id the sticker belongs to."),
        OpParam("sticker_id", ParamKind.SNOWFLAKE,
                "Custom sticker id to delete (from list_stickers)."),
    ],
    serialize=lambda info: info,
    agent_guidance=(
        "delete_sticker is irreversible — confirm intent before calling it. "
        "Messages already sent with the sticker keep rendering it."),
    scope=OpScope.GUILD,
    group="emojis",
)
async def delete_sticker(ctx: OpContext, guild, sticker_id: int):
    sticker = _require_guild_sticker(guild, _as_int(sticker_id, "sticker_id"))
    _guard_sticker_editable(sticker)
    info = {"deleted": True, "id": sticker.id, "name": sticker.name}
    await sticker.delete(
        reason=f"delete_sticker op by {ctx.author} ({ctx.author.id})",
    )
    return info


@registry.op(
    "download_emoji",
    "Download a custom emoji's (or, with sticker=true, a custom sticker's) "
    "image from Discord's CDN to a server-side file under media/tmp — the "
    "read half of cloning an asset between guilds (pair with "
    "create_emoji/create_sticker's file_path). Requires admin: it writes "
    "to the server filesystem, mirroring the attachment gate.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD,
                "Discord guild id the asset belongs to."),
        OpParam("emoji_id", ParamKind.SNOWFLAKE,
                "Custom emoji id (or sticker id when sticker=true) to "
                "download."),
        OpParam("sticker", ParamKind.BOOLEAN,
                "Treat the id as a sticker id instead of an emoji id "
                "(default false).",
                required=False, default=False),
    ],
    serialize=lambda payload: payload,
    agent_guidance=(
        "download_emoji returns the saved file_path — pass it straight to "
        "create_emoji (or create_sticker) on the destination guild to clone "
        "the asset."),
    scope=OpScope.GUILD,
    group="emojis",
)
async def download_emoji(ctx: OpContext, guild, emoji_id: int,
                         sticker: bool = False):
    asset_id = _as_int(emoji_id, "emoji_id")
    if sticker:
        asset = _require_guild_sticker(guild, asset_id)
        fmt = getattr(asset, "format", None)
        ext = "." + (getattr(fmt, "file_extension", None) or "png")
    else:
        asset = _require_guild_emoji(guild, asset_id)
        ext = ".gif" if getattr(asset, "animated", False) else ".png"
    data = await asset.read()
    # Asset names are Discord-validated (word characters), but sanitize
    # anyway — the name lands in a server-side filename.
    safe_name = re.sub(r"[^A-Za-z0-9_-]", "_", str(asset.name)) or "asset"
    dest = Path(EMOJI_DOWNLOAD_DIR) / f"{safe_name}_{asset.id}{ext}"
    # File I/O off the event loop, same policy as read_dms.
    def _write() -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    await asyncio.to_thread(_write)
    return {"file_path": str(dest), "bytes": len(data), "name": asset.name}


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
# Channels & threads (2026-08 channels-domain gap pass).
#
# Threads travel through the ordinary CHANNEL param kind — a thread IS a
# channel to the resolver and to the channel-visibility gate — with an
# in-impl isinstance guard where an op only makes sense for one or the
# other (edit_thread owns thread edits; edit_channel/set_slowmode refuse
# threads so there is exactly one op per edit surface).
#
# Deliberately NOT here (owner-tier decisions, see the channels gap sheet):
# channel create/delete/clone/move, overwrite writes, invites, webhooks,
# archived PRIVATE thread enumeration, and thread add_user/remove_user
# (an unsuppressable ping — conflicts with the never-ping invariant).
# ---------------------------------------------------------------------------

# Discord accepts exactly these auto-archive durations (minutes).
THREAD_AUTO_ARCHIVE_DURATIONS = (60, 1440, 4320, 10080)
# Discord's slowmode cap: 6 hours.
SLOWMODE_MAX_SECONDS = 21600


def _require_thread(channel: Any) -> Any:
    """Thread-only ops accept any CHANNEL on the wire; refuse non-threads
    with a clear error instead of an AttributeError mid-call."""
    if not isinstance(channel, discord.Thread):
        raise ValueError(
            f"Channel {getattr(channel, 'id', '?')} is not a thread "
            f"(got {type(channel).__name__})."
        )
    return channel


def serialize_thread(t: Any) -> Dict[str, Any]:
    created_at = getattr(t, "created_at", None)
    return {
        "id": t.id,
        "name": t.name,
        "parent_id": getattr(t, "parent_id", None),
        "owner_id": getattr(t, "owner_id", None),
        "archived": bool(getattr(t, "archived", False)),
        "locked": bool(getattr(t, "locked", False)),
        "member_count": getattr(t, "member_count", None),
        "message_count": getattr(t, "message_count", None),
        "slowmode_delay": getattr(t, "slowmode_delay", None),
        "auto_archive_duration": getattr(t, "auto_archive_duration", None),
        "created_at": created_at.isoformat() if created_at else None,
    }


@registry.op(
    "get_channel_info",
    "Read one channel's full details: topic, nsfw flag, slowmode, category, "
    "position, created_at — plus voice specifics (bitrate/user_limit), forum "
    "tags (available_tags), and the parent id for threads, where they apply. "
    "Pure read; same detail every member sees in the client's channel "
    "settings.",
    PermissionLevel.EVERYONE,
    params=[OpParam("channel", ParamKind.CHANNEL,
                    "Discord channel id to inspect.")],
    serialize=lambda payload: payload,
    agent_guidance=(
        "get_channel_info is the detail view to list_channels' index — use "
        "it when topic/nsfw/slowmode/category matters, and to read a forum's "
        "available_tags before create_forum_post."),
    scope=OpScope.GUILD,
    group="guild-info",
)
async def get_channel_info(ctx: OpContext, channel):
    category = getattr(channel, "category", None)
    created_at = getattr(channel, "created_at", None)
    payload: Dict[str, Any] = {
        "id": channel.id,
        "name": channel.name,
        "type": str(channel.type),
        "topic": getattr(channel, "topic", None),
        "nsfw": bool(getattr(channel, "nsfw", False)),
        "category_id": getattr(channel, "category_id", None),
        "category_name": getattr(category, "name", None),
        "position": getattr(channel, "position", None),
        "created_at": created_at.isoformat() if created_at else None,
        "slowmode_delay": getattr(channel, "slowmode_delay", None),
        "default_auto_archive_duration": getattr(
            channel, "default_auto_archive_duration", None),
    }
    # Type-specific facets appear only where the channel type carries them,
    # so a text channel's payload doesn't grow null voice fields.
    if hasattr(channel, "bitrate"):
        payload["bitrate"] = channel.bitrate
        payload["user_limit"] = getattr(channel, "user_limit", None)
    tags = getattr(channel, "available_tags", None)
    if tags is not None:
        payload["available_tags"] = [{"id": t.id, "name": t.name}
                                     for t in tags]
    if isinstance(channel, discord.Thread):
        payload["thread_parent_id"] = channel.parent_id
    return payload


def _drop_threads_actor_cannot_see(ctx: OpContext, guild, threads):
    """Guild-wide thread enumeration can surface threads whose PARENT channel
    the invoking user cannot read — same actor-visibility policy as
    _drop_hits_actor_cannot_see (real Members filtered; bare id-holder
    actors are the MCP frontend's documented accepted risk and pass
    through). Threads whose parent no longer resolves are dropped as
    unverifiable."""
    actor = getattr(ctx, "author", None)
    if actor is None or not hasattr(actor, "guild_permissions"):
        return threads
    visible = []
    for t in threads:
        parent = (guild.get_channel(getattr(t, "parent_id", None))
                  if guild else None)
        if parent is None or not hasattr(parent, "permissions_for"):
            continue
        try:
            if parent.permissions_for(actor).read_messages:
                visible.append(t)
        except Exception:  # noqa: BLE001 - odd channel types err to hidden
            continue
    return visible


@registry.op(
    "list_threads",
    "List threads: active guild-wide by default, or one parent channel's "
    "threads (optionally including its archived PUBLIC threads). Read-only. "
    "Archived private threads are never listed.",
    PermissionLevel.EVERYONE,
    params=[
        OpParam("guild", ParamKind.GUILD,
                "Discord guild id to enumerate (active threads guild-wide)."),
        OpParam("channel", ParamKind.CHANNEL,
                "Optional channel id to restrict to one parent channel.",
                required=False),
        OpParam("include_archived", ParamKind.BOOLEAN,
                "Also list archived public threads (requires channel_id; "
                "default false).",
                required=False, default=False),
        OpParam("limit", ParamKind.INTEGER,
                "Max threads to return (default 100).",
                required=False, default=100, minimum=1, maximum=500),
    ],
    serialize=lambda payload: payload,
    agent_guidance=(
        "list_threads answers 'what threads exist' — guild-wide for active "
        "ones, or per channel (add include_archived for that channel's "
        "archived public threads). Thread ids are channel ids: pass them as "
        "channel_id to read_history, join_thread, edit_thread, etc."),
    scope=OpScope.GUILD,
    group="threads",
)
async def list_threads(ctx: OpContext, guild, channel=None,
                       include_archived: bool = False, limit: int = 100):
    if include_archived and channel is None:
        raise ValueError(
            "include_archived requires a channel_id — Discord has no "
            "guild-wide archived-thread listing.")
    if channel is not None:
        if not hasattr(channel, "threads"):
            raise ValueError(
                f"Channel {channel.id} ({type(channel).__name__}) cannot "
                f"parent threads.")
        threads = list(channel.threads)
        if include_archived:
            # Public archived threads only. archived_threads(private=True)
            # needs manage_threads and enumerates invite-only conversations
            # — deliberately out of scope (thread MEMBERSHIP, not channel
            # readability, is the real boundary there).
            async for t in channel.archived_threads(limit=limit):
                threads.append(t)
    else:
        threads = list(await guild.active_threads())
    threads = _drop_threads_actor_cannot_see(ctx, guild, threads)
    rows = [serialize_thread(t) for t in threads[:limit]]
    return {"threads": rows, "count": len(rows)}


@registry.op(
    "list_thread_members",
    "List a thread's members (threads have explicit membership, unlike "
    "channels). Read-only; same list the client shows in the thread header.",
    PermissionLevel.EVERYONE,
    params=[OpParam("channel", ParamKind.CHANNEL,
                    "Discord thread id whose members to list (threads are "
                    "channels; a non-thread channel id is refused).")],
    serialize=lambda payload: payload,
    agent_guidance=(
        "list_thread_members answers 'who is in this thread' — explicit "
        "joins only, not everyone who could read the parent channel "
        "(list_members answers that)."),
    scope=OpScope.GUILD,
    group="threads",
)
async def list_thread_members(ctx: OpContext, channel):
    thread = _require_thread(channel)
    guild = getattr(thread, "guild", None)
    rows = []
    for tm in await thread.fetch_members():
        member = guild.get_member(tm.id) if guild else None
        joined_at = getattr(tm, "joined_at", None)
        rows.append({
            "id": tm.id,
            # Resolved via the guild member cache; None when the member is
            # not cached — the id is always present.
            "display_name": getattr(member, "display_name", None),
            "joined_at": joined_at.isoformat() if joined_at else None,
        })
    return {"members": rows, "count": len(rows)}


@registry.op(
    "join_thread",
    "Join the BOT to a thread, so it follows the conversation there. "
    "Requires admin. Only changes the bot's own membership; reversible via "
    "leave_thread.",
    PermissionLevel.ADMIN,
    params=[OpParam("channel", ParamKind.CHANNEL,
                    "Discord thread id to join.")],
    scope=OpScope.GUILD,
    group="threads",
)
async def join_thread(ctx: OpContext, channel):
    thread = _require_thread(channel)
    await thread.join()
    return True


@registry.op(
    "leave_thread",
    "Remove the BOT from a thread. Requires admin. Inverse of join_thread; "
    "only changes the bot's own membership.",
    PermissionLevel.ADMIN,
    params=[OpParam("channel", ParamKind.CHANNEL,
                    "Discord thread id to leave.")],
    scope=OpScope.GUILD,
    group="threads",
)
async def leave_thread(ctx: OpContext, channel):
    thread = _require_thread(channel)
    await thread.leave()
    return True


@registry.op(
    "edit_thread",
    "Edit a thread: rename, archive/unarchive, lock/unlock, slowmode, or "
    "auto-archive duration. Requires admin. Every flag is reversible "
    "(unarchive/unlock/rename back).",
    PermissionLevel.ADMIN,
    params=[
        OpParam("channel", ParamKind.CHANNEL, "Discord thread id to edit."),
        OpParam("name", ParamKind.STRING, "New thread name.", required=False),
        OpParam("archived", ParamKind.BOOLEAN,
                "Archive (true) or unarchive (false).", required=False),
        OpParam("locked", ParamKind.BOOLEAN,
                "Lock so only moderators can unarchive/reply.",
                required=False),
        OpParam("slowmode_delay", ParamKind.INTEGER,
                "Seconds between messages per user, 0 disables (max 21600).",
                required=False, minimum=0, maximum=SLOWMODE_MAX_SECONDS),
        OpParam("auto_archive_duration", ParamKind.INTEGER,
                "Minutes of inactivity before auto-archive: 60, 1440, 4320, "
                "or 10080.",
                required=False),
    ],
    serialize=lambda payload: payload,
    agent_guidance=(
        "edit_thread with archived=true is the reversible alternative to "
        "deleting a thread — the conversation is preserved and any member "
        "can reopen it (locked=true additionally restricts reopening to "
        "moderators)."),
    scope=OpScope.GUILD,
    group="threads",
)
async def edit_thread(ctx: OpContext, channel, name: Optional[str] = None,
                      archived: Optional[bool] = None,
                      locked: Optional[bool] = None,
                      slowmode_delay: Optional[int] = None,
                      auto_archive_duration: Optional[int] = None):
    thread = _require_thread(channel)
    kwargs: Dict[str, Any] = {}
    if name is not None:
        kwargs["name"] = name
    if archived is not None:
        kwargs["archived"] = archived
    if locked is not None:
        kwargs["locked"] = locked
    if slowmode_delay is not None:
        kwargs["slowmode_delay"] = slowmode_delay
    if auto_archive_duration is not None:
        if auto_archive_duration not in THREAD_AUTO_ARCHIVE_DURATIONS:
            raise ValueError(
                f"auto_archive_duration must be one of "
                f"{', '.join(str(d) for d in THREAD_AUTO_ARCHIVE_DURATIONS)} "
                f"(minutes), got {auto_archive_duration}.")
        kwargs["auto_archive_duration"] = auto_archive_duration
    if not kwargs:
        raise ValueError(
            "Nothing to edit: pass at least one of name/archived/locked/"
            "slowmode_delay/auto_archive_duration.")
    edited = await thread.edit(
        reason=f"edit_thread op by {ctx.author} ({ctx.author.id})", **kwargs)
    target = edited if edited is not None else thread
    return {
        "thread_id": target.id,
        "name": target.name,
        "archived": bool(getattr(target, "archived", False)),
        "locked": bool(getattr(target, "locked", False)),
        "slowmode_delay": getattr(target, "slowmode_delay", None),
        "auto_archive_duration": getattr(target, "auto_archive_duration",
                                         None),
    }


@registry.op(
    "set_slowmode",
    "Set a channel's slowmode (seconds between messages per user; 0 "
    "disables). Requires admin. Reversible; a deliberately atomic op — "
    "channel name/topic edits are edit_channel's job, thread slowmode is "
    "edit_thread's.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("channel", ParamKind.CHANNEL,
                "Discord channel id to set slowmode on."),
        OpParam("seconds", ParamKind.INTEGER,
                "Seconds between messages per user; 0 disables. Max 21600 "
                "(6h).",
                minimum=0, maximum=SLOWMODE_MAX_SECONDS),
    ],
    serialize=lambda payload: payload,
    scope=OpScope.GUILD,
    group="guild-info",
)
async def set_slowmode(ctx: OpContext, channel, seconds: int):
    if isinstance(channel, discord.Thread):
        raise ValueError(
            "set_slowmode does not accept threads — use edit_thread's "
            "slowmode_delay.")
    if not hasattr(channel, "slowmode_delay"):
        raise ValueError(
            f"Channel {channel.id} ({type(channel).__name__}) has no "
            f"slowmode.")
    await channel.edit(
        slowmode_delay=seconds,
        reason=f"set_slowmode op by {ctx.author} ({ctx.author.id})")
    return {"channel_id": channel.id, "slowmode_delay": seconds}


@registry.op(
    "edit_channel",
    "Edit a channel's name, topic, or nsfw flag. Requires admin. Reversible "
    "in-place edits only — position/category moves are move_channel's job, "
    "permission overwrites are set_channel_overwrite's. Threads are refused "
    "(edit_thread owns those).",
    PermissionLevel.ADMIN,
    params=[
        OpParam("channel", ParamKind.CHANNEL, "Discord channel id to edit."),
        OpParam("name", ParamKind.STRING, "New channel name.",
                required=False),
        OpParam("topic", ParamKind.STRING,
                "New channel topic (text/forum channels).", required=False),
        OpParam("nsfw", ParamKind.BOOLEAN, "Age-restrict the channel.",
                required=False),
    ],
    serialize=lambda payload: payload,
    scope=OpScope.GUILD,
    group="guild-info",
)
async def edit_channel(ctx: OpContext, channel, name: Optional[str] = None,
                       topic: Optional[str] = None,
                       nsfw: Optional[bool] = None):
    if isinstance(channel, discord.Thread):
        raise ValueError(
            "edit_channel does not accept threads — use edit_thread.")
    kwargs: Dict[str, Any] = {}
    if name is not None:
        kwargs["name"] = name
    if topic is not None:
        if not hasattr(channel, "topic"):
            raise ValueError(
                f"Channel {channel.id} ({type(channel).__name__}) has no "
                f"topic.")
        kwargs["topic"] = topic
    if nsfw is not None:
        kwargs["nsfw"] = nsfw
    if not kwargs:
        raise ValueError(
            "Nothing to edit: pass at least one of name/topic/nsfw.")
    edited = await channel.edit(
        reason=f"edit_channel op by {ctx.author} ({ctx.author.id})", **kwargs)
    # channel.edit returns the updated channel, or None for edits discord.py
    # treats as purely positional — fall back to the original object.
    target = edited if edited is not None else channel
    return {
        "id": target.id,
        "name": target.name,
        "topic": getattr(target, "topic", None),
        "nsfw": bool(getattr(target, "nsfw", False)),
        "type": str(target.type),
    }


@registry.op(
    "get_member_permissions",
    "Compute one member's EFFECTIVE permissions in one channel — guild "
    "roles plus channel overwrites, fully resolved. Requires admin, same "
    "gate as list_channel_overwrites (this resolves ACLs of channels "
    "hidden from ordinary members).",
    PermissionLevel.ADMIN,
    params=[
        OpParam("channel", ParamKind.CHANNEL,
                "Discord channel id to evaluate in."),
        OpParam("member", ParamKind.MEMBER,
                "Discord user id whose effective permissions to compute."),
    ],
    serialize=lambda payload: payload,
    agent_guidance=(
        "get_member_permissions answers 'why can/can't X do Y here' with the "
        "fully-resolved result; list_channel_overwrites shows the raw ACL "
        "entries it was computed from."),
    scope=OpScope.GUILD,
    group="guild-info",
)
async def get_member_permissions(ctx: OpContext, channel, member):
    perms = channel.permissions_for(member)
    return {
        "channel_id": channel.id,
        "user_id": member.id,
        "permissions": [name for name, value in perms if value],
    }


@registry.op(
    "create_forum_post",
    "Create a forum post: a thread with its required starter message in a "
    "forum channel (create_thread cannot serve forums — they refuse "
    "threads without content). Never pings. Optional forum tags.",
    PermissionLevel.EVERYONE,
    params=[
        OpParam("channel", ParamKind.CHANNEL,
                "Discord forum channel id to post in."),
        OpParam("name", ParamKind.STRING, "Post title."),
        OpParam("content", ParamKind.STRING,
                "Starter message body (forums require one)."),
        OpParam("tag_ids", ParamKind.STRING_LIST,
                "Optional forum tag ids to apply (from get_channel_info's "
                "available_tags).",
                required=False),
    ],
    serialize=lambda tm: {"thread_id": tm.thread.id, "name": tm.thread.name,
                          "message_id": tm.message.id},
    agent_guidance=(
        "create_forum_post is for forum channels only — text channels take "
        "create_thread. Tag ids must come from get_channel_info's "
        "available_tags, never guessed."),
    scope=OpScope.GUILD,
    group="threads",
)
async def create_forum_post(ctx: OpContext, channel, name: str, content: str,
                            tag_ids: Optional[List[str]] = None):
    if not isinstance(channel, discord.ForumChannel):
        raise ValueError(
            f"Channel {channel.id} ({type(channel).__name__}) is not a "
            f"forum channel — use create_thread for text channels.")
    if not str(content).strip():
        raise ValueError(
            "create_forum_post requires non-empty content — forums require "
            "a starter message.")
    applied = []
    if tag_ids:
        available = {t.id: t for t in channel.available_tags}
        for raw_id in tag_ids:
            tid = _as_int(raw_id, "tag_ids")
            if tid not in available:
                raise ValueError(
                    f"No forum tag with id {tid} in channel {channel.id} — "
                    f"see get_channel_info's available_tags.")
            applied.append(available[tid])
    kwargs: Dict[str, Any] = {
        "name": name,
        "content": content,
        # Same never-ping policy as every send-class op.
        "allowed_mentions": discord.AllowedMentions.none(),
    }
    if applied:
        kwargs["applied_tags"] = applied
    return await channel.create_thread(**kwargs)


# ---------------------------------------------------------------------------
# Guild & members (2026-08 guild-domain gap pass).
#
# SAFE_NOW bar for this section: an admin could already do it by hand in the
# Discord client without a confirmation dialog. That is why timeout_member is
# here (duration picker, no confirm, reversible any moment via remove_timeout)
# while kick/ban/prune/unban live in the NEEDS_OWNER tier further down —
# the client confirms those and they eject people irreversibly.
#
# Datetimes serialize as ISO strings; ids stay ints in results like every
# serializer above (the string-snowflake rule is a WIRE-INPUT rule).
# ---------------------------------------------------------------------------

# Discord's timeout cap: 28 days.
TIMEOUT_MAX_MINUTES = 40320


def _iso(dt: Any) -> Optional[str]:
    return dt.isoformat() if dt else None


def _op_audit_reason(ctx: OpContext, op_name: str,
                     reason: Optional[str]) -> str:
    """Audit-log reason: the caller's own text when given, else the same
    actor stamp edit_thread/edit_channel write."""
    if reason is not None and str(reason).strip():
        return str(reason)
    return f"{op_name} op by {ctx.author} ({ctx.author.id})"


def _serialize_activity(a: Any) -> Dict[str, Any]:
    """One presence activity: type (playing/listening/custom/...), name,
    and details when the activity carries them."""
    a_type = getattr(a, "type", None)
    row: Dict[str, Any] = {
        "type": getattr(a_type, "name", None) or (str(a_type) if a_type is not None else None),
        "name": getattr(a, "name", None),
    }
    details = getattr(a, "details", None)
    if details:
        row["details"] = details
    return row


def serialize_member_profile(m: Any) -> Dict[str, Any]:
    avatar = getattr(m, "display_avatar", None)
    guild_avatar = getattr(m, "guild_avatar", None)
    top_role = getattr(m, "top_role", None)
    return {
        "id": m.id,
        "name": m.name,
        "global_name": getattr(m, "global_name", None),
        "display_name": m.display_name,
        "nick": getattr(m, "nick", None),
        "bot": bool(getattr(m, "bot", False)),
        # @everyone is implicit membership, not information — excluded.
        "roles": [
            {"id": r.id, "name": r.name}
            for r in getattr(m, "roles", [])
            if not (hasattr(r, "is_default") and r.is_default())
        ],
        "top_role": ({"id": top_role.id, "name": top_role.name}
                     if top_role is not None else None),
        "joined_at": _iso(getattr(m, "joined_at", None)),
        "created_at": _iso(getattr(m, "created_at", None)),
        "premium_since": _iso(getattr(m, "premium_since", None)),
        "timed_out_until": _iso(getattr(m, "timed_out_until", None)),
        "status": str(getattr(m, "status", "offline")),
        # Per-platform presence, same enum-to-string treatment as status.
        "client_status": {
            "desktop": str(getattr(m, "desktop_status", "offline")),
            "mobile": str(getattr(m, "mobile_status", "offline")),
            "web": str(getattr(m, "web_status", "offline")),
        },
        "activities": [_serialize_activity(a)
                       for a in (getattr(m, "activities", None) or [])],
        # Asset.__str__ is the CDN url; display_avatar always resolves
        # (custom avatar or default) on a real Member.
        "avatar_url": str(avatar) if avatar else None,
        # The per-guild avatar override, when the member set one.
        "guild_avatar_url": str(guild_avatar) if guild_avatar else None,
        "pending": bool(getattr(m, "pending", False)),
    }


@registry.op(
    "get_member",
    "Read one member's full profile: nick, roles, join/creation dates, "
    "timeout state, booster status, presence, avatar url. Pure read — the "
    "detail view to list_members' index.",
    PermissionLevel.EVERYONE,
    params=[
        OpParam("guild", ParamKind.GUILD,
                "Discord guild id the member belongs to. Optional when the "
                "invoking context already carries a guild (in-guild "
                "commands); required over guild-less frontends like MCP.",
                required=False),
        OpParam("member", ParamKind.MEMBER, "Discord user id to read."),
    ],
    serialize=serialize_member_profile,
    agent_guidance=(
        "get_member answers 'what roles does X have', 'when did X join', "
        "'is X timed out' in one call — use it instead of scanning every "
        "role with list_role_members. Get the user id from search_members "
        "or the visible context, never by guessing."),
    scope=OpScope.GUILD,
    group="guild-info",
)
async def get_member(ctx: OpContext, member, guild=None):
    # The MEMBER resolver already fetched the member; bare pass-through to
    # the serializer, same as get_message.
    return member


@registry.op(
    "get_guild_info",
    "Read a guild's metadata: name, description, owner, member count, boost "
    "tier, features, verification level, locale, vanity code, icon/banner "
    "urls. Pure cache read — the same facts any member sees on the server "
    "banner.",
    PermissionLevel.EVERYONE,
    params=[OpParam("guild", ParamKind.GUILD, "Discord guild id to read.")],
    serialize=lambda payload: payload,
    agent_guidance=(
        "get_guild_info is the detail view to list_guilds' index — use it "
        "for member_count, boost tier, features, or the owner's user id."),
    scope=OpScope.GUILD,
    group="guild-info",
)
async def get_guild_info(ctx: OpContext, guild):
    icon = getattr(guild, "icon", None)
    banner = getattr(guild, "banner", None)
    verification = getattr(guild, "verification_level", None)
    locale = getattr(guild, "preferred_locale", None)
    return {
        "id": guild.id,
        "name": guild.name,
        "description": getattr(guild, "description", None),
        "owner_id": getattr(guild, "owner_id", None),
        "member_count": getattr(guild, "member_count", None),
        "created_at": _iso(getattr(guild, "created_at", None)),
        "premium_tier": getattr(guild, "premium_tier", None),
        "premium_subscription_count": getattr(
            guild, "premium_subscription_count", None),
        "features": list(getattr(guild, "features", None) or []),
        "verification_level": (str(verification)
                               if verification is not None else None),
        "preferred_locale": str(locale) if locale is not None else None,
        "vanity_url_code": getattr(guild, "vanity_url_code", None),
        "icon_url": str(icon) if icon else None,
        "banner_url": str(banner) if banner else None,
    }


@registry.op(
    "search_members",
    "Search a guild's members by name/nick prefix (gateway member search). "
    "Read-only — the path from a name to a user_id without dumping the "
    "whole member list.",
    PermissionLevel.EVERYONE,
    params=[
        OpParam("guild", ParamKind.GUILD, "Discord guild id to search."),
        OpParam("query", ParamKind.STRING,
                "Name/nick prefix to match (Discord matches username and "
                "nickname prefixes, case-insensitively)."),
        OpParam("limit", ParamKind.INTEGER,
                "Max matches to return (default 10, clamped to 100).",
                required=False, default=10, minimum=1, maximum=100),
    ],
    serialize=lambda ms: {"members": ms, "count": len(ms)},
    agent_guidance=(
        "search_members resolves a name to a user_id ('what roles does "
        "Alice have' -> search_members, then get_member). It matches "
        "PREFIXES only — search the shortest distinctive prefix, and never "
        "guess an id when zero rows come back."),
    scope=OpScope.GUILD,
    group="guild-info",
)
async def search_members(ctx: OpContext, guild, query: str, limit: int = 10):
    members = await guild.query_members(query=query, limit=limit, cache=True)
    return [
        {
            "id": m.id,
            "display_name": m.display_name,
            "name": m.name,
            "status": str(getattr(m, "status", "offline")),
        }
        for m in members
    ]


@registry.op(
    "set_nickname",
    "Set or clear a member's nickname. Requires admin (Manage Nicknames "
    "class action). Omit nick, or pass an empty string, to clear the "
    "nickname back to the username. Fully reversible.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD,
                "Discord guild id the member belongs to. Optional when the "
                "invoking context already carries a guild (in-guild "
                "commands); required over guild-less frontends like MCP.",
                required=False),
        OpParam("member", ParamKind.MEMBER,
                "Discord user id whose nickname to set."),
        OpParam("nick", ParamKind.STRING,
                "New nickname (max 32 chars). Omit or pass empty to clear.",
                required=False),
        OpParam("reason", ParamKind.STRING,
                "Optional audit-log reason.", required=False),
    ],
    serialize=lambda payload: payload,
    scope=OpScope.GUILD,
    group="members",
)
async def set_nickname(ctx: OpContext, member, nick: Optional[str] = None,
                       reason: Optional[str] = None, guild=None):
    # Empty/whitespace clears, same as omitting: there is no meaningful
    # all-whitespace nickname, and 'empty clears' matches the client.
    cleaned = nick if nick is not None and str(nick).strip() else None
    await member.edit(nick=cleaned,
                      reason=_op_audit_reason(ctx, "set_nickname", reason))
    return {"member_id": member.id, "nick": cleaned}


@registry.op(
    "timeout_member",
    "Timeout a member (Discord's native mute-everything) for a duration in "
    "minutes, max 28 days. Requires admin. Auto-expires and reversible at "
    "any moment via remove_timeout. Refuses members the bot cannot "
    "moderate (role hierarchy) with a Forbidden error.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD,
                "Discord guild id the member belongs to. Optional when the "
                "invoking context already carries a guild (in-guild "
                "commands); required over guild-less frontends like MCP.",
                required=False),
        OpParam("member", ParamKind.MEMBER,
                "Discord user id to timeout."),
        OpParam("duration_minutes", ParamKind.INTEGER,
                "Timeout length in minutes (max 40320 = Discord's 28-day "
                "cap).",
                minimum=1, maximum=TIMEOUT_MAX_MINUTES),
        OpParam("reason", ParamKind.STRING,
                "Optional audit-log reason.", required=False),
    ],
    serialize=lambda payload: payload,
    agent_guidance=(
        "timeout_member is reversible moderation: it auto-expires at "
        "timed_out_until and remove_timeout undoes it early. It is NOT "
        "kick or ban — the bot has no ejection ops at all; if asked to "
        "kick or ban, say that is not available."),
    scope=OpScope.GUILD,
    group="members",
)
async def timeout_member(ctx: OpContext, member, duration_minutes: int,
                         reason: Optional[str] = None, guild=None):
    until = discord.utils.utcnow() + timedelta(minutes=duration_minutes)
    await member.timeout(until,
                         reason=_op_audit_reason(ctx, "timeout_member",
                                                 reason))
    return {"member_id": member.id, "timed_out_until": until.isoformat()}


@registry.op(
    "remove_timeout",
    "Remove a member's timeout early. Requires admin. The reversal half of "
    "timeout_member; strictly permission-restoring.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD,
                "Discord guild id the member belongs to. Optional when the "
                "invoking context already carries a guild (in-guild "
                "commands); required over guild-less frontends like MCP.",
                required=False),
        OpParam("member", ParamKind.MEMBER,
                "Discord user id whose timeout to remove."),
        OpParam("reason", ParamKind.STRING,
                "Optional audit-log reason.", required=False),
    ],
    scope=OpScope.GUILD,
    group="members",
)
async def remove_timeout(ctx: OpContext, member,
                         reason: Optional[str] = None, guild=None):
    await member.timeout(None,
                         reason=_op_audit_reason(ctx, "remove_timeout",
                                                 reason))
    return True


@registry.op(
    "list_bans",
    "Read the guild ban list (user id, name, reason) — the same list "
    "Server Settings → Bans shows an admin. Read-only; there is no "
    "ban/unban op. Pages by user id via after_user_id.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD, "Discord guild id to read."),
        OpParam("limit", ParamKind.INTEGER,
                "Max ban entries to return (default 100, clamped to 1000).",
                required=False, default=100, minimum=1, maximum=1000),
        OpParam("after_user_id", ParamKind.SNOWFLAKE,
                "Optional pagination cursor: only bans of user ids greater "
                "than this (Discord pages bans by user id). Pass the "
                "previous page's last user_id to walk forward.",
                required=False),
    ],
    serialize=lambda bs: {"bans": bs, "count": len(bs)},
    agent_guidance=(
        "list_bans answers 'why can't X rejoin' — it reads only; the bot "
        "has no ban or unban ops. Page forward with after_user_id set to "
        "the last row's user_id until a page comes back short."),
    scope=OpScope.GUILD,
    group="moderation",
)
async def list_bans(ctx: OpContext, guild, limit: int = 100,
                    after_user_id: Optional[int] = None):
    kwargs: Dict[str, Any] = {"limit": limit}
    if after_user_id is not None:
        kwargs["after"] = discord.Object(id=after_user_id)
    rows = []
    async for entry in guild.bans(**kwargs):
        rows.append({
            "user_id": entry.user.id,
            "name": entry.user.name,
            "reason": entry.reason,
        })
    return rows


def _audit_change_value(value: Any) -> Any:
    """AuditLogDiff values include live objects (roles, overwrites, colours);
    keep JSON scalars as-is and stringify everything else defensively."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


@registry.op(
    "fetch_audit_logs",
    "Read the guild audit log ('who did that?'): action, actor, target, "
    "reason, and per-attribute before/after changes. Read-only; the same "
    "view Server Settings → Audit Log shows an admin. Requires the bot to "
    "hold View Audit Log.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD, "Discord guild id to read."),
        OpParam("limit", ParamKind.INTEGER,
                "Max entries to return, newest first (default 50, clamped "
                "to 100).",
                required=False, default=50, minimum=1, maximum=100),
        OpParam("user", ParamKind.USER,
                "Optional filter — only actions performed by this user id.",
                required=False),
        OpParam("action", ParamKind.STRING,
                "Optional filter — an AuditLogAction name like 'ban', "
                "'kick', 'member_update', 'role_create'.",
                required=False),
        OpParam("before", ParamKind.SNOWFLAKE,
                "Optional pagination cursor: only entries older than this "
                "entry id. Pass the previous page's last entry id to walk "
                "further back.",
                required=False),
    ],
    serialize=lambda es: {"entries": es, "count": len(es)},
    agent_guidance=(
        "fetch_audit_logs answers 'who did that?' — filter by user_id for "
        "one actor's actions or by action name (e.g. 'ban', 'role_create') "
        "for one kind. Summarize the entries in plain text; never paste "
        "them raw."),
    scope=OpScope.GUILD,
    group="guild-info",
)
async def fetch_audit_logs(ctx: OpContext, guild, limit: int = 50,
                           user=None, action: Optional[str] = None,
                           before: Optional[int] = None):
    kwargs: Dict[str, Any] = {"limit": limit}
    if user is not None:
        kwargs["user"] = user
    if action is not None:
        resolved = getattr(discord.AuditLogAction, str(action), None)
        if not isinstance(resolved, discord.AuditLogAction):
            raise ValueError(
                f"Unknown audit-log action {action!r} — use a "
                f"discord.AuditLogAction name like 'ban', 'kick', "
                f"'member_update', 'role_create'.")
        kwargs["action"] = resolved
    if before is not None:
        kwargs["before"] = discord.Object(id=before)
    entries = []
    async for e in guild.audit_logs(**kwargs):
        # AuditLogDiff iterates as (attribute, value) pairs; union the
        # before/after keys so one-sided changes (e.g. a create) still show.
        before_diff = dict(e.changes.before)
        after_diff = dict(e.changes.after)
        changes = [
            {
                "attribute": key,
                "before": _audit_change_value(before_diff.get(key)),
                "after": _audit_change_value(after_diff.get(key)),
            }
            for key in dict.fromkeys(list(before_diff) + list(after_diff))
        ]
        target = getattr(e, "target", None)
        entries.append({
            "id": e.id,
            "action": getattr(e.action, "name", str(e.action)),
            "user_id": getattr(getattr(e, "user", None), "id", None),
            "target_id": getattr(target, "id", None),
            "target_type": (type(target).__name__
                            if target is not None else None),
            "reason": getattr(e, "reason", None),
            "created_at": _iso(getattr(e, "created_at", None)),
            "changes": changes,
        })
    return entries


@registry.op(
    "estimate_prune",
    "Estimate how many members a prune would remove (dry-run) — the number "
    "the client shows before a prune. Pure read; there is no prune op.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD, "Discord guild id to estimate in."),
        OpParam("days", ParamKind.INTEGER,
                "Inactivity window in days (default 30, clamped to 1-30).",
                required=False, default=30, minimum=1, maximum=30),
    ],
    serialize=lambda payload: payload,
    scope=OpScope.GUILD,
    group="guild-info",
)
async def estimate_prune(ctx: OpContext, guild, days: int = 30):
    estimated = await guild.estimate_pruned_members(days=days)
    return {"days": days, "estimated_members": estimated}


@registry.op(
    "list_integrations",
    "List a guild's integrations (connected bots, Twitch/YouTube subs) — "
    "the same list Server Settings → Integrations shows an admin. "
    "Read-only; there is no integration delete op.",
    PermissionLevel.ADMIN,
    params=[OpParam("guild", ParamKind.GUILD,
                    "Discord guild id to enumerate.")],
    serialize=lambda rows: {"integrations": rows, "count": len(rows)},
    scope=OpScope.GUILD,
    group="guild-info",
)
async def list_integrations(ctx: OpContext, guild):
    rows = []
    for integration in await guild.integrations():
        account = getattr(integration, "account", None)
        row: Dict[str, Any] = {
            "id": integration.id,
            "name": integration.name,
            "type": getattr(integration, "type", None),
            "enabled": bool(getattr(integration, "enabled", False)),
            "account_id": getattr(account, "id", None),
            "account_name": getattr(account, "name", None),
        }
        # Bot integrations only: the connected application's bot user id.
        bot_user = getattr(getattr(integration, "application", None),
                           "user", None)
        if bot_user is not None:
            row["application_bot_user_id"] = bot_user.id
        rows.append(row)
    return rows


@registry.op(
    "list_invites",
    "List a guild's active invites (code, channel, inviter, uses, expiry, "
    "plus the vanity code when the guild has one) — the same list Server "
    "Settings → Invites shows an admin. Read-only; the entry point for "
    "revoke_invite. The bot needs Manage Guild or the call fails.",
    PermissionLevel.ADMIN,
    params=[OpParam("guild", ParamKind.GUILD,
                    "Discord guild id to enumerate.")],
    serialize=lambda payload: payload,
    scope=OpScope.GUILD,
    group="invites",
)
async def list_invites(ctx: OpContext, guild):
    rows = []
    for invite in await guild.invites():
        inviter = getattr(invite, "inviter", None)
        rows.append({
            "code": invite.code,
            "channel_id": getattr(getattr(invite, "channel", None),
                                  "id", None),
            "inviter_id": getattr(inviter, "id", None),
            "inviter_name": getattr(inviter, "name", None),
            "uses": getattr(invite, "uses", None),
            "max_uses": getattr(invite, "max_uses", None),
            "max_age": getattr(invite, "max_age", None),
            "created_at": _iso(getattr(invite, "created_at", None)),
            "expires_at": _iso(getattr(invite, "expires_at", None)),
            "temporary": bool(getattr(invite, "temporary", False)),
        })
    return {
        "invites": rows,
        # Cache read; None when the guild has no vanity URL set.
        "vanity_code": getattr(guild, "vanity_url_code", None),
        "count": len(rows),
    }


# Discord's invite caps: max_age tops out at 7 days, max_uses at 100.
INVITE_MAX_AGE_SECONDS = 604800
INVITE_MAX_USES = 100


@registry.op(
    "create_invite",
    "Create an invite link for a channel. Requires admin. Defaults to a "
    "24-hour expiry and unlimited uses (pass max_age_seconds=0 for a "
    "never-expiring link — deliberate, never the default). Fully "
    "reversible via revoke_invite.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("channel", ParamKind.CHANNEL,
                "Discord channel id the invite lands in."),
        OpParam("max_age_seconds", ParamKind.INTEGER,
                "Seconds until the link expires (default 86400 = 24h; 0 = "
                "never; max 604800 = 7 days).",
                required=False, default=86400, minimum=0,
                maximum=INVITE_MAX_AGE_SECONDS),
        OpParam("max_uses", ParamKind.INTEGER,
                "How many joins the link allows (default 0 = unlimited; "
                "max 100).",
                required=False, default=0, minimum=0, maximum=INVITE_MAX_USES),
        OpParam("temporary", ParamKind.BOOLEAN,
                "Grant temporary membership (kicked on disconnect unless "
                "given a role). Default false.",
                required=False, default=False),
    ],
    serialize=lambda payload: payload,
    agent_guidance=(
        "create_invite returns the full url — hand THAT to the user. The "
        "default link expires in 24h; only pass max_age_seconds=0 when a "
        "permanent link was explicitly asked for, and mention that "
        "revoke_invite undoes it."),
    scope=OpScope.GUILD,
    group="invites",
)
async def create_invite(ctx: OpContext, channel, max_age_seconds: int = 86400,
                        max_uses: int = 0, temporary: bool = False):
    if not hasattr(channel, "create_invite"):
        raise ValueError(
            f"Channel {getattr(channel, 'id', '?')} "
            f"({type(channel).__name__}) cannot carry invites.")
    invite = await channel.create_invite(
        max_age=max_age_seconds, max_uses=max_uses, temporary=temporary,
        unique=True,
        reason=f"create_invite op by {ctx.author} ({ctx.author.id})",
    )
    expires_at = getattr(invite, "expires_at", None)
    if expires_at is None and max_age_seconds:
        created_at = getattr(invite, "created_at", None)
        if created_at is not None:
            expires_at = created_at + timedelta(seconds=max_age_seconds)
    return {
        "code": invite.code,
        "url": getattr(invite, "url", None),
        "channel_id": getattr(channel, "id", None),
        "max_age": max_age_seconds,
        "max_uses": max_uses,
        "expires_at": _iso(expires_at),
    }


@registry.op(
    "revoke_invite",
    "Revoke one of THIS guild's active invites by code. Requires admin. "
    "Codes not found in the guild's own invite list are refused — a foreign "
    "or expired code is never deleted blind. Low-stakes destructive: "
    "create_invite mints a replacement in one call.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD,
                "Discord guild id the invite belongs to."),
        OpParam("code", ParamKind.STRING,
                "Invite code to revoke (from list_invites; a full "
                "discord.gg URL is also accepted)."),
    ],
    serialize=lambda payload: payload,
    agent_guidance=(
        "revoke_invite kills the link for everyone holding it — get the "
        "code from list_invites, never from memory. uses_at_revoke in the "
        "result says how many joins it had served."),
    scope=OpScope.GUILD,
    group="invites",
)
async def revoke_invite(ctx: OpContext, guild, code: str):
    # Accept a bare code or a pasted invite URL; the last path segment is
    # the code either way.
    wanted = str(code).strip().rstrip("/").rsplit("/", 1)[-1]
    if not wanted:
        raise ValueError("revoke_invite requires a non-empty invite code.")
    # Match against the guild's OWN invite list — never Client.delete_invite
    # on an unverified code, which would reach invites of foreign guilds.
    target = next((inv for inv in await guild.invites()
                   if inv.code == wanted), None)
    if target is None:
        raise ValueError(
            f"No active invite with code '{wanted}' in guild "
            f"'{guild.name}' — see list_invites.")
    uses = getattr(target, "uses", None)
    await target.delete(
        reason=f"revoke_invite op by {ctx.author} ({ctx.author.id})")
    return {"revoked": True, "code": wanted, "uses_at_revoke": uses}


@registry.op(
    "list_webhooks",
    "List a guild's webhooks (or one channel's): id, name, channel, type, "
    "creator — the same integration audit Server Settings → Integrations → "
    "Webhooks shows an admin. Read-only, and the webhook URL/token (a "
    "bearer credential that posts without any auth) is NEVER included. The "
    "bot needs Manage Webhooks or the call fails.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD,
                "Discord guild id to enumerate."),
        OpParam("channel", ParamKind.CHANNEL,
                "Optional channel id to narrow the audit to one channel.",
                required=False),
    ],
    serialize=lambda payload: payload,
    agent_guidance=(
        "list_webhooks is an audit read: there are deliberately NO ops to "
        "create, edit, execute, or delete webhooks, and the webhook URL is "
        "never available — if asked for one, say the surface doesn't "
        "exist."),
    scope=OpScope.GUILD,
    group="integrations",
)
async def list_webhooks(ctx: OpContext, guild, channel=None):
    if channel is not None:
        if not hasattr(channel, "webhooks"):
            raise ValueError(
                f"Channel {getattr(channel, 'id', '?')} "
                f"({type(channel).__name__}) cannot carry webhooks.")
        hooks = await channel.webhooks()
    else:
        hooks = await guild.webhooks()
    rows = []
    for w in hooks:
        creator = getattr(w, "user", None)
        wtype = getattr(w, "type", None)
        # Deliberately NO url and NO token — either one is a persistent
        # unauthenticated posting credential.
        rows.append({
            "id": w.id,
            "name": w.name,
            "channel_id": getattr(w, "channel_id", None),
            "type": getattr(wtype, "name",
                            str(wtype) if wtype is not None else None),
            "creator_id": getattr(creator, "id", None),
            "creator_name": getattr(creator, "name", None),
        })
    return {"webhooks": rows, "count": len(rows)}


# ---------------------------------------------------------------------------
# Voice, scheduled-event, and automod ops (2026-08 voice-domain gap pass).
#
# Same SAFE_NOW bar as the guild-domain section above: everything here is
# either a read any member gets in the client (the voice sidebar, the events
# tab) or a no-confirmation reversible client action (dragging a member
# between voice channels, the server-mute checkbox). Kick/ban/prune, event
# delete/cancel, automod CRUD, and stage go-live live in the NEEDS_OWNER
# tier (2026-08 owner-tier pass) — destructive or guild-notifying.
# Bot voice PRESENCE (connect/play) is structurally out of scope: a stateful
# gateway session, not an atomic request/response op.
#
# All voice writes ride Member.move_to / Member.edit, which 400 when the
# target is not connected to voice — Op.__call__ surfaces that HTTPException
# as the op's uniform error string.
# ---------------------------------------------------------------------------


def _actor_can_see_channel(ctx: OpContext, channel: Any) -> bool:
    """The channel-visibility policy of _check_channel_visibility, reusable
    for targets the generic gate can't reach (a member's voice channel, the
    guild-wide voice walk). Real Members are checked; bare id-holder actors
    (the MCP frontend's documented accepted risk) always pass."""
    actor = getattr(ctx, "author", None)
    if actor is None or not hasattr(actor, "guild_permissions"):
        return True
    if channel is None or not hasattr(channel, "permissions_for"):
        return True
    try:
        return bool(channel.permissions_for(actor).read_messages)
    except Exception:  # noqa: BLE001 - odd channel types err to hidden
        return False


def _voice_flags(vs: Any) -> Dict[str, Any]:
    """The per-member voice flags every voice read returns — one shape, so
    get_voice_state and list_voice_states can't drift apart."""
    return {
        "mute": bool(getattr(vs, "mute", False)),
        "deaf": bool(getattr(vs, "deaf", False)),
        "self_mute": bool(getattr(vs, "self_mute", False)),
        "self_deaf": bool(getattr(vs, "self_deaf", False)),
        "streaming": bool(getattr(vs, "self_stream", False)),
        "video": bool(getattr(vs, "self_video", False)),
    }


@registry.op(
    "get_voice_state",
    "Read one member's live voice state: which voice/stage channel they are "
    "in and their mute/deafen/streaming/video flags — the same info any "
    "member sees in the channel sidebar. Returns in_voice=false when the "
    "member is not connected (or their channel is not visible to you).",
    PermissionLevel.EVERYONE,
    params=[
        OpParam("guild", ParamKind.GUILD,
                "Discord guild id the member belongs to. Optional when the "
                "invoking context already carries a guild (in-guild "
                "commands); required over guild-less frontends like MCP.",
                required=False),
        OpParam("member", ParamKind.MEMBER,
                "Discord user id whose voice state to read."),
    ],
    serialize=lambda payload: payload,
    scope=OpScope.GUILD,
    group="voice",
)
async def get_voice_state(ctx: OpContext, member, guild=None):
    vs = getattr(member, "voice", None)
    channel = getattr(vs, "channel", None) if vs is not None else None
    if channel is None:
        return {"in_voice": False}
    # Same policy as list_members: an actor who cannot see the channel gets
    # in_voice=false rather than a leak of WHERE the member is.
    if not _actor_can_see_channel(ctx, channel):
        return {"in_voice": False}
    return {
        "in_voice": True,
        "channel_id": channel.id,
        "channel_name": getattr(channel, "name", None),
        **_voice_flags(vs),
        "suppress": bool(getattr(vs, "suppress", False)),
        "requested_to_speak_at": _iso(
            getattr(vs, "requested_to_speak_at", None)),
    }


@registry.op(
    "list_voice_states",
    "Guild-wide 'who is in voice': every voice/stage channel the invoking "
    "user can see, with connected members and their mute/deafen/streaming/"
    "video flags — exactly the voice sidebar. Read-only cache walk.",
    PermissionLevel.EVERYONE,
    params=[
        OpParam("guild", ParamKind.GUILD,
                "Discord guild id to enumerate. Optional when the invoking "
                "context already carries a guild (in-guild commands); "
                "required over guild-less frontends like MCP.",
                required=False),
    ],
    serialize=lambda cs: {"channels": cs, "count": len(cs)},
    agent_guidance=(
        "list_voice_states answers 'who is in voice right now' in one call "
        "— never iterate get_voice_state over the member list. Empty "
        "channels are included, like the sidebar."),
    scope=OpScope.GUILD,
    group="voice",
)
async def list_voice_states(ctx: OpContext, guild=None):
    guild = guild or ctx.guild
    if guild is None:
        raise ValueError("list_voice_states needs a guild context.")
    channels = []
    tagged = [(c, "voice") for c in getattr(guild, "voice_channels", [])]
    tagged += [(c, "stage") for c in getattr(guild, "stage_channels", [])]
    for channel, ctype in tagged:
        # Same visibility policy as search_history's per-hit drop: channels
        # the invoking user can't see are omitted entirely.
        if not _actor_can_see_channel(ctx, channel):
            continue
        members = []
        for m in getattr(channel, "members", []):
            vs = getattr(m, "voice", None)
            members.append({
                "id": m.id,
                "display_name": m.display_name,
                **_voice_flags(vs),
            })
        channels.append({
            "channel_id": channel.id,
            "name": getattr(channel, "name", None),
            "type": ctype,
            "members": members,
        })
    return channels


def _require_vocal_channel(channel: Any) -> None:
    """Voice writes only make sense against voice/stage channels; refuse
    anything else locally rather than relaying a raw Discord 400."""
    if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
        raise ValueError(
            f"Channel {getattr(channel, 'id', '?')} "
            f"({type(channel).__name__}) is not a voice or stage channel.")


@registry.op(
    "move_member",
    "Move a member to another voice/stage channel (they must already be "
    "connected to voice). Requires admin. Client-parity drag action — "
    "instantly reversible by moving them back.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD,
                "Discord guild id the member belongs to. Optional when the "
                "invoking context already carries a guild (in-guild "
                "commands); required over guild-less frontends like MCP.",
                required=False),
        OpParam("member", ParamKind.MEMBER,
                "Discord user id to move (must be connected to voice)."),
        OpParam("channel", ParamKind.CHANNEL,
                "Voice or stage channel id to move the member into."),
    ],
    serialize=lambda payload: payload,
    scope=OpScope.GUILD,
    group="voice",
)
async def move_member(ctx: OpContext, member, channel, guild=None):
    _require_vocal_channel(channel)
    await member.move_to(channel,
                         reason=_op_audit_reason(ctx, "move_member", None))
    return {"moved": True, "channel_id": channel.id}


@registry.op(
    "disconnect_member",
    "Disconnect a member from voice. Requires admin. Client-parity "
    "right-click action with no confirmation; the member can rejoin "
    "immediately. Deliberately a separate op from move_member — explicit "
    "intent beats a magic null channel.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD,
                "Discord guild id the member belongs to. Optional when the "
                "invoking context already carries a guild (in-guild "
                "commands); required over guild-less frontends like MCP.",
                required=False),
        OpParam("member", ParamKind.MEMBER,
                "Discord user id to disconnect from voice."),
    ],
    scope=OpScope.GUILD,
    group="voice",
)
async def disconnect_member(ctx: OpContext, member, guild=None):
    await member.move_to(
        None, reason=_op_audit_reason(ctx, "disconnect_member", None))
    return True


@registry.op(
    "set_voice_mute",
    "Server-mute or unmute a member in voice (one op, boolean — the client "
    "checkbox). Requires admin. Symmetric and instantly reversible; errors "
    "if the member is not connected to voice.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD,
                "Discord guild id the member belongs to. Optional when the "
                "invoking context already carries a guild (in-guild "
                "commands); required over guild-less frontends like MCP.",
                required=False),
        OpParam("member", ParamKind.MEMBER,
                "Discord user id to server-mute/unmute."),
        OpParam("muted", ParamKind.BOOLEAN,
                "true to server-mute, false to unmute."),
    ],
    serialize=lambda payload: payload,
    scope=OpScope.GUILD,
    group="voice",
)
async def set_voice_mute(ctx: OpContext, member, muted: bool, guild=None):
    await member.edit(mute=muted,
                      reason=_op_audit_reason(ctx, "set_voice_mute", None))
    return {"member_id": member.id, "muted": muted}


@registry.op(
    "set_voice_deafen",
    "Server-deafen or undeafen a member in voice (one op, boolean — the "
    "client checkbox). Requires admin. Symmetric and instantly reversible; "
    "errors if the member is not connected to voice.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD,
                "Discord guild id the member belongs to. Optional when the "
                "invoking context already carries a guild (in-guild "
                "commands); required over guild-less frontends like MCP.",
                required=False),
        OpParam("member", ParamKind.MEMBER,
                "Discord user id to server-deafen/undeafen."),
        OpParam("deafened", ParamKind.BOOLEAN,
                "true to server-deafen, false to undeafen."),
    ],
    serialize=lambda payload: payload,
    scope=OpScope.GUILD,
    group="voice",
)
async def set_voice_deafen(ctx: OpContext, member, deafened: bool,
                           guild=None):
    await member.edit(deafen=deafened,
                      reason=_op_audit_reason(ctx, "set_voice_deafen", None))
    return {"member_id": member.id, "deafened": deafened}


@registry.op(
    "set_stage_suppress",
    "Move a stage speaker to the audience (suppressed=true) or approve them "
    "as a speaker (suppressed=false). Requires admin. Stage-moderator "
    "client action, fully reversible; only valid while the member is in a "
    "stage channel.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD,
                "Discord guild id the member belongs to. Optional when the "
                "invoking context already carries a guild (in-guild "
                "commands); required over guild-less frontends like MCP.",
                required=False),
        OpParam("member", ParamKind.MEMBER,
                "Discord user id whose stage suppression to set."),
        OpParam("suppressed", ParamKind.BOOLEAN,
                "true = move to audience, false = make speaker."),
    ],
    serialize=lambda payload: payload,
    scope=OpScope.GUILD,
    group="voice",
)
async def set_stage_suppress(ctx: OpContext, member, suppressed: bool,
                             guild=None):
    vs = getattr(member, "voice", None)
    channel = getattr(vs, "channel", None) if vs is not None else None
    if not isinstance(channel, discord.StageChannel):
        raise ValueError(
            "set_stage_suppress requires the member to be in a stage "
            "channel.")
    await member.edit(suppress=suppressed)
    return {"member_id": member.id, "suppressed": suppressed}


@registry.op(
    "get_stage_instance",
    "Read the live stage instance of a stage channel (topic, privacy "
    "level), or live=false when nothing is live — the same view any member "
    "who can see the stage channel gets.",
    PermissionLevel.EVERYONE,
    params=[
        OpParam("channel", ParamKind.CHANNEL,
                "Stage channel id to read."),
    ],
    serialize=lambda payload: payload,
    scope=OpScope.GUILD,
    group="voice",
)
async def get_stage_instance(ctx: OpContext, channel):
    if not isinstance(channel, discord.StageChannel):
        raise ValueError(
            f"Channel {getattr(channel, 'id', '?')} "
            f"({type(channel).__name__}) is not a stage channel.")
    instance = channel.instance
    if instance is None:
        try:
            instance = await channel.fetch_instance()
        except discord.NotFound:
            instance = None
    if instance is None:
        return {"live": False}
    privacy = getattr(instance, "privacy_level", None)
    return {
        "live": True,
        "topic": getattr(instance, "topic", None),
        "privacy_level": getattr(privacy, "name",
                                 str(privacy) if privacy is not None
                                 else None),
        "scheduled_event_id": getattr(instance, "scheduled_event_id", None),
    }


# -- scheduled events -------------------------------------------------------

def _parse_event_time(value: Any, param: str) -> datetime:
    """ISO-8601 → aware datetime, defaulting naive input to UTC (same
    lenience as read_dms' 'since'). 'Z' suffix accepted."""
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"'{param}' must be an ISO-8601 timestamp "
            f"(e.g. '2026-09-01T20:00:00+00:00'), got {value!r}.") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def serialize_scheduled_event(e: Any) -> Dict[str, Any]:
    status = getattr(e, "status", None)
    entity_type = getattr(e, "entity_type", None)
    return {
        "id": e.id,
        "name": e.name,
        "description": getattr(e, "description", None),
        "status": getattr(status, "name",
                          str(status) if status is not None else None),
        "entity_type": getattr(entity_type, "name",
                               str(entity_type) if entity_type is not None
                               else None),
        "start_time": _iso(getattr(e, "start_time", None)),
        "end_time": _iso(getattr(e, "end_time", None)),
        "channel_id": getattr(e, "channel_id", None),
        "location": getattr(e, "location", None),
        "creator_id": getattr(e, "creator_id", None),
        "user_count": getattr(e, "user_count", None),
    }


def serialize_scheduled_event_full(e: Any) -> Dict[str, Any]:
    """The single-event shape: the list row plus url and cover image."""
    cover = getattr(e, "cover_image", None)
    return {
        **serialize_scheduled_event(e),
        "url": getattr(e, "url", None),
        "image_url": str(cover) if cover else None,
    }


@registry.op(
    "list_scheduled_events",
    "List a guild's scheduled events with interest counts — the same events "
    "tab every member sees. Read-only.",
    PermissionLevel.EVERYONE,
    params=[
        OpParam("guild", ParamKind.GUILD,
                "Discord guild id to enumerate. Optional when the invoking "
                "context already carries a guild (in-guild commands); "
                "required over guild-less frontends like MCP.",
                required=False),
    ],
    serialize=lambda es: {"events": es, "count": len(es)},
    scope=OpScope.GUILD,
    group="events",
)
async def list_scheduled_events(ctx: OpContext, guild=None):
    guild = guild or ctx.guild
    if guild is None:
        raise ValueError("list_scheduled_events needs a guild context.")
    events = await guild.fetch_scheduled_events(with_counts=True)
    return [serialize_scheduled_event(e) for e in events]


@registry.op(
    "get_scheduled_event",
    "Get one scheduled event by id, with its interest count, url, and cover "
    "image. Read-only.",
    PermissionLevel.EVERYONE,
    params=[
        OpParam("guild", ParamKind.GUILD,
                "Discord guild id the event belongs to. Optional when the "
                "invoking context already carries a guild (in-guild "
                "commands); required over guild-less frontends like MCP.",
                required=False),
        OpParam("event_id", ParamKind.SNOWFLAKE,
                "Scheduled event id (from list_scheduled_events)."),
    ],
    serialize=serialize_scheduled_event_full,
    scope=OpScope.GUILD,
    group="events",
)
async def get_scheduled_event(ctx: OpContext, event_id: int, guild=None):
    guild = guild or ctx.guild
    if guild is None:
        raise ValueError("get_scheduled_event needs a guild context.")
    return await guild.fetch_scheduled_event(event_id, with_counts=True)


@registry.op(
    "list_scheduled_event_users",
    "List the users interested in a scheduled event (RSVPs) — the same "
    "interest list any member sees on the event card. Read-only.",
    PermissionLevel.EVERYONE,
    params=[
        OpParam("guild", ParamKind.GUILD,
                "Discord guild id the event belongs to. Optional when the "
                "invoking context already carries a guild (in-guild "
                "commands); required over guild-less frontends like MCP.",
                required=False),
        OpParam("event_id", ParamKind.SNOWFLAKE,
                "Scheduled event id (from list_scheduled_events)."),
        OpParam("limit", ParamKind.INTEGER,
                "Max users to return (default 100, clamped to 1000).",
                required=False, default=100, minimum=1, maximum=1000),
    ],
    serialize=lambda us: {"users": us, "count": len(us)},
    scope=OpScope.GUILD,
    group="events",
)
async def list_scheduled_event_users(ctx: OpContext, event_id: int,
                                     limit: int = 100, guild=None):
    guild = guild or ctx.guild
    if guild is None:
        raise ValueError("list_scheduled_event_users needs a guild context.")
    event = await guild.fetch_scheduled_event(event_id)
    rows = []
    async for user in event.users(limit=limit):
        rows.append({
            "id": user.id,
            "display_name": getattr(user, "display_name",
                                    getattr(user, "name", None)),
        })
    return rows


_EVENT_ENTITY_TYPES = ("voice", "stage_instance", "external")


@registry.op(
    "create_scheduled_event",
    "Create a guild scheduled event (voice, stage_instance, or external). "
    "Requires admin. voice/stage_instance events need a channel id; "
    "external events need a location and an end_time. No notification "
    "blast on create; reversible by deleting the event in the client "
    "before it accrues RSVPs (there is deliberately no delete op).",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD,
                "Discord guild id to create the event in. Optional when "
                "the invoking context already carries a guild (in-guild "
                "commands); required over guild-less frontends like MCP.",
                required=False),
        OpParam("name", ParamKind.STRING, "Event name."),
        OpParam("start_time", ParamKind.STRING,
                "ISO-8601 start time (must be in the future; naive input "
                "is taken as UTC)."),
        OpParam("entity_type", ParamKind.STRING,
                "Where the event happens: 'voice', 'stage_instance', or "
                "'external'."),
        OpParam("channel", ParamKind.CHANNEL,
                "Voice/stage channel id — required for voice and "
                "stage_instance events.",
                required=False),
        OpParam("location", ParamKind.STRING,
                "Freeform location — required for external events.",
                required=False),
        OpParam("end_time", ParamKind.STRING,
                "ISO-8601 end time — required for external events.",
                required=False),
        OpParam("description", ParamKind.STRING,
                "Optional event description.", required=False),
    ],
    serialize=serialize_scheduled_event_full,
    agent_guidance=(
        "create_scheduled_event returns the event's url — hand THAT to the "
        "user. Times are ISO-8601 and must be in the future; there is no "
        "delete/cancel op, so double-check details before creating."),
    scope=OpScope.GUILD,
    group="events",
)
async def create_scheduled_event(ctx: OpContext, name: str, start_time: str,
                                 entity_type: str, channel=None,
                                 location: Optional[str] = None,
                                 end_time: Optional[str] = None,
                                 description: Optional[str] = None,
                                 guild=None):
    guild = guild or ctx.guild
    if guild is None:
        raise ValueError("create_scheduled_event needs a guild context.")
    if entity_type not in _EVENT_ENTITY_TYPES:
        raise ValueError(
            f"entity_type must be one of {', '.join(_EVENT_ENTITY_TYPES)}; "
            f"got {entity_type!r}.")
    start = _parse_event_time(start_time, "start_time")
    if start <= datetime.now(timezone.utc):
        raise ValueError("start_time must be in the future.")
    kwargs: Dict[str, Any] = {
        "name": name,
        "start_time": start,
        "entity_type": getattr(discord.EntityType, entity_type),
        # The API requires a privacy level and guild_only is the only
        # valid value; discord.py omits it from the payload when unset.
        "privacy_level": discord.PrivacyLevel.guild_only,
        "reason": _op_audit_reason(ctx, "create_scheduled_event", None),
    }
    if entity_type == "external":
        if not (location and str(location).strip()):
            raise ValueError("external events require a location.")
        if end_time is None:
            raise ValueError("external events require an end_time.")
        kwargs["location"] = location
    else:
        if channel is None:
            raise ValueError(
                f"{entity_type} events require a voice/stage channel id.")
        _require_vocal_channel(channel)
        kwargs["channel"] = channel
    if end_time is not None:
        kwargs["end_time"] = _parse_event_time(end_time, "end_time")
    if description is not None and str(description).strip():
        kwargs["description"] = description
    return await guild.create_scheduled_event(**kwargs)


@registry.op(
    "edit_scheduled_event",
    "Edit a scheduled event: rename, retime, move, describe, or transition "
    "its status ('active' to start it, 'completed' to end an active one — "
    "forward-only per the API). Requires admin. All fields optional and "
    "sparse; cancel/delete is delete_scheduled_event's job.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD,
                "Discord guild id the event belongs to. Optional when the "
                "invoking context already carries a guild (in-guild "
                "commands); required over guild-less frontends like MCP.",
                required=False),
        OpParam("event_id", ParamKind.SNOWFLAKE,
                "Scheduled event id to edit (from list_scheduled_events)."),
        OpParam("name", ParamKind.STRING, "New event name.", required=False),
        OpParam("description", ParamKind.STRING,
                "New event description.", required=False),
        OpParam("channel", ParamKind.CHANNEL,
                "New voice/stage channel id.", required=False),
        OpParam("location", ParamKind.STRING,
                "New freeform location (external events).", required=False),
        OpParam("start_time", ParamKind.STRING,
                "New ISO-8601 start time.", required=False),
        OpParam("end_time", ParamKind.STRING,
                "New ISO-8601 end time.", required=False),
        OpParam("status", ParamKind.STRING,
                "Status transition: 'active' (start) or 'completed' (end). "
                "Forward-only; cancellation is not available.",
                required=False),
    ],
    serialize=serialize_scheduled_event_full,
    scope=OpScope.GUILD,
    group="events",
)
async def edit_scheduled_event(ctx: OpContext, event_id: int,
                               name: Optional[str] = None,
                               description: Optional[str] = None,
                               channel=None,
                               location: Optional[str] = None,
                               start_time: Optional[str] = None,
                               end_time: Optional[str] = None,
                               status: Optional[str] = None,
                               guild=None):
    guild = guild or ctx.guild
    if guild is None:
        raise ValueError("edit_scheduled_event needs a guild context.")
    event = await guild.fetch_scheduled_event(event_id)
    kwargs: Dict[str, Any] = {}
    if name is not None:
        kwargs["name"] = name
    if description is not None:
        kwargs["description"] = description
    if channel is not None:
        _require_vocal_channel(channel)
        kwargs["channel"] = channel
    if location is not None:
        kwargs["location"] = location
    if start_time is not None:
        kwargs["start_time"] = _parse_event_time(start_time, "start_time")
    if end_time is not None:
        kwargs["end_time"] = _parse_event_time(end_time, "end_time")
    if status is not None:
        if status not in ("active", "completed"):
            raise ValueError(
                "status must be 'active' or 'completed' — cancellation is "
                "delete_scheduled_event's job.")
        kwargs["status"] = getattr(discord.EventStatus, status)
    if not kwargs:
        raise ValueError(
            "Nothing to edit: pass at least one of name/description/"
            "channel/location/start_time/end_time/status.")
    return await event.edit(
        reason=_op_audit_reason(ctx, "edit_scheduled_event", None), **kwargs)


# -- automod ----------------------------------------------------------------

def _serialize_automod_rule(rule: Any) -> Dict[str, Any]:
    """Defensive per-field serialization: trigger fields vary by
    trigger_type, and absent facets serialize as empty/None rather than
    raising."""
    trigger = getattr(rule, "trigger", None)
    trigger_type = getattr(trigger, "type", None)
    event_type = getattr(rule, "event_type", None)
    presets = getattr(trigger, "presets", None)
    try:
        preset_names = ([name for name, value in presets if value]
                        if presets is not None else [])
    except TypeError:
        preset_names = []
    actions = []
    for action in (getattr(rule, "actions", None) or []):
        action_type = getattr(action, "type", None)
        duration = getattr(action, "duration", None)
        actions.append({
            "type": getattr(action_type, "name",
                            str(action_type) if action_type is not None
                            else None),
            "channel_id": getattr(action, "channel_id", None),
            "duration_s": (duration.total_seconds()
                           if duration is not None else None),
        })
    return {
        "id": rule.id,
        "name": rule.name,
        "enabled": bool(getattr(rule, "enabled", False)),
        "event_type": getattr(event_type, "name",
                              str(event_type) if event_type is not None
                              else None),
        "trigger_type": getattr(trigger_type, "name",
                                str(trigger_type) if trigger_type is not None
                                else None),
        "keyword_filter": list(getattr(trigger, "keyword_filter", None)
                               or []),
        "regex_patterns": list(getattr(trigger, "regex_patterns", None)
                               or []),
        "allow_list": list(getattr(trigger, "allow_list", None) or []),
        "mention_limit": getattr(trigger, "mention_limit", None),
        "presets": preset_names,
        "actions": actions,
        "exempt_role_ids": list(getattr(rule, "exempt_role_ids", None)
                                or []),
        "exempt_channel_ids": list(getattr(rule, "exempt_channel_ids", None)
                                   or []),
        "creator_id": getattr(rule, "creator_id", None),
    }


@registry.op(
    "list_automod_rules",
    "Read a guild's automod rules (triggers, keyword lists, actions, "
    "exemptions) — the same view Server Settings → AutoMod shows an admin. "
    "Requires admin, NOT everyone: keyword rules expose the guild's "
    "filtered-word lists. Read-only; create_automod_rule / edit_automod_rule "
    "/ delete_automod_rule own the writes.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD,
                "Discord guild id to read. Optional when the invoking "
                "context already carries a guild (in-guild commands); "
                "required over guild-less frontends like MCP.",
                required=False),
    ],
    serialize=lambda rs: {"rules": rs, "count": len(rs)},
    agent_guidance=(
        "list_automod_rules is an admin-only read of moderation policy — "
        "summarize what the rules do; never paste a guild's full "
        "filtered-word list into a public channel."),
    scope=OpScope.GUILD,
    group="moderation",
)
async def list_automod_rules(ctx: OpContext, guild=None):
    guild = guild or ctx.guild
    if guild is None:
        raise ValueError("list_automod_rules needs a guild context.")
    rules = await guild.fetch_automod_rules()
    return [_serialize_automod_rule(rule) for rule in rules]


# ===========================================================================
# NEEDS_OWNER-tier destructive / privileged ops (2026-08 owner-tier pass).
#
# Everything below fills the deliberately-omitted DESTRUCTIVE or PRIVILEGED
# half of a domain above — the actions the client itself fronts with a
# confirmation dialog, or that mint credentials / expand the permission
# model / broadcast irreversibly. They exist as ops because an owner opted
# in, and they are gated accordingly:
#
#   - every one is at least PermissionLevel.ADMIN;
#   - edit_guild_settings, bulk_ban and purge_messages are SUPERADMIN —
#     server-wide blast radius (guild identity, 200-member ban, unbounded
#     mass delete);
#   - default_gate() is always off, so even a whitelisted destructive op
#     stays hidden from a guild's agent until a guild admin opts it in.
#
# Snowflake-input rule as everywhere: ids on the wire are strings, results
# keep ids as ints. Bulk operations cap their batch (bulk_ban <=200 per the
# API, purge/bulk_delete <=100). Webhooks are resolved against the guild's
# own webhook list so a foreign id is never edited/deleted/executed blind.
# ===========================================================================


# -- channels ---------------------------------------------------------------

_CHANNEL_KIND_CREATORS = {
    "text": "create_text_channel",
    "voice": "create_voice_channel",
    "forum": "create_forum",
    "stage": "create_stage_channel",
    "category": "create_category",
}


def _serialize_channel_ref(ch: Any) -> Dict[str, Any]:
    """The small {id,name,type} ref every channel-CRUD op returns."""
    return {
        "id": getattr(ch, "id", None),
        "name": getattr(ch, "name", None),
        "type": str(getattr(ch, "type", None)),
        "parent_id": getattr(getattr(ch, "category", None), "id", None),
    }


@registry.op(
    "create_channel",
    "Create a channel in a guild (text, voice, forum, stage, or category). "
    "Requires admin. Server-structure-expanding — pair of delete_channel.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD,
                "Discord guild id to create the channel in."),
        OpParam("name", ParamKind.STRING, "New channel name."),
        OpParam("kind", ParamKind.STRING,
                "Channel kind: 'text', 'voice', 'forum', 'stage', or "
                "'category'.",
                required=False, default="text"),
        OpParam("category", ParamKind.CHANNEL,
                "Optional category channel id to nest the new channel under "
                "(ignored when kind is 'category').",
                required=False),
        OpParam("topic", ParamKind.STRING,
                "Optional topic (text/forum channels).", required=False),
    ],
    serialize=_serialize_channel_ref,
    agent_guidance=(
        "create_channel expands the server's structure — confirm the kind and "
        "name with the user before creating, and reuse the returned id rather "
        "than calling list_channels again."),
    scope=OpScope.GUILD,
    group="channels",
)
async def create_channel(ctx: OpContext, guild, name: str,
                         kind: str = "text", category=None,
                         topic: Optional[str] = None):
    creator_name = _CHANNEL_KIND_CREATORS.get(str(kind).lower())
    if creator_name is None:
        raise ValueError(
            f"kind must be one of {', '.join(_CHANNEL_KIND_CREATORS)}; got "
            f"{kind!r}.")
    kwargs: Dict[str, Any] = {
        "name": name,
        "reason": _op_audit_reason(ctx, "create_channel", None),
    }
    if kind != "category":
        if category is not None:
            if not isinstance(category, discord.CategoryChannel):
                raise ValueError(
                    f"category must be a category channel id, got "
                    f"{type(category).__name__}.")
            kwargs["category"] = category
        # topic is only valid on text/forum channels.
        if topic is not None and kind in ("text", "forum"):
            kwargs["topic"] = topic
    creator = getattr(guild, creator_name)
    return await creator(**kwargs)


@registry.op(
    "delete_channel",
    "Delete a channel and its entire history. Requires admin. The canonical "
    "destructive channel action — irreversible.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("channel", ParamKind.CHANNEL, "Discord channel id to delete."),
    ],
    serialize=lambda info: info,
    agent_guidance=(
        "delete_channel is irreversible and destroys the channel with all of "
        "its messages — confirm intent with the user before calling it, and "
        "never on a busy channel without explicit confirmation."),
    scope=OpScope.GUILD,
    group="channels",
)
async def delete_channel(ctx: OpContext, channel):
    info = {"deleted_channel_id": channel.id,
            "name": getattr(channel, "name", None)}
    await channel.delete(
        reason=_op_audit_reason(ctx, "delete_channel", None))
    return info


@registry.op(
    "clone_channel",
    "Clone a channel, copying its settings AND its permission overwrites into "
    "a new channel. Requires admin. This is create_channel plus an "
    "overwrite-write in one call.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("channel", ParamKind.CHANNEL, "Discord channel id to clone."),
        OpParam("name", ParamKind.STRING,
                "Optional name for the clone (defaults to the source name).",
                required=False),
    ],
    serialize=_serialize_channel_ref,
    scope=OpScope.GUILD,
    group="channels",
)
async def clone_channel(ctx: OpContext, channel, name: Optional[str] = None):
    if isinstance(channel, discord.Thread):
        raise ValueError("clone_channel does not accept threads.")
    kwargs: Dict[str, Any] = {
        "reason": _op_audit_reason(ctx, "clone_channel", None)}
    if name is not None:
        kwargs["name"] = name
    return await channel.clone(**kwargs)


@registry.op(
    "move_channel",
    "Move/reorder a channel: set its position and/or its category. Requires "
    "admin. Positions shift globally around the moved channel (like role "
    "positions, but more visible).",
    PermissionLevel.ADMIN,
    params=[
        OpParam("channel", ParamKind.CHANNEL, "Discord channel id to move."),
        OpParam("position", ParamKind.INTEGER,
                "New absolute position within its category/top level "
                "(0 = first).",
                required=False, minimum=0),
        OpParam("category_id", ParamKind.SNOWFLAKE,
                "Optional category channel id to move the channel into — a "
                "SNOWFLAKE (not a CHANNEL param) so it does not collide with "
                "the moved channel's own channel_id wire slot; resolved and "
                "guild-confined in-impl.",
                required=False),
    ],
    serialize=_serialize_channel_ref,
    agent_guidance=(
        "move_channel shifts the whole layout around the moved channel — "
        "after a batch of moves, call list_channels once to see the settled "
        "order rather than assuming each landed exactly where requested."),
    scope=OpScope.GUILD,
    group="channels",
)
async def move_channel(ctx: OpContext, channel, position: Optional[int] = None,
                       category_id: Optional[int] = None):
    if isinstance(channel, discord.Thread):
        raise ValueError("move_channel does not accept threads.")
    if position is None and category_id is None:
        raise ValueError(
            "Nothing to move: pass a position and/or a category_id.")
    category = None
    if category_id is not None:
        guild = getattr(channel, "guild", None)
        category = (guild.get_channel(int(category_id))
                    if guild is not None else None)
        if not isinstance(category, discord.CategoryChannel):
            raise ValueError(
                f"category_id {category_id} is not a category channel in "
                f"this guild.")
    reason = _op_audit_reason(ctx, "move_channel", None)
    if category is not None:
        kwargs: Dict[str, Any] = {"category": category, "reason": reason}
        if position is not None:
            kwargs["beginning"] = False
            kwargs["offset"] = position
        else:
            kwargs["end"] = True
        await channel.move(**kwargs)
    else:
        # Position-only move: the absolute-position form via edit.
        await channel.edit(position=position, reason=reason)
    return _serialize_channel_ref(channel)


def _overwrite_target(guild, target_type: str, target_id: int):
    """Resolve an overwrite target (a role or a member) IN THIS GUILD, so an
    overwrite write can never reach an entity outside the confined guild."""
    if target_type == "role":
        target = guild.get_role(target_id)
        if target is None:
            raise ValueError(
                f"No role with id {target_id} in guild {guild.id}.")
        return target
    if target_type == "member":
        target = guild.get_member(target_id)
        if target is None:
            raise ValueError(
                f"No member with id {target_id} in guild {guild.id}.")
        return target
    raise ValueError(
        f"target_type must be 'role' or 'member', got {target_type!r}.")


@registry.op(
    "set_channel_overwrite",
    "Write a permission overwrite (channel ACL) for a role or member on a "
    "channel. Requires admin — this rewrites who may do what in the channel. "
    "allow/deny are lists of permission names (e.g. 'read_messages', "
    "'send_messages'); a permission in neither list is left at inherit.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("channel", ParamKind.CHANNEL,
                "Discord channel id to set the overwrite on."),
        OpParam("target_type", ParamKind.STRING,
                "'role' or 'member' — what target_id refers to."),
        OpParam("target_id", ParamKind.SNOWFLAKE,
                "Role or member id (in this guild) the overwrite applies to."),
        OpParam("allow", ParamKind.STRING_LIST,
                "Permission names to explicitly ALLOW.", required=False),
        OpParam("deny", ParamKind.STRING_LIST,
                "Permission names to explicitly DENY.", required=False),
    ],
    serialize=lambda payload: payload,
    agent_guidance=(
        "set_channel_overwrite rewrites a channel ACL — use "
        "list_channel_overwrites first to see the current entries, and "
        "delete_channel_overwrite to remove one entirely (an empty allow+deny "
        "here writes an all-inherit overwrite, not a removal)."),
    scope=OpScope.GUILD,
    group="channels",
)
async def set_channel_overwrite(ctx: OpContext, channel, target_type: str,
                                target_id: int,
                                allow: Optional[List[str]] = None,
                                deny: Optional[List[str]] = None):
    if isinstance(channel, discord.Thread):
        raise ValueError(
            "set_channel_overwrite does not accept threads — overwrites live "
            "on the parent channel.")
    guild = getattr(channel, "guild", None)
    if guild is None:
        raise ValueError("set_channel_overwrite requires a guild channel.")
    target = _overwrite_target(guild, str(target_type),
                               _as_int(target_id, "target_id"))
    valid = {name for name, _ in discord.Permissions()}
    overwrite = discord.PermissionOverwrite()
    for name in (allow or []):
        if name not in valid:
            raise ValueError(f"Unknown permission name {name!r}.")
        setattr(overwrite, name, True)
    for name in (deny or []):
        if name not in valid:
            raise ValueError(f"Unknown permission name {name!r}.")
        setattr(overwrite, name, False)
    await channel.set_permissions(
        target, overwrite=overwrite,
        reason=_op_audit_reason(ctx, "set_channel_overwrite", None))
    return {
        "channel_id": channel.id,
        "target_type": str(target_type),
        "target_id": target.id,
        "allow": list(allow or []),
        "deny": list(deny or []),
    }


@registry.op(
    "delete_channel_overwrite",
    "Remove a role's or member's permission overwrite from a channel "
    "entirely (back to inherit). Requires admin.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("channel", ParamKind.CHANNEL,
                "Discord channel id to remove the overwrite from."),
        OpParam("target_type", ParamKind.STRING,
                "'role' or 'member' — what target_id refers to."),
        OpParam("target_id", ParamKind.SNOWFLAKE,
                "Role or member id (in this guild) whose overwrite to remove."),
    ],
    serialize=lambda payload: payload,
    scope=OpScope.GUILD,
    group="channels",
)
async def delete_channel_overwrite(ctx: OpContext, channel, target_type: str,
                                   target_id: int):
    if isinstance(channel, discord.Thread):
        raise ValueError(
            "delete_channel_overwrite does not accept threads.")
    guild = getattr(channel, "guild", None)
    if guild is None:
        raise ValueError("delete_channel_overwrite requires a guild channel.")
    target = _overwrite_target(guild, str(target_type),
                               _as_int(target_id, "target_id"))
    await channel.set_permissions(
        target, overwrite=None,
        reason=_op_audit_reason(ctx, "delete_channel_overwrite", None))
    return {
        "channel_id": channel.id,
        "target_type": str(target_type),
        "target_id": target.id,
        "removed": True,
    }


# -- threads (destructive / membership) -------------------------------------

@registry.op(
    "delete_thread",
    "Delete a thread and all its messages. Requires admin. Destructive — "
    "edit_thread with archived=true is the reversible alternative.",
    PermissionLevel.ADMIN,
    params=[OpParam("channel", ParamKind.CHANNEL,
                    "Discord thread id to delete.")],
    serialize=lambda info: info,
    agent_guidance=(
        "delete_thread is irreversible — prefer edit_thread with "
        "archived=true, which preserves the conversation and can be reopened."),
    scope=OpScope.GUILD,
    group="threads",
)
async def delete_thread(ctx: OpContext, channel):
    thread = _require_thread(channel)
    info = {"deleted_thread_id": thread.id, "name": thread.name}
    await thread.delete()
    return info


@registry.op(
    "add_thread_member",
    "Add a user to a thread. Requires admin. Note this fires a notification "
    "to the added user (it is the one add-class op that pings).",
    PermissionLevel.ADMIN,
    params=[
        OpParam("channel", ParamKind.CHANNEL,
                "Discord thread id to add the user to."),
        OpParam("member", ParamKind.MEMBER,
                "Discord user id to add to the thread."),
    ],
    serialize=lambda payload: payload,
    scope=OpScope.GUILD,
    group="threads",
)
async def add_thread_member(ctx: OpContext, channel, member):
    thread = _require_thread(channel)
    await thread.add_user(member)
    return {"thread_id": thread.id, "user_id": member.id, "added": True}


@registry.op(
    "remove_thread_member",
    "Remove a user from a thread (ejects them from the conversation). "
    "Requires admin. Reversible via add_thread_member.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("channel", ParamKind.CHANNEL,
                "Discord thread id to remove the user from."),
        OpParam("member", ParamKind.MEMBER,
                "Discord user id to remove from the thread."),
    ],
    serialize=lambda payload: payload,
    scope=OpScope.GUILD,
    group="threads",
)
async def remove_thread_member(ctx: OpContext, channel, member):
    thread = _require_thread(channel)
    await thread.remove_user(member)
    return {"thread_id": thread.id, "user_id": member.id, "removed": True}


@registry.op(
    "list_private_archived_threads",
    "List a channel's archived PRIVATE threads — invite-only conversations "
    "the actor may never have been in. Requires admin (privacy-sensitive "
    "beyond the channel-visibility gate: thread membership, not channel "
    "readability, is the real boundary).",
    PermissionLevel.ADMIN,
    params=[
        OpParam("channel", ParamKind.CHANNEL,
                "Parent channel id whose archived private threads to list."),
        OpParam("limit", ParamKind.INTEGER,
                "Max threads to return (default 100).",
                required=False, default=100, minimum=1, maximum=500),
    ],
    serialize=lambda payload: payload,
    scope=OpScope.GUILD,
    group="threads",
)
async def list_private_archived_threads(ctx: OpContext, channel,
                                        limit: int = 100):
    if not hasattr(channel, "archived_threads"):
        raise ValueError(
            f"Channel {getattr(channel, 'id', '?')} "
            f"({type(channel).__name__}) cannot parent threads.")
    rows = []
    async for t in channel.archived_threads(private=True, limit=limit):
        rows.append(serialize_thread(t))
    return {"threads": rows, "count": len(rows)}


# -- member moderation (kick / ban / prune / role edit) ---------------------

# Discord's bulk-ban and delete_message caps.
BULK_BAN_MAX = 200
BAN_DELETE_MESSAGE_SECONDS_MAX = 604800  # 7 days


@registry.op(
    "kick_member",
    "Kick a member from the guild. Requires admin. Irreversible by the bot — "
    "the member must be re-invited to return.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD,
                "Discord guild id the member belongs to. Optional in-guild; "
                "required over guild-less frontends like MCP.",
                required=False),
        OpParam("member", ParamKind.MEMBER, "Discord user id to kick."),
        OpParam("reason", ParamKind.STRING,
                "Optional audit-log reason.", required=False),
    ],
    serialize=lambda payload: payload,
    agent_guidance=(
        "kick_member ejects a member (they can rejoin only via a new invite) "
        "— confirm intent with the user before calling it. For a reversible "
        "silencing, timeout_member is the softer tool."),
    scope=OpScope.GUILD,
    group="members",
)
async def kick_member(ctx: OpContext, member, reason: Optional[str] = None,
                      guild=None):
    guild = guild or getattr(member, "guild", None) or ctx.guild
    if guild is None:
        raise ValueError("kick_member needs a guild context.")
    await guild.kick(member,
                     reason=_op_audit_reason(ctx, "kick_member", reason))
    return {"member_id": member.id, "kicked": True}


@registry.op(
    "ban_member",
    "Ban a member (or any user id) from the guild, optionally deleting their "
    "recent messages. Requires admin. Blocks rejoin until unbanned; can "
    "bulk-delete up to 7 days of the target's messages.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD,
                "Discord guild id to ban from. Optional in-guild; required "
                "over guild-less frontends like MCP.",
                required=False),
        OpParam("member", ParamKind.MEMBER, "Discord user id to ban."),
        OpParam("reason", ParamKind.STRING,
                "Optional audit-log reason.", required=False),
        OpParam("delete_message_seconds", ParamKind.INTEGER,
                "Seconds of the target's recent messages to delete (0 = "
                "none; max 604800 = 7 days).",
                required=False, default=0, minimum=0,
                maximum=BAN_DELETE_MESSAGE_SECONDS_MAX),
    ],
    serialize=lambda payload: payload,
    agent_guidance=(
        "ban_member blocks the user from rejoining and can erase up to 7 days "
        "of their messages (delete_message_seconds) — confirm both the ban "
        "and any message deletion with the user first. unban_member reverses "
        "the ban, but deleted messages are gone."),
    scope=OpScope.GUILD,
    group="members",
)
async def ban_member(ctx: OpContext, member, reason: Optional[str] = None,
                     delete_message_seconds: int = 0, guild=None):
    guild = guild or getattr(member, "guild", None) or ctx.guild
    if guild is None:
        raise ValueError("ban_member needs a guild context.")
    await guild.ban(
        member, reason=_op_audit_reason(ctx, "ban_member", reason),
        delete_message_seconds=delete_message_seconds)
    return {"member_id": member.id, "banned": True,
            "deleted_message_seconds": delete_message_seconds}


@registry.op(
    "unban_member",
    "Unban a user from the guild by id. Requires admin. The reversal half of "
    "ban_member; strictly access-restoring.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD,
                "Discord guild id to unban from. Optional in-guild; required "
                "over guild-less frontends like MCP.",
                required=False),
        OpParam("user", ParamKind.USER, "Discord user id to unban."),
        OpParam("reason", ParamKind.STRING,
                "Optional audit-log reason.", required=False),
    ],
    serialize=lambda payload: payload,
    scope=OpScope.GUILD,
    group="members",
)
async def unban_member(ctx: OpContext, user, reason: Optional[str] = None,
                       guild=None):
    guild = guild or ctx.guild
    if guild is None:
        raise ValueError("unban_member needs a guild context.")
    await guild.unban(user,
                      reason=_op_audit_reason(ctx, "unban_member", reason))
    return {"user_id": user.id, "unbanned": True}


@registry.op(
    "bulk_ban",
    "Ban up to 200 users at once by id, optionally deleting a day of their "
    "messages. Requires SUPERADMIN — a mass-destructive raid tool; one bad "
    "id list nukes 200 members.",
    PermissionLevel.SUPERADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD,
                "Discord guild id to ban from. Optional in-guild; required "
                "over guild-less frontends like MCP.",
                required=False),
        OpParam("user_ids", ParamKind.STRING_LIST,
                "User ids to ban (max 200)."),
        OpParam("reason", ParamKind.STRING,
                "Optional audit-log reason.", required=False),
        OpParam("delete_message_seconds", ParamKind.INTEGER,
                "Seconds of recent messages to delete per user (default "
                "86400 = 1 day; max 604800 = 7 days).",
                required=False, default=86400, minimum=0,
                maximum=BAN_DELETE_MESSAGE_SECONDS_MAX),
    ],
    serialize=lambda payload: payload,
    agent_guidance=(
        "bulk_ban is a superadmin-only mass action — never call it without "
        "explicit, specific confirmation of the exact id list; a wrong list "
        "bans up to 200 people irreversibly."),
    scope=OpScope.GUILD,
    group="members",
)
async def bulk_ban(ctx: OpContext, user_ids: List[str],
                   reason: Optional[str] = None,
                   delete_message_seconds: int = 86400, guild=None):
    guild = guild or ctx.guild
    if guild is None:
        raise ValueError("bulk_ban needs a guild context.")
    ids = [_as_int(uid, "user_ids") for uid in (user_ids or [])]
    if not ids:
        raise ValueError("bulk_ban requires at least one user id.")
    if len(ids) > BULK_BAN_MAX:
        raise ValueError(
            f"bulk_ban accepts at most {BULK_BAN_MAX} users, got {len(ids)}.")
    targets = [discord.Object(id=i) for i in ids]
    result = await guild.bulk_ban(
        targets, reason=_op_audit_reason(ctx, "bulk_ban", reason),
        delete_message_seconds=delete_message_seconds)
    banned = [getattr(o, "id", None) for o in getattr(result, "banned", [])]
    failed = [getattr(o, "id", None) for o in getattr(result, "failed", [])]
    return {"banned_user_ids": banned, "failed_user_ids": failed,
            "banned_count": len(banned)}


@registry.op(
    "prune_members",
    "Prune inactive members (no role, no activity in the given number of "
    "days). Requires admin. A mass kick with no undo — the client fronts it "
    "with an explicit confirmation flow (estimate_prune is the safe read "
    "half).",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD,
                "Discord guild id to prune. Optional in-guild; required over "
                "guild-less frontends like MCP.",
                required=False),
        OpParam("days", ParamKind.INTEGER,
                "Inactivity threshold in days (1-30).",
                minimum=1, maximum=30),
        OpParam("reason", ParamKind.STRING,
                "Optional audit-log reason.", required=False),
    ],
    serialize=lambda payload: payload,
    agent_guidance=(
        "prune_members mass-kicks inactive members with no undo — always run "
        "estimate_prune for the same days first and confirm the number with "
        "the user before pruning."),
    scope=OpScope.GUILD,
    group="members",
)
async def prune_members(ctx: OpContext, days: int,
                        reason: Optional[str] = None, guild=None):
    guild = guild or ctx.guild
    if guild is None:
        raise ValueError("prune_members needs a guild context.")
    pruned = await guild.prune_members(
        days=days, reason=_op_audit_reason(ctx, "prune_members", reason))
    return {"days": days, "pruned_members": pruned}


@registry.op(
    "edit_member_roles",
    "Add and/or remove multiple roles on a member in one call. Requires "
    "admin. Managed roles and @everyone are refused. Reversible by swapping "
    "the add/remove lists.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD,
                "Discord guild id the member belongs to. Optional in-guild; "
                "required over guild-less frontends like MCP.",
                required=False),
        OpParam("member", ParamKind.MEMBER, "Discord user id to edit."),
        OpParam("add_role_ids", ParamKind.STRING_LIST,
                "Role ids to grant.", required=False),
        OpParam("remove_role_ids", ParamKind.STRING_LIST,
                "Role ids to remove.", required=False),
    ],
    serialize=lambda payload: payload,
    agent_guidance=(
        "edit_member_roles is the batch form of add_role/remove_role — role "
        "ids must come from list_roles, never guessed; managed and @everyone "
        "roles are refused."),
    scope=OpScope.GUILD,
    group="members",
)
async def edit_member_roles(ctx: OpContext, member,
                            add_role_ids: Optional[List[str]] = None,
                            remove_role_ids: Optional[List[str]] = None,
                            guild=None):
    guild = guild or getattr(member, "guild", None) or ctx.guild
    if guild is None:
        raise ValueError("edit_member_roles needs a guild context.")
    if not add_role_ids and not remove_role_ids:
        raise ValueError(
            "Nothing to do: pass add_role_ids and/or remove_role_ids.")

    def _resolve(ids):
        out = []
        for rid in (ids or []):
            role = guild.get_role(_as_int(rid, "role_ids"))
            if role is None:
                raise ValueError(
                    f"No role with id {rid} in guild {guild.id}.")
            _guard_editable(role)
            out.append(role)
        return out

    to_add = _resolve(add_role_ids)
    to_remove = _resolve(remove_role_ids)
    reason = _op_audit_reason(ctx, "edit_member_roles", None)
    if to_add:
        await member.add_roles(*to_add, reason=reason)
    if to_remove:
        await member.remove_roles(*to_remove, reason=reason)
    return {
        "member_id": member.id,
        "added_role_ids": [r.id for r in to_add],
        "removed_role_ids": [r.id for r in to_remove],
    }


# -- message moderation -----------------------------------------------------

# Discord's bulk-delete cap (a single delete_messages / purge batch).
BULK_DELETE_MAX = 100


@registry.op(
    "bulk_delete_messages",
    "Bulk-delete specific messages by id in one channel (max 100, and none "
    "older than 14 days — Discord's bulk-delete limits). Requires admin. "
    "Destructive and irreversible.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("channel", ParamKind.CHANNEL,
                "Discord channel id the messages are in."),
        OpParam("message_ids", ParamKind.STRING_LIST,
                "Message ids to delete (max 100, all under 14 days old)."),
    ],
    serialize=lambda payload: payload,
    agent_guidance=(
        "bulk_delete_messages erases the listed messages permanently — "
        "confirm the id list with the user; Discord refuses messages older "
        "than 14 days and batches over 100."),
    scope=OpScope.GUILD,
    group="message-mod",
)
async def bulk_delete_messages(ctx: OpContext, channel,
                               message_ids: List[str]):
    if not hasattr(channel, "delete_messages"):
        raise ValueError(
            f"Channel {getattr(channel, 'id', '?')} "
            f"({type(channel).__name__}) does not support bulk delete.")
    ids = [_as_int(mid, "message_ids") for mid in (message_ids or [])]
    if not ids:
        raise ValueError("bulk_delete_messages requires at least one id.")
    if len(ids) > BULK_DELETE_MAX:
        raise ValueError(
            f"bulk_delete_messages accepts at most {BULK_DELETE_MAX} ids, "
            f"got {len(ids)}.")
    targets = [discord.Object(id=i) for i in ids]
    await channel.delete_messages(
        targets, reason=_op_audit_reason(ctx, "bulk_delete_messages", None))
    return {"channel_id": channel.id, "deleted_count": len(ids)}


@registry.op(
    "purge_messages",
    "Purge the most recent messages in a channel (up to a limit, optionally "
    "only a given author's). Requires SUPERADMIN — unbounded mass delete, "
    "the server-wide-blast-radius sibling of the !cleanup command.",
    PermissionLevel.SUPERADMIN,
    params=[
        OpParam("channel", ParamKind.CHANNEL,
                "Discord channel id to purge."),
        OpParam("limit", ParamKind.INTEGER,
                "How many recent messages to scan/delete (max 100).",
                minimum=1, maximum=BULK_DELETE_MAX),
        OpParam("author", ParamKind.MEMBER,
                "Optional: only delete messages by this member.",
                required=False),
    ],
    serialize=lambda payload: payload,
    agent_guidance=(
        "purge_messages is superadmin-only and deletes in bulk — confirm the "
        "channel, the count, and any author filter explicitly before calling."),
    scope=OpScope.GUILD,
    group="message-mod",
)
async def purge_messages(ctx: OpContext, channel, limit: int, author=None):
    if not hasattr(channel, "purge"):
        raise ValueError(
            f"Channel {getattr(channel, 'id', '?')} "
            f"({type(channel).__name__}) does not support purge.")
    kwargs: Dict[str, Any] = {
        "limit": limit,
        "reason": _op_audit_reason(ctx, "purge_messages", None),
    }
    if author is not None:
        kwargs["check"] = lambda m: m.author.id == author.id
    deleted = await channel.purge(**kwargs)
    return {"channel_id": channel.id, "deleted_count": len(deleted)}


@registry.op(
    "publish_message",
    "Publish (crosspost) a message in an announcement channel to every "
    "following server. Requires admin. Irreversible broadcast — there is no "
    "un-publish — under a tight 10/hour rate limit.",
    PermissionLevel.ADMIN,
    params=[OpParam("message", ParamKind.MESSAGE,
                    "Announcement-channel message id to publish.")],
    serialize=lambda m: {"message_id": m.id, "published": True},
    agent_guidance=(
        "publish_message broadcasts irreversibly to every server following "
        "the announcement channel — confirm with the user before publishing, "
        "and note the 10/hour rate limit."),
    scope=OpScope.GUILD,
    group="message-mod",
)
async def publish_message(ctx: OpContext, message):
    await message.publish()
    return message


@registry.op(
    "send_tts",
    "Send a text-to-speech message to a channel — Discord reads it ALOUD to "
    "everyone currently focused on the channel. Requires admin (audible "
    "interruption of everyone in the channel).",
    PermissionLevel.ADMIN,
    params=[
        OpParam("channel", ParamKind.CHANNEL, "Channel to send into."),
        OpParam("content", ParamKind.STRING, "Text to speak (non-empty)."),
    ],
    serialize=_serialize_sent_message,
    agent_guidance=(
        "send_tts is audible to everyone in the channel — use it only when "
        "the user explicitly asks for a spoken/TTS message, never for an "
        "ordinary reply (that is send_message)."),
    scope=OpScope.GUILD,
    group="message-mod",
)
async def send_tts(ctx: OpContext, channel, content: str):
    if not str(content).strip():
        raise ValueError("send_tts requires non-empty content.")
    return await channel.send(
        content, tts=True,
        allowed_mentions=discord.AllowedMentions.none())


@registry.op(
    "send_sticker",
    "Send a message consisting of a guild sticker. Requires admin. The "
    "sticker must belong to THIS guild (foreign ids are refused, preserving "
    "guild confinement).",
    PermissionLevel.ADMIN,
    params=[
        OpParam("channel", ParamKind.CHANNEL, "Channel to send into."),
        OpParam("sticker_id", ParamKind.SNOWFLAKE,
                "Guild sticker id to send (see list_stickers)."),
    ],
    serialize=_serialize_sent_message,
    scope=OpScope.GUILD,
    group="message-mod",
)
async def send_sticker(ctx: OpContext, channel, sticker_id: int):
    guild = getattr(channel, "guild", None)
    if guild is None:
        raise ValueError("send_sticker requires a guild channel.")
    sticker = _require_guild_sticker(guild, _as_int(sticker_id, "sticker_id"))
    return await channel.send(
        stickers=[sticker],
        allowed_mentions=discord.AllowedMentions.none())


@registry.op(
    "remove_reaction_other",
    "Remove ANOTHER user's specific emoji reaction from a message. Requires "
    "admin. This deliberately overrides the 'other users' reactions are "
    "untouchable' rule of remove_reaction — a moderation action (it can also "
    "strip reaction-role toggles, so use it carefully).",
    PermissionLevel.ADMIN,
    params=[
        OpParam("message", ParamKind.MESSAGE,
                "Message to remove the reaction from."),
        OpParam("member", ParamKind.MEMBER,
                "Whose reaction to remove."),
        OpParam("emoji", ParamKind.STRING,
                "Emoji to remove (unicode emoji or `name:id` custom emoji)."),
    ],
    serialize=lambda payload: payload,
    agent_guidance=(
        "remove_reaction_other strips another member's reaction — it can "
        "remove a reaction-role toggle by mistake; confirm the target and "
        "emoji with the user first."),
    scope=OpScope.GUILD,
    group="message-mod",
)
async def remove_reaction_other(ctx: OpContext, message, member, emoji: str):
    await message.remove_reaction(emoji, member)
    return {"message_id": message.id, "user_id": member.id, "removed": True}


@registry.op(
    "clear_reactions",
    "Clear reactions from a message: one emoji, or ALL reactions. Requires "
    "admin. Destructive (also strips reaction-role toggles); no undo.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("message", ParamKind.MESSAGE,
                "Message whose reactions to clear."),
        OpParam("emoji", ParamKind.STRING,
                "Optional: clear only this emoji. Omit to clear ALL "
                "reactions.",
                required=False),
    ],
    serialize=lambda payload: payload,
    agent_guidance=(
        "clear_reactions with no emoji wipes EVERY reaction (including "
        "reaction-role toggles) — confirm intent; pass an emoji to clear just "
        "that one."),
    scope=OpScope.GUILD,
    group="message-mod",
)
async def clear_reactions(ctx: OpContext, message, emoji: Optional[str] = None):
    if emoji is not None and str(emoji).strip():
        await message.clear_reaction(emoji)
        return {"message_id": message.id, "cleared_emoji": emoji}
    await message.clear_reactions()
    return {"message_id": message.id, "cleared_all": True}


# -- webhooks ---------------------------------------------------------------

async def _resolve_guild_webhook(guild, webhook_id: int):
    """Resolve a webhook by id against THIS guild's own webhook list, so an
    edit/delete/execute can never reach a webhook outside the confined guild.
    Returns a full Webhook (carrying the token needed to execute/edit)."""
    for w in await guild.webhooks():
        if w.id == webhook_id:
            return w
    raise ValueError(
        f"No webhook with id {webhook_id} in guild {guild.id} — see "
        f"list_webhooks.")


def _serialize_webhook_ref(w: Any) -> Dict[str, Any]:
    """Webhook ref WITHOUT url/token — a bearer credential is never serialized
    (same rule list_webhooks follows)."""
    return {
        "id": getattr(w, "id", None),
        "name": getattr(w, "name", None),
        "channel_id": getattr(w, "channel_id", None),
    }


@registry.op(
    "create_webhook",
    "Create a webhook on a channel. Requires admin. Mints a persistent "
    "unauthenticated posting credential — anyone holding the URL can post as "
    "it, outside every permission gate. The URL/token is NEVER returned by "
    "this op.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("channel", ParamKind.CHANNEL,
                "Discord channel id to create the webhook on."),
        OpParam("name", ParamKind.STRING, "Webhook name."),
    ],
    serialize=_serialize_webhook_ref,
    agent_guidance=(
        "create_webhook mints a posting credential; this op deliberately "
        "never returns the webhook URL/token, so tell the user to copy it "
        "from Server Settings — never try to surface it."),
    scope=OpScope.GUILD,
    group="webhooks",
)
async def create_webhook(ctx: OpContext, channel, name: str):
    if not hasattr(channel, "create_webhook"):
        raise ValueError(
            f"Channel {getattr(channel, 'id', '?')} "
            f"({type(channel).__name__}) cannot carry webhooks.")
    webhook = await channel.create_webhook(
        name=name, reason=_op_audit_reason(ctx, "create_webhook", None))
    return webhook


@registry.op(
    "edit_webhook",
    "Rename a webhook and/or move it to another channel. Requires admin. Can "
    "silently redirect/rebrand webhooks owned by third-party integrations.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD,
                "Discord guild id the webhook belongs to."),
        OpParam("webhook_id", ParamKind.SNOWFLAKE,
                "Webhook id to edit (from list_webhooks)."),
        OpParam("name", ParamKind.STRING, "New webhook name.", required=False),
        OpParam("channel", ParamKind.CHANNEL,
                "New channel id to move the webhook to.", required=False),
    ],
    serialize=_serialize_webhook_ref,
    scope=OpScope.GUILD,
    group="webhooks",
)
async def edit_webhook(ctx: OpContext, guild, webhook_id: int,
                       name: Optional[str] = None, channel=None):
    webhook = await _resolve_guild_webhook(
        guild, _as_int(webhook_id, "webhook_id"))
    kwargs: Dict[str, Any] = {
        "reason": _op_audit_reason(ctx, "edit_webhook", None)}
    if name is not None:
        kwargs["name"] = name
    if channel is not None:
        # Confine the destination to this guild.
        if getattr(channel, "guild", None) is None or channel.guild.id != guild.id:
            raise ValueError(
                "edit_webhook destination channel must be in the same guild.")
        kwargs["channel"] = channel
    if "name" not in kwargs and "channel" not in kwargs:
        raise ValueError("Nothing to edit: pass a name and/or a channel.")
    edited = await webhook.edit(**kwargs)
    return edited if edited is not None else webhook


@registry.op(
    "delete_webhook",
    "Delete a webhook. Requires admin. Destructive — irreversibly kills any "
    "third-party integration depending on it; the same URL can never be "
    "recreated.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD,
                "Discord guild id the webhook belongs to."),
        OpParam("webhook_id", ParamKind.SNOWFLAKE,
                "Webhook id to delete (from list_webhooks)."),
    ],
    serialize=lambda payload: payload,
    agent_guidance=(
        "delete_webhook is irreversible and can break a GitHub/RSS-style "
        "integration silently — confirm with the user which webhook (by id "
        "from list_webhooks) before deleting."),
    scope=OpScope.GUILD,
    group="webhooks",
)
async def delete_webhook(ctx: OpContext, guild, webhook_id: int):
    webhook = await _resolve_guild_webhook(
        guild, _as_int(webhook_id, "webhook_id"))
    info = {"deleted_webhook_id": webhook.id, "name": webhook.name}
    await webhook.delete(reason=_op_audit_reason(ctx, "delete_webhook", None))
    return info


@registry.op(
    "execute_webhook",
    "Post a message through a webhook, optionally under a custom display name "
    "and avatar. Requires admin. Impersonation surface — it sends under an "
    "arbitrary name/face, sidestepping the bot's own identity; never pings.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD,
                "Discord guild id the webhook belongs to."),
        OpParam("webhook_id", ParamKind.SNOWFLAKE,
                "Webhook id to post through (from list_webhooks)."),
        OpParam("content", ParamKind.STRING, "Message text to post."),
        OpParam("username", ParamKind.STRING,
                "Optional display name to post under.", required=False),
        OpParam("avatar_url", ParamKind.STRING,
                "Optional avatar image URL to post with.", required=False),
    ],
    serialize=lambda payload: payload,
    agent_guidance=(
        "execute_webhook posts under an arbitrary name/avatar — that is "
        "impersonation; use it only when the user explicitly wants a "
        "webhook-authored message, and it never pings anyone."),
    scope=OpScope.GUILD,
    group="webhooks",
)
async def execute_webhook(ctx: OpContext, guild, webhook_id: int,
                          content: str, username: Optional[str] = None,
                          avatar_url: Optional[str] = None):
    if not str(content).strip():
        raise ValueError("execute_webhook requires non-empty content.")
    webhook = await _resolve_guild_webhook(
        guild, _as_int(webhook_id, "webhook_id"))
    kwargs: Dict[str, Any] = {
        "wait": True,
        "allowed_mentions": discord.AllowedMentions.none(),
    }
    if username is not None:
        kwargs["username"] = username
    if avatar_url is not None:
        kwargs["avatar_url"] = avatar_url
    sent = await webhook.send(content, **kwargs)
    return {"webhook_id": webhook.id,
            "message_id": getattr(sent, "id", None)}


# -- guild settings ---------------------------------------------------------

@registry.op(
    "edit_guild_settings",
    "Edit guild-identity settings: name, description, and verification "
    "level. Requires SUPERADMIN — guild-identity and safety-posture changes "
    "have server-wide blast radius. Deliberately narrow: dangerous sub-fields "
    "(owner transfer, MFA level, icon) are not exposed.",
    PermissionLevel.SUPERADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD, "Discord guild id to edit."),
        OpParam("name", ParamKind.STRING, "New guild name.", required=False),
        OpParam("description", ParamKind.STRING,
                "New guild description.", required=False),
        OpParam("verification_level", ParamKind.STRING,
                "New verification level: 'none', 'low', 'medium', 'high', or "
                "'highest'.",
                required=False),
    ],
    serialize=lambda payload: payload,
    agent_guidance=(
        "edit_guild_settings is superadmin-only and changes the server's "
        "public identity — confirm every field with the user before "
        "applying; raising verification_level can lock out new members."),
    scope=OpScope.GUILD,
    group="guild-info",
)
async def edit_guild_settings(ctx: OpContext, guild,
                              name: Optional[str] = None,
                              description: Optional[str] = None,
                              verification_level: Optional[str] = None):
    kwargs: Dict[str, Any] = {}
    if name is not None:
        kwargs["name"] = name
    if description is not None:
        kwargs["description"] = description
    if verification_level is not None:
        level = getattr(discord.VerificationLevel,
                        str(verification_level), None)
        if not isinstance(level, discord.VerificationLevel):
            raise ValueError(
                f"Unknown verification_level {verification_level!r} — use "
                f"none/low/medium/high/highest.")
        kwargs["verification_level"] = level
    if not kwargs:
        raise ValueError(
            "Nothing to edit: pass at least one of name/description/"
            "verification_level.")
    await guild.edit(reason=_op_audit_reason(ctx, "edit_guild_settings", None),
                     **kwargs)
    return {
        "id": guild.id,
        "name": guild.name,
        "description": getattr(guild, "description", None),
        "verification_level": str(getattr(guild, "verification_level", None)),
    }


# -- automod CRUD -----------------------------------------------------------

_AUTOMOD_TRIGGER_TYPES = {
    "keyword": "keyword",
    "spam": "spam",
    "keyword_preset": "keyword_preset",
    "mention_spam": "mention_spam",
}


def _build_automod_trigger(trigger_type: str,
                           keyword_filter: Optional[List[str]],
                           regex_patterns: Optional[List[str]],
                           mention_limit: Optional[int]):
    ttype = getattr(discord.AutoModRuleTriggerType, trigger_type, None)
    if not isinstance(ttype, discord.AutoModRuleTriggerType):
        raise ValueError(
            f"trigger_type must be one of "
            f"{', '.join(_AUTOMOD_TRIGGER_TYPES)}; got {trigger_type!r}.")
    kwargs: Dict[str, Any] = {"type": ttype}
    if trigger_type == "keyword":
        if not keyword_filter and not regex_patterns:
            raise ValueError(
                "keyword triggers need a keyword_filter and/or "
                "regex_patterns.")
        if keyword_filter:
            kwargs["keyword_filter"] = list(keyword_filter)
        if regex_patterns:
            kwargs["regex_patterns"] = list(regex_patterns)
    elif trigger_type == "mention_spam":
        if mention_limit is None:
            raise ValueError("mention_spam triggers need a mention_limit.")
        kwargs["mention_limit"] = mention_limit
    return discord.AutoModTrigger(**kwargs)


def _serialize_automod_ref(rule: Any) -> Dict[str, Any]:
    return {"id": rule.id, "name": rule.name,
            "enabled": bool(getattr(rule, "enabled", False))}


@registry.op(
    "create_automod_rule",
    "Create an AutoMod rule (guild-wide enforcement policy that blocks "
    "matching messages). Requires admin. v0 supports the common 'keyword' "
    "trigger (keyword_filter/regex_patterns) and 'mention_spam' "
    "(mention_limit); the block action is applied automatically.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD, "Discord guild id to create in."),
        OpParam("name", ParamKind.STRING, "Rule name."),
        OpParam("trigger_type", ParamKind.STRING,
                "'keyword' or 'mention_spam'."),
        OpParam("keyword_filter", ParamKind.STRING_LIST,
                "Substrings to block (keyword trigger).", required=False),
        OpParam("regex_patterns", ParamKind.STRING_LIST,
                "Regex patterns to block (keyword trigger).", required=False),
        OpParam("mention_limit", ParamKind.INTEGER,
                "Max mentions allowed before the rule fires (mention_spam "
                "trigger).",
                required=False, minimum=1, maximum=50),
        OpParam("enabled", ParamKind.BOOLEAN,
                "Enable the rule immediately (default true).",
                required=False, default=True),
    ],
    serialize=_serialize_automod_ref,
    agent_guidance=(
        "create_automod_rule installs a guild-wide filter the whole server "
        "feels — confirm the trigger and keywords with the user, and use "
        "list_automod_rules to review existing rules first."),
    scope=OpScope.GUILD,
    group="automod",
)
async def create_automod_rule(ctx: OpContext, guild, name: str,
                              trigger_type: str,
                              keyword_filter: Optional[List[str]] = None,
                              regex_patterns: Optional[List[str]] = None,
                              mention_limit: Optional[int] = None,
                              enabled: bool = True):
    trigger = _build_automod_trigger(str(trigger_type), keyword_filter,
                                     regex_patterns, mention_limit)
    action = discord.AutoModRuleAction(
        type=discord.AutoModRuleActionType.block_message)
    return await guild.create_automod_rule(
        name=name,
        event_type=discord.AutoModRuleEventType.message_send,
        trigger=trigger,
        actions=[action],
        enabled=bool(enabled),
        reason=_op_audit_reason(ctx, "create_automod_rule", None),
    )


@registry.op(
    "edit_automod_rule",
    "Edit an AutoMod rule: rename, enable/disable, or replace its keyword/"
    "regex filters. Requires admin. Disabling a rule silently drops the "
    "protection it provided.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD,
                "Discord guild id the rule belongs to."),
        OpParam("rule_id", ParamKind.SNOWFLAKE,
                "AutoMod rule id to edit (from list_automod_rules)."),
        OpParam("name", ParamKind.STRING, "New rule name.", required=False),
        OpParam("enabled", ParamKind.BOOLEAN,
                "Enable (true) or disable (false) the rule.", required=False),
        OpParam("keyword_filter", ParamKind.STRING_LIST,
                "Replacement keyword list (keyword rules).", required=False),
        OpParam("regex_patterns", ParamKind.STRING_LIST,
                "Replacement regex list (keyword rules).", required=False),
    ],
    serialize=_serialize_automod_ref,
    scope=OpScope.GUILD,
    group="automod",
)
async def edit_automod_rule(ctx: OpContext, guild, rule_id: int,
                            name: Optional[str] = None,
                            enabled: Optional[bool] = None,
                            keyword_filter: Optional[List[str]] = None,
                            regex_patterns: Optional[List[str]] = None):
    rule = await guild.fetch_automod_rule(_as_int(rule_id, "rule_id"))
    kwargs: Dict[str, Any] = {}
    if name is not None:
        kwargs["name"] = name
    if enabled is not None:
        kwargs["enabled"] = enabled
    if keyword_filter is not None or regex_patterns is not None:
        existing = getattr(rule, "trigger", None)
        ttype = getattr(existing, "type", None)
        if ttype != discord.AutoModRuleTriggerType.keyword:
            raise ValueError(
                "keyword_filter/regex_patterns can only be edited on a "
                "keyword-trigger rule.")
        trigger = discord.AutoModTrigger(
            type=discord.AutoModRuleTriggerType.keyword,
            keyword_filter=list(keyword_filter)
            if keyword_filter is not None
            else list(getattr(existing, "keyword_filter", None) or []),
            regex_patterns=list(regex_patterns)
            if regex_patterns is not None
            else list(getattr(existing, "regex_patterns", None) or []),
        )
        kwargs["trigger"] = trigger
    if not kwargs:
        raise ValueError(
            "Nothing to edit: pass name/enabled/keyword_filter/"
            "regex_patterns.")
    edited = await rule.edit(
        reason=_op_audit_reason(ctx, "edit_automod_rule", None), **kwargs)
    return edited if edited is not None else rule


@registry.op(
    "delete_automod_rule",
    "Delete an AutoMod rule. Requires admin. Destructive — removes the "
    "protection with no undo.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD,
                "Discord guild id the rule belongs to."),
        OpParam("rule_id", ParamKind.SNOWFLAKE,
                "AutoMod rule id to delete (from list_automod_rules)."),
    ],
    serialize=lambda payload: payload,
    agent_guidance=(
        "delete_automod_rule removes a guild protection with no undo — "
        "confirm which rule (by id from list_automod_rules) with the user "
        "first."),
    scope=OpScope.GUILD,
    group="automod",
)
async def delete_automod_rule(ctx: OpContext, guild, rule_id: int):
    rule = await guild.fetch_automod_rule(_as_int(rule_id, "rule_id"))
    info = {"deleted_rule_id": rule.id, "name": rule.name}
    await rule.delete(reason=_op_audit_reason(ctx, "delete_automod_rule", None))
    return info


# -- scheduled-event delete + stage lifecycle -------------------------------

@registry.op(
    "delete_scheduled_event",
    "Delete/cancel a scheduled event. Requires admin. Destructive — destroys "
    "the event and its accumulated RSVP/interest list irreversibly.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD,
                "Discord guild id the event belongs to. Optional in-guild; "
                "required over guild-less frontends like MCP.",
                required=False),
        OpParam("event_id", ParamKind.SNOWFLAKE,
                "Scheduled event id to delete (from list_scheduled_events)."),
    ],
    serialize=lambda payload: payload,
    agent_guidance=(
        "delete_scheduled_event destroys the event AND its RSVP list with no "
        "undo — confirm with the user before calling; to merely end an active "
        "event, edit_scheduled_event with status='completed' preserves it."),
    scope=OpScope.GUILD,
    group="events",
)
async def delete_scheduled_event(ctx: OpContext, event_id: int, guild=None):
    guild = guild or ctx.guild
    if guild is None:
        raise ValueError("delete_scheduled_event needs a guild context.")
    event = await guild.fetch_scheduled_event(
        _as_int(event_id, "event_id"))
    info = {"deleted_event_id": event.id, "name": event.name}
    await event.delete()
    return info


def _require_stage_channel(channel: Any):
    if not isinstance(channel, discord.StageChannel):
        raise ValueError(
            f"Channel {getattr(channel, 'id', '?')} "
            f"({type(channel).__name__}) is not a stage channel.")
    return channel


def _serialize_stage_instance(inst: Any) -> Dict[str, Any]:
    privacy = getattr(inst, "privacy_level", None)
    return {
        "channel_id": getattr(inst, "channel_id", None),
        "topic": getattr(inst, "topic", None),
        "privacy_level": getattr(privacy, "name",
                                 str(privacy) if privacy is not None
                                 else None),
    }


@registry.op(
    "create_stage",
    "Go live on a stage channel: create its stage instance with a topic. "
    "Requires admin. Can push a start notification to the whole guild.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("channel", ParamKind.CHANNEL, "Stage channel id to go live on."),
        OpParam("topic", ParamKind.STRING, "Stage topic (1-120 chars)."),
        OpParam("send_notification", ParamKind.BOOLEAN,
                "Notify the guild that the stage went live (default false).",
                required=False, default=False),
    ],
    serialize=_serialize_stage_instance,
    agent_guidance=(
        "create_stage goes live and can notify the whole guild — only call it "
        "when the user is actually running a stage event, and confirm before "
        "send_notification=true."),
    scope=OpScope.GUILD,
    group="voice",
)
async def create_stage(ctx: OpContext, channel, topic: str,
                       send_notification: bool = False):
    stage = _require_stage_channel(channel)
    instance = await stage.create_instance(
        topic=topic, send_start_notification=bool(send_notification),
        reason=_op_audit_reason(ctx, "create_stage", None))
    return instance


@registry.op(
    "edit_stage",
    "Edit the live stage instance's topic on a stage channel. Requires admin. "
    "Reversible.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("channel", ParamKind.CHANNEL, "Stage channel id to edit."),
        OpParam("topic", ParamKind.STRING, "New stage topic."),
    ],
    serialize=_serialize_stage_instance,
    scope=OpScope.GUILD,
    group="voice",
)
async def edit_stage(ctx: OpContext, channel, topic: str):
    stage = _require_stage_channel(channel)
    instance = stage.instance
    if instance is None:
        try:
            instance = await stage.fetch_instance()
        except discord.NotFound:
            instance = None
    if instance is None:
        raise ValueError(
            f"Stage channel {stage.id} has no live stage instance to edit.")
    await instance.edit(topic=topic,
                        reason=_op_audit_reason(ctx, "edit_stage", None))
    return instance


@registry.op(
    "end_stage",
    "End the live stage instance on a stage channel (closes it, disconnecting "
    "the audience). Requires admin. Destructive to the live session.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("channel", ParamKind.CHANNEL, "Stage channel id to close."),
    ],
    serialize=lambda payload: payload,
    agent_guidance=(
        "end_stage closes the live stage and disconnects everyone in the "
        "audience — confirm with the user before ending an active stage."),
    scope=OpScope.GUILD,
    group="voice",
)
async def end_stage(ctx: OpContext, channel):
    stage = _require_stage_channel(channel)
    instance = stage.instance
    if instance is None:
        try:
            instance = await stage.fetch_instance()
        except discord.NotFound:
            instance = None
    if instance is None:
        raise ValueError(
            f"Stage channel {stage.id} has no live stage instance to end.")
    await instance.delete(reason=_op_audit_reason(ctx, "end_stage", None))
    return {"channel_id": stage.id, "ended": True}


# -- invite delete ----------------------------------------------------------

@registry.op(
    "delete_invite",
    "Delete/revoke one of THIS guild's invites by code. Requires admin. "
    "Codes not found in the guild's own invite list are refused — a foreign "
    "code is never deleted blind. (Functionally the same as revoke_invite; "
    "the delete_* name completes the destructive-op family.)",
    PermissionLevel.ADMIN,
    params=[
        OpParam("guild", ParamKind.GUILD,
                "Discord guild id the invite belongs to."),
        OpParam("code", ParamKind.STRING,
                "Invite code to delete (from list_invites; a full "
                "discord.gg URL is also accepted)."),
    ],
    serialize=lambda payload: payload,
    scope=OpScope.GUILD,
    group="invites",
)
async def delete_invite(ctx: OpContext, guild, code: str):
    wanted = str(code).strip().rstrip("/").rsplit("/", 1)[-1]
    if not wanted:
        raise ValueError("delete_invite requires a non-empty invite code.")
    target = next((inv for inv in await guild.invites()
                   if inv.code == wanted), None)
    if target is None:
        raise ValueError(
            f"No active invite with code '{wanted}' in guild "
            f"'{guild.name}' — see list_invites.")
    await target.delete(
        reason=_op_audit_reason(ctx, "delete_invite", None))
    return {"deleted": True, "code": wanted}


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
    print(f"core.ops — {len(registry.names())} ops registered")
    for gid, label, ops in registry.grouped():
        print(f"\n{label} ({gid}) — {len(ops)} ops")
        for op_ in ops:
            schema = op_.to_json_schema()
            wire = ", ".join(schema["properties"]) or "-"
            print(f"  [{op_.permission.name:>10}] [{op_.scope.value:>6}] "
                  f"{op_.name:<24} wire: {wire}")


if __name__ == "__main__":
    _print_schemas()
