import discord
from discord.ext import commands
from discord import app_commands
import random
import re


def get_options(options):
    if re.search(r',? ?\bor\b ?|, ?',options.lower()) is not None:
        values = re.split(r',? ?\bor\b ?|, ?',options.lower())
    elif ' ' in options:
        values = options.split(' ')
    else:
        values = re.split(r'\W',options)
    return [value for value in values if value != ""]
class Dice(commands.Cog):
    """This is a cog with dice roll commands, including !random."""
    def __init__(self, bot):
        self.bot = bot



    @commands.command(name='random', description='Picks a random item from a space-separated list.')
    async def random(self, ctx, *, options: str):
        """Picks a random item from a space-separated list."""
        values = get_options(options)
        if values:
            if random.random()<0.05:
                await ctx.channel.send( "All of these options are terrible. Please think about your life and try later.")
            else:
                await ctx.send(random.choice(values))
        else:
            await ctx.send("Please provide some options.")
            
    @commands.command(name='order', description='Randomly orders the given options.')
    async def order(self, ctx, *, options: str):
        vals = get_options(options)
        if vals:
            random.shuffle(vals)
            reply = "\n".join(f"{i+1}) {val}" for i, val in enumerate(vals))
            await ctx.send(reply)
        else:
            await ctx.send("No valid options given.")


    @commands.command(name='diceroll', aliases=['dice','d6'], description='Rolls a dice with the specified number of sides.')
    async def dice(self, ctx, sides: int = 6):
        """Rolls a dice with the specified number of sides."""
        if sides > 0:
            await ctx.send(random.randint(1, sides))
        else:
            await ctx.send("Please provide a valid number of sides.")

    @commands.command(name='d20', description='Rolls a twenty-sided die.')
    async def d20(self, ctx):
        """Rolls a twenty-sided die."""
        await ctx.send(random.randint(1, 20))

    @commands.command(name='multidice', description='Rolls multiple dice with the specified number of sides.')
    async def multidice(self, ctx, rolls: int, sides: int):
        """Rolls multiple dice with the specified number of sides."""
        if rolls > 0 and sides > 0:
            results = [random.randint(1, sides) for _ in range(rolls)]
            await ctx.send(f"Results: {', '.join(map(str, results))}")
        else:
            await ctx.send("Please provide valid numbers of rolls and sides.")
            
    @app_commands.command(name="roll_dice", description="Roll a six-sided die.")
    async def roll_dice(self, interaction: discord.Interaction):
        """Rolls a six-sided die and replies with the result."""
        result = random.randint(1, 6)
        await interaction.response.send_message(f"🎲 You rolled a **{result}**!")

async def setup(bot):
    """Every cog needs a setup function like this."""
    await bot.add_cog(Dice(bot))
