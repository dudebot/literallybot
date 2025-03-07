import discord
from discord.ext import commands
from discord import app_commands
import random
import re
from utils import smart_split


class RNG(commands.Cog):
    """This is a cog with dice roll commands, including !random."""
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name='random', description='Picks a random item from a space-separated list.')
    async def random(self, ctx, *, options: str):
        """Picks a random item from a space-separated list."""
        values = smart_split(options)
        if values:
            if random.random()<0.05:
                await ctx.channel.send( "All of these options are terrible. Please think about your life and try later.")
            else:
                await ctx.send(random.choice(values))
        else:
            await ctx.send("Please provide some options.")
            
    @commands.command(name='order', description='Randomly orders the given options.')
    async def order(self, ctx, *, options: str):
        vals = smart_split(options)
        if vals:
            random.shuffle(vals)
            reply = "\n".join(f"{i+1}) {val}" for i, val in enumerate(vals))
            await ctx.send(reply)
        else:
            await ctx.send("No valid options given.")


    async def handle_dice_roll(self, arg: str) -> str:
        pattern = r"^(?P<rolls>\d+)?d(?P<sides>\d+)$"
        match = re.fullmatch(pattern, arg, re.IGNORECASE)
        if not match:
            return None  # signal that the pattern did not match
        roll_count = int(match.group("rolls")) if match.group("rolls") else 1
        sides = int(match.group("sides"))
        if roll_count > 100:
            return "Too many dice. Please roll 100 or fewer dice."
        if not (2 <= sides <= 100):
            return "Please use a dice with between 2 and 100 sides."
        results = [random.randint(1, sides) for _ in range(roll_count)]
        if roll_count == 1:
            return f"🎲 You rolled: {results[0]}"
        else:
            return f"🎲 You rolled: {', '.join(map(str, results))} (Sum: {sum(results)})"

    @commands.command(name='dice', description='Roll dice in NdX format. E.g., !dice d6 or !2d20')
    async def dice(self, ctx, *, arg: str = "d6"):
        result = await self.handle_dice_roll(arg)
        if result is None:
            await ctx.send("Invalid format. Use something like d6 or 2d20.")
        else:
            await ctx.send(result)

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot:
            return

        # Determine the command prefix.
        prefix = self.bot.command_prefix(self.bot,message) #wonky but it works
        prefixes = prefix if isinstance(prefix, (list, tuple)) else [prefix]

        for p in prefixes:
            if message.content.startswith(p):
                # Grab the text after the prefix.
                content = message.content[len(p):].strip()
                # Only auto-handle messages that are exactly in the NdX format (no spaces)
                if " " not in content:
                    result = await self.handle_dice_roll(content)
                    if result is not None:
                        await message.channel.send(result)
                        return
            
    @app_commands.command(name="roll_dice", description="Roll a six-sided die.")
    async def roll_dice(self, interaction: discord.Interaction):
        """Rolls a six-sided die and replies with the result."""
        result = random.randint(1, 6)
        await interaction.response.send_message(f"🎲 You rolled a **{result}**!")

async def setup(bot):
    """Every cog needs a setup function like this."""
    await bot.add_cog(RNG(bot))
