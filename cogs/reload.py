from discord.ext import commands

class Reload(commands.Cog):
    """This is a cog with a reload command.
    Note:
        All cogs inherits from `commands.Cog`_.
        All cogs are classes.
        All cogs needs a setup function like this.
            
    Documentation:
        https://discordpy.readthedocs.io/en/latest/ext/commands/cogs.html
    """
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name='reload', hidden=True)
    @commands.is_owner()
    async def reload_cog(self, ctx, *, cog: str):
        """This command reloads the selected cog, as long as that cog is in the `./cogs` folder.
        
        Args:
            cog (str): The name of the cog to reload.
        Note:
            This command can be used only by the bot owner.
            This command is hidden from the help menu.
            This command deletes its messages after 20 seconds.
        """
        message = await ctx.send('Reloading...')
        await ctx.message.delete()
        try:
            self.bot.reload_extension(f'cogs.{cog}')
        except Exception as exc:
            await message.edit(content=f'An error has occurred: {exc}', delete_after=20)
        else:
            await message.edit(content=f'{cog} has been reloaded.', delete_after=20)

async def setup(bot):
    """Every cog needs a setup function like this."""
    await bot.add_cog(Reload(bot))
