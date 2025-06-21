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
@commands.command()
async def superadmin_only(self, ctx):
    """Command only the global superadmin can use"""
    superadmin = self.bot.config.get_global("superadmin")
    if ctx.author.id != superadmin:
        await ctx.send("Only the bot superadmin can use this command.")
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
        self.cleanup_task.start()  # Start background task
    
    def cog_unload(self):
        """Called when cog is unloaded"""
        self.cleanup_task.cancel()
    
    @tasks.loop(minutes=30)
    async def cleanup_task(self):
        """Clean up expired data every 30 minutes"""
        # Example: Clean expired reminders
        import time
        for guild in self.bot.guilds:
            reminders = self.bot.config.get(guild.id, "reminders", [])
            current_time = time.time()
            active_reminders = [r for r in reminders if r.get("expires", 0) > current_time]
            
            if len(active_reminders) != len(reminders):
                self.bot.config.set(guild.id, "reminders", active_reminders)
                self.bot.logger.info(f"Cleaned expired reminders for guild {guild.id}")
    
    @cleanup_task.before_loop
    async def before_cleanup_task(self):
        """Wait until bot is ready before starting task"""
        await self.bot.wait_until_ready()
```

### Complex Data Management
```python
import time

@commands.command()
async def add_memory(self, ctx, *, memory: str):
    """Add a memory that expires after 30 days"""
    memories = self.bot.config.get_user(ctx, "memories", [])
    
    memory_item = {
        "text": memory,
        "timestamp": time.time(),
        "expires": time.time() + (30 * 24 * 60 * 60),  # 30 days
        "guild_id": ctx.guild.id if ctx.guild else None
    }
    
    memories.append(memory_item)
    self.bot.config.set_user(ctx, "memories", memories)
    
    await ctx.send("Memory saved!")

@commands.command()
async def recall_memories(self, ctx):
    """Show your recent memories"""
    memories = self.bot.config.get_user(ctx, "memories", [])
    current_time = time.time()
    
    # Filter out expired memories
    active_memories = [m for m in memories if m.get("expires", 0) > current_time]
    
    # Save cleaned list if we removed any
    if len(active_memories) != len(memories):
        self.bot.config.set_user(ctx, "memories", active_memories)
    
    if active_memories:
        recent = active_memories[-5:]  # Last 5 memories
        memory_text = "\n".join(f"• {m['text']}" for m in recent)
        await ctx.send(f"**Your Recent Memories:**\n{memory_text}")
    else:
        await ctx.send("You don't have any memories yet!")
```

## Best Practices

### Logging
```python
@commands.command()
async def important_action(self, ctx):
    """Example of proper logging"""
    try:
        # Log command usage
        self.logger.info(f"User {ctx.author} (ID: {ctx.author.id}) used important_action in guild {ctx.guild.id}")
        
        # Do something important
        result = perform_important_operation()
        
        # Log success
        self.logger.info(f"Important action completed successfully for user {ctx.author.id}")
        await ctx.send("Action completed!")
        
    except Exception as e:
        # Log errors with full traceback
        self.logger.error(f"Error in important_action for user {ctx.author.id}: {e}", exc_info=True)
        await ctx.send("Something went wrong!")
```

### Input Validation
```python
@commands.command()
async def set_age(self, ctx, age: int):
    """Set your age with validation"""
    if age < 13 or age > 120:
        await ctx.send("Please enter a valid age between 13 and 120.")
        return
    
    self.bot.config.set_user(ctx, "age", age)
    await ctx.send(f"Age set to {age}")

@commands.command()
async def set_color(self, ctx, color: str):
    """Set favorite color with validation"""
    valid_colors = ["red", "blue", "green", "yellow", "purple", "orange", "pink"]
    color = color.lower()
    
    if color not in valid_colors:
        await ctx.send(f"Please choose from: {', '.join(valid_colors)}")
        return
    
    self.bot.config.set_user(ctx, "favorite_color", color)
    await ctx.send(f"Favorite color set to {color}!")
```

### Performance Considerations
```python
# Good: Batch config operations
@commands.command()
async def bulk_update(self, ctx):
    """Example of efficient config updates"""
    # Get all data at once
    user_data = self.bot.config.get_user(ctx, "profile", {})
    
    # Make changes
    user_data["last_active"] = time.time()
    user_data["command_count"] = user_data.get("command_count", 0) + 1
    user_data["favorite_guild"] = ctx.guild.id if ctx.guild else None
    
    # Save once
    self.bot.config.set_user(ctx, "profile", user_data)

# Avoid: Multiple individual config calls
# self.bot.config.set_user(ctx, "last_active", time.time())
# self.bot.config.set_user(ctx, "command_count", count + 1)
# self.bot.config.set_user(ctx, "favorite_guild", ctx.guild.id)
```

## Loading and Testing

### Loading Your Cog
1. Save your cog file as `cogs/dynamic/my_cog.py`
2. In Discord: `!load my_cog`
3. Test with `!help MyCog` or your custom commands

### Hot-Reloading During Development
```
!reload my_cog  # Reload after making changes
!unload my_cog  # Temporarily disable
!load my_cog    # Re-enable
```

### Error Debugging
- Check bot logs in `logs/bot.log`
- Use `!eval` command (superadmin only) for quick testing
- Add print statements or logger calls for debugging

## Example: Complete Feature Cog

```python
from discord.ext import commands
import time
import json

class UserStats(commands.Cog):
    """Track and display user statistics"""
    
    def __init__(self, bot):
        self.bot = bot
        self.logger = bot.logger
    
    @commands.command()
    async def stats(self, ctx):
        """Show your server statistics"""
        stats = self.bot.config.get_user(ctx, "stats", {
            "commands_used": 0,
            "messages_sent": 0,
            "first_seen": time.time(),
            "last_active": time.time()
        })
        
        # Calculate days since first seen
        days_active = (time.time() - stats["first_seen"]) / 86400
        
        embed = discord.Embed(title=f"Stats for {ctx.author.display_name}")
        embed.add_field(name="Commands Used", value=stats["commands_used"])
        embed.add_field(name="Messages Sent", value=stats["messages_sent"])
        embed.add_field(name="Days Active", value=f"{days_active:.1f}")
        
        await ctx.send(embed=embed)
    
    @commands.command()
    async def leaderboard(self, ctx):
        """Show server command usage leaderboard"""
        # This would require iterating through user configs
        # Implementation depends on specific needs
        await ctx.send("Leaderboard feature coming soon!")
    
    @commands.Cog.listener()
    async def on_command(self, ctx):
        """Track command usage"""
        if ctx.author.bot:
            return
            
        stats = self.bot.config.get_user(ctx, "stats", {
            "commands_used": 0,
            "messages_sent": 0,
            "first_seen": time.time(),
            "last_active": time.time()
        })
        
        stats["commands_used"] += 1
        stats["last_active"] = time.time()
        
        self.bot.config.set_user(ctx, "stats", stats)

async def setup(bot):
    await bot.add_cog(UserStats(bot))
```

This comprehensive guide should help you create powerful, well-integrated cogs for LiterallyBot!