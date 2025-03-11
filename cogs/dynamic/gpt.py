from discord.ext import commands
import openai
import os
import time
import re

from config import Config

class Gpt(commands.Cog):
    """This is a cog with a GPT question command."""
    def __init__(self, bot):
        self.bot = bot

    async def process_askgpt(self, ctx, question: str):
        history = []
        messages = []
        async for msg in ctx.channel.history(limit=10):
            messages.append(msg)
        
        # Build a mapping from user IDs to display names for non-bot messages
        user_mapping = {}
        for msg in messages:
            if not msg.author.bot:
                user_mapping[str(msg.author.id)] = msg.author.display_name
                # Extract user ids from user mentions in the message (formats like <@123456> and <@!123456>)
                mentioned_ids = [(str(user.id), user.name)  for user in msg.mentions]
                for uid, name in mentioned_ids:
                    if uid not in user_mapping and uid != str(self.bot.user.id):
                        member = ctx.guild.get_member(int(uid))
                        user_mapping[uid] = member.display_name if member else name
        
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
        
        # Construct history with bot messages unchanged and non-bot with user ID prefix
        for msg in reversed(messages):
            if msg.author.bot:
                history.append({"role": "assistant", "content": msg.content})
            else:
                history.append({"role": "user", "content": f"{msg.author.id}: {msg.content}"})            
        
        # Retrieve personality prompt; use a default if not set
        personality_prompt = Config(ctx).get("gpt_prompt")
        if not personality_prompt:
            personality_prompt = ("You are a helpful assistant. Respond to the following conversation "
                                  "matching the tone of the room. Make sure to end each response with Xiaohongshu followed by a contextually appropriate emoji.")
        
        # Retrieve saved context tidbits and build additional context string
        context_tidbits = Config(ctx).get("gpt_context_tidbits") or []
        # Filter out expired tidbits
        context_tidbits = [t for t in context_tidbits if t.get('expires', 0) > time.time()]
        tidbits_str = " ".join(t.get('text', '') for t in context_tidbits)
        additional_context = f" Additional context: {tidbits_str}" if tidbits_str else ""
        
        # Create a formatted string for the user mapping
        mapping_str = ", ".join([f"{uid}: {name}" for uid, name in user_mapping.items()])
        
        # Construct the overall prompt with detailed instructions
        prompt = (
            "You are a helpful assistant built for engaging Discord conversations. "
            "Below is the conversation history where each non-bot message is prefixed by the user's ID. "
            "A mapping of user IDs to their display names is provided for reference: "
            f"{mapping_str}. "
            "Prefer to respond only to the most recent message in the history. "
            "If you need to mention a user to get their attention, use the Discord text format <@[user_id]>. "
            "User-specified personality details follow: "
            f"{personality_prompt}{additional_context}"
        )
        
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
        
        # Update context by auto summarizing important tidbits from conversation
        #todo should be a little more comprehensive, and essentially set dynamic context window via summarizations as well
        #await self.auto_summarize_history(ctx, messages)

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
            await ctx.send("You do not have permission to use this command.", delete_after=10)
            await ctx.message.delete()
            return
        config.set("gpt_prompt", personality)
        response = await ctx.send(f"The current personality is now: {personality}", delete_after=10)
        await ctx.message.delete()

    @askgpt.error
    async def askgpt_error(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"You are on cooldown. Try again in {error.retry_after:.2f}s")
        else:
            raise error

    async def auto_summarize_history(self, ctx, messages):
        # Scans messages for important tidbits and saves them with an expiration timestamp.
        config = Config(ctx)
        existing_tidbits = config.get("gpt_context_tidbits") or []
        new_tidbits = []
        for msg in messages:
            content = msg.content
            # High priority tidbit extraction (long expiration)
            m = re.search(r"you'?re\s+to\s+always\s+(.+)", content, flags=re.I)
            if m:
                text = m.group(0)
                expires = time.time() + 604800  # 7 days
                new_tidbits.append({'text': text, 'expires': expires})
            # Lower priority tidbit extraction (shorter expiration)
            m2 = re.search(r"\bI(?:'m| am)\s+(.+)", content, flags=re.I)
            if m2:
                text = m2.group(0)
                expires = time.time() + 86400  # 1 day
                new_tidbits.append({'text': text, 'expires': expires})
        # Merge new tidbits, avoiding duplicates
        for nt in new_tidbits:
            if not any(nt['text'] == t.get('text', '') for t in existing_tidbits):
                existing_tidbits.append(nt)
        config.set("gpt_context_tidbits", existing_tidbits)

async def setup(bot):
    """Every cog needs a setup function like this."""
    await bot.add_cog(Gpt(bot))
