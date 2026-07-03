"""Minimal STUB ops registry — stand-in for `core/ops.py` from `feat/ops-registry`.

DEPENDENCY NOTE: this spike (feat/mcp-ops-spike) was built against a snapshot of
this worktree where `feat/ops-registry`'s `core/ops.py` was present only as an
UNCOMMITTED file (never landed in that branch's git history) and was lost to a
concurrent branch switch/reset in this shared checkout before this spike could
branch cleanly off it. Per the task's documented fallback, this file is a
minimal stub covering just enough surface (OpContext, PermissionLevel, OpResult,
OpsRegistry, and three ops: send_message, search_history, add_reaction) to let
the MCP server demonstrate the "world pattern" seam. It intentionally mirrors
the real module's shape (same class/method names) so that swapping this import
for the real `core.ops` later is a one-line change in `mcp_ops/server.py`.

Do NOT build further product features against this stub — it exists only to
unblock the MCP spike. When `feat/ops-registry` lands `core/ops.py` for real,
delete this file and repoint the import.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable, Dict, List, Optional


class PermissionLevel(IntEnum):
    EVERYONE = 0
    ADMIN = 1
    SUPERADMIN = 2


@dataclass
class OpContext:
    """Minimal actor/target context an op needs to run.

    Real version (core.ops.OpContext) builds this from a discord.py Context;
    the MCP server instead builds it from the bot's live guild/user lookups
    driven by ids supplied over MCP (see mcp_ops/server.py:_build_context).
    """
    bot: Any
    author: Any
    guild: Optional[Any] = None
    channel: Optional[Any] = None


@dataclass
class OpResult:
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
        return {
            "name": self.name,
            "description": self.description,
            "permission": self.permission.name,
            "params": self.params,
        }


def _check_permission(ctx: OpContext, level: PermissionLevel) -> "tuple[bool, Optional[str]]":
    """Stub permission gate. The real registry calls into core.utils.is_admin /
    is_superadmin; this stub only implements EVERYONE fully and treats
    ADMIN/SUPERADMIN as "allow if ctx.author has an `is_admin`/`is_superadmin`
    truthy attribute", so the spike stays runnable without a live bot config.
    NOT a real permission model — do not reuse for anything beyond this spike.
    """
    if level == PermissionLevel.EVERYONE:
        return True, None
    if ctx is None or ctx.author is None:
        return False, "No actor on context; cannot check permissions."
    if level == PermissionLevel.SUPERADMIN:
        if getattr(ctx.author, "is_superadmin", False):
            return True, None
        return False, "Requires superadmin."
    if level == PermissionLevel.ADMIN:
        if getattr(ctx.author, "is_admin", False) or getattr(ctx.author, "is_superadmin", False):
            return True, None
        return False, "Requires admin."
    return False, f"Unknown permission level: {level!r}"


class OpsRegistry:
    """Registry of ops, shared by any frontend (in-bot agent loop, MCP server, ...)."""

    def __init__(self):
        self._ops: Dict[str, Op] = {}

    def op(self, name: str, description: str, permission: PermissionLevel,
           params: Optional[Dict[str, Any]] = None):
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            if name in self._ops:
                raise ValueError(f"Op '{name}' is already registered.")
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
# Stub registry instance + the three ops this spike exposes over MCP.
# Real impls wrap discord.py calls (channel.send, channel.history,
# message.add_reaction) exactly like the real core/ops.py does.
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
    "add_reaction",
    "Add an emoji reaction to a message.",
    PermissionLevel.EVERYONE,
    params={"message": "discord.Message", "emoji": "str"},
)
async def add_reaction(ctx: OpContext, message, emoji: str):
    await message.add_reaction(emoji)
    return True
