from discord.ext import commands
import random
import re

class Interogative(commands.Cog):
    """Ask any interrogative questions like !should I eat pizza?"""
    def __init__(self, bot):
        self.bot = bot


    @commands.command(name='should', aliases=['would','could','can','will','does','may','might','shall','must','is','am','are','has','had','have','were','was','do','did'], description='Yes or no question. Compatible with most interrogatiges (eg is, are, will, etc).')
    async def should(self, ctx):
        if random.random()>0.5:
            await  ctx.send("No")
        else:
            await  ctx.send("Yes")

async def setup(bot):
    """Every cog needs a setup function like this."""
    await bot.add_cog(Interogative(bot))
