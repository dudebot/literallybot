"""
Enhanced error logging system with categories, severity levels, and per-guild channels.
Supports both global and per-guild error logging with flexible configuration.
"""

import discord
import traceback
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple
from enum import Enum


class ErrorCategory(Enum):
    """Error categories for better organization and routing."""
    COMMAND_ERROR = "command_error"
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


# Rate limiting storage: maps error_key to (last_sent_time, occurrence_count)
_error_history: Dict[str, Tuple[datetime, int]] = {}
_default_rate_limit_minutes = 5


def _get_error_config(bot, guild_id: Optional[int] = None) -> dict:
    """Get error configuration for a guild or global."""
    if guild_id:
        # Try guild-specific config first
        guild_config = bot.config.get(guild_id, "error_logging", scope="guild")
        if guild_config:
            return guild_config

    # Fallback to global config
    global_config = bot.config.get_global("error_logging", {})

    # Legacy support: if old error_channel_id exists, migrate it
    if not global_config and bot.config.get_global("error_channel_id"):
        global_config = {
            "default_channel": bot.config.get_global("error_channel_id"),
            "enabled": True
        }

    return global_config


def _should_send_error(error_key: str, rate_limit_minutes: int = None) -> Tuple[bool, int]:
    """
    Check if error should be sent based on rate limiting.

    Returns:
        Tuple of (should_send, occurrence_count)
    """
    if rate_limit_minutes is None:
        rate_limit_minutes = _default_rate_limit_minutes

    now = datetime.now()

    if error_key not in _error_history:
        _error_history[error_key] = (now, 1)
        return True, 1

    last_sent, count = _error_history[error_key]
    time_since_last = now - last_sent

    if time_since_last >= timedelta(minutes=rate_limit_minutes):
        # Time to send again
        _error_history[error_key] = (now, count + 1)
        return True, count + 1
    else:
        # Still in cooldown, increment count
        _error_history[error_key] = (last_sent, count + 1)
        return False, count + 1


def _create_error_key(error: Exception, context: str, category: ErrorCategory) -> str:
    """Create a unique key for error deduplication."""
    error_type = type(error).__name__
    error_msg = str(error)[:100]
    return f"{category.value}:{context}:{error_type}:{error_msg}"


def _get_target_channel(bot, config: dict, category: ErrorCategory, severity: ErrorSeverity) -> Optional[int]:
    """
    Determine which channel should receive this error based on configuration.

    Priority:
    1. Category-specific channel
    2. Severity-specific channel
    3. Default channel
    """
    if not config or not config.get("enabled", True):
        return None

    # Check for category-specific routing
    category_channels = config.get("category_channels", {})
    if category.value in category_channels:
        return category_channels[category.value]

    # Check for severity-specific routing
    severity_channels = config.get("severity_channels", {})
    if severity.severity_name in severity_channels:
        return severity_channels[severity.severity_name]

    # Fallback to default channel
    return config.get("default_channel")


def _create_error_embed(
    error: Exception,
    context: str,
    category: ErrorCategory,
    severity: ErrorSeverity,
    extra_info: str = "",
    occurrence_count: int = 1,
    guild_name: Optional[str] = None
) -> discord.Embed:
    """Create a rich embed for error logging."""

    # Build title
    title = f"{severity.emoji} Error Detected"
    if occurrence_count > 1:
        title += f" (×{occurrence_count})"

    embed = discord.Embed(
        title=title,
        color=severity.color,
        timestamp=datetime.now()
    )

    # Add guild context if available
    if guild_name:
        embed.add_field(name="Guild", value=f"`{guild_name}`", inline=True)

    # Core error information
    embed.add_field(name="Severity", value=f"`{severity.severity_name.upper()}`", inline=True)
    embed.add_field(name="Category", value=f"`{category.value}`", inline=True)
    embed.add_field(name="Error Type", value=f"`{type(error).__name__}`", inline=True)
    embed.add_field(name="Context", value=f"`{context}`", inline=True)

    # Add blank field for layout
    embed.add_field(name="\u200b", value="\u200b", inline=True)

    # Error message
    error_msg = str(error)
    if len(error_msg) > 1000:
        error_msg = error_msg[:997] + "..."
    embed.add_field(name="Error Message", value=f"```{error_msg}```", inline=False)

    # Additional info
    if extra_info:
        if len(extra_info) > 1000:
            extra_info = extra_info[:997] + "..."
        embed.add_field(name="Additional Info", value=f"```{extra_info}```", inline=False)

    # Traceback
    tb = traceback.format_exc()
    if len(tb) > 1000:
        tb = "..." + tb[-997:]
    embed.add_field(name="Traceback", value=f"```python\n{tb}\n```", inline=False)

    # Footer with metadata
    footer_text = f"Category: {category.value} | Severity: {severity.severity_name}"
    if occurrence_count > 1:
        footer_text += f" | Occurrences: {occurrence_count}"
    embed.set_footer(text=footer_text)

    return embed


async def log_error_to_discord(
    bot,
    error: Exception,
    context: str,
    category: ErrorCategory = ErrorCategory.OTHER,
    severity: ErrorSeverity = ErrorSeverity.ERROR,
    extra_info: str = "",
    guild_id: Optional[int] = None
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
    """
    if not hasattr(bot, 'config'):
        return

    # Create error key for deduplication
    error_key = _create_error_key(error, context, category)

    # Get rate limit from global config (use global for consistency)
    global_config = bot.config.get_global("error_logging", {})
    rate_limit = global_config.get("rate_limit_minutes", _default_rate_limit_minutes)

    # Check if we should send (using global rate limit)
    should_send, occurrence_count = _should_send_error(error_key, rate_limit)
    if not should_send:
        return

    # Get guild name if available
    guild_name = None
    if guild_id:
        guild = bot.get_guild(guild_id)
        if guild:
            guild_name = guild.name

    # Create embed once for reuse
    embed = _create_error_embed(
        error=error,
        context=context,
        category=category,
        severity=severity,
        extra_info=extra_info,
        occurrence_count=occurrence_count,
        guild_name=guild_name
    )

    # Track where we sent to avoid duplicates
    sent_channels = set()

    # 1. Send to guild channel if configured
    if guild_id:
        guild_config = bot.config.get(guild_id, "error_logging", {})
        if guild_config and guild_config.get("enabled", True):
            guild_channel_id = _get_target_channel(bot, guild_config, category, severity)
            if guild_channel_id and guild_channel_id not in sent_channels:
                guild_channel = bot.get_channel(guild_channel_id)
                if guild_channel:
                    try:
                        await guild_channel.send(embed=embed)
                        sent_channels.add(guild_channel_id)
                    except Exception as send_error:
                        print(f"Failed to send error to guild channel: {send_error}")

    # 2. ALWAYS send to global channel if configured (superadmin visibility)
    if global_config and global_config.get("enabled", True):
        global_channel_id = _get_target_channel(bot, global_config, category, severity)
        if global_channel_id and global_channel_id not in sent_channels:
            global_channel = bot.get_channel(global_channel_id)
            if global_channel:
                try:
                    # Add indicator that this is from another guild if applicable
                    if guild_id and global_channel_id not in sent_channels:
                        global_embed = embed.copy()
                        if guild_name:
                            # Update footer to indicate source
                            current_footer = global_embed.footer.text if global_embed.footer else ""
                            global_embed.set_footer(
                                text=f"From: {guild_name} | {current_footer}" if current_footer else f"From: {guild_name}"
                            )
                    else:
                        global_embed = embed

                    await global_channel.send(embed=global_embed)
                    sent_channels.add(global_channel_id)
                except Exception as send_error:
                    print(f"Failed to send error to global channel: {send_error}")


def get_error_statistics() -> Dict[str, any]:
    """Get statistics about logged errors for monitoring."""
    now = datetime.now()

    stats = {
        "total_unique_errors": len(_error_history),
        "total_occurrences": sum(count for _, count in _error_history.values()),
        "recent_errors": []
    }

    # Get errors from last hour
    for error_key, (last_time, count) in _error_history.items():
        if now - last_time <= timedelta(hours=1):
            stats["recent_errors"].append({
                "key": error_key,
                "last_occurrence": last_time,
                "count": count
            })

    return stats


def clear_error_history():
    """Clear error history (useful for testing or manual resets)."""
    _error_history.clear()


def _determine_severity(error: Exception, test_severity: Optional[str] = None) -> ErrorSeverity:
    """
    Determine the severity level for an error.

    Args:
        error: The exception that occurred
        test_severity: Optional test severity override from !testerror command

    Returns:
        ErrorSeverity enum value
    """
    # Check for test severity override
    if test_severity:
        severity_map = {
            'warning': ErrorSeverity.WARNING,
            'error': ErrorSeverity.ERROR,
            'critical': ErrorSeverity.CRITICAL
        }
        return severity_map.get(test_severity.lower(), ErrorSeverity.ERROR)

    # Import here to avoid circular dependency
    from discord.ext import commands
    from discord import app_commands

    # Determine severity based on error type
    if isinstance(error, (commands.CommandNotFound, commands.MissingPermissions, commands.CommandOnCooldown)):
        return ErrorSeverity.WARNING
    elif isinstance(error, app_commands.CommandOnCooldown):
        return ErrorSeverity.WARNING
    else:
        return ErrorSeverity.ERROR


def _determine_category(test_category: Optional[str] = None, default: ErrorCategory = ErrorCategory.COMMAND_ERROR) -> ErrorCategory:
    """
    Determine the error category.

    Args:
        test_category: Optional test category override from !testerror command
        default: Default category if no override

    Returns:
        ErrorCategory enum value
    """
    if test_category:
        category_map = {
            'command_error': ErrorCategory.COMMAND_ERROR,
            'event_error': ErrorCategory.EVENT_ERROR,
            'task_error': ErrorCategory.TASK_ERROR,
            'other': ErrorCategory.OTHER
        }
        return category_map.get(test_category.lower(), default)

    return default


async def handle_command_error(bot, ctx, error: Exception):
    """
    Handle errors from text commands with enhanced logging.

    Args:
        bot: The Discord bot instance
        ctx: Command context
        error: The exception that occurred
    """
    # Import here to avoid issues
    from discord.ext import commands
    import asyncio

    # Special handling for CommandNotFound with media handler
    if isinstance(error, commands.CommandNotFound):
        handled_ids = getattr(bot, '_lb_media_handled_ids', None)
        if handled_ids is not None and ctx.message.id in handled_ids:
            handled_ids.discard(ctx.message.id)
            return

    bot.logger.error(f'Error in command {ctx.command}: {error}', exc_info=True)

    try:
        command_name = ctx.command.name if ctx.command else 'unknown'

        # Check if this is a test error with custom settings
        test_severity = getattr(ctx, 'test_severity', None)
        test_category = getattr(ctx, 'test_category', None)

        # Determine severity and category
        severity = _determine_severity(error, test_severity)
        category = _determine_category(test_category, ErrorCategory.COMMAND_ERROR)

        # Build extra info
        guild_info = f"Guild: {ctx.guild.name} (ID: {ctx.guild.id})" if ctx.guild else "DM"
        extra_info = (
            f"User: {ctx.author} (ID: {ctx.author.id})\n"
            f"Channel: {ctx.channel}\n"
            f"{guild_info}"
        )

        # Get guild ID for per-guild logging
        guild_id = ctx.guild.id if ctx.guild else None

        # Unwrap CommandInvokeError to get the actual error
        actual_error = error
        if isinstance(error, commands.CommandInvokeError):
            actual_error = error.original

        asyncio.create_task(log_error_to_discord(
            bot, actual_error, f'command_{command_name}',
            category=category,
            severity=severity,
            extra_info=extra_info,
            guild_id=guild_id
        ))
    except Exception as log_error:
        bot.logger.error(f"Failed to log error to Discord: {log_error}", exc_info=True)


async def handle_app_command_error(bot, interaction, error: Exception):
    """
    Handle errors from slash commands with enhanced logging.

    Args:
        bot: The Discord bot instance
        interaction: Discord interaction object
        error: The exception that occurred
    """
    import asyncio

    bot.logger.exception(f'Unhandled exception in slash command', exc_info=True)

    try:
        # Check if this is a test error with custom settings
        test_severity = getattr(interaction, 'test_severity', None)
        test_category = getattr(interaction, 'test_category', None)

        # Determine severity and category
        severity = _determine_severity(error, test_severity)
        category = _determine_category(test_category, ErrorCategory.COMMAND_ERROR)

        # Build extra info
        guild_info = f"Guild: {interaction.guild.name} (ID: {interaction.guild.id})" if interaction.guild else "DM"
        extra_info = (
            f"User: {interaction.user} (ID: {interaction.user.id})\n"
            f"Command: /{interaction.command.name if interaction.command else 'unknown'}\n"
            f"Channel: {interaction.channel}\n"
            f"{guild_info}"
        )

        # Get guild ID for per-guild logging
        guild_id = interaction.guild.id if interaction.guild else None

        asyncio.create_task(log_error_to_discord(
            bot, error, f'slash_command_{interaction.command.name if interaction.command else "unknown"}',
            category=category,
            severity=severity,
            extra_info=extra_info,
            guild_id=guild_id
        ))
    except Exception as log_error:
        bot.logger.error(f"Failed to log slash command error to Discord: {log_error}", exc_info=True)


async def handle_event_error(bot, event: str, *args, **kwargs):
    """
    Handle errors from events with enhanced logging.

    Args:
        bot: The Discord bot instance
        event: Event name
        *args: Event arguments
        **kwargs: Event keyword arguments
    """
    import asyncio
    import sys
    import discord

    bot.logger.exception(f'Unhandled exception in event {event}', exc_info=True)

    try:
        err = sys.exc_info()[1]
        if err:
            # Build extra info
            extra_info = f"Event: {event}\nArgs: {str(args)[:500]}"

            # Try to extract guild context from args if available
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

            asyncio.create_task(log_error_to_discord(
                bot, err, f'event_{event}',
                category=ErrorCategory.EVENT_ERROR,
                severity=ErrorSeverity.ERROR,
                extra_info=extra_info,
                guild_id=guild_id
            ))
    except Exception as log_error:
        bot.logger.error(f"Failed to log error to Discord: {log_error}", exc_info=True)
