from discord.ext import commands
import discord
import random
from datetime import datetime

class Logging(commands.Cog):
    """Writes status messages to """
    def __init__(self, bot):
        self.bot = bot

    def get_log_channel(self, guild):
        return discord.utils.get(guild.text_channels, name="log")

    @commands.Cog.listener()
    async def on_member_join(self, member):
        channel = self.get_log_channel(member.guild)
        if not channel:
            return
        if member.guild.id == 125817769923969024:
            await channel.send(f"<@{member.id}>, DM this bot with a \"Kong Strong!\" for the kong role.")
        else:
            await channel.send(f"Welcome <@{member.id}>")
            
    @commands.Cog.listener()
    async def on_member_remove(self, member):
        channel = self.get_log_channel(member.guild)
        if channel:
            await channel.send(f"We sure told <@{member.id}> AKA {member.name}")

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        channel = self.get_log_channel(before.guild)
        if channel and before.name and after.name and before.name != after.name:
            await channel.send(f"{before.name} changed their name to {after.name}")
            
    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.name in ["AIRHORN SOLUTIONS"]:
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
    """Every cog needs a setup function like this."""
    await bot.add_cog(Logging(bot))
