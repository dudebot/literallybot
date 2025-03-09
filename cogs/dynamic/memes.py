import datetime
import random
from discord.ext import commands
from discord import File
import os

class Meme(commands.Cog):
    """This is a cog with meme commands."""
    def __init__(self, bot):
        self.bot = bot
        
    @commands.command(name='quoteme')  # removed the description argument
    async def quoteme(self, ctx, *, message):
        """Mock your enemies."""
        toggled = []
        for i, c in enumerate(message.replace(' ', '  ')): # temporarily double spaces so they make the letters alternate correclty
            toggled.append(c.upper() if i % 2 else c.lower())

        display_text = "".join(toggled).replace('  ', ' ')
        nickname = ctx.author.nick or ctx.author.name
        title = random.choice(["Someone Important","Chief Furry","Alcoholic",
                               "Literally Who","Basically Hitler","Mayor of Foofgens",
                               "Peta","Some random weeb", "Someone Mostly Literate"])
        await ctx.send(f"> {display_text}\n{nickname}\n{title}, {datetime.today().year}")

async def setup(bot):
    """Every cog needs a setup function like this."""
    await bot.add_cog(Meme(bot))
