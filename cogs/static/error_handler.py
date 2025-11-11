"""
Enhanced admin commands for error logging configuration.
Supports both text commands and slash commands.
"""

import discord
from discord import app_commands
from discord.ext import commands
from core.error_handler import ErrorCategory, ErrorSeverity, get_error_statistics, clear_error_history
from core.utils import is_admin, is_superadmin
from typing import Optional, Literal


class ErrorLoggingAdmin(commands.Cog):
    """Admin commands for configuring the enhanced error logging system."""

    def __init__(self, bot):
        self.bot = bot

    def cog_check(self, ctx):
        """Check if user has permission to use these commands."""
        superadmins = self.bot.config.get_global("superadmins", [])
        is_super = ctx.author.id in superadmins
        is_guild_admin = (
            ctx.guild is not None and (
                ctx.author.guild_permissions.administrator or ctx.author == ctx.guild.owner
            )
        )
        return is_super or is_guild_admin

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Check if user has permission to use slash commands."""
        superadmins = self.bot.config.get_global("superadmins", [])
        is_super = interaction.user.id in superadmins
        is_guild_admin = (
            interaction.guild is not None and (
                interaction.user.guild_permissions.administrator or
                interaction.user == interaction.guild.owner
            )
        )
        return is_super or is_guild_admin

    def _get_config_scope(self, ctx_or_interaction):
        """Determine if we're working with guild or global config."""
        if hasattr(ctx_or_interaction, 'guild'):
            guild = ctx_or_interaction.guild
        else:
            guild = None
        return guild

    def _get_error_config(self, guild: Optional[discord.Guild]) -> dict:
        """Get the current error config for a guild or global."""
        if guild:
            config = self.bot.config.get(guild.id, "error_logging", {})
            if not config:
                # Check if there's a global config to fall back on
                global_config = self.bot.config.get_global("error_logging", {})
                return global_config or {}
            return config
        else:
            return self.bot.config.get_global("error_logging", {})

    def _set_error_config(self, guild: Optional[discord.Guild], config: dict):
        """Set error config for a guild or global."""
        if guild:
            self.bot.config.set(guild.id, "error_logging", config)
        else:
            self.bot.config.set_global("error_logging", config)

    # ==================== TEXT COMMANDS ====================

    @commands.group(name="errorlog", invoke_without_command=True)
    async def errorlog(self, ctx):
        """
        Error logging configuration commands.
        Use !errorlog help to see all subcommands.
        """
        await ctx.send_help(ctx.command)

    @errorlog.command(name="status")
    @commands.check(is_admin)
    async def errorlog_status(self, ctx):
        """Show current error logging configuration. Requires: Guild admin or superadmin"""
        guild = self._get_config_scope(ctx)
        config = self._get_error_config(guild)

        embed = discord.Embed(
            title="Error Logging Configuration",
            color=discord.Color.blue()
        )

        scope = f"Guild: {guild.name}" if guild else "Global"
        embed.add_field(name="Scope", value=scope, inline=False)

        if not config:
            embed.description = "No error logging configured."
            await ctx.send(embed=embed)
            return

        # Status
        enabled = config.get("enabled", True)
        embed.add_field(
            name="Status",
            value="Enabled" if enabled else "Disabled",
            inline=True
        )

        # Default channel
        default_channel_id = config.get("default_channel")
        if default_channel_id:
            channel = self.bot.get_channel(default_channel_id)
            channel_str = channel.mention if channel else f"ID: {default_channel_id} (not found)"
        else:
            channel_str = "Not set"
        embed.add_field(name="Default Channel", value=channel_str, inline=True)

        # Rate limit
        rate_limit = config.get("rate_limit_minutes", 5)
        embed.add_field(name="Rate Limit", value=f"{rate_limit} minutes", inline=True)

        # Category routing
        category_channels = config.get("category_channels", {})
        if category_channels:
            cat_text = []
            for cat, ch_id in category_channels.items():
                ch = self.bot.get_channel(ch_id)
                cat_text.append(f"`{cat}`: {ch.mention if ch else ch_id}")
            embed.add_field(
                name="Category Routing",
                value="\n".join(cat_text) or "None",
                inline=False
            )

        # Severity routing
        severity_channels = config.get("severity_channels", {})
        if severity_channels:
            sev_text = []
            for sev, ch_id in severity_channels.items():
                ch = self.bot.get_channel(ch_id)
                sev_text.append(f"`{sev}`: {ch.mention if ch else ch_id}")
            embed.add_field(
                name="Severity Routing",
                value="\n".join(sev_text) or "None",
                inline=False
            )

        await ctx.send(embed=embed)

    @errorlog.command(name="setchannel")
    @commands.check(is_admin)
    async def errorlog_setchannel(self, ctx, channel: discord.TextChannel = None):
        """
        Set the default error logging channel.
        Usage: !errorlog setchannel #channel
        Requires: Guild admin or superadmin
        """
        if not channel:
            await ctx.send("Please specify a channel. Usage: `!errorlog setchannel #channel`")
            return

        guild = self._get_config_scope(ctx)
        config = self._get_error_config(guild)

        config["default_channel"] = channel.id
        config["enabled"] = True
        self._set_error_config(guild, config)

        embed = discord.Embed(
            title="Error Logging Configured",
            description=f"Default error channel set to {channel.mention}",
            color=discord.Color.green()
        )

        scope = f"Guild: {guild.name}" if guild else "Global"
        embed.add_field(name="Scope", value=scope, inline=True)
        embed.add_field(name="Rate Limit", value=f"{config.get('rate_limit_minutes', 5)} minutes", inline=True)

        await ctx.send(embed=embed)

        # Send test message to the channel
        test_embed = discord.Embed(
            title="Error Logging Configured",
            description="This channel will receive error notifications.",
            color=discord.Color.blue()
        )
        test_embed.add_field(
            name="Features",
            value=(
                "• Category-based routing\n"
                "• Severity levels\n"
                "• Rate limiting\n"
                "• Per-guild configuration\n"
                "• Detailed tracebacks"
            ),
            inline=False
        )
        await channel.send(embed=test_embed)

    @errorlog.command(name="setcategory")
    @commands.check(is_admin)
    async def errorlog_setcategory(
        self,
        ctx,
        category: str,
        channel: discord.TextChannel
    ):
        """
        Route a specific error category to a channel.
        Categories: command_error, event_error, task_error, other
        Usage: !errorlog setcategory command_error #channel
        Requires: Guild admin or superadmin
        """
        # Validate category
        valid_categories = [c.value for c in ErrorCategory]
        if category not in valid_categories:
            await ctx.send(
                f"Invalid category. Valid categories: {', '.join(valid_categories)}"
            )
            return

        guild = self._get_config_scope(ctx)
        config = self._get_error_config(guild)

        if "category_channels" not in config:
            config["category_channels"] = {}

        config["category_channels"][category] = channel.id
        self._set_error_config(guild, config)

        await ctx.send(
            f"Category `{category}` will now be logged to {channel.mention}"
        )

    @errorlog.command(name="setseverity")
    @commands.check(is_admin)
    async def errorlog_setseverity(
        self,
        ctx,
        severity: str,
        channel: discord.TextChannel
    ):
        """
        Route a specific severity level to a channel.
        Severities: info, warning, error, critical
        Usage: !errorlog setseverity critical #channel
        Requires: Guild admin or superadmin
        """
        # Validate severity
        valid_severities = [s.severity_name for s in ErrorSeverity]
        if severity.lower() not in valid_severities:
            await ctx.send(
                f"Invalid severity. Valid severities: {', '.join(valid_severities)}"
            )
            return

        guild = self._get_config_scope(ctx)
        config = self._get_error_config(guild)

        if "severity_channels" not in config:
            config["severity_channels"] = {}

        config["severity_channels"][severity.lower()] = channel.id
        self._set_error_config(guild, config)

        await ctx.send(
            f"Severity `{severity}` will now be logged to {channel.mention}"
        )

    @errorlog.command(name="ratelimit")
    @commands.check(is_admin)
    async def errorlog_ratelimit(self, ctx, minutes: int):
        """
        Set the rate limit for duplicate errors in minutes.
        Usage: !errorlog ratelimit 10
        Requires: Guild admin or superadmin
        """
        if minutes < 1 or minutes > 60:
            await ctx.send("Rate limit must be between 1 and 60 minutes.")
            return

        guild = self._get_config_scope(ctx)
        config = self._get_error_config(guild)

        config["rate_limit_minutes"] = minutes
        self._set_error_config(guild, config)

        await ctx.send(f"Rate limit set to {minutes} minutes between duplicate errors.")

    @errorlog.command(name="enable")
    @commands.check(is_admin)
    async def errorlog_enable(self, ctx):
        """Enable error logging. Requires: Guild admin or superadmin"""
        guild = self._get_config_scope(ctx)
        config = self._get_error_config(guild)

        config["enabled"] = True
        self._set_error_config(guild, config)

        await ctx.send("Error logging enabled.")

    @errorlog.command(name="disable")
    @commands.check(is_admin)
    async def errorlog_disable(self, ctx):
        """Disable error logging. Requires: Guild admin or superadmin"""
        guild = self._get_config_scope(ctx)
        config = self._get_error_config(guild)

        config["enabled"] = False
        self._set_error_config(guild, config)

        await ctx.send("Error logging disabled.")

    @errorlog.command(name="stats")
    @commands.check(is_admin)
    async def errorlog_stats(self, ctx):
        """Show error logging statistics. Requires: Guild admin or superadmin"""
        stats = get_error_statistics()

        embed = discord.Embed(
            title="Error Logging Statistics",
            color=discord.Color.blue()
        )

        embed.add_field(
            name="Total Unique Errors",
            value=str(stats["total_unique_errors"]),
            inline=True
        )
        embed.add_field(
            name="Total Occurrences",
            value=str(stats["total_occurrences"]),
            inline=True
        )
        embed.add_field(
            name="Recent Errors (1h)",
            value=str(len(stats["recent_errors"])),
            inline=True
        )

        if stats["recent_errors"]:
            recent_text = []
            for err in stats["recent_errors"][:5]:  # Show top 5
                key_parts = err["key"].split(":", 3)
                if len(key_parts) >= 3:
                    category, context, error_type = key_parts[:3]
                    recent_text.append(f"`{error_type}` in `{context}` (×{err['count']})")

            if recent_text:
                embed.add_field(
                    name="Recent Error Types",
                    value="\n".join(recent_text),
                    inline=False
                )

        await ctx.send(embed=embed)

    @errorlog.command(name="clear")
    @commands.check(is_admin)
    async def errorlog_clear(self, ctx):
        """Clear error history (resets rate limiting). Requires: Guild admin or superadmin"""
        clear_error_history()
        await ctx.send("Error history cleared. All rate limits have been reset.")

    @errorlog.command(name="globalsync")
    @commands.check(is_superadmin)
    async def errorlog_globalsync(self, ctx, channel: discord.TextChannel = None):
        """
        Set global error channel for ALL guilds (superadmin only).
        This is the fallback channel when guilds don't have their own config.
        Usage: !errorlog globalsync #channel
        """
        if not channel:
            # Show current global channel
            config = self.bot.config.get_global("error_logging", {})
            channel_id = config.get("default_channel")
            if channel_id:
                ch = self.bot.get_channel(channel_id)
                await ctx.send(f"Current global error channel: {ch.mention if ch else f'ID: {channel_id}'}")
            else:
                await ctx.send("No global error channel configured.")
            return

        # Set global channel
        config = self.bot.config.get_global("error_logging", {})
        config["default_channel"] = channel.id
        config["enabled"] = True
        self.bot.config.set_global("error_logging", config)

        await ctx.send(f"Global error channel set to {channel.mention}. This will be used as fallback for all guilds without their own config.")

    # ==================== LEGACY SUPPORT ====================

    @commands.command(name="seterrorlog", hidden=True)
    async def seterrorlog_legacy(self, ctx, channel: discord.TextChannel = None):
        """
        Legacy command for setting error log channel.
        Redirects to new command structure.
        """
        if not channel:
            # Show current config
            await self.errorlog_status(ctx)
        else:
            # Set channel
            await self.errorlog_setchannel(ctx, channel)

    # ==================== TEST COMMANDS ====================

    @commands.command(name="testerror")
    async def testerror(self, ctx,
        severity: Optional[str] = "error",
        category: Optional[str] = "command_error"
    ):
        """
        Trigger a test error for testing error logging.
        Usage: !testerror [severity] [category]
        Severities: warning, error, critical
        Categories: command_error, event_error, task_error, other
        """
        # Validate severity (info doesn't make sense for errors)
        valid_severities = ['warning', 'error', 'critical']
        if severity.lower() not in valid_severities:
            await ctx.send(f"Invalid severity. Use: {', '.join(valid_severities)}")
            return
        await ctx.send(f"Triggering test {severity} error in category {category}...")

        # Store test info for the error handler
        ctx.test_severity = severity
        ctx.test_category = category

        # Trigger an error
        raise ValueError(f"Test error - Severity: {severity}, Category: {category}")


async def setup(bot):
    await bot.add_cog(ErrorLoggingAdmin(bot))
