"""Registered guild event reports; independent of optional UI/listener cogs.

Producers register in cog_load and unregister in cog_unload. Configuration is
read per delivery and retained when a producer is disabled. Error reporting has
its own mandatory routing in core.error_handler and never passes through here.
"""
from copy import deepcopy
from dataclasses import dataclass
import re

import discord

from core.utils import recursive_split

KEY = "event_logging"


@dataclass(frozen=True)
class EventCategory:
    id: str
    label: str
    feature: str
    feature_label: str


class EventRegistry:
    def __init__(self):
        self._categories = {}
        self._owners = {}

    def register(self, owner, feature, label, categories):
        """Register a complete feature atomically; IDs are stable config keys."""
        valid = lambda value: bool(re.fullmatch(r"[a-z][a-z0-9_]*", value))
        if not valid(feature) or not label or not categories:
            raise ValueError("Event features need a stable ID, label and categories.")
        if any(c.feature == feature for c in self._categories.values()):
            raise ValueError(f"Event feature already registered: {feature}")
        pending = []
        for name, category_label in categories.items():
            if not valid(name) or not category_label:
                raise ValueError("Event categories need stable IDs and labels.")
            pending.append(EventCategory(f"{feature}.{name}", category_label, feature, label))
        for category in pending:
            self._categories[category.id] = category
            self._owners[category.id] = owner

    def unregister(self, owner):
        for name in [name for name, value in self._owners.items() if value is owner]:
            del self._owners[name]
            del self._categories[name]

    def categories(self):
        return sorted(self._categories.values(), key=lambda c: (c.feature_label, c.label))

    def get(self, name):
        return self._categories.get(name)


registry = EventRegistry()


def settings(bot, guild):
    return deepcopy(bot.config.get(guild.id, KEY, {}))


def routes():
    """The admin surface is derived from the live registrations."""
    result = [("default", "All event reports")]
    seen = set()
    for category in registry.categories():
        if category.feature not in seen:
            result.append((f"feature:{category.feature}", category.feature_label))
            seen.add(category.feature)
        result.append((f"category:{category.id}", f"{category.feature_label}: {category.label}"))
    return result


def route_settings(config, route):
    if route == "default":
        return config
    kind, name = route.split(":", 1)
    return config.get("features" if kind == "feature" else "categories", {}).get(name, {})


def update_route(bot, guild, route, **changes):
    """Storage service. Its UI caller must authorize the user first."""
    if route not in dict(routes()):
        raise ValueError("That event category is no longer registered. Reopen log settings.")
    config = settings(bot, guild)
    if route == "default":
        target = config
    else:
        kind, name = route.split(":", 1)
        target = config.setdefault("features" if kind == "feature" else "categories", {}).setdefault(name, {})
    for key, value in changes.items():
        if value is None:
            target.pop(key, None)
        else:
            target[key] = value
    bot.config.set(guild.id, KEY, config)


async def report(bot, guild, category_id, text):
    """Best effort delivery; a missing/failed destination cannot stop actions."""
    category = registry.get(category_id)
    if category is None:
        bot.logger.warning("Unregistered event report %s in guild %s", category_id, guild.id)
        return False
    try:
        config = settings(bot, guild)
        feature = config.get("features", {}).get(category.feature, {})
        entry = config.get("categories", {}).get(category.id, {})
        if any(part.get("enabled", True) is False for part in (config, feature, entry)):
            return False
        channel_id = entry.get("channel_id") or feature.get("channel_id") or config.get("default_channel")
        channel = (guild.get_channel(channel_id) if channel_id else
                   discord.utils.get(guild.text_channels, name="log"))
        if channel is None:
            bot.logger.warning("No event log destination for %s in guild %s", category_id, guild.id)
            return False
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild.id:
            bot.logger.warning("Invalid event log destination for %s in guild %s", category_id, guild.id)
            return False
        permissions = channel.permissions_for(guild.me)
        if not (permissions.view_channel and permissions.send_messages):
            bot.logger.warning("Cannot send event report %s to channel %s", category_id, channel.id)
            return False
        for chunk in recursive_split(text):
            await channel.send(chunk, allowed_mentions=discord.AllowedMentions.none())
        return True
    except Exception:
        # Local logging only: never recurse into error/Discord delivery.
        bot.logger.exception("Event report delivery failed: %s in guild %s", category_id, guild.id)
        return False
