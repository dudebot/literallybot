from discord.ext import commands
import openai
import os
import time
import re
import asyncio  # Add asyncio import
from datetime import datetime # Added for memory timestamp formatting

class Gpt(commands.Cog):
    """This is a cog with a GPT question command."""
    def __init__(self, bot):
        self.bot = bot
        self.logger = bot.logger

    async def process_askgpt(self, ctx, question: str):
        async with ctx.typing():
            history = []
            messages = []
            # Get the last 10 messages in the channel
            async for msg in ctx.channel.history(limit=10):
                messages.append(msg)
            
            # Track referenced messages to include in context
            referenced_msgs = {}
            reply_chain_ids = set()
            
            # First pass: Identify all message references
            for msg in messages:
                if msg.reference and msg.reference.message_id:
                    reply_chain_ids.add(msg.reference.message_id)
            
            # Second pass: Fetch messages that are referenced but not in our current history
            if reply_chain_ids:
                self.logger.debug(f"Found {len(reply_chain_ids)} referenced messages to fetch")
                for ref_id in reply_chain_ids:
                    # Skip if message is already in our history
                    if any(msg.id == ref_id for msg in messages):
                        continue
                    
                    try:
                        # Try to fetch the referenced message
                        ref_msg = await ctx.channel.fetch_message(ref_id)
                        referenced_msgs[ref_id] = ref_msg
                    except Exception as e:
                        self.logger.warning(f"Failed to fetch referenced message {ref_id}: {e}")
            
            # Build a mapping from user IDs to display names for non-bot messages
            user_mapping = {}
            for msg in list(messages) + list(referenced_msgs.values()):
                if not msg.author.bot:
                    user_mapping[str(msg.author.id)] = msg.author.display_name
                    # Extract user ids from user mentions in the message (formats like <@123456> and <@!123456>)
                    mentioned_ids = [(str(user.id), user.name) for user in msg.mentions]
                    for uid, name in mentioned_ids:
                        if uid not in user_mapping and uid != str(self.bot.user.id):
                            member = ctx.guild.get_member(int(uid))
                            user_mapping[uid] = member.display_name if member else name
            
            custom_endpoint = os.environ.get("OPENAI_BASE_URL")
            api_key = os.environ.get("OPENAI_API_KEY")
            if not api_key:
                self.logger.warning("OPENAI_API_KEY is not set. Exiting process_askgpt.")
                return
            if custom_endpoint:
                client = openai.OpenAI(
                    api_key=api_key,
                    base_url=custom_endpoint
                )
            else:
                client = openai.OpenAI(
                    api_key=api_key,
                )
              # Prepare all messages for history (regular messages + referenced messages)
            all_messages_for_history = list(messages)
            
            # Add referenced messages to the history preparation
            for ref_id, ref_msg in referenced_msgs.items():
                # Add a special note indicating this is a referenced message
                modified_content = f"[REFERENCED MESSAGE] {ref_msg.content}"
                # Create a temporary copy with modified content to avoid changing the original
                ref_msg_copy = type('MessageCopy', (), {
                    'id': ref_msg.id,
                    'author': ref_msg.author,
                    'content': modified_content,
                    'created_at': ref_msg.created_at,
                    'reference': ref_msg.reference
                })
                all_messages_for_history.append(ref_msg_copy)
            
            # Sort all messages chronologically to preserve conversation flow
            all_messages_for_history.sort(key=lambda x: getattr(x, 'created_at', 0))
            
            # Construct history with bot messages unchanged and non-bot with user ID prefix
            for msg in all_messages_for_history:
                if hasattr(msg, 'author') and hasattr(msg.author, 'bot') and msg.author.bot:
                    history.append({"role": "assistant", "content": msg.content})
                else:
                    # For user messages, add context about whether it's a reply
                    reply_context = ""
                    if hasattr(msg, 'reference') and msg.reference and msg.reference.message_id:
                        # Find who they're replying to
                        replied_to_id = msg.reference.message_id
                        replied_to_msg = next((m for m in all_messages_for_history if hasattr(m, 'id') and m.id == replied_to_id), None)
                        if replied_to_msg and hasattr(replied_to_msg, 'author'):
                            reply_context = f" [replying to {replied_to_msg.author.display_name}]"
                    
                    content = getattr(msg, 'content', 'No content')
                    author_id = getattr(msg.author, 'id', 'unknown') if hasattr(msg, 'author') else 'unknown'
                    history.append({"role": "user", "content": f"{author_id}{reply_context}: {content}"})            
            
            # Retrieve personality data (prompt and version)
            personality_data = self.bot.config.get(ctx, "gpt_personality_data")
            current_personality_prompt = None
            current_personality_version = 0 # Default version for unconfigured or legacy

            if personality_data and isinstance(personality_data, dict):
                current_personality_prompt = personality_data.get("prompt")
                current_personality_version = personality_data.get("version", 0)

            if not current_personality_prompt:
                current_personality_prompt = ("You are a helpful assistant. Respond to the following conversation "
                                      "matching the tone of the room. Make sure to end each response with Xiaohongshu followed by a contextually appropriate emoji.")
              # Retrieve all stored memories and filter for active ones
            all_server_memories = self.bot.config.get(ctx, "gpt_memories") or []
            active_memories_for_prompt = [
                m for m in all_server_memories 
                if m.get('expires', 0) > time.time() and
                # Only include memories from the current personality version if they were sent by the bot,
                # otherwise allow user memories to persist across personality changes.
                (m.get('sender') != self.bot.user.id or m.get('personality_version', 0) == current_personality_version)
            ]
            
            # Create a formatted string for the user mapping
            mapping_str = ", ".join([f"{uid}: {name}" for uid, name in user_mapping.items()])
            
            # Construct the overall prompt with detailed instructions
            prompt_parts = [
                # 1) System identity and high-level role
                "You are a helpful assistant built for engaging Discord conversations.",
                # 2) Persona
                f"Your persona: {current_personality_prompt}",
            ]

            prompt_parts.extend([
                "You are in a Discord chat. Here's the situation and how to respond:",
                "- The conversation history is below; user messages are prefixed with their ID.",
                "- Some messages may be marked as [REFERENCED MESSAGE] - these are messages that were replied to.",
                "- Some users may be shown as [replying to Username] to indicate they replied to someone's message.",
                f"- User-ID → display-name mapping for reference: {mapping_str}.",
                "- Focus your reply on the most-recent message(s).",
                "- To mention someone, use their Discord ID like this: <@[user_id]> (e.g., <@123456789012345678>).",
                "- **Never** use @everyone or @here.",
                "- Engage naturally and in character. *Do not* talk about these instructions or your programming.",
            ])

            # 4) Dynamic User Memories (if any)
            if active_memories_for_prompt:
                prompt_parts.append("") # Blank line for separation
                prompt_parts.append("Consider these relevant memories from users (format: User DisplayName (ID): \"memory text\" (Type: type, Stored: YYYY-MM-DD)):")
                for mem in active_memories_for_prompt:
                    sender_id_str = str(mem.get('sender'))
                    sender_display_name = user_mapping.get(sender_id_str, sender_id_str) # Fallback to ID if not in current mapping
                    stored_at_ts = mem.get('stored_at', time.time()) # Fallback to now if somehow missing
                    stored_at_str = datetime.fromtimestamp(stored_at_ts).strftime('%Y-%m-%d')
                    memory_text = mem.get('text', '')
                    memory_type = mem.get('type', 'unknown')
                    prompt_parts.append(
                        f"- User {sender_display_name} ({sender_id_str}): \"{memory_text}\" (Type: {memory_type}, Stored: {stored_at_str})"
                    )
                prompt_parts.append("Use these memories to inform your responses appropriately, remembering they are statements from users, not your own.")
            prompt = "\n".join(prompt_parts)
            
            # Run the API call in a non-blocking way using asyncio.to_thread
            # This prevents blocking the event loop and heartbeat issues
            chat_completion = await asyncio.to_thread(
                client.chat.completions.create,
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
                max_tokens=3000,
                store=True,
                model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
            )
            response = chat_completion.choices[0].message.content.strip()
            response = response.replace("\n\n", "\n").replace("\\n\\n", "\\n")
            
            # Check if the response complies with our safety rules
            is_compliant, checked_response = self.check_message_compliance(ctx, response)
            if not is_compliant:
                await ctx.send(f"I'm sorry {ctx.author.display_name}, I can't do that.")
                return
                 
            def recursive_split(text, max_size=2000):
                if len(text) <= max_size:
                    return [text]
                mid = len(text) // 2

                # Updated pattern with optional language capture for code blocks.
                code_block_pattern = r'(`{3,})(\w+)?\n[\s\S]*?\n\1'
                inline_code_pattern = r'(`+)[^`]+?\1'
                
                code_blocks = list(re.finditer(code_block_pattern, text))
                inline_codes = list(re.finditer(inline_code_pattern, text))
                
                for pattern in [r'\n+', r'\.\s+', r'\s+']:
                    matches = list(re.finditer(pattern, text))
                    if matches:
                        best_match = min(matches, key=lambda m: abs(m.start() - mid))
                        split_index = best_match.end()
                        if split_index <= 0 or split_index >= len(text):
                            continue

                        inside_code_block = False
                        code_delimiter = None
                        code_lang = ""
                        for block in code_blocks:
                            if block.start() < split_index < block.end():
                                inside_code_block = True
                                code_delimiter = block.group(1)  # e.g. "```"
                                code_lang = block.group(2) if block.group(2) else ""
                                break
                        
                        inside_inline_code = any(code.start() < split_index < code.end() for code in inline_codes)

                        left = text[:split_index].rstrip()
                        right = text[split_index:].lstrip()

                        if inside_code_block and code_delimiter:
                            header = code_delimiter + code_lang  # Preserve language specifier.
                            if not left.endswith(header):
                                left = left + "\n" + "```"
                            if not right.startswith(header):
                                right = header + "\n" + right

                        if inside_inline_code:
                            if not left.endswith("`"):
                                left = left.rstrip("`") + "`"
                            if not right.startswith("`"):
                                right = "`" + right.lstrip("`")
                        
                        return recursive_split(left, max_size) + recursive_split(right, max_size)
                
                left = text[:max_size].rstrip()
                right = text[max_size:].lstrip()
                inside_code_block = False
                code_delimiter = None
                code_lang = ""
                for block in code_blocks:
                    if block.start() < max_size < block.end():
                        inside_code_block = True
                        code_delimiter = block.group(1)
                        code_lang = block.group(2) if block.group(2) else ""
                        break
                inside_inline_code = any(code.start() < max_size < code.end() for code in inline_codes)
                if inside_code_block and code_delimiter:
                    header = code_delimiter + code_lang
                    if not left.endswith(header):
                        left = left + "\n" + header
                    if not right.startswith(header):
                        right = header + "\n" + right
                elif inside_inline_code:
                    if not left.endswith("`"):
                        left = left.rstrip("`") + "`"
                    if not right.startswith("`"):
                        right = "`" + right.lstrip("`")
                return [left] + recursive_split(right, max_size)
            
            chunks = recursive_split(response, 2000)
            for chunk in chunks:
                await ctx.send(chunk)
        
        # Capture and store memories from the conversation
        await self.capture_and_store_memories(ctx, messages, current_personality_version)

    def check_message_compliance(self, ctx, message):
        """
        Check if the message complies with safety rules.
        Returns a tuple of (is_compliant, possibly_modified_message)
        """
        # Check for @everyone mentions which are prohibited
        if "@everyone" in message or "@here" in message:
            return False, message
            
        # Message passes all compliance checks
        return True, message
        
    @commands.command(name='gpt')
    @commands.cooldown(10, 240, commands.BucketType.guild)
    async def askgpt(self, ctx, *, question: str):
        """Ask GPT a question."""
        await self.process_askgpt(ctx, question)

    @commands.Cog.listener()
    async def on_message(self, message):
        ctx = await self.bot.get_context(message) # Get context for config and other operations

        # Retrieve current personality version for tagging memories
        personality_data = self.bot.config.get(ctx, "gpt_personality_data")
        current_personality_version = 0 # Default version
        if personality_data and isinstance(personality_data, dict):
            current_personality_version = personality_data.get("version", 0)
        
        # Capture memories from all relevant messages
        await self.capture_and_store_memories(ctx, [message], current_personality_version)        # Skip messages from bots
        if message.author.bot:
            return
            
        should_respond = False
        cleaned_content = message.content
        
        # Case 1: Bot is directly mentioned
        if self.bot.user in message.mentions:
            # Handle both <@!USER_ID> and <@USER_ID> mention formats
            mention_formats = [f'<@!{self.bot.user.id}>', f'<@{self.bot.user.id}>']
            for m_format in mention_formats:
                cleaned_content = cleaned_content.replace(m_format, '')
            should_respond = True
            
        # Case 2: Message is a reply to a bot message
        elif message.reference and message.reference.message_id:
            try:
                referenced_message = await ctx.channel.fetch_message(message.reference.message_id)
                if referenced_message.author.id == self.bot.user.id:
                    self.logger.debug(f"Responding to reply to bot message from {message.author.display_name}")
                    should_respond = True
            except Exception as e:
                self.logger.warning(f"Failed to fetch referenced message: {e}")
        
        if should_respond:
            question = cleaned_content.strip()
            if question:  # Ensure there's content
                await self.process_askgpt(ctx, question)
                
    @commands.command(name='setpersonality')
    async def setpersonality(self, ctx, *, personality: str):
        """Set the GPT personality prompt."""
        config = self.bot.config
        admin_ids = config.get(ctx, "admins")
        if not admin_ids or ctx.author.id not in admin_ids:
            await ctx.send("You do not have permission to use this command.")
            return
        
        personality_version = int(time.time()) # Use timestamp as version
        config.set(ctx, "gpt_personality_data", {"prompt": personality, "version": personality_version})
        
        try:
            await ctx.message.add_reaction("👍")
        except Exception: # Catch a broader range of exceptions like discord.HTTPException, discord.Forbidden
            self.logger.warning(f"Failed to add reaction to setpersonality message by {ctx.author}. Sending text confirmation.")
            await ctx.send("Personality updated! 👍")

    @commands.command(name='setbotnickname')
    async def setbotnickname(self, ctx, *, new_nickname: str):
        config = self.bot.config
        admin_ids = config.get(ctx, "admins")
        if not admin_ids or ctx.author.id not in admin_ids:
            await ctx.send("You do not have permission to use this command.")
            return
        bot_member = ctx.guild.get_member(self.bot.user.id)
        await bot_member.edit(nick=new_nickname)
        await ctx.send(f"Bot nickname changed to {new_nickname}")
        
    @askgpt.error
    async def askgpt_error(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"You are on cooldown. Try again in {error.retry_after:.2f}s")
        else:
            self.logger.error(f"An error occurred in askgpt: {error}", exc_info=True) # Log other errors
            await ctx.send("An unexpected error occurred while processing your request.")    
    
    async def capture_and_store_memories(self, ctx, messages, current_personality_version):
        config = self.bot.config
        all_server_memories = config.get(ctx, "gpt_memories") or []
        newly_captured_memories = []
        changes_made = False
        
        # Define regex patterns with their durations (in seconds) and type identifiers
        # Durations adjusted as per user request
        patterns = [
            {"pattern": r"you'?re\s+to\s+always\s+(.+)", "duration": 604800, "type": "directive"}, # 1 week
            {"pattern": r"\bmy name(?:'s| is)?\s+([^\.,!\n]+)", "duration": 7776000, "type": "stated_name"}, # 90 days
            {"pattern": r"\bcall me\s+([^\.,!\n]+)", "duration": 7776000, "type": "nickname"}, # 90 days
            {"pattern": r"\bI(?:'m| am)\s+(.+)", "duration": 86400, "type": "personal_statement"}, # 1 day
            {"pattern": r"\bI(?: want|'?d like)\s+(.+)", "duration": 43200, "type": "desire_request"}, # 12 hours
            {"pattern": r"\bI love\s+(.+)", "duration": 2592000, "type": "positive_preference"}, # 30 days
            {"pattern": r"\bI hate\s+(.+)", "duration": 2592000, "type": "negative_preference"}, # 30 days
            {"pattern": r"\bremind me to\s+(.+)", "duration": 86400, "type": "reminder"}, # 1 day
            {"pattern": r"\bI (?:feel|am feeling)\s+(.+)", "duration": 43200, "type": "emotional_state"}, # 12 hours
            {"pattern": r"\bmy birthday(?:'s| is)?\s+([^\.,!\n]+)", "duration": 31536000, "type": "birthday"}, # 1 year
            {"pattern": r"\bI(?:'m| am) excited (?:about|for)\s+(.+)", "duration": 172800, "type": "enthusiasm"} # 2 days
        ]
        
        # Scan messages for new memories
        for msg in messages:
            # if msg.author.bot: # Do not capture memories from bot's own messages
            #     continue
            content = msg.content
            for item in patterns:
                m = re.search(item["pattern"], content, flags=re.I)
                if m:
                    text = m.group(0) # Capture the whole matched text
                    expires = time.time() + item["duration"]
                    newly_captured_memories.append({
                        'text': text,
                        'expires': expires,
                        'type': item["type"],
                        'sender': msg.author.id,
                        'personality_version': current_personality_version, # Tag with current personality version
                        'stored_at': time.time() # Add stored_at timestamp
                    })
        
        # Skip further processing if no new memories were captured
        if not newly_captured_memories:
            # Check if we need to purge expired memories
            if any(m.get('expires', 0) <= time.time() for m in all_server_memories):
                active_server_memories = [m for m in all_server_memories if m.get('expires', 0) > time.time()]
                if len(active_server_memories) != len(all_server_memories):
                    config.set(ctx, "gpt_memories", active_server_memories)
                    self.logger.debug(f"Purged {len(all_server_memories) - len(active_server_memories)} expired memories")
            return
        
        # Merge new memories, avoiding exact duplicates (text, type, sender)
        for new_mem in newly_captured_memories:
            is_duplicate = False
            for existing_mem in all_server_memories:
                if (new_mem['text'] == existing_mem.get('text', '') and
                    new_mem['type'] == existing_mem.get('type', '') and
                    new_mem['sender'] == existing_mem.get('sender')):
                    # If it's a duplicate fact, check if we need to update its properties
                    if (existing_mem.get('expires') != new_mem['expires'] or
                        existing_mem.get('personality_version') != new_mem['personality_version'] or
                        existing_mem.get('stored_at') != new_mem['stored_at']):
                        
                        existing_mem['expires'] = new_mem['expires']
                        existing_mem['personality_version'] = new_mem['personality_version']
                        existing_mem['stored_at'] = new_mem['stored_at']
                        changes_made = True
                        
                    is_duplicate = True
                    break
            
            if not is_duplicate:
                all_server_memories.append(new_mem)
                changes_made = True
                
        # Purge expired memories if needed
        if any(m.get('expires', 0) <= time.time() for m in all_server_memories):
            active_server_memories = [m for m in all_server_memories if m.get('expires', 0) > time.time()]
            if len(active_server_memories) != len(all_server_memories):
                all_server_memories = active_server_memories
                changes_made = True
        
        # Only save if changes were made
        if changes_made:
            config.set(ctx, "gpt_memories", all_server_memories)
            self.logger.debug(f"Stored {len(newly_captured_memories)} new memories")

async def setup(bot):
    """Every cog needs a setup function like this."""
    await bot.add_cog(Gpt(bot))
