"""Persistent per-user signed points balances, independent of feature activation."""


def get_points(bot, user) -> int:
    """Return the user's point total, or 0 if unset."""
    return bot.config.get_user(user, "points", 0)


def add_points(bot, user, amount: int) -> int:
    """Atomically add `amount` (negative to subtract), allowing negative balances."""
    def adjust(document):
        document["points"] = document.get("points", 0) + amount
        return document["points"]
    return bot.config.update_user(user, adjust)


def set_points(bot, user, amount: int) -> int:
    """Atomically set the user's signed balance. Returns the new total."""
    def assign(document):
        document["points"] = amount
        return amount
    return bot.config.update_user(user, assign)
