"""Membership and activity producers using the shared event delivery service."""
from discord.ext import commands

from utils.event_logging import registry, report


class Logging(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        registry.register(self, "membership", "Membership", {"join": "Joins", "leave": "Departures"})
        try:
            registry.register(self, "activity", "Activity", {"nickname": "Nickname changes", "voice": "Voice activity"})
        except Exception:
            registry.unregister(self)
            raise

    def cog_unload(self):
        registry.unregister(self)

    @commands.Cog.listener()
    async def on_member_join(self, member):
        await report(self.bot, member.guild, "membership.join", f"Welcome <@{member.id}>")

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        await report(self.bot, member.guild, "membership.leave",
                     f"<@{member.id}> AKA {member.name} has left the server")

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        if before.nick != after.nick:
            await report(self.bot, after.guild, "activity.nickname",
                         f"{before.display_name} changed their nickname to {after.display_name}")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot or before.channel == after.channel:
            return
        if any(channel is not None and "Voice Chat" in channel.name
               for channel in (before.channel, after.channel)):
            return
        if before.channel is None:
            text = f"{member.name} joined **{after.channel.name}**"
        elif after.channel is None:
            text = f"{member.name} left **{before.channel.name}**"
        elif before.channel.guild.id == after.channel.guild.id:
            text = f"{member.name} moved from **{before.channel.name}** to **{after.channel.name}**"
        else:
            return
        await report(self.bot, member.guild, "activity.voice", text)


async def setup(bot):
    await bot.add_cog(Logging(bot))
