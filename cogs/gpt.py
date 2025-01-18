from discord.ext import commands
import openai

class Gpt(commands.Cog):
    """This is a cog with a GPT question command."""
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name='askgpt', description='Ask a question to GPT-3.')
    async def askgpt(self, ctx, *, question: str):
        """Ask a question to GPT-3 and get a response."""
        openai.api_key = 'YOUR_OPENAI_API_KEY'
        response = openai.Completion.create(
            engine="davinci",
            prompt=question,
            max_tokens=150
        )
        await ctx.send(response.choices[0].text.strip())

async def setup(bot):
    """Every cog needs a setup function like this."""
    await bot.add_cog(Gpt(bot))
