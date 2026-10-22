"""Assign an existing role and clean recent messages after a trap is triggered."""
import asyncio
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

from core.ops import OpParam, OpScope, ParamKind, PermissionLevel, op
from core.permissions import is_admin
from core.panels import panel_slash_pin
from core.event_logging import registry as event_registry, report

KEY = "honeypot"
DEFAULT_MINUTES = 10
REASON = "Honeypot triggered"


class Honeypot(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._active = set()

    async def cog_load(self):
        event_registry.register(self, "honeypot", "Honeypot", {"incident": "Moderation incidents"})

    def cog_unload(self):
        event_registry.unregister(self)

    def _settings(self, guild):
        return deepcopy(self.bot.config.get(guild.id, KEY, {}))

    def _exempt(self, member):
        return member.bot or is_admin(SimpleNamespace(bot=self.bot, guild=member.guild, author=member))

    @staticmethod
    def _validate_role(guild, role):
        if role is None:
            raise ValueError("Specify an existing role the honeypot should assign.")
        if (role.guild.id != guild.id or role.is_default() or role.managed
                or role >= guild.me.top_role or role.permissions.administrator):
            raise ValueError("Choose a non-managed role below my highest role, without Administrator.")
        if not guild.me.guild_permissions.manage_roles:
            raise ValueError("I need Manage Roles to assign the selected role.")

    async def configure(self, guild, channel, role=None, enabled=True,
                        minutes=DEFAULT_MINUTES, strip_all_other_roles=False):
        """Shared configuration service; callers supply authorization and scope."""
        if type(minutes) is not int or not 0 <= minutes <= 10080:
            raise ValueError("Cleanup must be 0–10080 whole minutes (0 deletes only the trigger).")
        settings = self._settings(guild)
        if enabled:
            if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild.id:
                raise ValueError("Choose a text channel in this server.")
            role = role or guild.get_role(settings.get("role_id", 0))
            self._validate_role(guild, role)
            permissions = channel.permissions_for(guild.me)
            if not (permissions.view_channel and permissions.read_message_history and permissions.manage_messages):
                raise ValueError("I need View Channel, Read Message History, and Manage Messages in the trap.")
            settings = {"enabled": True, "channel_id": channel.id, "role_id": role.id,
                        "minutes": minutes, "strip_all_other_roles": bool(strip_all_other_roles)}
        else:
            settings["enabled"] = False
        self.bot.config.set(guild.id, KEY, settings)
        await asyncio.to_thread(self.bot.config.flush)
        result = {"status": "enabled" if enabled else "disabled"}
        if enabled:
            result.update(channel_id=str(channel.id), role_id=str(role.id), minutes=minutes,
                          strip_all_other_roles=bool(strip_all_other_roles))
        return result

    async def _report(self, guild, text):
        self.bot.logger.warning("Honeypot guild %s: %s", guild.id, text)
        await report(self.bot, guild, "honeypot.incident", text)

    async def _apply_roles(self, member, role, strip_all_other_roles):
        try:
            # Revalidate: an operator may have changed the role since setup.
            self._validate_role(member.guild, role)
            await member.add_roles(role, reason=REASON, atomic=True)
        except (ValueError, discord.HTTPException) as exc:
            return [], [f"role assignment: {type(exc).__name__}: {exc}"]
        if not strip_all_other_roles:
            return [], []
        try:
            current = await member.guild.fetch_member(member.id)
        except discord.HTTPException as exc:
            return [], [f"could not fetch current roles for stripping: {type(exc).__name__}"]
        removed, failures = [], []
        for other in current.roles:
            if other.is_default() or other.id == role.id:
                continue
            if other.managed or other >= current.guild.me.top_role:
                failures.append(f"could not strip role {other.id}: managed or at/above my highest role")
                continue
            try:
                await current.remove_roles(other, reason=REASON, atomic=True)
                removed.append(other.id)
            except discord.HTTPException as exc:
                failures.append(f"could not strip role {other.id}: {type(exc).__name__}")
        return removed, failures

    async def _delete(self, message, failures):
        try:
            await message.delete()
            return 1
        except discord.NotFound:
            return 0
        except discord.HTTPException as exc:
            failures.append(f"message {message.id}: {type(exc).__name__}")
            return 0

    async def _cleanup(self, message, minutes, *, before=None):
        before = before or discord.utils.utcnow()
        after = message.created_at - timedelta(minutes=minutes)
        failures = []
        deleted = await self._delete(message, failures)
        if minutes == 0:
            return deleted, failures
        seen = set()

        async def clean(channel):
            nonlocal deleted
            if channel.id in seen:
                return
            seen.add(channel.id)
            perms = channel.permissions_for(message.guild.me)
            if not (perms.view_channel and perms.read_message_history and perms.manage_messages):
                failures.append(f"history {channel.id}: missing View Channel, Read Message History or Manage Messages")
                return
            try:
                async for candidate in channel.history(limit=None, after=after, before=before):
                    if candidate.author.id == message.author.id and candidate.id != message.id:
                        deleted += await self._delete(candidate, failures)
            except discord.HTTPException as exc:
                failures.append(f"history {channel.id}: {type(exc).__name__}")

        # Also scan the trigger's thread if active-thread discovery is unavailable.
        await clean(message.channel)
        for channel in message.guild.channels:
            if hasattr(channel, "history"):
                await clean(channel)
            if not isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
                continue
            perms = channel.permissions_for(message.guild.me)
            if not (perms.view_channel and perms.read_message_history):
                failures.append(f"archives {channel.id}: missing View Channel or Read Message History")
                continue
            queries = [{"limit": None}]
            if isinstance(channel, discord.TextChannel):
                # Unjoined private archives require Manage Threads. If absent,
                # still enumerate joined archives and report incomplete coverage.
                if perms.manage_threads:
                    queries.append({"limit": None, "private": True})
                else:
                    failures.append(f"archives {channel.id}: missing Manage Threads for all private archives")
                    queries.append({"limit": None, "private": True, "joined": True})
            for query in queries:
                try:
                    async for thread in channel.archived_threads(**query):
                        # Joined private listings sort by join time, not archive
                        # time. They cannot use this early termination shortcut.
                        if (not query.get("joined") and thread.archive_timestamp
                                and thread.archive_timestamp < after):
                            break
                        await clean(thread)
                except discord.HTTPException as exc:
                    failures.append(f"archives {channel.id}: {type(exc).__name__}")
        try:
            for thread in await message.guild.active_threads():
                await clean(thread)
        except discord.HTTPException as exc:
            failures.append(f"active threads: {type(exc).__name__}")
        return deleted, failures

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.guild is None or not isinstance(message.author, discord.Member):
            return
        settings = self._settings(message.guild)
        if (not settings.get("enabled") or not settings.get("channel_id")
                or settings["channel_id"] not in (message.channel.id, getattr(message.channel, "parent_id", None))
                or self._exempt(message.author)):
            return
        key = (message.guild.id, message.author.id)
        if key in self._active:
            failures = []
            deleted = await self._delete(message, failures)
            # Report separately: this deletion is outside the running scan's count.
            await self._report(message.guild, f"Honeypot concurrent trigger {message.id} by {message.author.id}: "
                               + ("; ".join(failures) if failures else f"deleted {deleted} additional messages."))
            return
        self._active.add(key)
        try:
            failures, removed = [], []
            try:
                removed, failures = await self._apply_roles(
                    message.author, message.guild.get_role(settings.get("role_id", 0)),
                    settings.get("strip_all_other_roles", False))
            except Exception as exc:
                failures.append(f"role handling interrupted: {type(exc).__name__}")
                self.bot.logger.exception("Honeypot role handling failed")
            minutes = settings.get("minutes", DEFAULT_MINUTES)
            before = discord.utils.utcnow()
            after = message.created_at - timedelta(minutes=minutes)
            await self._report(message.guild,
                f"Honeypot triggered by <@{message.author.id}> ({message.author.id}) in <#{message.channel.id}>; "
                f"message {message.id}. Role {settings.get('role_id')}: "
                + ("handling incomplete. " if failures else "assigned. ")
                + f"Removed roles: {', '.join(map(str, removed)) or 'none'}. "
                + (f"Cleanup window: {after.isoformat()} through {before.isoformat()} ({minutes} minutes lookback)."
                   if minutes else "Cleanup: triggering message only.")
                + (" Failures: " + "; ".join(failures) if failures else ""))
            try:
                deleted, cleanup_failures = await self._cleanup(message, minutes, before=before)
                failures.extend(cleanup_failures)
            except Exception as exc:
                deleted = "an unknown number of"
                failures.append(f"cleanup interrupted: {type(exc).__name__}")
                self.bot.logger.exception("Honeypot cleanup failed")
            await self._report(message.guild,
                f"Honeypot result for <@{message.author.id}>: deleted {deleted} messages. "
                + ("Role assigned and cleanup complete." if not failures else
                   "INCOMPLETE — admin action needed: " + "; ".join(failures)))
        finally:
            self._active.discard(key)

    @op(
        "configure_honeypot",
        "Configure this guild's single honeypot using an existing text channel and role. "
        "Triggers assign the role, optionally remove other manageable roles, and delete the member's recent messages. "
        "Does not create resources or restore roles. Disabling retains saved settings and assigned roles.",
        PermissionLevel.ADMIN, scope=OpScope.GUILD, group="honeypot", group_label="Honeypot",
        params=[
            OpParam("channel", ParamKind.CHANNEL, "Existing trap text channel (explicit even when disabling)."),
            OpParam("role", ParamKind.ROLE, "Existing role; omit to reuse saved role.", required=False),
            OpParam("enabled", ParamKind.BOOLEAN, "Enable or disable.", required=False, default=True),
            OpParam("minutes", ParamKind.INTEGER, "Message cleanup lookback; 0 deletes only the trigger.",
                    required=False, default=DEFAULT_MINUTES, minimum=0, maximum=10080),
            OpParam("strip_all_other_roles", ParamKind.BOOLEAN, "Remove other manageable roles after assignment.",
                    required=False, default=False),
        ],
    )
    async def op_configure(self, ctx, channel, role=None, enabled=True,
                           minutes=DEFAULT_MINUTES, strip_all_other_roles=False):
        guild = getattr(channel, "guild", None)
        if guild is None:
            raise ValueError("Choose a channel in a server.")
        return await self.configure(guild, channel, role, enabled, minutes, strip_all_other_roles)

    @staticmethod
    def _configuration_message(result):
        if result["status"] == "disabled":
            return "Honeypot disabled. Previously assigned roles are unchanged."
        return (f"Honeypot enabled in <#{result['channel_id']}>. Assigns <@&{result['role_id']}>; "
                f"cleanup: {result['minutes']} minutes. Strip other roles: "
                f"{'on' if result['strip_all_other_roles'] else 'off'}. Reports follow /logsettings → Activity.")

    @app_commands.command(name="honeypot", description="Configure a trap that assigns a role and deletes recent messages")
    @app_commands.guild_only()
    @panel_slash_pin()
    @app_commands.check(is_admin)
    @app_commands.describe(action="Enable (default) or disable", role="Existing role (required on first enable)",
                           channel="Existing trap text channel (default: here)",
                           minutes="Cleanup lookback in minutes (default 10; 0 deletes only the trigger)",
                           strip_all_other_roles="Remove other manageable roles after assigning the role (default false)")
    async def honeypot_slash(self, interaction: discord.Interaction,
                             action: Literal["enable", "disable"] = "enable",
                             role: discord.Role = None, channel: discord.TextChannel = None,
                             minutes: app_commands.Range[int, 0, 10080] = DEFAULT_MINUTES,
                             strip_all_other_roles: bool = False):
        await interaction.response.defer(ephemeral=True)
        try:
            result = await self.configure(interaction.guild, channel or interaction.channel, role,
                                          action == "enable", minutes, strip_all_other_roles)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(self._configuration_message(result), ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="honeypot", hidden=True)
    @commands.guild_only()
    @commands.check(is_admin)
    async def honeypot_prefix(self, ctx, *, args: str = ""):
        """!honeypot [enable|disable] [@role] [#channel] [minutes] [true|false]"""
        parts = args.split()
        action = parts.pop(0).lower() if parts and parts[0].lower() in ("enable", "disable") else "enable"
        channel, role, minutes, strip = ctx.channel, None, DEFAULT_MINUTES, False
        try:
            if parts and parts[0].startswith("<@&"):
                role = ctx.guild.get_role(int(parts.pop(0).removeprefix("<@&").removesuffix(">")))
                if role is None:
                    raise ValueError("Unknown role.")
            if parts and parts[0].startswith("<#"):
                channel = ctx.guild.get_channel(int(parts.pop(0).removeprefix("<#").removesuffix(">")))
            if parts and parts[0].lower() not in ("true", "false"):
                minutes = int(parts.pop(0))
            if parts:
                value = parts.pop(0).lower()
                if value not in ("true", "false"):
                    raise ValueError("Use true or false.")
                strip = value == "true"
            if parts:
                raise ValueError("Too many arguments.")
        except ValueError:
            await ctx.send("Usage: !honeypot [enable|disable] [@role] [#channel] [minutes] [true|false]")
            return
        try:
            result = await self.configure(ctx.guild, channel, role, action == "enable", minutes, strip)
        except ValueError as exc:
            await ctx.send(str(exc))
            return
        await ctx.send(self._configuration_message(result), allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(Honeypot(bot))
