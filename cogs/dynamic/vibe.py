from discord.ext import commands
import openai
import os
import time
import re
import asyncio
import json
from typing import Dict, List, Any
import shutil
from datetime import datetime
from core.ai import call_chat_completion

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
            
        # Use shared OpenAI-compatible helper
        base_url = provider_info.get("base_url")
        return await call_chat_completion(
            api_key=api_key,
            base_url=base_url,
            model=model,
            messages=messages,
            metadata=metadata,
            provider_info=provider_info,
        )

    @commands.command(name='vibe')
    @commands.cooldown(1, 120, commands.BucketType.guild)
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
            
            impl_prompt = (
                "Generate a Discord bot cog implementation.\n\n"
                f"ORIGINAL USER REQUEST: \"{description}\"\n\n"
                "SPECIFICATION FROM ANALYSIS:\n"
                f"{spec_for_impl}\n\n"
                "IMPORTANT: Follow the ORIGINAL USER REQUEST exactly. Use any specific values mentioned (user IDs, exact phrases, etc).\n\n"
                "Here's a simple auto-response example:\n\n"
                "```python\n"
                "from discord.ext import commands\n\n"
                "class SimpleResponder(commands.Cog):\n"
                "    \"\"\"Responds to specific messages.\"\"\"\n"
                "    def __init__(self, bot):\n"
                "        self.bot = bot\n"
                "        self.logger = bot.logger\n"
                "    \n"
                "    @commands.Cog.listener()\n"
                "    async def on_message(self, message):\n"
                "        # Skip bot messages\n"
                "        if message.author.bot:\n"
                "            return\n"
                "        \n"
                "        # Check conditions and respond\n"
                "        if message.author.id == 123456789 and \"hello\" in message.content.lower():\n"
                "            await message.channel.send(\"world\")\n"
                "```\n\n"
                "Config system reference:\n"
                "- Guild: config.get(ctx, \"key\", default) / config.set(ctx, \"key\", value)\n"
                "- Global: config.get(None, \"key\") / config.set(None, \"key\", value)\n"
                "- IMPORTANT: In on_message, create context first: ctx = await self.bot.get_context(message)\n\n"
                "Requirements:\n"
                "- PRIORITY: Follow the ORIGINAL USER REQUEST exactly, including specific IDs and phrases\n"
                "- Keep it SIMPLE - don't add features that weren't requested\n"
                "- Use hardcoded values when appropriate (especially for simple auto-responses)\n"
                "- Only use config if the user wants toggles/configuration\n"
                "- Use minimal imports\n"
                "- Include the async def setup(bot) function at the end\n"
                "- Add brief docstrings\n"
                "- For auto-responses, use @commands.Cog.listener() with on_message\n\n"
                "Persistence guidance:\n"
                "- Never rely on in-memory state across restarts. Read state from self.bot.config at the start of each command or listener.\n"
                "- Choose scope explicitly: use guild-scoped config (config.get(ctx, key)/config.set(ctx, key, val)) for per-server data, or global for shared across servers.\n"
                "- Do not require on_ready to rebuild state. Avoid one-time initialization unless absolutely necessary.\n\n"
                "Generate ONLY the Python code, no explanations.\n"
            )

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
                    # Save pending vibe (global, no guild/channel coupling)
                    pending_vibes = self.bot.config.get(None, "pending_vibes", scope="global") or {}
                    vibe_id = f"{int(time.time())}_{ctx.author.id}"
                    pending_vibes[vibe_id] = {
                        "filepath": str(filepath),
                        "implementation": implementation,
                        "spec": spec,
                        "requester_id": ctx.author.id,
                        "description": description,
                    }
                    self.bot.config.set(None, "pending_vibes", pending_vibes, scope="global")
                    
                    # DM owner
                    dm_message = f"""**New Vibe Request**
Requester: {ctx.author} (ID: {ctx.author.id})
Guild: {ctx.guild.name if ctx.guild else 'DM'}
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
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(implementation)
                
            # Track active vibes
            active_vibes = self.bot.config.get(None, "active_vibes", scope="global") or {}
            cog_name = f"cogs.vibes.{os.path.basename(filepath)[:-3]}"
            active_vibes[cog_name] = {
                "spec": spec,
                "created_at": time.time(),
                "filepath": filepath
            }
            self.bot.config.set(None, "active_vibes", active_vibes, scope="global")
            
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
                    if ctx and hasattr(ctx, 'send'):
                        await ctx.send(f"✅ Vibe loaded! Commands: {', '.join(command_names)}")
                else:
                    # No commands (e.g., auto-responder)
                    if ctx and hasattr(ctx, 'send'):
                        await ctx.send(f"✅ Vibe loaded! Auto-responder: {spec.get('class_name', 'Unknown')}")
            except Exception as e:
                # Clean up on failure
                os.remove(filepath)
                active_vibes.pop(cog_name, None)
                self.bot.config.set(None, "active_vibes", active_vibes, scope="global")
                raise e
                
        except Exception as e:
            self.logger.error(f"Failed to load vibe: {e}", exc_info=True)
            if ctx and hasattr(ctx, 'send'):
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
        
        # Load globally and notify requester via DM (no guild/channel coupling)
        await self.save_and_load_vibe(
            None,
            vibe_data["filepath"],
            vibe_data["implementation"],
            vibe_data["spec"]
        )
        try:
            requester = self.bot.get_user(vibe_data.get("requester_id"))
            if requester:
                await requester.send("Your vibe has been approved and loaded!")
        except Exception:
            pass
            
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

        # Notify requester via DM
        try:
            requester = self.bot.get_user(vibe_data.get("requester_id"))
            if requester:
                await requester.send("Your vibe request was rejected.")
        except Exception:
            pass

        await ctx.send("Vibe rejected.")

    @commands.command(name='revibe')
    async def revibe(self, ctx, *, args: str):
        """Modify an existing vibe by name and replace its code (admin only).

        Usage: !revibe <VibeClassName> <new description>
        - Creates a timestamped backup of the existing file.
        - Generates new implementation based on the new description, preserving class name.
        - Reloads the cog in place.
        """
        if not self.is_authorized(ctx):
            await ctx.send("You do not have permission to use this command.")
            return

        try:
            target, new_description = args.strip().split(" ", 1)
        except ValueError:
            await ctx.send("Usage: !revibe <VibeClassName> <new description>")
            return

        # Locate existing vibe by class name from active_vibes or by scanning files (global)
        config = self.bot.config
        active_vibes = config.get(None, "active_vibes", scope="global") or {}

        matching_cog = None
        matching_info = None
        for cog_name, info in active_vibes.items():
            cls = (info.get("spec") or {}).get("class_name", "")
            if cls.lower() == target.lower():
                matching_cog = cog_name
                matching_info = info
                break

        import pathlib, re
        bot_root = pathlib.Path(__file__).parent.parent.parent
        vibes_dir = bot_root / "cogs" / "vibes"
        vibes_dir.mkdir(exist_ok=True)

        def scan_for_class(filepath: pathlib.Path) -> List[str]:
            try:
                text = filepath.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                return []
            return re.findall(r"class\s+([A-Za-z_][A-Za-z0-9_]*)\(commands\.Cog\):", text)

        if not matching_cog:
            # Fallback: scan vibe files for class name match
            for file in vibes_dir.iterdir():
                if file.suffix == ".py" and not file.name.startswith("_"):
                    classes = scan_for_class(file)
                    if any(c.lower() == target.lower() for c in classes):
                        module_name = file.stem
                        matching_cog = f"cogs.vibes.{module_name}"
                        matching_info = {
                            "filepath": str(file),
                            "spec": {"class_name": target},
                        }
                        break

        if not matching_cog or not matching_info:
            await ctx.send("Vibe not found. Make sure the class name matches exactly.")
            return

        filepath = pathlib.Path(matching_info.get("filepath"))
        if not filepath.exists():
            await ctx.send("Existing vibe file not found on disk.")
            return

        # Read current implementation (for context and backup)
        try:
            current_code = filepath.read_text(encoding="utf-8")
        except Exception as e:
            self.logger.error(f"Failed reading existing vibe file: {e}", exc_info=True)
            await ctx.send("Could not read existing vibe file.")
            return

        # Backup existing file
        ts = int(time.time())
        backup_path = filepath.with_suffix(f".bak.{ts}.py")
        try:
            shutil.copy2(str(filepath), str(backup_path))
        except Exception as e:
            self.logger.error(f"Failed to create backup: {e}", exc_info=True)
            await ctx.send("Failed to create a backup; aborting.")
            return

        await ctx.send(f"🔧 Updating vibe '{target}'. Backup created: `{backup_path.name}`. Generating new implementation…")

        # Build a spec prompt focused on modification
        mod_spec_prompt = f"""You are updating an existing Discord bot cog.
Cog class name: {target}
Original description (context): {new_description}

Goals:
- Modify/extend the current behavior per the new description while keeping the same class name '{target}'.
- Preserve persisted data compatibility (same config keys/types) unless the request requires changes; if changes are needed, add migration logic.
- Keep commands names stable when possible.

Generate a specification JSON with keys: class_name, commands, imports, storage_needs, safety_notes, approach.
"""

        repo_context = await self.get_repo_context()
        spec_messages = [
            {"role": "system", "content": "You are an expert Discord bot developer. Generate detailed, safe specifications."},
            {"role": "user", "content": mod_spec_prompt + "\n\nREPO STRUCTURE:\n" + repo_context},
        ]

        all_providers = self.bot.config.get(None, "ai_providers", scope="global") or {}
        spec_provider = {
            "provider": "openai",
            "model": "o3",
            "provider_info": all_providers.get("openai", {}),
        }

        try:
            spec_response = await self.call_ai_api(spec_provider, spec_messages, {})
            json_match = re.search(r'\{.*\}', spec_response, re.DOTALL)
            if not json_match:
                raise ValueError("No JSON found in spec response")
            new_spec = json.loads(json_match.group(0))
        except Exception as e:
            self.logger.error(f"revibe spec generation failed: {e}", exc_info=True)
            await ctx.send("❌ Failed to generate modification spec.")
            return

        # Build implementation prompt using old code as context
        impl_prompt = (
            "Modify the following Discord bot cog code in full to satisfy the NEW REQUEST.\n\n"
            f"NEW REQUEST: \"{new_description}\"\n"
            f"CLASS NAME TO KEEP: {target}\n"
            "Preserve existing config keys and data structures when possible so persisted data keeps working across restarts.\n"
            "Do not rely on in-memory state across restarts; read from self.bot.config as needed.\n"
            "Keep command names unless the request mandates changes.\n"
            "Output ONLY the full Python module, including async def setup(bot).\n\n"
            "CURRENT CODE:\n" + current_code + "\n\n"
            "SPECIFICATION FOR UPDATE:\n" + json.dumps(new_spec, indent=2)
        )

        impl_messages = [
            {"role": "system", "content": "You are an expert Python developer specializing in Discord bots. Generate clean, safe, working code."},
            {"role": "user", "content": impl_prompt},
        ]

        impl_provider = {
            "provider": "openai",
            "model": "o4-mini",
            "provider_info": all_providers.get("openai", {}),
        }
        impl_provider["provider_info"] = impl_provider["provider_info"].copy()
        if "models" not in impl_provider["provider_info"]:
            impl_provider["provider_info"]["models"] = {}
        impl_provider["provider_info"]["models"]["o4-mini"] = {
            "timeout_multiplier": 2.0,
            "max_completion_tokens": 16000,
        }

        implementation = None
        for attempt in range(2):
            try:
                implementation = await self.call_ai_api(impl_provider, impl_messages, {})
                if implementation and len(implementation.strip()) > 50:
                    break
            except Exception as e:
                self.logger.error(f"revibe implementation generation failed on attempt {attempt+1}: {e}")
            await asyncio.sleep(1)

        if not implementation:
            await ctx.send("❌ Failed to generate updated implementation.")
            return

        # Cleanup fencing
        code = implementation.strip()
        if code.startswith("```python"):
            code = code[9:]
        if code.startswith("```"):
            code = code[3:]
        if code.endswith("```"):
            code = code[:-3]
        code = code.strip()

        # Unload, write, reload
        try:
            if matching_cog in self.bot.extensions:
                await self.bot.unload_extension(matching_cog)
        except Exception as e:
            self.logger.warning(f"Failed to unload before revibe (continuing): {e}")

        try:
            filepath.write_text(code, encoding="utf-8")
        except Exception as e:
            self.logger.error(f"Failed writing updated vibe file: {e}", exc_info=True)
            # attempt restore
            try:
                shutil.copy2(str(backup_path), str(filepath))
            except Exception:
                pass
            await ctx.send("❌ Failed to write updated file; original restored.")
            return

        try:
            await self.bot.load_extension(matching_cog)
        except Exception as e:
            self.logger.error(f"Failed to load updated vibe: {e}", exc_info=True)
            # Restore from backup on failure
            try:
                if matching_cog in self.bot.extensions:
                    await self.bot.unload_extension(matching_cog)
            except Exception:
                pass
            try:
                shutil.copy2(str(backup_path), str(filepath))
                await self.bot.load_extension(matching_cog)
            except Exception:
                pass
            await ctx.send("❌ Failed to reload updated vibe; original restored.")
            return

        # Record history entry for undo (global)
        history = config.get(None, "vibe_rev_history", scope="global") or []
        history.append({
            "cog_name": matching_cog,
            "filepath": str(filepath),
            "backup_path": str(backup_path),
            "prev_spec": matching_info.get("spec"),
            "timestamp": ts,
        })
        config.set(None, "vibe_rev_history", history, scope="global")

        # Update active_vibes spec (global)
        active_vibes = config.get(None, "active_vibes", scope="global") or {}
        entry = active_vibes.get(matching_cog, {})
        entry["spec"] = new_spec
        entry["created_at"] = time.time()
        entry["filepath"] = str(filepath)
        active_vibes[matching_cog] = entry
        config.set(None, "active_vibes", active_vibes, scope="global")

        await ctx.send(f"✅ Updated vibe '{target}' and reloaded.")

    @commands.command(name='unvibe')
    async def unvibe(self, ctx):
        """Undo the last !revibe (global, admin only)."""
        if not self.is_authorized(ctx):
            await ctx.send("You do not have permission to use this command.")
            return

        config = self.bot.config
        history = config.get(None, "vibe_rev_history", scope="global") or []
        if not history:
            await ctx.send("No revibe history to undo.")
            return

        last = history.pop()
        config.set(None, "vibe_rev_history", history, scope="global")

        cog_name = last.get("cog_name")
        filepath = last.get("filepath")
        backup_path = last.get("backup_path")
        prev_spec = last.get("prev_spec")

        try:
            if cog_name in self.bot.extensions:
                await self.bot.unload_extension(cog_name)
        except Exception:
            pass

        try:
            shutil.copy2(backup_path, filepath)
        except Exception as e:
            self.logger.error(f"Failed to restore backup: {e}", exc_info=True)
            await ctx.send("❌ Failed to restore backup.")
            return

        try:
            await self.bot.load_extension(cog_name)
        except Exception as e:
            self.logger.error(f"Failed to reload original after undo: {e}", exc_info=True)
            await ctx.send("❌ Restored file but failed to reload the cog.")
            return

        # Restore active_vibes metadata
        active_vibes = config.get(None, "active_vibes", scope="global") or {}
        entry = active_vibes.get(cog_name, {})
        entry["spec"] = prev_spec or entry.get("spec")
        entry["filepath"] = filepath
        active_vibes[cog_name] = entry
        config.set(None, "active_vibes", active_vibes, scope="global")

        await ctx.send("✅ Undid last revibe and restored the previous version.")

    @commands.command(name='listvibes')
    async def listvibes(self, ctx):
        """List active vibes (global)"""
        active_vibes = self.bot.config.get(None, "active_vibes", scope="global") or {}
        
        if not active_vibes:
            await ctx.send("No active vibes.")
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
        """Delete a vibe (admin only, global)"""
        if not self.is_authorized(ctx):
            await ctx.send("You do not have permission to use this command.")
            return

        config = self.bot.config
        active_vibes = config.get(None, "active_vibes", scope="global") or {}

        # Find matching vibe
        matching_cog = None
        for cog_name, vibe_data in active_vibes.items():
            if (
                vibe_name.lower() in cog_name.lower()
                or vibe_name.lower() == vibe_data.get("spec", {}).get("class_name", "").lower()
            ):
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
            active_vibes.pop(matching_cog, None)
            config.set(None, "active_vibes", active_vibes, scope="global")

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