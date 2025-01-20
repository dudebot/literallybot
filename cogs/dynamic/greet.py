from discord.ext import commands

class Greet(commands.Cog):
    """This is a cog with a greet command.
    Note:
        All cogs inherits from `commands.Cog`_.
        All cogs are classes.
        All cogs needs a setup function (see below).
            
    Documentation:
        https://discordpy.readthedocs.io/en/latest/ext/commands/cogs.html
    """
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name='greet', description='Sends a greeting message.')
    async def greet(self, ctx):
        """Sends a greeting message in the channel where the command is invoked."""
        await ctx.send('Hello!')

async def setup(bot):
    """Every cog needs a setup function like this."""
    await bot.add_cog(Greet(bot))
