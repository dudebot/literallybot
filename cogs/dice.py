from discord.ext import commands
import random

class Dice(commands.Cog):
    """This is a cog with dice roll commands, including !random."""
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name='random', description='Picks a random item from a space-separated list.')
    async def random(self, ctx, *, options: str):
        """Picks a random item from a space-separated list."""
        values = options.split()
        if values:
            await ctx.send(random.choice(values))
        else:
            await ctx.send("Please provide some options.")

    @commands.command(name='diceroll', description='Rolls a dice with the specified number of sides.')
    async def diceroll(self, ctx, sides: int):
        """Rolls a dice with the specified number of sides."""
        if sides > 0:
            await ctx.send(random.randint(1, sides))
        else:
            await ctx.send("Please provide a valid number of sides.")

    @commands.command(name='multidice', description='Rolls multiple dice with the specified number of sides.')
    async def multidice(self, ctx, rolls: int, sides: int):
        """Rolls multiple dice with the specified number of sides."""
        if rolls > 0 and sides > 0:
            results = [random.randint(1, sides) for _ in range(rolls)]
            await ctx.send(f"Results: {', '.join(map(str, results))}")
        else:
            await ctx.send("Please provide valid numbers of rolls and sides.")

async def setup(bot):
    """Every cog needs a setup function like this."""
    await bot.add_cog(Dice(bot))
