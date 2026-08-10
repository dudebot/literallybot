from discord.ext import commands
import discord
import random
from datetime import datetime

class Logging(commands.Cog):
    """Writes status messages to a log channel."""
    def __init__(self, bot):
        self.bot = bot

    def get_log_channel(self, guild):
        return discord.utils.get(guild.text_channels, name="log")

    @commands.Cog.listener()
    async def on_member_join(self, member):
        channel = self.get_log_channel(member.guild)
        if not channel:
            return
        await channel.send(f"Welcome <@{member.id}>")
            
    @commands.Cog.listener()
    async def on_member_remove(self, member):
        channel = self.get_log_channel(member.guild)
        if channel:
            await channel.send(f"<@{member.id}> AKA {member.name} has left the server")

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        # on_member_update fires for guild nickname changes (username changes
        # are on_user_update, so the old before.name comparison never fired).
        channel = self.get_log_channel(before.guild)
        if channel and before.nick != after.nick:
            await channel.send(f"{before.display_name} changed their nickname to {after.display_name}")
            
    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot:
            return
        if (after.channel is not None and "Voice Chat" in after.channel.name) or (before.channel is not None and 'Voice Chat' in before.channel.name):
            return
        if before.channel is None and before.channel != after.channel:
            channel = self.get_log_channel(after.channel.guild)
            if channel:
                await channel.send(f"{member.name} joined **{after.channel.name}**")
        if after.channel is None and before.channel != after.channel:
            channel = self.get_log_channel(before.channel.guild)
            if channel:
                await channel.send(f"{member.name} left **{before.channel.name}**")
        if before.channel is not None and after.channel is not None and before.channel != after.channel and before.channel.guild.id == after.channel.guild.id:
            channel = self.get_log_channel(before.channel.guild)
            if channel:
                await channel.send(f"{member.name} moved from **{before.channel.name}** to **{after.channel.name}**")

async def setup(bot):
    await bot.add_cog(Logging(bot))
