# Cog Development Guide

This guide covers creating custom cogs for LiterallyBot, from basic commands to advanced features with configuration management.

## Cog Architecture Overview

### Cog Categories
- **Static Cogs (`cogs/static/`)** - Core functionality (admin, dev tools) - always loaded
- **Dynamic Cogs (`cogs/dynamic/`)** - Features that can be loaded/unloaded - main extension point

### Basic Cog Structure
```python
from discord.ext import commands

class MyCog(commands.Cog):
    """Description of what this cog does"""
    
    def __init__(self, bot):
        self.bot = bot
        self.logger = bot.logger  # Access central logging
        # Initialize any cog-specific data here
    
    @commands.command()
    async def my_command(self, ctx):
        """Command description for help system"""
        await ctx.send("Hello from MyCog!")

async def setup(bot):
    """Required function for cog loading"""
    await bot.add_cog(MyCog(bot))
```

## Command Development

### Basic Commands
```python
@commands.command(name="greet", aliases=["hello", "hi"])
async def greet_command(self, ctx, *, name: str = None):
    """Greet a user or yourself"""
    if name:
        await ctx.send(f"Hello {name}!")
    else:
        await ctx.send(f"Hello {ctx.author.mention}!")
```

### Commands with Arguments
```python
@commands.command()
async def add(self, ctx, num1: int, num2: int):
    """Add two numbers"""
    result = num1 + num2
    await ctx.send(f"{num1} + {num2} = {result}")

@commands.command()
async def say(self, ctx, *, message: str):
    """Make the bot repeat a message"""
    await ctx.send(message)
```

### Error Handling
```python
@commands.command()
async def divide(self, ctx, num1: float, num2: float):
    """Divide two numbers"""
    try:
        if num2 == 0:
            await ctx.send("Cannot divide by zero!")
            return
        result = num1 / num2
        await ctx.send(f"{num1} / {num2} = {result}")
    except Exception as e:
        self.logger.error(f"Error in divide command: {e}", exc_info=True)
        await ctx.send("Something went wrong with the calculation!")
```

## Configuration Integration

### Basic Config Usage
```python
@commands.command()
async def set_prefix(self, ctx, new_prefix: str):
    """Set a custom prefix for this server"""
    self.bot.config.set(ctx, "prefix", new_prefix)
    await ctx.send(f"Prefix changed to: {new_prefix}")

@commands.command()
async def get_prefix(self, ctx):
    """Show current server prefix"""
    prefix = self.bot.config.get(ctx, "prefix", "!")
    await ctx.send(f"Current prefix: {prefix}")
```

### User-Specific Settings
```python
@commands.command()
async def set_timezone(self, ctx, timezone: str):
    """Set your personal timezone"""
    # You could add timezone validation here
    self.bot.config.set_user(ctx, "timezone", timezone)
    await ctx.send(f"Your timezone set to: {timezone}")

@commands.command()
async def my_settings(self, ctx):
    """View your personal settings"""
    timezone = self.bot.config.get_user(ctx, "timezone", "UTC")
    theme = self.bot.config.get_user(ctx, "theme", "default")
    await ctx.send(f"**Your Settings:**\nTimezone: {timezone}\nTheme: {theme}")
```

### Managing Lists and Arrays
```python
@commands.command()
async def add_favorite(self, ctx, *, item: str):
    """Add an item to your favorites list"""
    favorites = self.bot.config.get_user(ctx, "favorites", [])
    if item not in favorites:
        favorites.append(item)
        self.bot.config.set_user(ctx, "favorites", favorites)
        await ctx.send(f"Added '{item}' to your favorites!")
    else:
        await ctx.send("That's already in your favorites!")

@commands.command()
async def list_favorites(self, ctx):
    """Show your favorites list"""
    favorites = self.bot.config.get_user(ctx, "favorites", [])
    if favorites:
        items = "\n".join(f"• {item}" for item in favorites)
        await ctx.send(f"**Your Favorites:**\n{items}")
    else:
        await ctx.send("You don't have any favorites yet!")
```

## Permission Management

### Basic Permission Checks
```python
@commands.command()
async def admin_only(self, ctx):
    """Command only admins can use"""
    admins = self.bot.config.get(ctx, "admins", [])
    if ctx.author.id not in admins:
        await ctx.send("You don't have permission to use this command.")
        return
    
    await ctx.send("Admin command executed!")

@commands.command()
@commands.has_permissions(administrator=True)
async def discord_admin_only(self, ctx):
    """Command only Discord admins can use"""
    await ctx.send("Discord admin command executed!")
```

### Global Superadmin Check
```python
from core.utils import is_superadmin

@commands.command()
async def superadmin_only(self, ctx):
    """Command only bot superadmins can use."""
    if not is_superadmin(self.bot.config, ctx.author.id):
        await ctx.send("Only a bot superadmin can use this command.")
        return
    
    await ctx.send("Superadmin command executed!")
```

## Advanced Features

### Event Listeners
```python
@commands.Cog.listener()
async def on_member_join(self, member):
    """Triggered when someone joins the server"""
    # Get welcome channel from config
    channel_id = self.bot.config.get(member.guild, "welcome_channel")
    if channel_id:
        channel = member.guild.get_channel(channel_id)
        if channel:
            await channel.send(f"Welcome {member.mention}!")

@commands.Cog.listener()
async def on_message(self, message):
    """Triggered on every message (be careful with performance)"""
    if message.author.bot:
        return
    
    # Example: Track message count per user
    count = self.bot.config.get_user(message.author.id, "message_count", 0)
    self.bot.config.set_user(message.author.id, "message_count", count + 1)
```

### Background Tasks
```python
from discord.ext import tasks
import asyncio

class MyTaskCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.background_task.start()
    
    def cog_unload(self):
        self.background_task.cancel()
    
    @tasks.loop(minutes=1)
    async def background_task(self):
        # Do something every minute
        await asyncio.sleep(0)
```

### Dynamic Config Access without Context
```python
# Direct guild ID
self.bot.config.set(1234567890, "setting_name", True)

# Direct user ID
self.bot.config.set_user(987654321, "preference", "value")
```

## Testing & Reloading Tips
- Use `!load my_cog` / `!reload my_cog` for hot-reload during development
- Wrap risky code with try/except blocks and log errors
- Keep commands async-friendly and avoid blocking calls
