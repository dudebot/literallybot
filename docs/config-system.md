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

## Key Registry

Every key the codebase actually reads or writes, by scope. Keep this current
when adding keys — it is the schema documentation for `configs/*.json`.
(The code examples elsewhere in this doc use invented keys like `prefix` for
illustration; they are not real.)

### Global scope (`global.json`)

| Key | Shape | Written by | Notes |
|-----|-------|-----------|-------|
| `superadmins` | `list[int]` user ids | `!addsuperadmin` / `!removesuperadmin` | Read through `core.utils.get_superadmins`, which normalizes a bare int to a list and re-persists — the one "read that writes" |
| `ai_providers` | `{provider_id: {name, base_url, default_model, requires_api_key?, models: {model_id: {cost_per_mtok_output?, max_completion_tokens?, reasoning_effort?}}}}` | `/ai settings` → Providers tab, `/ai setapikey` discovery | Absent ⇒ readers substitute the built-in `DEFAULT_PROVIDERS` seed |
| `<PROVIDER>_API_KEY` | `str` (e.g. `XAI_API_KEY`) | `/ai setapikey`, Providers-tab key modal | Env var of the same name is the fallback; removed with its provider |
| `DANBOORU_API_KEY`, `DANBOORU_LOGIN` | `str` | *no command surface* | Hand-edit or env only |
| `cooldown_tier_bases` | `{tier: seconds}` | `/ai settings` → Cooldowns | Absent/malformed ⇒ per-tier defaults from `COOLDOWN_TIERS` |
| `cooldown_windows` | `list[[count, period_mult]]` | `/ai settings` → Cooldowns | Absent/malformed ⇒ `DEFAULT_COOLDOWN_WINDOWS`; validated on both write and read |
| `mcp_tools_enabled` | `list[str]` op names | `/ai settings` → MCP tools | Read at MCP server build ⇒ restart-bound; absent ⇒ all exposed ops |
| `error_logging` | `{default_channel?, category_channels?, severity_channels?, rate_limit_minutes?}` | `!errorlog` subcommands | Same shape also exists per-guild (guild overrides global) |
| `reminders` | `list[{user_id, timestamp, text}]` | `!remindme` | Deliberately ONE global list across all guilds/DMs, filtered by `user_id` on read |
| `hue_bridge_ip` | `str` | `!sethuebridgeip` | signal cog |
| `command_author_allowlist` | `list[int]` | *no command surface* | bot.py bot-authored-command dispatch; hand-edit only |

### Guild scope (`<guild_id>.json`)

| Key | Shape | Written by | Notes |
|-----|-------|-----------|-------|
| `admins` | `list[int]` user ids | `!addadmin` / `!removeadmin` / `!claimadmin` | Read via `core.utils.is_admin` |
| `current_ai_provider` | `str` provider id | `/ai settings` → Server tab | Absent ⇒ `DEFAULT_PROVIDER` — deleting a provider must account for guilds relying on that implicit default (`_do_removeprovider` does) |
| `current_ai_model` | `str` or absent | `/ai settings` → Server tab | Absent ⇒ provider's `default_model` |
| `gpt_personality_data` | `{prompt: str, version: int}` | `/ai settings` → Personality modal | version = unix ts, tags memories |
| `gpt_memories` | `list[{text, expires, type, sender, personality_version, stored_at}]` | gpt.py memory capture | TTL-purged on read/write |
| `bot_tools_enabled` | `list[str]` ⊆ `AGENT_OPS` | `/ai settings` → Bot tools | Empty/absent ⇒ plain chat (no agent loop) |
| `whitelist_roles` | `list[str]` role NAMES | nothing (legacy) | Orphaned by the removal of the command/panel role-claiming path (`!setrole`, `/roles claim`, `/roles settings`) — reaction roles are the sole assignment path now. Data left in guild jsons; no live reader or writer |
| `emoji_role_toggles` | `{message_id_str: {emoji_key_str: role_id_int}}` | `/setemojiroletoggle` / `/removeemojiroletoggle` | emoji_key is custom-emoji id as str, or the unicode emoji itself |
| `error_logging` | as global | `!errorlog` in-guild | |

### User scope (`user_<id>.json`)

Currently **unused** — the API supports it but no live code writes user files.

### Conventions

- Bare-int ctx is the idiom for context-free access: `config.set(guild_id, key, value)` resolves guild scope from the int (used by panels, migrations, raw-reaction handlers).
- `config.set(None, key, value, scope="global")` (or `set_global`) is the global-write idiom.
- A few call sites in `ai_admin.py`/`gpt.py` scan `config._configs` directly (provider-in-use check, one-shot migrations). They rely on the file-id convention: guild configs are `"<digits>"`, the global config is `"global"`, user configs are `"user_<digits>"`. If you add a new file-id class, update those scans.

## Gotchas

### `get()` Is Read-Only

`get()` never writes to disk - it just returns the value or default. To persist a value, use `set()` explicitly:

```python
# This does NOT save "!" to disk - just returns it
prefix = config.get(ctx, "prefix", "!")

# To actually persist a default on first use:
if not config.has(ctx, "prefix"):
    config.set(ctx, "prefix", "!")
prefix = config.get(ctx, "prefix")
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

Two cogs can coordinate by writing to shared config keys (illustrative
pattern — nothing in the current codebase uses it):

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
