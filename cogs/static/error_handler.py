"""
Enhanced admin commands for error logging configuration.
Supports both text commands and slash commands.
"""

import discord
from discord import app_commands
from discord.ext import commands
from core.error_handler import ErrorCategory, ErrorSeverity
from core.utils import is_admin, is_superadmin
from typing import Optional


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

    def _get_guild_error_config(self, guild_id: int) -> dict:
        """Get error config for a specific guild only (no global fallback)."""
        return self.bot.config.get(guild_id, "error_logging", {})

    def _set_guild_error_config(self, guild_id: int, config: dict):
        """Set error config for a specific guild."""
        self.bot.config.set(guild_id, "error_logging", config)

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
        if not ctx.guild:
            await ctx.send("This command must be run in a guild.")
            return

        guild_config = self._get_guild_error_config(ctx.guild.id)
        global_config = self.bot.config.get_global("error_logging", {})

        embed = discord.Embed(
            title="Error Logging Configuration",
            color=discord.Color.blue()
        )

        # Guild config
        default_channel_id = guild_config.get("default_channel") if guild_config else None
        if default_channel_id:
            embed.add_field(
                name=f"Guild: {ctx.guild.name}",
                value="✅ Enabled",
                inline=False
            )

            channel = self.bot.get_channel(default_channel_id)
            channel_str = channel.mention if channel else f"ID: {default_channel_id} (not found)"
            embed.add_field(name="Guild Channel", value=channel_str, inline=True)

            # Category routing
            category_channels = guild_config.get("category_channels", {})
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
            severity_channels = guild_config.get("severity_channels", {})
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
        else:
            embed.add_field(
                name=f"Guild: {ctx.guild.name}",
                value="❌ Disabled - Errors will go to global channel if configured.",
                inline=False
            )

        # Global config (show if superadmin)
        superadmins = self.bot.config.get_global("superadmins", [])
        if ctx.author.id in superadmins:
            global_channel_id = global_config.get("default_channel") if global_config else None
            if global_channel_id:
                ch = self.bot.get_channel(global_channel_id)
                embed.add_field(
                    name="Global Channel (Superadmin)",
                    value=ch.mention if ch else f"ID: {global_channel_id}",
                    inline=False
                )
            else:
                embed.add_field(
                    name="Global Channel (Superadmin)",
                    value="❌ Not configured",
                    inline=False
                )

        await ctx.send(embed=embed)

    @errorlog.command(name="setchannel")
    @commands.check(is_admin)
    async def errorlog_setchannel(self, ctx, channel: discord.TextChannel = None):
        """
        Set the default error logging channel for this guild.
        Usage: !errorlog setchannel #channel
        Requires: Guild admin or superadmin
        """
        if not ctx.guild:
            await ctx.send("This command must be run in a guild.")
            return

        if not channel:
            await ctx.send("Please specify a channel. Usage: `!errorlog setchannel #channel`")
            return

        # Try sending to the target channel first (test permissions)
        test_embed = discord.Embed(
            title="Guild Error Logging Enabled",
            description=f"This channel will receive error notifications from {ctx.guild.name}.",
            color=discord.Color.blue()
        )
        test_embed.add_field(
            name="What gets logged here:",
            value=(
                "• Command errors in this guild\n"
                "• Event errors in this guild\n"
                "• Can be customized with category/severity routing"
            ),
            inline=False
        )
        test_embed.add_field(
            name="Additional Configuration",
            value=(
                "`!errorlog setcategory <category> #channel` - Route specific error types\n"
                "`!errorlog setseverity <severity> #channel` - Route by severity\n"
                "`!errorlog status` - View current config\n"
                "`!errorlog disable` - Remove all error logging for this guild"
            ),
            inline=False
        )

        try:
            await channel.send(embed=test_embed)
        except discord.Forbidden:
            await ctx.send(f"❌ Cannot send messages to {channel.mention}. Please grant me message permissions in that channel.")
            return
        except Exception as e:
            await ctx.send(f"❌ Failed to send test message to {channel.mention}: {e}")
            return

        # Only save config if we successfully sent to the channel
        config = self._get_guild_error_config(ctx.guild.id)
        config["default_channel"] = channel.id
        self._set_guild_error_config(ctx.guild.id, config)

        # Send confirmation to command channel (unless it's the same channel)
        if ctx.channel.id != channel.id:
            await ctx.send(f"✅ Error logging enabled for {ctx.guild.name} → {channel.mention}")

    @errorlog.command(name="setcategory")
    @commands.check(is_admin)
    async def errorlog_setcategory(
        self,
        ctx,
        category: str,
        channel: discord.TextChannel
    ):
        """
        Route a specific error category to a channel for this guild.
        Categories: command_error, event_error, task_error, other
        Usage: !errorlog setcategory command_error #channel
        Requires: Guild admin or superadmin
        """
        if not ctx.guild:
            await ctx.send("This command must be run in a guild.")
            return

        # Validate category
        valid_categories = [c.value for c in ErrorCategory]
        if category not in valid_categories:
            await ctx.send(
                f"Invalid category. Valid categories: {', '.join(valid_categories)}"
            )
            return

        config = self._get_guild_error_config(ctx.guild.id)
        if "category_channels" not in config:
            config["category_channels"] = {}

        config["category_channels"][category] = channel.id
        self._set_guild_error_config(ctx.guild.id, config)

        await ctx.send(
            f"Category `{category}` errors in {ctx.guild.name} will now be logged to {channel.mention}"
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
        Route a specific severity level to a channel for this guild.
        Severities: info, warning, error, critical
        Usage: !errorlog setseverity critical #channel
        Requires: Guild admin or superadmin
        """
        if not ctx.guild:
            await ctx.send("This command must be run in a guild.")
            return

        # Validate severity
        valid_severities = [s.severity_name for s in ErrorSeverity]
        if severity.lower() not in valid_severities:
            await ctx.send(
                f"Invalid severity. Valid severities: {', '.join(valid_severities)}"
            )
            return

        config = self._get_guild_error_config(ctx.guild.id)
        if "severity_channels" not in config:
            config["severity_channels"] = {}

        config["severity_channels"][severity.lower()] = channel.id
        self._set_guild_error_config(ctx.guild.id, config)

        await ctx.send(
            f"Severity `{severity}` errors in {ctx.guild.name} will now be logged to {channel.mention}"
        )

    @errorlog.command(name="ratelimit")
    @commands.check(is_superadmin)
    async def errorlog_ratelimit(self, ctx, minutes: int):
        """
        Set the global rate limit for duplicate errors in minutes (superadmin only).
        This applies to all error deduplication across all guilds.
        Usage: !errorlog ratelimit 10
        """
        if minutes < 1 or minutes > 60:
            await ctx.send("Rate limit must be between 1 and 60 minutes.")
            return

        config = self.bot.config.get_global("error_logging", {})
        config["rate_limit_minutes"] = minutes
        self.bot.config.set_global("error_logging", config)

        await ctx.send(f"Global rate limit set to {minutes} minutes between duplicate errors.")

    @errorlog.command(name="disable")
    @commands.check(is_admin)
    async def errorlog_disable(self, ctx):
        """
        Disable error logging for this guild by removing all error config.
        Re-enable with !errorlog setchannel
        Requires: Guild admin or superadmin
        """
        if not ctx.guild:
            await ctx.send("This command must be run in a guild.")
            return

        # Remove the entire error_logging config for this guild
        if self.bot.config.rem(ctx.guild.id, "error_logging"):
            await ctx.send(f"Error logging disabled for {ctx.guild.name}. Use `!errorlog setchannel` to re-enable.")
        else:
            await ctx.send(f"Error logging is already disabled for {ctx.guild.name}.")

    @errorlog.command(name="setglobal")
    @commands.check(is_superadmin)
    async def errorlog_setglobal(self, ctx, *, channel: str = None):
        """
        Set or disable global error channel (superadmin only).
        Receives errors from: cog load failures, DMs, uncaught errors, and guilds without their own config.
        Usage: !errorlog setglobal #channel
        Usage: !errorlog setglobal disable
        """
        # Show current if no arg
        if not channel:
            config = self.bot.config.get_global("error_logging", {})
            channel_id = config.get("default_channel") if config else None
            if channel_id:
                ch = self.bot.get_channel(channel_id)
                await ctx.send(f"Current global error channel: {ch.mention if ch else f'ID: {channel_id}'}")
            else:
                await ctx.send("No global error channel configured.")
            return

        # Handle disable
        if channel.lower() == "disable":
            if self.bot.config.rem_global("error_logging"):
                await ctx.send("Global error logging disabled.")
            else:
                await ctx.send("Global error logging is already disabled.")
            return

        # Try to convert channel mention/ID to TextChannel
        try:
            # Try to convert channel mention or ID
            converter = commands.TextChannelConverter()
            text_channel = await converter.convert(ctx, channel)
        except commands.BadArgument:
            await ctx.send("Please specify a valid channel mention or 'disable'. Usage: `!errorlog setglobal #channel` or `!errorlog setglobal disable`")
            return

        # Try sending to the target channel first (test permissions)
        test_embed = discord.Embed(
            title="Global Error Logging Enabled",
            description="This channel will receive global error notifications.",
            color=discord.Color.blue()
        )
        test_embed.add_field(
            name="What gets logged here:",
            value=(
                "• All cog failures\n"
                "• All DM errors\n"
                "• All uncaught exceptions\n"
                "• Errors from guilds without their own error channel"
            ),
            inline=False
        )
        test_embed.add_field(
            name="Configuration (Superadmin)",
            value=(
                "`!errorlog ratelimit <minutes>` - Set global rate limit\n"
                "`!errorlog setglobal disable` - Disable global error logging\n"
                "`!errorlog status` - View current config"
            ),
            inline=False
        )

        try:
            await text_channel.send(embed=test_embed)
        except discord.Forbidden:
            await ctx.send(f"❌ Cannot send messages to {text_channel.mention}. Please grant me message permissions in that channel.")
            return
        except Exception as e:
            await ctx.send(f"❌ Failed to send test message to {text_channel.mention}: {e}")
            return

        # Only save config if we successfully sent to the channel
        config = self.bot.config.get_global("error_logging", {})
        config["default_channel"] = text_channel.id
        self.bot.config.set_global("error_logging", config)

        # Send confirmation to command channel (unless it's the same channel)
        if ctx.channel.id != text_channel.id:
            await ctx.send(f"✅ Global error logging enabled → {text_channel.mention}")

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


async def setup(bot):
    await bot.add_cog(ErrorLoggingAdmin(bot))
