"""Central exception and command-activity reporting.

Guild and global destinations are independent; both receive unexpected errors
and command denials. Unknown commands are opt-in per destination. See
docs/error-handling.md for suppression, routing, and the settings panel.
"""
import asyncio
import logging
import re
import sys
import traceback
from collections.abc import Callable
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands


class ErrorCategory(Enum):
    """Error categories for better organization and routing."""
    COMMAND_ERROR = "command_error"
    COMMAND_NOT_FOUND = "command_not_found"
    COMMAND_DENIED = "command_denied"
    EVENT_ERROR = "event_error"
    TASK_ERROR = "task_error"
    OTHER = "other"


class ErrorSeverity(Enum):
    """Severity levels for error prioritization."""
    INFO = ("info", discord.Color.blue(), "ℹ️")
    WARNING = ("warning", discord.Color.gold(), "⚠️")
    ERROR = ("error", discord.Color.orange(), "❌")
    CRITICAL = ("critical", discord.Color.red(), "🚨")

    def __init__(self, name, color, emoji):
        self.severity_name = name
        self.color = color
        self.emoji = emoji


# Rate limiting storage: maps error_key to last_sent_time
# Auto-purges entries older than rate limit to prevent unbounded growth
_error_history: dict[str, datetime] = {}

# Whitelist hooks: callables that take (ctx, error) and return True to suppress error logging
_command_error_whitelist_hooks: list[Callable] = []


def register_error_whitelist_hook(hook: Callable):
    """
    Register a hook to whitelist certain CommandNotFound errors.
    Hook should take (ctx, error) and return True if error should be suppressed.
    """
    if hook not in _command_error_whitelist_hooks:
        _command_error_whitelist_hooks.append(hook)


def unregister_error_whitelist_hook(hook: Callable):
    """Remove a previously registered whitelist hook."""
    if hook in _command_error_whitelist_hooks:
        _command_error_whitelist_hooks.remove(hook)


def suppress_command_not_found(*patterns: str | re.Pattern):
    """Cog-class decorator: full-match prefix-stripped, trimmed message text.

    Patterns compile at definition time. Only loaded cogs participate; this
    never suppresses invocation/event errors, even on matching messages.
    Pass the SAME compiled pattern used by the listener to avoid drift.
    """
    compiled = tuple(re.compile(pattern) for pattern in patterns)

    def decorate(cog_class):
        cog_class.__command_not_found_patterns__ = (
            getattr(cog_class, "__command_not_found_patterns__", ()) + compiled
        )
        return cog_class
    return decorate


def _suppresses_command_not_found(bot, ctx, error) -> bool:
    if not isinstance(error, commands.CommandNotFound):
        return False
    prefix = getattr(ctx, "prefix", None)
    content = ctx.message.content
    if prefix and content.startswith(prefix) and not ctx.message.author.bot:
        text = content[len(prefix):].strip()
        for cog in getattr(bot, "cogs", {}).values():
            for pattern in getattr(cog, "__command_not_found_patterns__", ()):
                if pattern.fullmatch(text):
                    return True
    for hook in tuple(_command_error_whitelist_hooks):
        try:
            if hook(ctx, error):
                return True
        except Exception as hook_error:
            # A failed suppression hook is itself an unexpected exception.
            # Report it without passing through suppression again.
            bot.logger.exception("Command suppression hook failed")
            asyncio.create_task(log_error_to_discord(
                bot, hook_error, "command_suppression_hook",
                guild_id=ctx.guild.id if ctx.guild else None))
    return False


_default_rate_limit_minutes = 5


def _should_send_error(error_key: str, rate_limit_minutes: int = None) -> bool:
    """
    Check if error should be sent based on rate limiting.
    Auto-purges old entries from history.

    Returns:
        bool: True if error should be sent, False if still in cooldown
    """
    if rate_limit_minutes is None:
        rate_limit_minutes = _default_rate_limit_minutes

    now = datetime.now()
    cutoff = now - timedelta(minutes=rate_limit_minutes)

    keys_to_remove = [key for key, last_sent in _error_history.items() if last_sent < cutoff]
    for key in keys_to_remove:
        del _error_history[key]

    if error_key not in _error_history:
        _error_history[error_key] = now
        return True

    last_sent = _error_history[error_key]
    time_since_last = now - last_sent

    if time_since_last >= timedelta(minutes=rate_limit_minutes):
        _error_history[error_key] = now
        return True
    else:
        return False


def _create_error_key(error: Exception, context: str, category: ErrorCategory, guild_id: Optional[int] = None) -> str:
    """Create a unique key for error deduplication (per-guild)."""
    error_type = type(error).__name__
    error_msg = str(error)[:100]
    guild_part = f"{guild_id}" if guild_id else "dm"
    return f"{guild_part}:{category.value}:{context}:{error_type}:{error_msg}"


def _get_target_channel(bot, config: dict, category: ErrorCategory, severity: ErrorSeverity) -> Optional[int]:
    """
    Determine which channel should receive this error based on configuration.

    Priority:
    1. Category-specific channel
    2. Severity-specific channel
    3. Default channel

    Returns None if config doesn't exist or has no default_channel (disabled state).
    """
    if not config or not config.get("default_channel"):
        return None
    if (category == ErrorCategory.COMMAND_NOT_FOUND
            and not config.get("log_unknown_commands", False)):
        return None

    category_channels = config.get("category_channels", {})
    if category.value in category_channels:
        return category_channels[category.value]

    # Preserve existing command_error routes as the fallback for the new
    # activity categories; a specific denial/not-found route wins above.
    if category in (ErrorCategory.COMMAND_NOT_FOUND, ErrorCategory.COMMAND_DENIED):
        if ErrorCategory.COMMAND_ERROR.value in category_channels:
            return category_channels[ErrorCategory.COMMAND_ERROR.value]

    severity_channels = config.get("severity_channels", {})
    if severity.severity_name in severity_channels:
        return severity_channels[severity.severity_name]

    return config.get("default_channel")


def _create_error_embed(
    error: Exception,
    context: str,
    category: ErrorCategory,
    severity: ErrorSeverity,
    extra_info: str = "",
    guild_name: Optional[str] = None
) -> discord.Embed:
    """Create a rich embed for error logging."""

    embed = discord.Embed(
        title=f"{severity.emoji} " + {
            ErrorCategory.COMMAND_NOT_FOUND: "Command not found",
            ErrorCategory.COMMAND_DENIED: "Command denied",
        }.get(category, "Error detected" if severity in (
            ErrorSeverity.ERROR, ErrorSeverity.CRITICAL) else "Command warning"),
        color=severity.color,
        timestamp=datetime.now()
    )

    if guild_name:
        embed.add_field(name="Guild", value=f"`{guild_name}`", inline=True)

    embed.add_field(name="Severity", value=f"`{severity.severity_name.upper()}`", inline=True)
    embed.add_field(name="Category", value=f"`{category.value}`", inline=True)
    embed.add_field(name="Error Type", value=f"`{type(error).__name__}`", inline=True)
    embed.add_field(name="Context", value=f"`{context[:1000]}`", inline=True)

    # Add blank field for layout
    embed.add_field(name="\u200b", value="\u200b", inline=True)

    error_msg = str(error)
    if len(error_msg) > 1000:
        error_msg = error_msg[:997] + "..."
    embed.add_field(name="Error Message", value=f"```{error_msg}```", inline=False)

    if extra_info:
        if len(extra_info) > 1000:
            extra_info = extra_info[:997] + "..."
        embed.add_field(name="Additional Info", value=f"```{extra_info}```", inline=False)

    if severity in (ErrorSeverity.ERROR, ErrorSeverity.CRITICAL):
        tb = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        if len(tb) > 1000:
            tb = "..." + tb[-997:]
        embed.add_field(name="Traceback", value=f"```python\n{tb}\n```", inline=False)

    embed.set_footer(text=f"Category: {category.value} | Severity: {severity.severity_name}")

    return embed


async def log_error_to_discord(
    bot,
    error: Exception,
    context: str,
    category: ErrorCategory = ErrorCategory.OTHER,
    severity: ErrorSeverity = ErrorSeverity.ERROR,
    extra_info: str = "",
    guild_id: Optional[int] = None,
    actor_id: Optional[int] = None,
):
    """
    Log an error to Discord with enhanced categorization and routing.

    Sends to BOTH guild channel (if configured) AND global channel (if configured).
    This ensures superadmins always see all errors regardless of guild configs.

    Args:
        bot: The Discord bot instance
        error: The exception that occurred
        context: Context string (e.g., 'command_ping', 'event_message')
        category: Error category for routing
        severity: Severity level for prioritization
        extra_info: Additional contextual information
        guild_id: Guild ID for per-guild logging (optional)
        actor_id: Keep distinct users' command activity out of each other's cooldown
    """
    if not hasattr(bot, 'config'):
        return

    global_config = bot.config.get_global("error_logging", {})
    rate_limit = global_config.get("rate_limit_minutes", _default_rate_limit_minutes)
    guild = bot.get_guild(guild_id) if guild_id else None
    guild_name = guild.name if guild else None
    embed = _create_error_embed(error, context, category, severity, extra_info, guild_name)
    configs = [global_config]
    if guild_id:
        configs.insert(0, bot.config.get(guild_id, "error_logging", {}))
    sent_channels = set()
    for config in configs:
        channel_id = _get_target_channel(bot, config, category, severity)
        if not channel_id or channel_id in sent_channels:
            continue
        channel = bot.get_channel(channel_id)
        if channel is None:
            bot.logger.warning("Log channel %s is unavailable", channel_id)
            continue
        error_key = f"{channel_id}:" + _create_error_key(error, context, category, guild_id)
        if actor_id is not None and category in (
                ErrorCategory.COMMAND_NOT_FOUND, ErrorCategory.COMMAND_DENIED):
            error_key += f":actor:{actor_id}"
        if not _should_send_error(error_key, rate_limit):
            continue
        try:
            await channel.send(embed=embed)
            sent_channels.add(channel_id)
        except Exception:
            # Permit a later attempt after a transient delivery failure.
            _error_history.pop(error_key, None)
            bot.logger.exception("Failed to send log to channel %s", channel_id)


def _required_gate(ctx) -> Optional[str]:
    from core.utils import GATE_ADMIN, GATE_SUPERADMIN, gate_of

    node = getattr(ctx, "command", None)
    gates = set()
    while node is not None:
        gates.update(gate_of(check) for check in (getattr(node, "checks", None) or []))
        node = getattr(node, "parent", None)
    if GATE_SUPERADMIN in gates:
        return GATE_SUPERADMIN
    if GATE_ADMIN in gates:
        return GATE_ADMIN
    return None


def _user_level(ctx) -> str:
    from core.utils import is_admin, is_superadmin

    if is_superadmin(ctx):
        return "superadmin"
    if is_admin(ctx):
        return "admin"
    return "everyone"


def _denial_details(ctx, error) -> str:
    if isinstance(error, (commands.NoPrivateMessage, app_commands.NoPrivateMessage)):
        requirement = "server context"
    elif isinstance(error, (commands.MissingPermissions, app_commands.MissingPermissions)):
        requirement = "Discord permissions: " + ", ".join(error.missing_permissions)
    elif isinstance(error, commands.MissingRole):
        requirement = f"Discord role: {error.missing_role}"
    elif isinstance(error, commands.MissingAnyRole):
        requirement = "one of Discord roles: " + ", ".join(map(str, error.missing_roles))
    else:
        requirement = _required_gate(ctx) or "custom command check (no declared level)"
    return f"Required: {requirement}\nUser level: {_user_level(ctx)}"


def _denial_message(ctx, error) -> str:
    if isinstance(error, (commands.NoPrivateMessage, app_commands.NoPrivateMessage)):
        return "This command can only be used in a server.\n" + _denial_details(ctx, error)
    return "Command denied.\n" + _denial_details(ctx, error)


def app_command_denial_message(interaction, error: Exception) -> str:
    return _denial_message(interaction, error)


def prefix_denial_message(ctx, error: Exception) -> str:
    return _denial_message(ctx, error)


def _is_expected_app_denial(error: Exception) -> bool:
    """True for a slash refusal the user caused (gate / guild_only).

    BotMissingPermissions is the bot's own Discord permission gap.
    CommandOnCooldown subclasses CheckFailure in app_commands — it is
    not a permission refusal and must not be acked as one.
    """
    if isinstance(error, app_commands.NoPrivateMessage):
        return True
    if isinstance(error, (app_commands.BotMissingPermissions,
                          app_commands.CommandOnCooldown)):
        return False
    return isinstance(error, app_commands.CheckFailure)


def _is_expected_prefix_denial(error: Exception) -> bool:
    if isinstance(error, commands.NoPrivateMessage):
        return True
    if isinstance(error, commands.BotMissingPermissions):
        return False
    return isinstance(error, commands.CheckFailure)


async def _ack_app_error(interaction, message: str) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except (discord.InteractionResponded, discord.HTTPException):
        pass


def _determine_severity(error: Exception) -> ErrorSeverity:
    if isinstance(error, commands.CommandNotFound):
        return ErrorSeverity.INFO
    # A stale slash registration and a bot permission gap need operator action.
    if isinstance(error, (commands.BotMissingPermissions, app_commands.BotMissingPermissions,
                          commands.CommandInvokeError, app_commands.CommandInvokeError,
                          app_commands.CommandNotFound)):
        return ErrorSeverity.ERROR
    if isinstance(error, (commands.CheckFailure, commands.CommandOnCooldown,
                          commands.UserInputError, app_commands.CheckFailure)):
        return ErrorSeverity.WARNING
    return ErrorSeverity.ERROR


def _command_info(ctx, *, slash: bool, denial: bool, error) -> tuple[str, str]:
    cmd = getattr(ctx, "command", None)
    name = (getattr(cmd, "qualified_name", None) or getattr(cmd, "name", None)
            or getattr(ctx, "invoked_with", None) or "unknown")
    actor = ctx.user if slash else ctx.author
    guild = ctx.guild
    info = (f"User: {actor} (ID: {actor.id})\n"
            f"Command: {'/' if slash else getattr(ctx, 'prefix', '!')}{name}\n"
            f"Channel: {ctx.channel} (ID: {getattr(ctx.channel, 'id', 'unknown')})\n"
            f"{'Guild: ' + guild.name + f' (ID: {guild.id})' if guild else 'DM'}")
    # Keep a source link for investigation without copying arbitrary message bodies.
    link = getattr(getattr(ctx, "message", None), "jump_url", None)
    if link:
        info += f"\nMessage: {link}"
    if denial:
        info = _denial_details(ctx, error) + "\n" + info
    return name, info


async def _report_command_error(bot, ctx, error, *, slash: bool, denial: bool):
    severity = _determine_severity(error)
    category = (ErrorCategory.COMMAND_DENIED if denial else
                ErrorCategory.COMMAND_NOT_FOUND if isinstance(error, commands.CommandNotFound)
                else ErrorCategory.COMMAND_ERROR)
    actual_error = (error.original if isinstance(error, (commands.CommandInvokeError,
                    app_commands.CommandInvokeError)) else error)
    name, info = _command_info(ctx, slash=slash, denial=denial, error=error)
    level = getattr(logging, severity.severity_name.upper())
    bot.logger.log(level, "%s: %s\n%s", category.value, actual_error, info,
                   exc_info=(type(actual_error), actual_error, actual_error.__traceback__)
                   if severity in (ErrorSeverity.ERROR, ErrorSeverity.CRITICAL) else None)
    await log_error_to_discord(
        bot, actual_error, f"{'slash_command' if slash else 'command'}_{name}",
        category=category, severity=severity, extra_info=info,
        guild_id=ctx.guild.id if ctx.guild else None,
        actor_id=(ctx.user if slash else ctx.author).id)


async def handle_command_error(bot, ctx, error: Exception):
    if _suppresses_command_not_found(bot, ctx, error):
        return
    denial = _is_expected_prefix_denial(error)
    if denial:
        try:
            await ctx.send(prefix_denial_message(ctx, error))
        except discord.HTTPException:
            bot.logger.exception("Failed to send command denial")
    await _report_command_error(bot, ctx, error, slash=False, denial=denial)


async def handle_app_command_error(bot, interaction, error: Exception):
    denial = _is_expected_app_denial(error)
    if denial:
        message = app_command_denial_message(interaction, error)
    elif isinstance(error, app_commands.CommandOnCooldown):
        message = f"Please try again in {error.retry_after:.0f} seconds."
    elif isinstance(error, app_commands.CommandNotFound):
        message = "Command not found. The bot's slash commands need to be synced."
    else:
        message = "❌ Something went wrong running that command. The error has been logged."
    await _ack_app_error(interaction, message)
    await _report_command_error(bot, interaction, error, slash=True, denial=denial)


async def handle_event_error(bot, event: str, *args, **kwargs):
    """
    Handle errors from events with enhanced logging.

    Args:
        bot: The Discord bot instance
        event: Event name
        *args: Event arguments
        **kwargs: Event keyword arguments
    """
    bot.logger.exception(f'Unhandled exception in event {event}', exc_info=True)

    try:
        err = sys.exc_info()[1]
        if err:
            extra_info = f"Event: {event}\nArgs: {str(args)[:500]}"

            guild_id = None
            for arg in args:
                if hasattr(arg, 'guild') and arg.guild:
                    guild_id = arg.guild.id
                    extra_info += f"\nGuild: {arg.guild.name} (ID: {guild_id})"
                    break
                elif isinstance(arg, discord.Guild):
                    guild_id = arg.id
                    extra_info += f"\nGuild: {arg.name} (ID: {guild_id})"
                    break

            await log_error_to_discord(
                bot, err, f'event_{event}',
                category=ErrorCategory.EVENT_ERROR,
                severity=ErrorSeverity.ERROR,
                extra_info=extra_info,
                guild_id=guild_id
            )
    except Exception as log_error:
        bot.logger.error(f"Failed to log error to Discord: {log_error}", exc_info=True)
