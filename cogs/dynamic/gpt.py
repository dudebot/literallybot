from discord.ext import commands
import openai
import os
import discord

class Gpt(commands.Cog):
    """This is a cog with a GPT question command."""
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name='askgpt', aliases=['gpt'], description='Ask a question to GPT.')
    @commands.cooldown(10, 240, commands.BucketType.guild)
    async def askgpt(self, ctx, *, question: str):
        """Ask a question to GPT-4o-mini and get a response."""
        client = openai.OpenAI(
            api_key=os.environ.get("OPENAI_API_KEY"),  # This is the default and can be omitted
        )
        chat_completion = client.chat.completions.create(
            messages=[
                {
                    "role": "user",
                    "content": question,
                }
            ],
        max_tokens=500,
        store=True,
        model="gpt-4o-mini",
)
        await ctx.send(chat_completion.choices[0].message.content.strip())

    @askgpt.error
    async def askgpt_error(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"You are on cooldown. Try again in {error.retry_after:.2f}s")
        else:
            raise error

    @discord.app_commands.command(name="askgpt", description="Ask a question to GPT.")
    @discord.app_commands.checks.cooldown(10, 240, key=lambda i: (i.guild_id,))
    async def askgpt_app_command(self, interaction: discord.Interaction, question: str):
        """Ask a question to GPT-4o-mini and get a response."""
        client = openai.OpenAI(
            api_key=os.environ.get("OPENAI_API_KEY"),  # This is the default and can be omitted
        )
        chat_completion = client.chat.completions.create(
            messages=[
                {
                    "role": "user",
                    "content": question,
                }
            ],
        max_tokens=500,
        store=True,
        model="gpt-4o-mini",
)
        await interaction.response.send_message(chat_completion.choices[0].message.content.strip())

    @askgpt_app_command.error
    async def askgpt_app_command_error(self, interaction: discord.Interaction, error):
        if isinstance(error, discord.app_commands.CommandOnCooldown):
            await interaction.response.send_message(f"You are on cooldown. Try again in {error.retry_after:.2f}s")
        else:
            raise error

async def setup(bot):
    """Every cog needs a setup function like this."""
    await bot.add_cog(Gpt(bot))
    bot.tree.add_command(Gpt.askgpt_app_command)
