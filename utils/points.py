"""Per-user points store.

Cogs award and read points through these helpers rather than importing
each other. The balance lives on the user config key `points`.
"""


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
