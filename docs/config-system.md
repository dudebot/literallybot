# Configuration System Documentation

LiterallyBot uses a JSON-based configuration system that supports per-guild, per-user, and global settings with automatic persistence and write buffering.

## Overview

### Storage Types
- **Global Config**: `configs/global.json` - Bot-wide settings (superadmins, global features)
- **Guild Config**: `configs/{guild_id}.json` - Server-specific settings (admins, features, server preferences)
- **User Config**: `configs/user_{user_id}.json` - Individual user preferences and data

### Key Features
- **5-second write buffering**: Batches rapid config changes to reduce filesystem I/O
- **Atomic writes**: Uses temporary files with atomic rename to prevent corruption
- **Cross-platform**: Handles Windows and Unix filesystem differences
- **Thread-safe**: Safe for concurrent access from multiple cogs

## Basic Usage

### Accessing the Config System
The config system is available as `self.bot.config` in all cogs:

```python
class MyCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
    
    @commands.command()
    async def my_command(self, ctx):
        # Access config here
        value = self.bot.config.get(ctx, "my_setting", "default")
```

### Guild-Specific Settings (Default)
```python
# Get guild setting with default
prefix = self.bot.config.get(ctx, "prefix", "!")

# Set guild setting
self.bot.config.set(ctx, "prefix", "?")

# Example: Server-specific feature toggles
music_enabled = self.bot.config.get(ctx, "music_enabled", True)
self.bot.config.set(ctx, "music_enabled", False)
```

### User-Specific Settings
```python
# Get user preference
timezone = self.bot.config.get_user(ctx, "timezone", "UTC")

# Set user preference
self.bot.config.set_user(ctx, "theme", "dark")

# Alternative syntax with scope parameter
theme = self.bot.config.get(ctx, "theme", "light", scope="user")
self.bot.config.set(ctx, "theme", "dark", scope="user")
```

### Global Settings
```python
# Get global setting
maintenance_mode = self.bot.config.get_global("maintenance", False)

# Set global setting
self.bot.config.set_global("maintenance", True)

# Alternative syntax
superadmins = self.bot.config.get(None, "superadmins", scope="global")
```

## Working with Lists and Arrays

Managing arrays in config requires manual operations:

```python
# Add item to list
admins = self.bot.config.get(ctx, "admins", [])
if user_id not in admins:  # Check for duplicates if needed
    admins.append(user_id)
    self.bot.config.set(ctx, "admins", admins)

# Remove item from list
blocked_users = self.bot.config.get(ctx, "blocked_users", [])
if user_id in blocked_users:
    blocked_users.remove(user_id)
    self.bot.config.set(ctx, "blocked_users", blocked_users)
```

### Cleanup Expired Data
For data with expiration timestamps:

```python
import time

# Remove expired items manually
items = self.bot.config.get(ctx, "temp_data", [])
current_time = time.time()
active_items = [item for item in items if item.get("expires", 0) > current_time]
if len(active_items) != len(items):
    self.bot.config.set(ctx, "temp_data", active_items)
```

## Real-World Examples

### Permission System (Admin Cog)
```python
# Check if user is admin
admins = self.bot.config.get(ctx, "admins", [])
if ctx.author.id not in admins:
    await ctx.send("You don't have permission.")
    return

# Add new admin
admins = self.bot.config.get(ctx, "admins", [])
if new_user.id not in admins:
    admins.append(new_user.id)
    self.bot.config.set(ctx, "admins", admins)
```

### User Preferences (Hypothetical Settings Cog)
```python
@commands.command()
async def set_timezone(self, ctx, timezone: str):
    """Set your personal timezone"""
    # Validate timezone here...
    self.bot.config.set_user(ctx, "timezone", timezone)
    await ctx.send(f"Timezone set to {timezone}")

@commands.command()
async def my_settings(self, ctx):
    """View your personal settings"""
    timezone = self.bot.config.get_user(ctx, "timezone", "UTC")
    theme = self.bot.config.get_user(ctx, "theme", "default")
    await ctx.send(f"Timezone: {timezone}, Theme: {theme}")
```

### Feature Toggles (Per-Guild)
```python
@commands.command()
async def toggle_music(self, ctx):
    """Toggle music functionality for this server"""
    current = self.bot.config.get(ctx, "music_enabled", True)
    self.bot.config.set(ctx, "music_enabled", not current)
    status = "enabled" if not current else "disabled"
    await ctx.send(f"Music {status} for this server")
```

### Memory System (GPT Cog Pattern)
```python
# Store user interaction with expiration
memory_item = {
    "text": user_message,
    "expires": time.time() + (30 * 24 * 60 * 60),  # 30 days
    "type": "conversation",
    "user_id": ctx.author.id
}

memories = self.bot.config.get(ctx, "memories", [])
memories.append(memory_item)
self.bot.config.set(ctx, "memories", memories)

# Later, clean up expired memories
memories = self.bot.config.get(ctx, "memories", [])
current_time = time.time()
active_memories = [m for m in memories if m.get("expires", 0) > current_time]
if len(active_memories) != len(memories):
    self.bot.config.set(ctx, "memories", active_memories)
```

## Advanced Usage

### Manual Flush
Force immediate save (useful before bot shutdown):
```python
# Force all pending writes to disk
self.bot.config.flush()
```

### Context-Free Usage
When you don't have a Discord context:
```python
# Using user ID directly
self.bot.config.set_user(user_id, "last_seen", time.time())

# Using guild ID directly
self.bot.config.set(guild_id, "maintenance", True)
```

### Complex Data Structures
```python
# Store nested objects
user_stats = {
    "commands_used": 42,
    "last_active": time.time(),
    "preferences": {
        "notifications": True,
        "compact_mode": False
    }
}
self.bot.config.set_user(ctx, "stats", user_stats)
```

### Combining Scopes
```python
# Example: Allow global override falling back to guild setting
def get_prefix(self, ctx):
    return (
        self.bot.config.get_global("prefix_override")
        or self.bot.config.get(ctx, "prefix", "!")
    )
```
