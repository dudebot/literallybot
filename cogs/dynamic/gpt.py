from discord.ext import commands
import openai
import os

from config import Config

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
        
        custom_endpoint = os.environ.get("OPENAI_BASE_URL")
        if custom_endpoint:
            client = openai.OpenAI(
                api_key=os.environ.get("OPENAI_API_KEY"),
                base_url=custom_endpoint
            )
        else:
            client = openai.OpenAI(
                api_key=os.environ.get("OPENAI_API_KEY"),
            )
        
        for msg in reversed(messages):
            if msg.author.bot:
                history.append({"role": "assistant", "content": msg.content})
            else:
                history.append({"role": "user", "content": f"{msg.author.display_name}: {msg.content}"})            
        # Get the bot's nickname in the current server or fall back to the username
        bot_name = ctx.message.guild.me.nick if ctx.message.guild.me.nick is not None else self.bot.user.name
        
        prompt = Config(ctx).get("gpt_prompt")
        if not prompt:
            prompt = f"You are a helpful assistant. Respond to the following conversation matching the tone of the room. Make sure to end each response with Xiaohongshu followed by a contextually appropriate emoji."
        
        chat_completion = client.chat.completions.create(
            messages=[
            {
                "role": "system",
                "content": prompt
            },
            *history
            ],
            metadata={
                "service": "literallybot",
                "sender": str(ctx.author.id),
                "channel": str(ctx.channel.id),
                "guild": str(ctx.guild.id)
            },
            max_tokens=800,
            store=True,
            model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        )
        response = chat_completion.choices[0].message.content.strip()
        response = response.replace("\n\n", "\n").replace("\\n\\n", "\\n")
        chunks = [response[i:i+2000] for i in range(0, len(response), 2000)]
        for chunk in chunks:
            await ctx.send(chunk)

    @commands.command(name='gpt')
    @commands.cooldown(10, 240, commands.BucketType.guild)
    async def askgpt(self, ctx, *, question: str):
        """Ask GPT a question."""
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
                
    @commands.command(name='setpersonality')
    async def setpersonality(self, ctx, *, personality: str):
        """Set the GPT personality prompt."""
        config = Config(ctx)
        admin_ids = config.get("admins")
        if not admin_ids or ctx.author.id not in admin_ids:
            await ctx.send("You do not have permission to use this command.")
            return
        config.set("gpt_prompt", personality)
        await ctx.send("GPT personality prompt updated.")

    @askgpt.error
    async def askgpt_error(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"You are on cooldown. Try again in {error.retry_after:.2f}s")
        else:
            raise error

async def setup(bot):
    """Every cog needs a setup function like this."""
    await bot.add_cog(Gpt(bot))
