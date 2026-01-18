# Configuration System

A JSON-based key-value store with per-guild, per-user, and global scopes. Features write buffering, atomic saves, and live reloading when files change externally.

## Storage Layout

```
configs/
├── global.json              # Bot-wide settings (superadmins, global features)
├── 123456789.json           # Guild-specific (guild ID as filename)
├── user_987654321.json      # User-specific (user ID as filename)
└── ...
```

## Core Features

| Feature | Details |
|---------|---------|
| Write buffering | Changes batch for 5 seconds before writing to disk |
| Atomic writes | Uses temp file + rename to prevent corruption |
| Live reload | Polls every 2 seconds for external file changes |
| Merge on conflict | External changes win; conflicts logged to console |
| Thread-safe | Lock-protected timer operations |

## API Reference

Access via `self.bot.config` in any cog.

### CRUD Operations

```python
# Get a value (returns default if key missing)
value = config.get(ctx, "key", default)

# Set a value
config.set(ctx, "key", value)

# Check if key exists
if config.has(ctx, "key"):
    ...

# Remove a key (returns True if existed)
removed = config.rem(ctx, "key")
```

### Scopes

**Guild scope** (default) - uses `ctx.guild.id`, falls back to global in DMs:
```python
config.get(ctx, "prefix", "!")
config.set(ctx, "prefix", "?")
```

**User scope** - uses `ctx.author.id`:
```python
config.get_user(ctx, "timezone", "UTC")
config.set_user(ctx, "theme", "dark")
# Or with explicit scope:
config.get(ctx, "timezone", scope="user")
```

**Global scope** - bot-wide:
```python
config.get_global("maintenance", False)
config.set_global("maintenance", True)
# Or:
config.get(None, "superadmins", scope="global")
```

### Context-Free Access

When you have an ID but no Discord context:
```python
# Guild by ID
config.set(guild_id, "setting", value)

# User by ID
config.set_user(user_id, "preference", value)
```

## Gotchas

### `get()` Persists Defaults

When you call `get()` with a default and the key doesn't exist, the default is written to disk:

```python
# First call: key doesn't exist, writes "!" to disk
prefix = config.get(ctx, "prefix", "!")

# Second call: reads "!" from disk
prefix = config.get(ctx, "prefix", "!")
```

This is usually fine, but be aware if you're checking for "unconfigured" state:

```python
# To check without persisting:
if config.has(ctx, "prefix"):
    prefix = config.get(ctx, "prefix")
else:
    prefix = "!"  # Use default without saving
```

### Lists Are References

When you get a list, modify it, and set it back, you're modifying the same object in memory:

```python
admins = config.get(ctx, "admins", [])
admins.append(user_id)
config.set(ctx, "admins", admins)  # Works, but admins is already modified in memory
```

This is fine for normal use. Just don't assume `get()` returns a copy.

### DMs Fall Back to Global

Guild-scoped operations in DMs use the global config:

```python
# In a DM, this writes to global.json, not a guild file
config.set(ctx, "some_setting", value)
```

Check `ctx.guild` if you need to handle DMs differently.

## Live Reload

The config system watches for external file changes every 2 seconds. If you edit a JSON file directly:

1. Bot detects the mtime change
2. Reloads the file into memory
3. If there were unsaved in-memory changes, logs a conflict warning
4. External changes win

This enables patterns like:
- Hot-editing config without restarting the bot
- External tools writing to config files
- Two cogs communicating via a shared user config file

### Inter-Cog Communication Example

Two cogs can coordinate by writing to shared config keys:

```python
# Cog A: Producer
config.set_user(user_id, "pending_action", {"type": "verify", "data": "..."})

# Cog B: Consumer (on next command or event)
action = config.get_user(user_id, "pending_action")
if action:
    # Process it
    config.rem_user(user_id, "pending_action")
```

Since both cogs share the same `bot.config` instance, changes are immediately visible in memory. The file write is just for persistence.

## Working with Lists

```python
# Add to list
items = config.get(ctx, "items", [])
if new_item not in items:
    items.append(new_item)
    config.set(ctx, "items", items)

# Remove from list
items = config.get(ctx, "items", [])
if old_item in items:
    items.remove(old_item)
    config.set(ctx, "items", items)
```

### Expiring Data

For data with TTL:

```python
import time

# Store with expiration
record = {
    "value": "...",
    "expires": time.time() + 3600  # 1 hour
}
records = config.get(ctx, "records", [])
records.append(record)
config.set(ctx, "records", records)

# Cleanup expired
records = config.get(ctx, "records", [])
now = time.time()
active = [r for r in records if r.get("expires", 0) > now]
if len(active) != len(records):
    config.set(ctx, "records", active)
```

## Shutdown

For clean shutdown (flush pending writes, cancel timers):

```python
config.shutdown()
```

Called automatically if you use the bot's shutdown handler.

## Manual Flush

Force immediate write of all pending changes:

```python
config.flush()
```

Useful before operations that might crash, or when you need guaranteed persistence.
