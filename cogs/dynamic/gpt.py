from discord.ext import commands
import openai
import os
import discord
import random

class Gpt(commands.Cog):
    """This is a cog with a GPT question command."""
    def __init__(self, bot):
        self.bot = bot

    async def process_askgpt(self, ctx, question: str):
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
                    "content": "You’re a meme bot with a sharp wit and a love for sharing your bold opinions. Make sure to sign off with 'Xiaohongshu' and an emoji that nails the vibe."
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

    @commands.command(name='askgpt', aliases=['gpt'], description='Ask a question to GPT.', hidden=True)
    @commands.cooldown(10, 240, commands.BucketType.guild)
    async def askgpt(self, ctx, *, question: str):
        await self.process_askgpt(ctx, question)

    @commands.Cog.listener()
    async def on_message(self, message):
        # Avoid processing bot messages
        if message.author.bot:
            return
        # Trigger when bot is mentioned
        if self.bot.user in message.mentions:
            ctx = await self.bot.get_context(message)
            # Remove the bot mention from the message
            mention_str = f'<@!{self.bot.user.id}>'
            question = message.content.replace(mention_str, 'assistant').strip()
            if question:
                await self.process_askgpt(ctx, question)

    @askgpt.error
    async def askgpt_error(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"You are on cooldown. Try again in {error.retry_after:.2f}s")
        else:
            raise error

async def setup(bot):
    """Every cog needs a setup function like this."""
    await bot.add_cog(Gpt(bot))
