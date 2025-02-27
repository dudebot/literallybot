from discord.ext import commands
import openai
import os
import discord
import random

class Gpt(commands.Cog):
    """This is a cog with a GPT question command."""
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name='askgpt', aliases=['gpt'], description='Ask a question to GPT.', hidden=True)
    @commands.cooldown(10, 240, commands.BucketType.guild)
    async def askgpt(self, ctx, *, question: str):
        """Ask a question to GPT-4o-mini and get a response."""
        # Retrieve last 10 messages from channel history using async for loop
        history = []
        messages = []
        async for msg in ctx.channel.history(limit=10):
            messages.append(msg)
        
        
        client = openai.OpenAI(
            api_key=os.environ.get("OPENAI_API_KEY"),
        )
        
        for msg in reversed(messages):
            role = "assistant" if msg.author.bot else "user"
            history.append({"role": role, "content": f"{msg.author.display_name}: {msg.content}"})
            
        chat_completion = client.chat.completions.create(
            messages=[
                {
                    "role": "system",
                    "content": "You are a helpful assistant with slight libertarian bias. Always end your responses with \"Xiaohongshu\", followed by an appropriate emoji from the history."
                },
                *history,
                {
                    "role": "user",
                    "content": question,
                }
            ],
            metadata={
                "service": "literallybot",
                "sender": str(ctx.author.id),
                "channel": str(ctx.channel.id),
                "guild": str(ctx.guild.id)
            },
            max_tokens=800,
            store=True,
            model="gpt-4o-mini",
        )
        response = chat_completion.choices[0].message.content.strip()
        response = response.replace("\n\n", "\n").replace("\\n\\n", "\\n")
        chunks = [response[i:i+2000] for i in range(0, len(response), 2000)]
        for chunk in chunks:
            await ctx.send(chunk)

    @askgpt.error
    async def askgpt_error(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"You are on cooldown. Try again in {error.retry_after:.2f}s")
        else:
            raise error

async def setup(bot):
    """Every cog needs a setup function like this."""
    await bot.add_cog(Gpt(bot))
