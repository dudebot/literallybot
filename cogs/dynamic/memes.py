import datetime
import random
from discord.ext import commands
from discord import File
import os

class Meme(commands.Cog):
    """This is a cog with dice roll commands, including !random."""
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name='squish', description='Cat command.')
    async def squish(self, ctx):
        await ctx.send(file=File("media/squish.webm"))

    @commands.command(name='ding', description='Cute command.')
    async def ding(self, ctx):
        await ctx.send(file=File("media/dingdingdoo.webm"))

    @commands.command(name='aaa', aliases=["aaaa", "aaaaa", "aaaaaa", "aaaaaaa"], description='Hell command.')
    async def aaa(self, ctx):
        await ctx.send(file=File("media/aaaaaaaa_hd.mp4"))

    @commands.command(name='pog', description='Pog command.')
    async def pog(self, ctx):
        await ctx.send(file=File("media/poggers.mp4"))

    @commands.command(name='hi', aliases=["yahallo", "hello"], description='hi :3 command.')
    async def yahallo(self, ctx):
        await ctx.send(file=File("media/yahallo.mp4"))
        
    @commands.command(name='quoteme', description='Mock your enemies.')
    async def quoteme(self, ctx, *, message):
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
