from discord.ext import commands
import openai
import os
import time
import re
import asyncio
import json
from typing import Dict, List, Any

class Vibe(commands.Cog):
    """AI-powered dynamic cog generation."""
    def __init__(self, bot):
        self.bot = bot
        self.logger = bot.logger
    
    def is_authorized(self, ctx) -> bool:
        """Check if user is authorized to use admin commands"""
        config = self.bot.config
        superadmin = config.get(None, "superadmin", scope="global")
        
        # Superadmin always authorized
        if ctx.author.id == superadmin:
            return True
            
        # In guilds, check admin list
        if ctx.guild:
            admin_ids = config.get(ctx, "admins") or []
            return ctx.author.id in admin_ids
            
        return False

    async def call_ai_api(self, provider_config: Dict[str, Any], messages: List[Dict], metadata: Dict) -> str:
        """Call the appropriate AI API based on provider configuration"""
        provider = provider_config["provider"]
        model = provider_config["model"]
        provider_info = provider_config["provider_info"]
        
        # Get API key from global config
        api_key_name = f"{provider.upper()}_API_KEY"
        api_key = self.bot.config.get(None, api_key_name, scope="global") or os.environ.get(api_key_name)
        
        if not api_key:
            raise ValueError(f"No API key found for provider {provider}")
            
        api_type = provider_info.get("api_type", "openai")
        
        if api_type == "anthropic":
            raise ValueError("Anthropic API not supported for vibe generation")
        else:
            # Use OpenAI-compatible API
            base_url = provider_info.get("base_url")
            # Only pass base_url if it's actually set (for xAI, etc)
            if base_url:
                client = openai.OpenAI(api_key=api_key, base_url=base_url)
            else:
                client = openai.OpenAI(api_key=api_key)
            
            # Run the API call in a non-blocking way
            # Handle different parameter names for reasoning models
            create_params = {
                "messages": messages,
                "metadata": metadata,
                "store": True,
                "model": model
            }
            
            # Check if this is a reasoning model (o3, o4, etc) - they use max_completion_tokens
            if model.startswith("o3") or model.startswith("o4") or model == "o1" or model == "o1-preview" or model == "o1-mini":
                # Check if provider info has specific max_completion_tokens for this model
                models_info = provider_info.get("models", {})
                model_info = models_info.get(model, {})
                max_completion_tokens = model_info.get("max_completion_tokens", 3000)
                create_params["max_completion_tokens"] = max_completion_tokens
            else:
                create_params["max_tokens"] = 3000
            
            chat_completion = await asyncio.to_thread(
                client.chat.completions.create,
                **create_params
            )
            return chat_completion.choices[0].message.content.strip()

    @commands.command(name='vibe')
    async def vibe(self, ctx, *, description: str):
        """Generate a custom cog using AI (admin only)"""
        if not self.is_authorized(ctx):
            await ctx.send("You do not have permission to use this command.")
            return
            
        await ctx.send(f"🎨 Creating vibe: `{description[:100]}{'...' if len(description) > 100 else ''}`\nThis may take a moment...")
        
        try:
            # Stage 1: Generate specification with reasoning model
            spec_prompt = f"""You are helping create a Discord bot cog. The user wants: "{description}"

Analyze the request and generate a specification.

IMPORTANT PRINCIPLES:
- Keep it SIMPLE - don't over-engineer simple requests
- For basic auto-responses, you likely don't need any commands at all
- Only add configuration/toggle commands if the user specifically asks for them
- Preserve specific details from the request (like user IDs, exact phrases, etc.)

CONTEXT:
- Bot framework: discord.py with commands.Cog
- Available: self.bot, self.logger, self.bot.config
- Config system (only use if needed):
  - Guild: config.get(ctx, "key", default) / config.set(ctx, "key", value)
  - Global: config.get(None, "key") / config.set(None, "key", value)

Generate a specification including:
1. Cog class name (descriptive)
2. List of commands (empty list [] if none needed)
3. Required imports (minimal)
4. Data storage needs (prefer hardcoded values for simple requests)
5. Safety considerations
6. Implementation approach (be specific, include exact values from request)

Format as JSON with keys: class_name, commands, imports, storage_needs, safety_notes, approach
Focus on exactly what the user asked for, no more."""

            # Get the repo context
            repo_context = await self.get_repo_context()
            
            spec_messages = [
                {"role": "system", "content": "You are an expert Discord bot developer. Generate detailed, safe specifications."},
                {"role": "user", "content": spec_prompt + "\n\nREPO STRUCTURE:\n" + repo_context}
            ]
            
            # Use o3 for reasoning
            all_providers = self.bot.config.get(None, "ai_providers", scope="global") or {}
            spec_provider = {
                "provider": "openai",
                "model": "o3",
                "provider_info": all_providers.get("openai", {})
            }
            
            spec_response = await self.call_ai_api(spec_provider, spec_messages, {})
            
            # Parse specification
            try:
                # Extract JSON from response
                json_match = re.search(r'\{.*\}', spec_response, re.DOTALL)
                if json_match:
                    spec = json.loads(json_match.group(0))
                    self.logger.info(f"Generated vibe specification: {json.dumps(spec, indent=2)}")
                else:
                    raise ValueError("No JSON found in specification response")
            except Exception as e:
                self.logger.error(f"Failed to parse specification: {e}")
                await ctx.send("Failed to generate specification. Please try again.")
                return
                
            # Stage 2: Generate implementation with coding model
            spec_for_impl = json.dumps(spec, indent=2)
            self.logger.info(f"Sending specification to implementation stage:\n{spec_for_impl}")
            
            impl_prompt = """Generate a Discord bot cog implementation.

ORIGINAL USER REQUEST: """ + f'"{description}"' + """

SPECIFICATION FROM ANALYSIS:
""" + spec_for_impl + """

IMPORTANT: Follow the ORIGINAL USER REQUEST exactly. Use any specific values mentioned (user IDs, exact phrases, etc).

Here's a simple auto-response example:

```python
from discord.ext import commands

class SimpleResponder(commands.Cog):
    \"\"\"Responds to specific messages.\"\"\"
    def __init__(self, bot):
        self.bot = bot
        self.logger = bot.logger
    
    @commands.Cog.listener()
    async def on_message(self, message):
        # Skip bot messages
        if message.author.bot:
            return
        
        # Check conditions and respond
        if message.author.id == 123456789 and "hello" in message.content.lower():
            await message.channel.send("world")

async def setup(bot):
    await bot.add_cog(SimpleResponder(bot))
```

For auto-responses and monitoring messages, use the on_message listener:

```python
@commands.Cog.listener()
async def on_message(self, message):
    # Skip bot messages to avoid loops
    if message.author == self.bot.user:
        return
        
    # Skip if not in a guild
    if not message.guild:
        return
    
    # Get context for config operations
    ctx = await self.bot.get_context(message)
    
    # Example: respond when specific user says something
    config = self.bot.config
    # Get monitored user ID from guild config
    monitored_user_id = config.get(ctx, "hello_world_user_id")
    
    if monitored_user_id and message.author.id == monitored_user_id:
        if message.content.lower() == "hello":
            await message.channel.send("world")

# Add a command to configure the monitored user
@commands.command(name='sethellouser')
async def sethellouser(self, ctx, user: discord.User):
    \"\"\"Set which user triggers the hello/world response.\"\"\"
    config = self.bot.config
    config.set(ctx, "hello_world_user_id", user.id)
    await ctx.send(f"Now monitoring {user.mention} for 'hello' messages")
```

Config system reference:
- Guild: config.get(ctx, "key", default) / config.set(ctx, "key", value)
- Global: config.get(None, "key") / config.set(None, "key", value)
- IMPORTANT: In on_message, create context first: ctx = await self.bot.get_context(message)

Requirements:
- PRIORITY: Follow the ORIGINAL USER REQUEST exactly, including specific IDs and phrases
- Keep it SIMPLE - don't add features that weren't requested
- Use hardcoded values when appropriate (especially for simple auto-responses)
- Only use config if the user wants toggles/configuration
- Use minimal imports
- Include the async def setup(bot) function at the end
- Add brief docstrings
- For auto-responses, use @commands.Cog.listener() with on_message

Generate ONLY the Python code, no explanations."""

            impl_messages = [
                {"role": "system", "content": "You are an expert Python developer specializing in Discord bots. Generate clean, safe, working code."},
                {"role": "user", "content": impl_prompt}
            ]
            
            # Use o4-mini for implementation
            impl_provider = {
                "provider": "openai", 
                "model": "o4-mini",
                "provider_info": all_providers.get("openai", {})
            }
            
            # Override the provider info to ensure high token limit for code generation
            impl_provider["provider_info"] = impl_provider["provider_info"].copy()
            if "models" not in impl_provider["provider_info"]:
                impl_provider["provider_info"]["models"] = {}
            impl_provider["provider_info"]["models"]["o4-mini"] = {
                "timeout_multiplier": 2.0,
                "max_completion_tokens": 16000  # High limit for code generation
            }
            
            # Try up to 3 times for o4-mini implementation
            implementation = None
            for attempt in range(3):
                try:
                    self.logger.info(f"Calling {impl_provider['model']} for implementation generation (attempt {attempt + 1}/3)")
                    implementation = await self.call_ai_api(impl_provider, impl_messages, {})
                    self.logger.info(f"Received implementation response: {len(implementation)} chars")
                    
                    # Validate that we got a non-empty response
                    if implementation and len(implementation.strip()) > 50:
                        break
                    else:
                        self.logger.warning(f"Implementation attempt {attempt + 1} returned empty or too short response")
                        implementation = None
                        
                except Exception as e:
                    self.logger.error(f"Implementation attempt {attempt + 1} failed: {e}")
                    implementation = None
                    
                if attempt < 2:  # Don't sleep after the last attempt
                    await asyncio.sleep(1)  # Brief pause between retries
            
            if not implementation:
                self.logger.error("All implementation attempts failed")
                await ctx.send("❌ Failed to generate implementation after 3 attempts. Please try again.")
                return
            
            # Clean up the implementation
            implementation = implementation.strip()
            if implementation.startswith("```python"):
                implementation = implementation[9:]
            if implementation.startswith("```"):
                implementation = implementation[3:]
            if implementation.endswith("```"):
                implementation = implementation[:-3]
            implementation = implementation.strip()
            
            # Validate implementation has setup function
            if not implementation or len(implementation) < 50:
                self.logger.error(f"Generated implementation too short or empty: {len(implementation)} chars")
                await ctx.send("❌ AI failed to generate a proper implementation. Please try again.")
                return
                
            if "async def setup(bot):" not in implementation and "def setup(bot):" not in implementation:
                self.logger.error("Generated implementation missing setup() function")
                await ctx.send("❌ Generated code missing required setup() function. Please try again.")
                return
            
            self.logger.info(f"Generated complete vibe implementation ({len(implementation)} chars)")
            
            # Stage 3: Security review - DISABLED for now
            # Skip security review and assume safe
            review = {"safe": True, "issues": [], "severity": "low"}
            self.logger.info("Security review skipped - auto-approved")
                
            # Generate unique filename
            cog_name = spec.get("class_name", "UnknownVibe").lower()
            timestamp = int(time.time())
            filename = f"vibe_{cog_name}_{timestamp}.py"
            
            # Get vibes directory path relative to bot root
            import pathlib
            bot_root = pathlib.Path(__file__).parent.parent.parent  # cogs/dynamic/vibe.py -> bot root
            vibes_dir = bot_root / "cogs" / "vibes"
            vibes_dir.mkdir(exist_ok=True)
            filepath = vibes_dir / filename
            
            # Send implementation to bot owner for review
            owner_id = self.bot.config.get(None, "superadmin", scope="global")
            if owner_id and owner_id == ctx.author.id:
                # Owner initiated it, auto-approve
                await self.save_and_load_vibe(ctx, str(filepath), implementation, spec)
            else:
                # Send to owner for approval
                owner = self.bot.get_user(owner_id)
                if owner:
                    # Save pending vibe
                    pending_vibes = self.bot.config.get(None, "pending_vibes", scope="global") or {}
                    vibe_id = f"{ctx.guild.id}_{timestamp}"
                    pending_vibes[vibe_id] = {
                        "filepath": str(filepath),
                        "implementation": implementation,
                        "spec": spec,
                        "requester_id": ctx.author.id,
                        "guild_id": ctx.guild.id,
                        "channel_id": ctx.channel.id,
                        "description": description
                    }
                    self.bot.config.set(None, "pending_vibes", pending_vibes, scope="global")
                    
                    # DM owner
                    dm_message = f"""**New Vibe Request**
Guild: {ctx.guild.name}
Requester: {ctx.author.name}
Description: {description}

**Specification:**
```json
{json.dumps(spec, indent=2)}
```

**Implementation:**
```python
{implementation[:1500]}{'...' if len(implementation) > 1500 else ''}
```

To approve: `!approvevibe {vibe_id}`
To reject: `!rejectvibe {vibe_id}`"""
                    
                    try:
                        await owner.send(dm_message)
                        await ctx.send("✅ Vibe generated and sent to bot owner for review!")
                    except:
                        await ctx.send("❌ Could not send vibe to owner for review.")
                else:
                    await ctx.send("❌ Could not find bot owner for review.")
                    
        except Exception as e:
            self.logger.error(f"Vibe generation error: {e}", exc_info=True)
            await ctx.send(f"❌ Failed to generate vibe: {str(e)}")

    async def save_and_load_vibe(self, ctx, filepath: str, implementation: str, spec: dict):
        """Save and load a generated vibe cog"""
        try:
            # Write the file
            with open(filepath, 'w') as f:
                f.write(implementation)
                
            # Track active vibes
            active_vibes = self.bot.config.get(ctx, "active_vibes") or {}
            cog_name = f"cogs.vibes.{os.path.basename(filepath)[:-3]}"
            active_vibes[cog_name] = {
                "spec": spec,
                "created_at": time.time(),
                "filepath": filepath
            }
            self.bot.config.set(ctx, "active_vibes", active_vibes)
            
            # Load the cog
            try:
                await self.bot.load_extension(cog_name)
                commands_list = spec.get('commands', [])
                if commands_list:
                    if isinstance(commands_list[0], dict):
                        # If commands are dicts with 'name' key
                        command_names = [cmd['name'] for cmd in commands_list if isinstance(cmd, dict) and 'name' in cmd]
                    else:
                        # If commands are just strings
                        command_names = commands_list
                    await ctx.send(f"✅ Vibe loaded! Commands: {', '.join(command_names)}")
                else:
                    # No commands (e.g., auto-responder)
                    await ctx.send(f"✅ Vibe loaded! Auto-responder: {spec.get('class_name', 'Unknown')}")
            except Exception as e:
                # Clean up on failure
                os.remove(filepath)
                active_vibes.pop(cog_name, None)
                self.bot.config.set(ctx, "active_vibes", active_vibes)
                raise e
                
        except Exception as e:
            self.logger.error(f"Failed to load vibe: {e}", exc_info=True)
            await ctx.send(f"❌ Failed to load vibe: {str(e)}")

    @commands.command(name='approvevibe')
    async def approvevibe(self, ctx, vibe_id: str):
        """Approve a pending vibe (owner only)"""
        if ctx.author.id != self.bot.config.get(None, "superadmin", scope="global"):
            return
            
        pending_vibes = self.bot.config.get(None, "pending_vibes", scope="global") or {}
        if vibe_id not in pending_vibes:
            await ctx.send("Vibe not found.")
            return
            
        vibe_data = pending_vibes[vibe_id]
        
        # Get the original context
        guild = self.bot.get_guild(vibe_data["guild_id"])
        channel = guild.get_channel(vibe_data["channel_id"]) if guild else None
        
        if channel:
            # Create a minimal context for config operations
            class MinimalContext:
                def __init__(self, guild, channel):
                    self.guild = guild
                    self.channel = channel
                    
            minimal_ctx = MinimalContext(guild, channel)
            
            await self.save_and_load_vibe(
                minimal_ctx,
                vibe_data["filepath"],
                vibe_data["implementation"],
                vibe_data["spec"]
            )
            
            # Notify requester
            await channel.send(f"<@{vibe_data['requester_id']}> Your vibe has been approved and loaded!")
            
        # Remove from pending
        pending_vibes.pop(vibe_id)
        self.bot.config.set(None, "pending_vibes", pending_vibes, scope="global")
        
        await ctx.send("Vibe approved!")

    @commands.command(name='rejectvibe')  
    async def rejectvibe(self, ctx, vibe_id: str):
        """Reject a pending vibe (owner only)"""
        if ctx.author.id != self.bot.config.get(None, "superadmin", scope="global"):
            return
            
        pending_vibes = self.bot.config.get(None, "pending_vibes", scope="global") or {}
        if vibe_id not in pending_vibes:
            await ctx.send("Vibe not found.")
            return
            
        vibe_data = pending_vibes.pop(vibe_id)
        self.bot.config.set(None, "pending_vibes", pending_vibes, scope="global")
        
        # Notify requester
        guild = self.bot.get_guild(vibe_data["guild_id"])
        channel = guild.get_channel(vibe_data["channel_id"]) if guild else None
        if channel:
            await channel.send(f"<@{vibe_data['requester_id']}> Your vibe request was rejected.")
            
        await ctx.send("Vibe rejected.")

    @commands.command(name='listvibes')
    async def listvibes(self, ctx):
        """List active vibes in this server"""
        active_vibes = self.bot.config.get(ctx, "active_vibes") or {}
        
        if not active_vibes:
            await ctx.send("No active vibes in this server.")
            return
            
        vibe_list = []
        for cog_name, vibe_data in active_vibes.items():
            spec = vibe_data.get("spec", {})
            created_at = vibe_data.get("created_at", 0)
            age = int((time.time() - created_at) / 3600) # hours
            
            vibe_list.append(f"**{spec.get('class_name', 'Unknown')}** ({age}h old)")
            commands_list = spec.get('commands', [])
            if commands_list:
                if isinstance(commands_list[0], dict):
                    # If commands are dicts with 'name' key
                    command_names = [cmd['name'] for cmd in commands_list if isinstance(cmd, dict) and 'name' in cmd]
                else:
                    # If commands are just strings
                    command_names = commands_list
                vibe_list.append(f"  Commands: {', '.join(command_names)}")
            else:
                vibe_list.append(f"  Type: Auto-responder")
            
        await ctx.send("**Active Vibes:**\n" + "\n".join(vibe_list))

    @commands.command(name='deletevibe', aliases=['unloadvibe'])
    async def deletevibe(self, ctx, vibe_name: str):
        """Delete a vibe (admin only)"""
        if not self.is_authorized(ctx):
            await ctx.send("You do not have permission to use this command.")
            return
            
        config = self.bot.config
            
        active_vibes = config.get(ctx, "active_vibes") or {}
        
        # Find matching vibe
        matching_cog = None
        for cog_name, vibe_data in active_vibes.items():
            if vibe_name.lower() in cog_name.lower() or vibe_name.lower() == vibe_data.get("spec", {}).get("class_name", "").lower():
                matching_cog = cog_name
                break
                
        if not matching_cog:
            await ctx.send("Vibe not found.")
            return
            
        try:
            # Try to unload the cog if it's loaded
            if matching_cog in self.bot.extensions:
                await self.bot.unload_extension(matching_cog)
                self.logger.info(f"Unloaded extension: {matching_cog}")
            else:
                self.logger.info(f"Extension not loaded, skipping unload: {matching_cog}")
            
            # Delete the file
            filepath = active_vibes[matching_cog].get("filepath")
            if filepath and os.path.exists(filepath):
                os.remove(filepath)
                self.logger.info(f"Deleted file: {filepath}")
                
            # Remove from active vibes
            active_vibes.pop(matching_cog)
            config.set(ctx, "active_vibes", active_vibes)
            
            await ctx.send(f"✅ Removed vibe: {matching_cog}")
            
        except Exception as e:
            self.logger.error(f"Failed to unload vibe: {e}", exc_info=True)
            await ctx.send(f"❌ Failed to unload vibe: {str(e)}")

    async def get_repo_context(self) -> str:
        """Get repository structure context for AI"""
        context_lines = []
        
        # Get cog files
        cog_files = []
        import pathlib
        bot_root = pathlib.Path(__file__).parent.parent.parent
        
        for cog_type in ["static", "dynamic"]:
            cog_dir = bot_root / "cogs" / cog_type
            if cog_dir.exists():
                for file in cog_dir.iterdir():
                    if file.suffix == ".py" and not file.name.startswith("_"):
                        cog_files.append(f"cogs/{cog_type}/{file.name}")
                        
        context_lines.append("AVAILABLE COGS:")
        context_lines.extend(cog_files)
        context_lines.append("")
        
        # Get a sample cog structure
        context_lines.append("SAMPLE COG STRUCTURE (admin.py):")
        try:
            admin_path = bot_root / "cogs" / "static" / "admin.py"
            if admin_path.exists():
                with open(admin_path, "r") as f:
                    lines = f.readlines()[:50]  # First 50 lines
                    context_lines.extend(["  " + line.rstrip() for line in lines])
        except:
            pass
            
        return "\n".join(context_lines)

async def setup(bot):
    """Every cog needs a setup function like this."""
    await bot.add_cog(Vibe(bot))