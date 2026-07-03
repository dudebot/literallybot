"""Decorator-based tool/ops registry wrapping atomic Discord actions.

The "world pattern": one ops layer, many frontends. Every atomic Discord
action (send a message, add a role, ...) is registered here exactly once,
with its permission requirement declared alongside it. Any frontend — an
in-bot agent loop, a slash-command cog, an MCP server — calls into the same
`registry.call(op_name, ctx, **kwargs)` and gets the same permission
enforcement and the same error shape back. No frontend re-implements
Discord call plumbing or permission checks.

This module is frontend-agnostic on purpose: it does not import
`discord.ext.commands`, does not know about cogs, and does not get wired
into `bot.py`. It only knows how to run an op against an `OpContext`.

Permission gates route through `core.utils.is_admin` / `is_superadmin`,
the same helpers `cogs/dynamic/cleanup.py` and `cogs/dynamic/setrole.py`
already use via `@commands.check(...)`. Those helpers expect a duck-typed
ctx with `.author`, `.guild`, and `.bot.config` — `OpContext` below mirrors
that shape so the existing helpers work unmodified.

NOT wired into any cog yet. This module exists to be imported by a future
in-bot agent loop and/or an MCP server (see the retired `feat/mcp-ops-spike`
branch for a proof-of-concept frontend) — see this file's bottom-of-file
smoke test for a no-bot-required sanity check.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable, Dict, List, Optional

from core.utils import is_admin, is_superadmin


class PermissionLevel(IntEnum):
    """Ordered so a numeric comparison also makes sense, but permission
    checks below dispatch by exact level rather than relying on ordering."""
    EVERYONE = 0
    ADMIN = 1
    SUPERADMIN = 2


@dataclass
class OpContext:
    """Minimal actor/target context an op needs to run.

    Duck-types the subset of `discord.ext.commands.Context` that
    `core.utils.is_admin` / `is_superadmin` and the op implementations
    below actually touch: `.bot` (with `.config`), `.author`, `.guild`,
    and `.channel`. A real `commands.Context` satisfies this directly —
    pass it straight through. A non-cog frontend (agent loop, MCP server)
    builds one of these from whatever ids/objects it has on hand.
    """
    bot: Any
    author: Any
    guild: Optional[Any] = None
    channel: Optional[Any] = None


@dataclass
class OpResult:
    """Uniform result shape every op returns — frontends branch on `.ok`
    rather than catching exceptions, so a failed permission check and a
    failed Discord API call look the same to a caller."""
    ok: bool
    value: Any = None
    error: Optional[str] = None


@dataclass
class Op:
    name: str
    description: str
    permission: PermissionLevel
    impl: Callable[..., Any]
    params: Dict[str, Any] = field(default_factory=dict)

    async def __call__(self, ctx: OpContext, **kwargs) -> OpResult:
        allowed, reason = _check_permission(ctx, self.permission)
        if not allowed:
            return OpResult(ok=False, error=reason)
        try:
            value = await self.impl(ctx, **kwargs)
        except Exception as exc:  # noqa: BLE001 - ops surface failure, not raise
            return OpResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        return OpResult(ok=True, value=value)

    def to_schema(self) -> Dict[str, Any]:
        """A frontend-agnostic description of this op — enough for an MCP
        tool listing or an agent-loop tool spec without importing discord.py."""
        return {
            "name": self.name,
            "description": self.description,
            "permission": self.permission.name,
            "params": self.params,
        }


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
           params: Optional[Dict[str, Any]] = None):
        """Decorator: `@registry.op("name", "...", PermissionLevel.ADMIN)`
        registers an `async def impl(ctx, **kwargs)` under `name`."""
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            if name in self._ops:
                raise ValueError(f"Op '{name}' is already registered.")
            if not inspect.iscoroutinefunction(func):
                raise TypeError(f"Op '{name}' implementation must be an async function.")
            self._ops[name] = Op(
                name=name, description=description, permission=permission,
                impl=func, params=params or {},
            )
            return func
        return decorator

    def get(self, name: str) -> Optional[Op]:
        return self._ops.get(name)

    def list_tools(self) -> List[Dict[str, Any]]:
        return [op.to_schema() for op in self._ops.values()]

    def names(self) -> List[str]:
        return list(self._ops.keys())

    async def call(self, op_name: str, ctx: OpContext, **kwargs) -> OpResult:
        op = self._ops.get(op_name)
        if op is None:
            return OpResult(ok=False, error=f"Unknown op: {op_name}")
        return await op(ctx, **kwargs)


# ---------------------------------------------------------------------------
# The shared registry instance. Frontends import this, not the class.
# ---------------------------------------------------------------------------

registry = OpsRegistry()


@registry.op(
    "send_message",
    "Send a text message to a channel.",
    PermissionLevel.EVERYONE,
    params={"channel": "discord.abc.Messageable", "content": "str"},
)
async def send_message(ctx: OpContext, channel, content: str):
    return await channel.send(content)


@registry.op(
    "edit_message",
    "Edit the content of a message the bot previously sent.",
    PermissionLevel.EVERYONE,
    params={"message": "discord.Message", "content": "str"},
)
async def edit_message(ctx: OpContext, message, content: str):
    return await message.edit(content=content)


@registry.op(
    "delete_message",
    "Delete a message. Requires admin — mirrors cogs/dynamic/cleanup.py's "
    "bulk-delete gate, which restricts message deletion to superadmin/admin.",
    PermissionLevel.ADMIN,
    params={"message": "discord.Message"},
)
async def delete_message(ctx: OpContext, message):
    await message.delete()
    return True


@registry.op(
    "add_reaction",
    "Add an emoji reaction to a message.",
    PermissionLevel.EVERYONE,
    params={"message": "discord.Message", "emoji": "str"},
)
async def add_reaction(ctx: OpContext, message, emoji: str):
    await message.add_reaction(emoji)
    return True


@registry.op(
    "search_history",
    "Search a channel's message history, optionally filtered by author id "
    "and/or a substring match on content.",
    PermissionLevel.EVERYONE,
    params={
        "channel": "discord.abc.Messageable",
        "limit": "int = 100",
        "author_id": "Optional[int] = None",
        "contains": "Optional[str] = None",
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
    params={"member": "discord.Member", "role": "discord.Role"},
)
async def add_role(ctx: OpContext, member, role):
    await member.add_roles(role)
    return True


@registry.op(
    "remove_role",
    "Remove a role from a member. Requires admin, mirroring add_role.",
    PermissionLevel.ADMIN,
    params={"member": "discord.Member", "role": "discord.Role"},
)
async def remove_role(ctx: OpContext, member, role):
    await member.remove_roles(role)
    return True


@registry.op(
    "pin_message",
    "Pin a message in its channel. Requires admin (pins are surfaced to "
    "the whole channel and Discord itself caps pins at 50 per channel).",
    PermissionLevel.ADMIN,
    params={"message": "discord.Message"},
)
async def pin_message(ctx: OpContext, message):
    await message.pin()
    return True


@registry.op(
    "create_thread",
    "Create a thread, either attached to an existing message or standalone "
    "on a channel.",
    PermissionLevel.EVERYONE,
    params={
        "channel": "discord.TextChannel",
        "name": "str",
        "message": "Optional[discord.Message] = None",
    },
)
async def create_thread(ctx: OpContext, channel, name: str, message=None):
    if message is not None:
        return await message.create_thread(name=name)
    return await channel.create_thread(name=name)


# ---------------------------------------------------------------------------
# In-file smoke test — instantiates the module-level registry and lists
# tools WITHOUT a live bot/Discord connection. Run directly:
#     python3 -m core.ops
# ---------------------------------------------------------------------------

def _smoke_test() -> None:
    expected = {
        "send_message", "edit_message", "delete_message", "add_reaction",
        "search_history", "add_role", "remove_role", "pin_message",
        "create_thread",
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

    # A no-actor context must fail closed on anything above EVERYONE, and
    # must never raise — permission failures surface as OpResult.error.
    empty_ctx = OpContext(bot=None, author=None, guild=None, channel=None)
    import asyncio

    result = asyncio.run(registry.call("delete_message", empty_ctx, message=None))
    assert result.ok is False
    assert "actor" in (result.error or "").lower()

    unknown = asyncio.run(registry.call("not_a_real_op", empty_ctx))
    assert unknown.ok is False
    assert "Unknown op" in (unknown.error or "")

    print(f"core.ops smoke test OK — {len(names)} ops registered:")
    for tool in tools:
        print(f"  [{tool['permission']:>10}] {tool['name']:<16} {tool['description']}")


if __name__ == "__main__":
    _smoke_test()
