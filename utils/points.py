"""Per-user points store.

Cogs award and read points through these helpers rather than importing
each other. The balance lives on the user config key `points`.

Agent/MCP tools are declared HERE (not on the Points cog): the store
exists whether the `!points` UI cog is loaded. `bot.py` registers them
via `registry.register_module_ops`.
"""
from core.ops import (OpParam, OpScope, ParamKind, PermissionLevel, op)


def get_points(bot, user) -> int:
    """Return the user's point total, or 0 if unset."""
    return bot.config.get_user(user, "points", 0)


def add_points(bot, user, amount: int) -> int:
    """Add `amount` (negative to subtract). Clamps at 0. Returns the new total."""
    new_total = max(0, get_points(bot, user) + amount)
    bot.config.set_user(user, "points", new_total)
    return new_total


def set_points(bot, user, amount: int) -> int:
    """Set the user's points to `amount` (clamped at 0). Returns the new total."""
    new_total = max(0, amount)
    bot.config.set_user(user, "points", new_total)
    return new_total


def _payload(user, total: int) -> dict:
    return {"user_id": str(getattr(user, "id", user)), "points": total}


@op(
    "get_user_points",
    "Read a user's point balance. Does not change anything.",
    PermissionLevel.EVERYONE,
    params=[
        OpParam("user", ParamKind.MEMBER, "Member whose balance to read."),
    ],
    serialize=lambda p: p,
    scope=OpScope.GUILD,
    group="points",
    group_label="Points",
)
async def op_get_user_points(ctx, user) -> dict:
    return _payload(user, get_points(ctx.bot, user))


@op(
    "add_user_points",
    "Add points to a user (negative amount subtracts). Clamps at 0.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("user", ParamKind.MEMBER, "Member to credit or debit."),
        OpParam("amount", ParamKind.INTEGER, "Points to add; negative subtracts."),
    ],
    serialize=lambda p: p,
    scope=OpScope.GUILD,
    group="points",
    group_label="Points",
)
async def op_add_user_points(ctx, user, amount: int) -> dict:
    return _payload(user, add_points(ctx.bot, user, int(amount)))


@op(
    "set_user_points",
    "Set a user's point balance to an exact non-negative amount.",
    PermissionLevel.ADMIN,
    params=[
        OpParam("user", ParamKind.MEMBER, "Member whose balance to set."),
        OpParam("amount", ParamKind.INTEGER, "New balance (clamped at 0)."),
    ],
    serialize=lambda p: p,
    scope=OpScope.GUILD,
    group="points",
    group_label="Points",
)
async def op_set_user_points(ctx, user, amount: int) -> dict:
    return _payload(user, set_points(ctx.bot, user, int(amount)))
