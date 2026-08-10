import datetime
import platform

import discord
from discord.ext import commands

from core.utils import is_admin


class Tools(commands.Cog):
    """Small general-purpose utility commands."""

    def __init__(self, bot):
        self.bot = bot
        self.start_time = datetime.datetime.now(datetime.timezone.utc)

    @commands.command(name='echo')
    @commands.check(is_admin)
    async def echo(self, ctx, *, message):
        """Repost the given text as the bot (admin only — deleting the
        invoking message makes this full impersonation of the bot)."""
        await ctx.message.delete()
        await ctx.send(message)

    @commands.command(name='ping')
    async def ping(self, ctx):
        """Sends a message with bot's latency in ms in the channel where the command has been invoked.

        Note:
            `bot.latency` outputs the latency in seconds.
        """
        await ctx.send(f'🏓 {round(self.bot.latency * 1000)} ms.')

    @commands.command(name='info')
    async def get_info(self, ctx):
        """Show bot info: server count, latency, uptime, and library versions."""
        now = datetime.datetime.now(datetime.timezone.utc)
        uptime = now - self.start_time
        days, remainder = divmod(int(uptime.total_seconds()), 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime_str = f'{days}d {hours}h {minutes}m {seconds}s'

        embed = discord.Embed(
            title=self.bot.user.name,
            description='Browse commands with `!help` or `/help`.',
            colour=discord.Colour.blurple(),
            timestamp=now,
        )
        embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        embed.add_field(name='Servers', value=str(len(self.bot.guilds)), inline=True)
        embed.add_field(name='Latency', value=f'{round(self.bot.latency * 1000)} ms', inline=True)
        embed.add_field(name='Uptime', value=uptime_str, inline=True)
        embed.add_field(name='discord.py', value=discord.__version__, inline=True)
        embed.add_field(name='Python', value=platform.python_version(), inline=True)

        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Tools(bot))
