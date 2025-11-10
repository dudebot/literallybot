"""Shared helper utilities for permission checks and config access."""
from typing import List


def get_superadmins(config) -> List[int]:
    """Return the list of global superadmins."""
    superadmins = config.get(None, "superadmins", scope="global") or []

    if not isinstance(superadmins, list):
        superadmins = [int(superadmins)]
        config.set(None, "superadmins", superadmins, scope="global")

    return [int(item) for item in superadmins]


def is_superadmin(config, user_id: int) -> bool:
    """Check whether the provided user ID is a global superadmin."""
    return user_id in get_superadmins(config)


def is_admin(config, ctx) -> bool:
    """Determine if the invoking context has bot admin privileges."""
    if is_superadmin(config, ctx.author.id):
        return True

    if ctx.guild is None:
        return False

    admins = config.get(ctx, "admins", [])
    if ctx.author.id in (admins or []):
        return True

    if getattr(ctx.author.guild_permissions, "administrator", False):
        return True

    if ctx.author == getattr(ctx.guild, "owner", None):
        return True

    return False
