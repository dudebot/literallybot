# Configuration System

A JSON-based key-value store with per-guild, per-user, and global scopes.
Features write buffering, temporary-file saves, and polling for external changes.

## Storage Layout

```
configs/
├── global.json              # Bot-wide settings (superadmins, global features)
├── 123456789.json           # Guild-specific (guild ID as filename)
├── user_<id>.json           # Per-user (points, …)
└── ...
```

## Core Features

| Feature | Details |
|---------|---------|
| Write buffering | Saves are deferred until about 5 seconds after the latest write |
| File replacement | Temp file + atomic rename on POSIX; Windows removes the old file before renaming |
| Live reload | Polls every 2 seconds for external file changes |
| External reload | A detected newer file replaces that scope's in-memory document; no per-key merge |
| Locking | Individual store operations and timer callbacks use locks; get/modify/set is not a transaction |

## API Reference

Access via `self.bot.config` in any cog. This API stores data; it does not
perform permission checks. Settings handlers must authorize the invoking user
and validate scope before writing; feature consumers must not grant their own
access. Prefer the existing panel for keys it already owns.

### CRUD Operations

```python
# Get a value (returns default if key missing)
value = config.get(ctx, "key", default)

# Set a value
config.set(ctx, "key", value)

# Remove a key (returns True if existed)
removed = config.rem(ctx, "key")
```

### Scopes

**Guild scope** (default) - uses `ctx.guild.id`, falls back to global in DMs:
```python
config.get(ctx, "prefix", "!")
config.set(ctx, "prefix", "?")
```

**Global scope** - bot-wide:
```python
config.get_global("maintenance", False)
config.set_global("maintenance", True)
# Or:
config.get(None, "superadmins", scope="global")
```

**User scope** - `user_<id>.json`, via a User/Member (anything with `.id`) or a bare user id:
```python
config.get_user(user, "points", 0)
config.set_user(user, "points", 10)
# Or:
config.get(user_id, "points", 0, scope="user")
```

`get_user` is read-only the same way `get` is: a missing user file returns the default and does **not** create `user_<id>.json`. Only `set_user` / `set(..., scope="user")` writes.

Config stores JSON values without a shared timestamp format. Follow each key's
consumer when interpreting time: reminder `timestamp` values are integer Unix
seconds, for example. Do not strip timezone information from Discord timestamps
as a general conversion rule.

### Context-Free Access

When you have an ID but no Discord context:
```python
config.set(guild_id, "setting", value)
```

### Enabling cog tools

API primitives (raw Discord actions) and behavioral primitives (cog services)
share the ops registry and the same exposure settings. For example, the media
cog already registers `list_media` and `post_media`; adding a second sending
tool is unnecessary.

1. Ensure the cog is loaded (`media` must not be in `disabled_cogs`).
2. As superadmin, enable both names under `/aisettings` → In-chat ops. This
   writes `agent_ops_whitelist` in **global** scope.
3. Under Server config for the target guild, select Everyone or Admin for
   each tool. This writes `agent_ops_gate` in **guild** scope. A global tick
   alone leaves everyone, superadmins included, with the default Off gate.
   An op marked `confirm_everyone` asks the admin to type YES before it is
   first set to Everyone.
4. For external MCP callers, separately enable the names under the MCP ops tab
   if `mcp_tools_enabled` is an explicit list. MCP uses neither agent gate.

Use `/aisettings` (or `!aisettings`) for normal setup. Cogs declare their
capabilities; the superadmin and guild admins decide their exposure through
the permission-checked settings handlers. A cog must not enable itself by
writing these settings during registration, startup or tool execution.
The Config API is a storage mechanism, not an authorization check.

For inspecting stored configuration, these are **partial JSON examples**,
not replacement files. Preserve unrelated keys, whitelist entries and gates.

Global scope, `configs/global.json`:

```json
{
  "agent_ops_whitelist": {
    "list_media": true,
    "post_media": true
  },
  "mcp_tools_enabled": ["list_media", "post_media"]
}
```

The MCP array illustrates an explicit allowlist; retain any other enabled
names in a real configuration. An absent `mcp_tools_enabled` already exposes
the registered ops, while `[]` exposes none. MCP startup also requires the
separate `mcp_ops_enabled` setting and authenticated connection.

Guild scope, `configs/<guild_id>.json`:

```json
{
  "agent_ops_gate": {
    "list_media": "everyone",
    "post_media": "everyone"
  }
}
```

Agent settings apply on the next invocation (external changes reload within
about two seconds). MCP exposure and cog/code changes require a bot restart.
The saved guild gate authorizes calls, and the model is offered only the
tools the invoking user may call. Test as a regular member; then ask the bot to call `list_media` and
`post_media` with a returned filename and the current channel ID.

## Key Registry

Every key the codebase actually reads or writes, by scope. Keep this current
when adding keys — it is the schema documentation for `configs/*.json`.
(The code examples elsewhere in this doc use invented keys like `prefix` for
illustration; they are not real.)

### Global scope (`global.json`)

| Key | Shape | Written by | Notes |
|-----|-------|-----------|-------|
| `discord_token` | `str` | `core.bootstrap` first-run prompt only (no command surface; never a panel field) | The bot's own Discord token. Resolution order is **`DISCORD_TOKEN` env var → this key → interactive prompt → exit with instructions**; the env var is **NEVER persisted here** (a panel-supplied secret belongs only where the operator set it). Written only *after* discord.py confirms the login, so a typo'd token never lands on disk. Plaintext, protected by the store's 0600/0700 modes — see the hardening note below |
| `superadmins` | `list[int]` user ids | `!addsuperadmin` / `!removesuperadmin`, plus the first-run bootstrap in `core.bootstrap` (any key also editable via the `!config` panel, superadmin) | Read through `core.permissions.get_superadmins`, which normalizes a bare int to a list and re-persists — the one "read that writes". On a first successful login with this list **empty or absent**, the Discord application owner (the team owner for team-owned apps) is added automatically and logged, retiring `!claimsuper` for new installs. Empty-list gate ONLY: an existing deployment is never touched |
| `ai_providers` | `{provider_id: {name, base_url, default_model, api_type?, requires_api_key?, models: {model_id: {cost_per_mtok_input?, cost_per_mtok_output?, max_completion_tokens?, reasoning_effort?, vision?, cache_input_ratio?, cache_ttl_seconds?}}}}` | `!aisettings` → Models & Providers (superadmin) | Absent ⇒ readers substitute the built-in `DEFAULT_PROVIDERS` seed. `api_type`: `"openai"` (default, OpenAI-compatible) or `"anthropic"`. `vision: false` marks a text-only model. `cost_per_mtok_output` sets the cooldown tier; with `cost_per_mtok_input` also set, both prices (and `cache_input_ratio`) drive cost estimates instead of the built-in table. See [model cache policy](#model-cache-policy) |
| `<PROVIDER>_API_KEY` | `str` (e.g. `XAI_API_KEY`) | `!aisettings` → Models & Providers key modal | Env var of the same name is the fallback; removed with its provider. Keys are entered ONLY via the panel modal (no slash parameter) |
| `DANBOORU_API_KEY`, `DANBOORU_LOGIN` | `str` | *no command surface* | Hand-edit or env only |
| `cooldown_tier_bases` | `{tier: seconds}` | *no command surface* — hand-edit | Absent/malformed ⇒ per-tier defaults from `COOLDOWN_TIERS`; the model modal's tier dropdown covers the common case |
| `cooldown_windows` | `list[[count, period_mult]]` | *no command surface* — hand-edit | Absent/malformed ⇒ `DEFAULT_COOLDOWN_WINDOWS`; validated on both write and read |
| `agent_ops_whitelist` | `{op_name: bool}` | `!aisettings` → 🛠 Agent Ops (superadmin) | The GLOBAL ceiling for the in-chat AGENT tier (`core/agent_gate`). Only ops set `true` here can EVER reach the agent — in any guild or DM — and only whitelisted GUILD/SELF ops render on a server's ⚙ tab. Absent/empty ⇒ nothing enabled (fail closed, the owner opts ops in). A whitelisted name whose op is currently unregistered (cog unloaded) is KEPT and just dropped from the effective set (same live-registry doctrine as `mcp_tools_enabled`). Governs the agent path ONLY; MCP uses its authenticated operator surface; direct registry callers authorize their own entrypoints |
| `ai_self_tools_enabled` | `bool` | *no command surface* — hand-edit | Master switch for SELF tools (`core.agent_gate.self_tools_enabled`). Absent ⇒ true. Each SELF tool still needs its whitelist entry and guild gate. See [SELF tool policy](#self-tool-policy-contracts) |
| `mcp_tools_enabled` | `list[str]` op names | `!aisettings` → MCP (superadmin) | Read at MCP server build ⇒ restart-bound; absent ⇒ all exposed ops |
| `mcp_ops_enabled` | `bool` | `!aisettings` → MCP (🔌 toggle, superadmin) | The MCP server's on/off switch. Read at bot startup ⇒ restart-bound; absent ⇒ off (fail closed) |
| `mcp_ops_token` | `str` | *no command surface* — hand-edit, or auto-generated | Bearer token for the MCP server. Config-first with an `MCP_OPS_TOKEN` env fallback; if the server is enabled with neither set, the bot generates one (`secrets.token_urlsafe(32)`) and writes it here. Never logged — read it out of `global.json` to connect a client |
| `mcp_ops_port` | `int` | *no command surface* — hand-edit | Port for the MCP server (loopback by default; `MCP_OPS_HOST` selects the bind). Config-first, then `MCP_OPS_PORT`, then `8765`. Read at bot startup ⇒ restart-bound |
| `error_logging` | `{default_channel?: int, category_channels?: {category: channel_id}, severity_channels?: {severity: channel_id}, log_unknown_commands?: bool, rate_limit_minutes?: int}` | `/logsettings` → Global (superadmin) | Global and guild destinations are independent; both receive errors/denials. Unknown prefix commands opt-in (default false). Global duplicate interval is 1–60 minutes (default 5). Category routes take priority; existing `command_error` is a fallback for new `command_denied` / `command_not_found` categories. See [error-handling.md](error-handling.md) |
| `reminders` | `list[{user_id, timestamp, text, delay}]` | `!remindme` + snooze buttons | Deliberately ONE global list across all guilds/DMs, filtered by `user_id` on read. `delay` (original duration, seconds) scales the snooze options; legacy rows without it get static 10m/1h/1d |
| `disabled_cogs` | `list[str]` bare lowercase cog names (e.g. `"gpt"`), never paths | `!cogs` panel, `!disable` / `!enable` | Deployment-level off switch: listed cogs stay on disk but are skipped by startup (filtered inside `core.cog_discovery.list_cog_modules`). Edits are config-only and bind at the next restart — the cog set is fixed at boot (#86). Only `control` and `admin` are exempt, with recovery reasons in `core.cog_discovery.PROTECTED_COGS`; folders do not grant protection. Filtering happens before feature import. Bare names survive folder moves; historical qualified names are normalized on read. How downstream forks carry upstream cogs without running them |
| `command_author_allowlist` | `list[int]` | *no command surface* | bot.py bot-authored-command dispatch; hand-edit only |

### Guild scope (`<guild_id>.json`)

| Key | Shape | Written by | Notes |
|-----|-------|-----------|-------|
| `admins` | `list[int]` user ids | `!addadmin` / `!removeadmin` / `!claimadmin` | Read via `core.permissions.is_admin` |
| `current_ai_provider` | `str` provider id | `!aisettings` → Server config | Absent ⇒ `DEFAULT_PROVIDER` — deleting a provider must account for guilds relying on that implicit default (`_do_removeprovider` does) |
| `current_ai_model` | `str` or absent | `!aisettings` → Server config | Absent ⇒ provider's `default_model` |
| `ai_history_min_messages` | `int`, default 15 | `/aisettings` → Server config → History Min / Max | Fresh/sliding context size; 1–1000 messages |
| `ai_history_max_messages` | `int`, default 30 | `/aisettings` → Server config → History Min / Max | Cache-reuse ceiling, at least the minimum and at most 1000. Equal bounds give fixed history; referenced messages may add more. The previous window's first message is kept while the last successful send is within `cache_ttl_seconds` and the window still fits in this maximum. See [AI history caching](ai-caching.md) |
| `ai_tool_budget` | `int`, default 8 | `/aisettings` → Server config (4/8/12/16) | Action tool calls per chat turn, clamped to 1–16 on read. See [guild tool effort](#guild-tool-effort) |
| `gpt_personality_data` | `{prompt: str}` | `!aisettings` → Personality modal | Guild persona for `!gpt` |
| `ai_enabled` | `bool` | `!aisettings` → Server config (💬 toggle) | Per-guild AI kill switch. Absent ⇒ ON. Gates the mention/reply chat path only — the panel stays reachable to turn it back on. Chat is guild-only: DMs never answer |
| `agent_ops_gate` | `{op_name: "off"\|"admin"\|"everyone"}` | `!aisettings` → ⚙ Server config, per-op select (**bot admin**) | The per-guild agent gate (`core/agent_gate`) under the global `agent_ops_whitelist`. For each whitelisted GUILD/SELF op a bot admin picks Off / Admin / Everyone; a missing or invalid entry is `"off"`. `off` withholds the op from everyone in that guild, `admin` offers it to bot admins only, `everyone` to any member. Ops declare no permission level. Off and whitelist revocation bind every actor, including superadmins, and are re-read at dispatch. Setting a `confirm_everyone` op to Everyone for the first time asks for a typed YES; the call path never checks that flag |
| `emoji_role_toggles` | `list[{channel_id, message_id, emoji, role_id}]` | `/role add\|delete\|sync` (setrole.py) | emoji is canonical str form (unicode char or `<:name:id>`); channel_id None = legacy-migrated entry pending `/role sync`; legacy nested-dict shape auto-migrates on first read |
| `error_logging` | as global, except `rate_limit_minutes` is global-only | `/logsettings` → Server (admin) | Independent destination/routes and `log_unknown_commands` toggle; cannot disable global reporting |
| `honeypot` | `{enabled: bool, channel_id: int, role_id: int, minutes: int, strip_all_other_roles: bool}` | `/honeypot`, `!honeypot`, `configure_honeypot` (admin) | Absent ⇒ disabled. One trap per guild; existing role/channel only. Minutes 0–10080 (default 10), stripping defaults false on each enable. Disable preserves saved settings and assigned roles. See [honeypot](honeypot.md) |
| `event_logging` | `{enabled?: bool, default_channel?: int, features?: {feature_id: {enabled?: bool, channel_id?: int}}, categories?: {category_id: {enabled?: bool, channel_id?: int}}}` | `/logsettings` → Activity (admin) | Guild event reports only; absent ⇒ enabled with #log destination. Category → feature → default channel → #log. All parent/child toggles must be on. Registered features drive controls; unloaded-feature config is retained. Independent of mandatory `error_logging` routes. See [event logging](event-logging.md) |
| `auto_responses` | `list[{triggers: list[str], responses: list[str], match?: "full"\|"contains"\|"regex", auto_delete?: bool}]` | `!autoresponse` panel, `add_autoresponse` / `edit_autoresponse` ops | Per-guild canned replies. `match` (default `full`) = whole-message equality; `contains` = substring; `regex` = case-insensitive RE2 pattern, with at most 200 characters per pattern and 25 triggers per entry. RE2 matches in linear time and rejects lookarounds and backreferences. Save and runtime use shared validation; malformed entries are rejected or skipped. Responses are strings and picked uniformly. Regex triggers keep their case; other triggers are stored lowercased. `auto_delete` deletes the triggering message. Capped at 25 entries (panel dropdown limit). Absent/empty ⇒ cog inert. First match wins; `list_autoresponses` reports the positional index. Bot-authored messages are ignored |
| `counting_channels` | `list[int]` | `!counter` / `/counter`, `configure_counter` op | Channels where members count up by one; Discord history holds the count. Absent/empty ⇒ cog inert. Channels the bot can no longer read or react in are removed |
| `counting_nonstrict_channels` | `list[int]` | `counter` cog | Subset of `counting_channels` without Manage Messages: wrong numbers get ❌ instead of being deleted, and chatter is allowed. Set at enable time and on a forbidden delete or a startup permission check (logged); re-enabling with Manage Messages restores strict |

### User scope (`user_<id>.json`)

| Key | Shape | Written by | Notes |
|-----|-------|-----------|-------|
| `points` | `int` | `core/points.py` (`add_points` / `set_points`) | Per-user balance; absent ⇒ 0. Negative balances are valid; adjustments and assignments use `Config.update_user` for atomic persistence. Other cogs import the helpers; they do not import the Points cog |

### Conventions

- Bare-int ctx is the idiom for context-free access: `config.set(guild_id, key, value)` resolves guild scope from the int (used by panels, migrations, raw-reaction handlers).
- `config.set(None, key, value, scope="global")` (or `set_global`) is the global-write idiom.
- Iterating all guilds goes through `config.guild_ids()` — the public enumeration API (guild configs are the digit-named files; the global config is `"global"`, user configs are `"user_<digits>"`). Never scan `config._configs` directly; the layout is private to `core/config.py`.

### File permissions (hardening, #83)

The global store holds **plaintext secrets** — the Discord token, provider API
keys, and the MCP bearer token — so the store is owner-only on disk:

- `configs/` is chmod **0700** at `Config.__init__`, on every start. That
  re-tightens an existing install too: `os.makedirs(mode=...)` is ignored when
  the directory already exists, so the mode is set explicitly either way.
- Every config file lands **0600**. Saves use the temporary-file replacement
  described above; the
  temp file is *created* with `os.open(..., 0o600)` **and `fchmod`'d on the
  open descriptor before any content is written**, so a secret is never on
  disk under a looser mode. Both steps are needed: O_CREAT's mode is masked by
  umask, and is ignored outright when the temp file already exists — a
  leftover `.tmp` from a previous crash keeps its old, possibly
  world-readable, mode. The landed file's mode is re-pinned after the rename.
- Both calls are **best-effort**: a filesystem that can't represent POSIX modes
  (a Windows share, some container volume mounts) is a downgrade in hardening,
  not a reason to refuse to start, so `OSError` is swallowed.

There is deliberately **no separate `secrets.json`** (owner decision, #83).
Provider API keys already lived in `global.json` as a documented accepted risk;
a second plaintext file would be separation without a distinct threat model.
Filesystem permissions are the control that actually does the work.

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

Do not assume `get()` returns a copy. Prefer copying a collection before
editing it so validation failures do not mutate the store before `set()`.
Neither pattern makes a read/modify/write sequence atomic; use the scoped
transaction API where one exists (`update_user` for user documents).

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
3. Replaces that scope's whole in-memory document and clears its pending-write flag
4. Prints a reload notice (there is no separate conflict warning)

This is polling, not coordination between concurrent writers. If the bot's
buffered save runs before it notices an external edit, that save can overwrite
the edit. Prefer the panel for normal configuration. For maintenance edits,
stop the bot cleanly first, preserve unrelated data, and replace valid JSON
with the documented owner-only permissions before starting it again. File
deletion is not a supported live-reset operation; remove keys via the editor.

This enables patterns like:
- Hot-editing config without restarting the bot
- External tools writing to config files

It is NOT a channel for cog-to-cog communication: cogs never consume each
other, and a config-key mailbox between two cogs is exactly the dependency
edge that doctrine forbids (a producer whose consumer is in `disabled_cogs`
strands data silently). Shared capability between cogs belongs in a headless
module — see `docs/cog-development.md` § Composing cogs (#88).

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

Called from `bot.py`'s finalizer on normal Python shutdown. Use `!restart` or
the documented graceful shell signal; SIGTERM/SIGKILL do not guarantee that
Python finalizers run, so pending buffered writes can be lost.

## Manual Flush

Force immediate write of all pending changes:

```python
config.flush()
```

Use this to finish pending writes before a dependent action. It is not a
power-loss durability guarantee: the store does not call `fsync`, and Windows
replacement has a remove/rename gap. A successful flush is also not a lock
against another process overwriting the file later.

### Model cache policy

Two per-model keys in `ai_providers[provider].models[model]`, edited by
superadmins under `/aisettings` → Models & Providers → **Input cache**:

- `cache_ttl_seconds` (default `300`, range 0–86400) is the assumed provider
  cache lifetime. It decides how long the history window keeps its first
  message (see [AI history caching](ai-caching.md)); 0 always starts a fresh
  window. It does not force provider retention.
- `cache_input_ratio` (default `0.10`, entered as a percentage) is the cached
  input price divided by the full input price, not a hit rate. It is used
  only for cost estimates, and only when the model also has both
  `cost_per_mtok_input` and `cost_per_mtok_output`; otherwise the built-in
  price table in `core/llm/usage.py` applies. It has no effect on history.

xAI requests carry a stable `x-grok-conv-id` per guild/channel/model.

### Guild tool effort

`ai_tool_budget`: guild-scoped integer, default 8, read clamped to 1–16.
Server config offers 4/8/12/16. Calls past the budget are refused in-band; the
hard stop is a model-request limit of budget + 2 (`core/agent_loop.py`).

### Image input capability

Image input is enabled unless the selected model entry explicitly sets
`vision: false` in `ai_providers.<provider>.models.<model>`. A render can send up
to eight images from message attachments and embed images/thumbnails, including
referenced messages and bot posts. Trigger images have first priority, then the
reply target; remaining slots retain earlier selections and fill from recent
history. Images stay attached to their source turn. Images beyond the limit,
failed loads and images disabled by `vision: false` remain text placeholders.
Details are in [AI history caching](ai-caching.md#image-inputs). Verify image
capability for the exact configured provider/model and opt text-only models out.

### Atomic user-document updates

`Config.update_user(user, mutate)` commits related values together immediately.
The callback receives a deep copy, may modify it and return a result, and must not
call Config methods. A callback or save failure leaves the previous in-memory
state intact. Use this for balances and associated reservation receipts rather
than multiple independent `set_user` calls; ordinary settings can keep using the
existing deferred-save API. This uses the existing temporary-file rename; it
is not an fsync-backed power-loss durability guarantee.

MCP binding is configured at startup with `MCP_OPS_HOST` (default `127.0.0.1`). Wildcard binds also require `MCP_OPS_TRUSTED_HOSTS`, a comma-separated list of client-facing IPs or hostnames. See [security](security.md#mcp-ops-server).

### SELF tool policy contracts

`ai_self_tools_enabled` is a global master switch for SELF tools (absent
means `true`), read by `core.agent_gate.self_tools_enabled` /
`resolve_agent_tools`. There is no per-guild copy.

The global `agent_ops_whitelist` still requires each tool name to be explicitly
true. Guild SELF also requires a non-off `agent_ops_gate` entry, which defaults
off, so a new guild starts with no tools; Admin/Everyone still apply. Off
binds superadmins too. Readers do
not persist defaults or automatically grant tools. There is no panel switch
for `ai_self_tools_enabled`; hand-edit it.

All GUILD/SELF action tools share one `ai_tool_budget` counter per turn.
Terminal `stay_quiet` is outside that counter.
