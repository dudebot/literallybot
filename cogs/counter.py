"""Explicitly enabled counting channels; Discord history owns the count.

Strict channels delete anything except the next integer. Non-strict
channels allow chatter and mark wrong numbers with a reaction instead; the
history scan skips messages the bot marked, so no count is stored. A channel
falls back to non-strict when the bot lacks Manage Messages there.
"""

import asyncio
import re
from typing import Literal
from weakref import WeakValueDictionary

import discord
from discord import app_commands
from discord.ext import commands

from core.ops import OpParam, OpScope, ParamKind, PermissionLevel, op
from core.permissions import is_admin
from core.panels import panel_slash_pin


COUNTING_CHANNELS = "counting_channels"
NONSTRICT_CHANNELS = "counting_nonstrict_channels"
INTEGER = re.compile(r"[0-9]+")
WRONG = "❌"


def _number(content):
    text = content.strip()
    if not INTEGER.fullmatch(text):
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _marked_wrong(message):
    return any(reaction.me and str(reaction.emoji) == WRONG for reaction in message.reactions)


class Counter(commands.Cog):
    """Keep enabled channels counting up by one."""

    def __init__(self, bot):
        self.bot = bot
        self._locks = WeakValueDictionary()

    def _enabled(self, guild_id):
        return self.bot.config.get(guild_id, COUNTING_CHANNELS, [])

    def _nonstrict(self, guild_id):
        return self.bot.config.get(guild_id, NONSTRICT_CHANNELS, [])

    def _set_strict(self, guild_id, channel_id, strict):
        channels = [cid for cid in self._nonstrict(guild_id) if cid != channel_id]
        if not strict:
            channels.append(channel_id)
        if channels != self._nonstrict(guild_id):
            self.bot.config.set(guild_id, NONSTRICT_CHANNELS, channels)

    def _downgrade(self, guild_id, channel_id):
        self.bot.logger.warning("Counting in channel %s switched to non-strict: missing Manage Messages",
                                channel_id)
        self._set_strict(guild_id, channel_id, False)

    def _remove(self, guild_id, channel_id):
        channels = self._enabled(guild_id)
        if channel_id in channels:
            self.bot.config.set(guild_id, COUNTING_CHANNELS,
                                [cid for cid in channels if cid != channel_id])
        self._set_strict(guild_id, channel_id, True)

    async def _configure(self, guild, channel, action):
        if guild is None or getattr(channel, "guild", None) != guild:
            raise ValueError("Choose a text channel in this server.")
        if not isinstance(channel, discord.TextChannel):
            raise ValueError("Counting requires a server text channel.")
        if action not in ("enable", "disable"):
            raise ValueError("Use enable or disable.")
        strict = False
        async with self._locks.setdefault(channel.id, asyncio.Lock()):
            if action == "enable":
                permissions = channel.permissions_for(guild.me)
                if not all(getattr(permissions, name) for name in (
                        "view_channel", "read_message_history", "add_reactions")):
                    raise ValueError("I need View Channel, Read Message History, and Add Reactions.")
                strict = permissions.manage_messages
                channels = self._enabled(guild.id)
                if channel.id not in channels:
                    self.bot.config.set(guild.id, COUNTING_CHANNELS, channels + [channel.id])
                self._set_strict(guild.id, channel.id, strict)
            else:
                self._remove(guild.id, channel.id)
        return {"channel_id": str(channel.id), "enabled": action == "enable", "strict": strict}

    @commands.command(name="counter", hidden=True)
    @commands.guild_only()
    @commands.check(is_admin)
    async def counter(self, ctx, action: str = ""):
        """Enable or disable counting here: !counter enable|disable."""
        try:
            result = await self._configure(ctx.guild, ctx.channel, action.lower())
        except ValueError as error:
            await ctx.send(str(error))
            return
        await ctx.send(self._confirmation(result))

    @app_commands.command(name="counter", description="Enable or disable counting in a channel")
    @app_commands.guild_only()
    @panel_slash_pin()
    @app_commands.check(is_admin)
    @app_commands.describe(action="Enable or disable counting", channel="Defaults to this channel")
    async def counter_slash(self, interaction: discord.Interaction,
                            action: Literal["enable", "disable"],
                            channel: discord.TextChannel | None = None):
        try:
            result = await self._configure(interaction.guild, channel or interaction.channel, action)
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        await interaction.response.send_message(self._confirmation(result), ephemeral=True)

    @staticmethod
    def _confirmation(result):
        if not result["enabled"]:
            return f"Counting disabled in <#{result['channel_id']}>."
        if result["strict"]:
            return f"Strict counting enabled in <#{result['channel_id']}>."
        return (f"Counting enabled in <#{result['channel_id']}> without Manage Messages: "
                f"wrong numbers get {WRONG} instead of being deleted.")

    @op("configure_counter", "Enable or disable counting in a server text channel. "
        "Strict (deletes non-counts) when the bot has Manage Messages, otherwise "
        "non-strict (reacts to wrong numbers). Uses existing channel history; "
        "does not clear messages or award rewards.",
        PermissionLevel.ADMIN,
        params=[OpParam("channel", ParamKind.CHANNEL, "Server text channel."),
                OpParam("action", ParamKind.STRING, "enable or disable")],
        scope=OpScope.GUILD, group="counting", group_label="Counting")
    async def op_configure_counter(self, ctx, channel, action: str):
        return await self._configure(ctx.guild, channel, action)

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or message.guild is None:
            return
        if message.channel.id not in self._enabled(message.guild.id):
            return
        async with self._locks.setdefault(message.channel.id, asyncio.Lock()):
            if message.channel.id not in self._enabled(message.guild.id):
                return
            try:
                await self._check(message)
            except (discord.Forbidden, discord.NotFound):
                self._remove(message.guild.id, message.channel.id)
                self.bot.logger.warning("Counting disabled in channel %s: channel inaccessible or reactions denied",
                                        message.channel.id)

    async def _check(self, message):
        strict = message.channel.id not in self._nonstrict(message.guild.id)
        number = _number(message.content)
        if not strict and number is None:
            return  # Non-strict channels allow chatter.
        previous = None
        # before is essential: later burst messages must not move this
        # message's baseline. No saved number can survive a deletion.
        async for prior in message.channel.history(limit=None, before=message, oldest_first=False):
            if prior.author.bot or _marked_wrong(prior):
                continue
            previous = _number(prior.content)
            if previous is not None:
                break
        valid = number in (0, 1) if previous is None else number == previous + 1
        if strict and not valid:
            try:
                await message.delete()
                return
            except discord.NotFound:
                return  # Already deleted by a moderator or another listener.
            except discord.Forbidden:
                self._downgrade(message.guild.id, message.channel.id)
                if number is None:
                    return
        if valid:
            if _marked_wrong(message):  # An edit fixed a marked number.
                await message.remove_reaction(WRONG, message.guild.me)
        elif not _marked_wrong(message):
            await message.add_reaction(WRONG)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload):
        if payload.guild_id is None or payload.channel_id not in self._enabled(payload.guild_id):
            return
        try:
            channel = self.bot.get_channel(payload.channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(payload.channel_id)
        except (discord.Forbidden, discord.NotFound):
            self._remove(payload.guild_id, payload.channel_id)
            self.bot.logger.warning("Counting disabled in channel %s: channel inaccessible",
                                    payload.channel_id)
            return
        try:
            message = await channel.fetch_message(payload.message_id)
        except discord.NotFound:
            return  # A deleted message does not mean its channel was deleted.
        except discord.Forbidden:
            self._remove(payload.guild_id, payload.channel_id)
            self.bot.logger.warning("Counting disabled in channel %s: message history inaccessible",
                                    payload.channel_id)
            return
        await self.on_message(message)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        self._remove(channel.guild.id, channel.id)

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            for channel_id in list(self._enabled(guild.id)):
                try:
                    channel = guild.get_channel(channel_id)
                    if channel is None:
                        channel = await self.bot.fetch_channel(channel_id)
                    if getattr(channel, "guild", None) != guild:
                        self._remove(guild.id, channel_id)
                        continue
                    permissions = channel.permissions_for(guild.me)
                    if not (permissions.view_channel and permissions.read_message_history
                            and permissions.add_reactions):
                        self._remove(guild.id, channel_id)
                        self.bot.logger.warning("Counting disabled in channel %s: missing View Channel, "
                                                "Read Message History or Add Reactions", channel_id)
                    elif (not permissions.manage_messages
                          and channel_id not in self._nonstrict(guild.id)):
                        self._downgrade(guild.id, channel_id)
                except (discord.Forbidden, discord.NotFound):
                    self._remove(guild.id, channel_id)
                except discord.HTTPException:
                    self.bot.logger.warning("Could not check counting channel %s; keeping its configuration",
                                            channel_id, exc_info=True)


async def setup(bot):
    await bot.add_cog(Counter(bot))
