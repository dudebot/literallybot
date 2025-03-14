import json
import os
import asyncio
from discord.ext import commands, tasks
import time

class ConfigCog(commands.Cog, name="Config"):
    """Configuration management system for the bot."""
    
    _instance = None
    _configs = {}
    _dirty_configs = set()
    _last_access = {}
    _write_lock = asyncio.Lock()
    
    def __new__(cls, *args, **kwargs):
        # Implement singleton pattern
        if cls._instance is None:
            cls._instance = super(ConfigCog, cls).__new__(cls)
        return cls._instance
    
    def __init__(self, bot):
        if not hasattr(self, 'initialized'):
            self.bot = bot
            self.config_dir = os.path.join("configs")
            if not os.path.exists(self.config_dir):
                os.makedirs(self.config_dir)
            self.cache_ttl = 300  # 5 minutes
            # Initialize configs for all guilds at startup
            self.initialize_all_configs()
            self.autosave.start()
            self.initialized = True
    
    def initialize_all_configs(self):
        """Initialize configs for all guilds the bot is in and load existing configs."""
        # Load global config first
        self._load_config("global")
        
        # Load configs from disk first
        if os.path.exists(self.config_dir):
            for filename in os.listdir(self.config_dir):
                if filename.endswith('.json'):
                    config_id = filename[:-5]  # Remove .json extension
                    self._load_config(config_id)
        
        # Initialize configs for all guilds the bot is in
        for guild in self.bot.guilds:
            guild_id = str(guild.id)
            if guild_id not in self._configs:
                self._configs[guild_id] = {
                    "guild_name": guild.name
                }
                self._dirty_configs.add(guild_id)
                self._last_access[guild_id] = time.time()
        
        # Save all configs immediately
        asyncio.create_task(self._save_all_dirty())
    
    def cog_unload(self):
        self.autosave.cancel()
        # Force save all dirty configs
        asyncio.create_task(self._save_all_dirty())
    
    @tasks.loop(seconds=30)
    async def autosave(self):
        """Periodically save dirty configs and clean up cache."""
        await self._save_all_dirty()
        self._clean_cache()
    
    async def _save_all_dirty(self):
        """Save all dirty configs to disk."""
        if not self._dirty_configs:
            return
            
        async with self._write_lock:
            dirty_copies = list(self._dirty_configs)
            for config_id in dirty_copies:
                if config_id in self._configs:
                    config_path = self._get_config_path(config_id)
                    try:
                        with open(config_path, "w") as f:
                            json.dump(self._configs[config_id], f, indent=4)
                        self._dirty_configs.discard(config_id)
                    except Exception as e:
                        print(f"Error saving config {config_id}: {e}")
    
    def _clean_cache(self):
        """Remove configs from memory that haven't been accessed recently."""
        current_time = time.time()
        to_remove = []
        
        for config_id, last_access in self._last_access.items():
            if current_time - last_access > self.cache_ttl and config_id not in self._dirty_configs:
                to_remove.append(config_id)
                
        for config_id in to_remove:
            if config_id in self._configs:
                del self._configs[config_id]
            if config_id in self._last_access:
                del self._last_access[config_id]
    
    def _get_config_path(self, config_id):
        """Get the file path for a config ID."""
        return os.path.join(self.config_dir, f"{config_id}.json")
    
    def _get_config_id(self, ctx_or_id=None):
        """Determine the config ID from a context or ID."""
        if ctx_or_id is None:
            return "global"
        if isinstance(ctx_or_id, int):
            return str(ctx_or_id)
        elif ctx_or_id and hasattr(ctx_or_id, 'guild') and hasattr(ctx_or_id.guild, 'id'):
            return str(ctx_or_id.guild.id)
        return "global"
    
    def _load_config(self, config_id):
        """Load a config from disk if it exists, otherwise create it."""
        config_path = self._get_config_path(config_id)
        
        if os.path.exists(config_path):
            try:
                with open(config_path, "r") as f:
                    self._configs[config_id] = json.load(f)
            except json.JSONDecodeError:
                print(f"Error decoding JSON in {config_path}, creating empty config")
                self._configs[config_id] = {}
        else:
            # Create new config with original logic from Config class
            self._configs[config_id] = {}
            if config_id != "global" and config_id.isdigit():
                guild = self.bot.get_guild(int(config_id))
                if guild:
                    self._configs[config_id]["guild_name"] = guild.name
            
            # Mark as dirty to save the new config
            self._dirty_configs.add(config_id)
        
        # Update last access time
        self._last_access[config_id] = time.time()

    # Legacy compatibility methods that match the original Config class
    def load_config_for_ctx(self, ctx=None):
        """Legacy method to load config for compatibility with original Config class."""
        config_id = self._get_config_id(ctx)
        if config_id not in self._configs:
            self._load_config(config_id)
        return self._configs[config_id]
        
    def save_config_for_ctx(self, ctx=None, config_data=None):
        """Legacy method to save config for compatibility with original Config class."""
        config_id = self._get_config_id(ctx)
        if config_data:
            self._configs[config_id] = config_data
            
        self._dirty_configs.add(config_id)
    
    def get(self, ctx_or_id=None, key=None, default=None):
        """Get a config value. If ctx_or_id is None, use global config."""
        config_id = self._get_config_id(ctx_or_id)
        
        # Load config if not in memory
        if config_id not in self._configs:
            self._load_config(config_id)
        else:
            # Update last access time
            self._last_access[config_id] = time.time()
        
        # If key is None, return the entire config
        if key is None:
            return self._configs[config_id]
            
        # Get value, set default if needed
        if key not in self._configs[config_id]:
            self._configs[config_id][key] = default
            self._dirty_configs.add(config_id)
        
        return self._configs[config_id].get(key, default)
    
    def set(self, ctx_or_id=None, key=None, value=None):
        """Set a config value. If ctx_or_id is None, use global config."""
        if key is None:
            return None
            
        config_id = self._get_config_id(ctx_or_id)
        
        # Load config if not in memory
        if config_id not in self._configs:
            self._load_config(config_id)
        else:
            # Update last access time
            self._last_access[config_id] = time.time()
        
        # Set the value and mark as dirty
        self._configs[config_id][key] = value
        self._dirty_configs.add(config_id)
        
        return value
    
    def add_bot_operator(self, ctx_or_id, user_id):
        """Add a user ID to the bot operators list."""
        config_id = self._get_config_id(ctx_or_id)
        operators = self.get(config_id, "bot_operators", [])
        if user_id not in operators:
            operators.append(user_id)
            self.set(config_id, "bot_operators", operators)
            return True
        return False
    
    # Commands for managing config via Discord
    @commands.group(invoke_without_command=True)
    @commands.has_permissions(administrator=True)
    async def config(self, ctx):
        """Config management commands."""
        await ctx.send("Available commands: `config list`, `config get <key>`, `config set <key> <value>`")
    
    @config.command(name="list")
    @commands.has_permissions(administrator=True)
    async def config_list(self, ctx):
        """List all config keys."""
        config_id = self._get_config_id(ctx)
        if config_id not in self._configs:
            self._load_config(config_id)
        
        keys = list(self._configs[config_id].keys())
        if keys:
            await ctx.send(f"Config keys: {', '.join(keys)}")
        else:
            await ctx.send("No config keys set.")
    
    @config.command(name="get")
    @commands.has_permissions(administrator=True)
    async def config_get(self, ctx, key: str):
        """Get a config value."""
        value = self.get(ctx, key)
        await ctx.send(f"{key}: {value}")
    
    @config.command(name="set")
    @commands.has_permissions(administrator=True)
    async def config_set(self, ctx, key: str, *, value: str):
        """Set a config value."""
        try:
            # Try to evaluate the value as a Python literal
            import ast
            parsed_value = ast.literal_eval(value)
            self.set(ctx, key, parsed_value)
        except (ValueError, SyntaxError):
            # If evaluation fails, store as string
            self.set(ctx, key, value)
        
        await ctx.send(f"Set {key} to {self.get(ctx, key)}")

async def setup(bot):
    await bot.add_cog(ConfigCog(bot))
