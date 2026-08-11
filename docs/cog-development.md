# Cog Development Guide

This guide covers creating custom cogs for LiterallyBot, from basic commands to advanced features with configuration management.

## Cog Architecture Overview

### Cog Categories

The split is one question: **if this cog is off, can you still turn it — or
anything else — back on from Discord alone?**

- **Core Cogs (`cogs/core/`)** - The recovery surface. `control.py` (cog
  load/unload/reload, `!enable`/`!disable`, `!config`, restart) and
  `admin.py` (the `claimsuper` bootstrap). Never filtered by
  `disabled_cogs`, because disabling them leaves shell access as the only
  way back. Adding a cog here should be rare and needs this justification.
- **Optional Cogs (`cogs/optional/`)** - Everything else, and the main
  extension point. "Optional" means the deployment chooses, not that the
  cog is unimportant — error handling's `!errorlog` surface lives here,
  since `bot.py` wires error handling from `core/error_handler.py` and
  does not need the cog loaded.

New cogs go in `cogs/optional/` unless they are part of the recovery path.

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

Unhandled errors are automatically logged to Discord channels via the global error handler. For user input validation, parse arguments yourself rather than relying on discord.py converters - see [error-handling.md](error-handling.md) for details.

```python
@commands.command()
async def divide(self, ctx, *, args: str = None):
    """Divide two numbers"""
    usage = "Usage: `!divide <num1> <num2>`"

    if not args:
        await ctx.send(usage)
        return

    parts = args.split()
    if len(parts) != 2:
        await ctx.send(usage)
        return

    try:
        num1, num2 = float(parts[0]), float(parts[1])
    except ValueError:
        await ctx.send(usage)
        return

    if num2 == 0:
        await ctx.send("Cannot divide by zero!")
        return

    await ctx.send(f"{num1} / {num2} = {num1 / num2}")
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
from core.utils import is_admin

@commands.command()
async def admin_only(self, ctx):
    """Command only admins can use"""
    if not is_admin(self.bot.config, ctx):
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

## Registering ops from a cog

An **op** is one atomic Discord-affecting capability, declared once in the ops
registry (`core/ops.py`) with its permission requirement and typed parameters.
Every frontend — the in-chat agent loop, the MCP server, the `!aisettings`
panel — is *generated* from those declarations. Registering an op from your cog
therefore exposes your capability to all of them at once, without writing any
frontend code.

Register an op when the capability is something an agent or an external
operator should be able to *invoke*. Keep it a plain command when it is a piece
of interactive UI (a panel, a modal flow, a paginated browser).

### The service-function pattern (required)

**Factor the logic so both the command and the op call it.** Never expose an
interaction handler as an op.

A command handler and an op have incompatible contracts: the command handler
owns presentation (it sends messages, edits embeds, answers an
`Interaction` exactly once), while an op must return plain data and send
nothing. Calling a command handler from an op means an agent's tool call
side-effects a message into the channel, and an `Interaction`-typed handler
simply cannot be called at all without one.

So the logic goes in a third place both can call:

```python
class SetRole(commands.Cog):

    # --- services ---------------------------------------------------------
    # Headless logic shared by the slash commands and the cog ops. These take
    # plain Discord objects, return plain data, and never touch an Interaction
    # or send anything — the caller presents the outcome.

    async def _add_toggle(self, guild, channel, message_id: int, emoji: str,
                          role, replace_existing: bool = False) -> dict:
        ...
        return {"status": "created", "emoji": emoji_str,
                "message_id": message_id, "role_id": role.id}
```

Three rules for a service function:

1. **Plain objects in, plain data out.** Discord objects (`Guild`, `Member`,
   `Role`, channel) and scalars in; a JSON-serializable `dict` out.
2. **It sends nothing.** No `ctx.send`, no `interaction.response`. The caller
   decides how to present the result.
3. **Failures are raises or status fields, not messages.** Raise
   `RuntimeError` with a caller-presentable message for a hard failure; use a
   `status` field for expected outcomes the caller must branch on (`"created"`
   vs `"exists"` vs `"unchanged"`). The registry turns a raise into
   `OpResult(ok=False, error=...)` for every frontend identically.

### Declaring the op

Import the module-level `op` decorator from `core.ops` and decorate an **async**
method. The decorator only *attaches* a spec to the function — it does not touch
the registry at import time.

```python
from core.ops import OpParam, OpScope, ParamKind, PermissionLevel, op

class SetRole(commands.Cog):

    @op(
        "add_emoji_role_toggle",
        "Add an emoji role toggle: reacting to the given message with the "
        "given emoji grants or removes the role. If that emoji on that "
        "message already toggles a different role, nothing is written unless "
        "replace_existing is true.",
        PermissionLevel.ADMIN,
        params=[
            OpParam("channel", ParamKind.CHANNEL,
                    "Channel containing the target message."),
            OpParam("message_id", ParamKind.SNOWFLAKE,
                    "Message users react to."),
            OpParam("emoji", ParamKind.STRING,
                    "Emoji to react with: a unicode character, or "
                    "'<:name:id>' for a custom emoji."),
            OpParam("role", ParamKind.ROLE, "Role the reaction toggles."),
            OpParam("replace_existing", ParamKind.BOOLEAN,
                    "Retarget the emoji if it already toggles a different role.",
                    required=False, default=False),
        ],
        agent_guidance=(
            "Check the returned status: 'exists' means a different role is "
            "already bound and NOTHING was written — report the conflict to "
            "the user rather than silently retrying with replace_existing."),
        scope=OpScope.GUILD,
        group="role-automation",
    )
    async def op_add_emoji_role_toggle(self, ctx, channel, message_id: int,
                                       emoji: str, role,
                                       replace_existing: bool = False) -> dict:
        guild = getattr(channel, "guild", None)
        if guild is None:
            raise ValueError("add_emoji_role_toggle requires a guild channel.")
        return await self._add_toggle(guild, channel, int(message_id), emoji,
                                      role, replace_existing=bool(replace_existing))
```

The op impl is a thin adapter: validate, coerce, delegate to the service.

#### The fields

| Field | What it does |
|-------|--------------|
| **name** | Unique registry-wide (core ops included). A collision fails the whole cog's batch — pick something specific. This is the literal tool name a model sees. |
| **description** | Written *for a model*, not a changelog. State what it does, what it returns, and what it does **not** do. |
| **permission** | `PermissionLevel.EVERYONE` / `ADMIN` / `SUPERADMIN`, checked against the **invoking user** on every call, before any Discord id is resolved. Config never overrides this. |
| **params** | Typed `OpParam`s. The registry generates the JSON schema *and* resolves Discord entities from ids — see the kinds below. |
| **scope** | `OpScope.GUILD` / `DM` / `GLOBAL`. See below; this is a safety boundary, not a label. |
| **group** | Kebab-case id from `OP_GROUPS` in `core/ops.py`; decides which panel section renders it. Each group must stay under Discord's 25-option select cap. |
| **agent_guidance** | Optional. Extra instruction injected for the agent loop — use it for non-obvious result semantics ("status 'exists' means nothing was written"). |
| **serialize** | Optional callable turning a non-JSON return value into a dict. Unneeded if you already return plain data. |

There is deliberately **no `origin` parameter**. Origin is stamped by the
registration *path* (`'core'` for `core/ops.py`'s inline registrations,
`'cog'` for this one), so a cog cannot claim to be a core primitive.

#### Choosing `scope`

`scope` answers *where the op acts*, and it is what makes each frontend's
universe derivable instead of hand-listed:

- **`OpScope.GUILD`** — acts on or inside a guild (channels, members, roles,
  emojis). **The in-guild agent universe is exactly this set**, queried live.
  This is what almost every cog op should be.
- **`OpScope.DM`** — acts on a one-to-one DM conversation, no guild involved.
  Never offered to the guild-confined agent loop.
- **`OpScope.GLOBAL`** — acts on the bot itself across guilds (`list_guilds`).
  Also never offered to the agent loop.

Declaring `GUILD` is what puts your op in front of a guild's members, so an op
that can reach outside the invoking guild must not claim it.

#### Param kinds

Discord entities travel as **ids on the wire** and are resolved to live objects
before your impl runs, with guild confinement applied. Snowflakes are carried as
decimal *strings* (they exceed 2**53 and would round as JSON numbers).

| Kind | Wire param | Your impl receives |
|------|-----------|--------------------|
| `CHANNEL` | `channel_id` | channel object |
| `MESSAGE` | `channel_id` + `message_id` | `discord.Message` |
| `MEMBER` / `USER` | `user_id` | `Member` / `User` |
| `ROLE` | `role_id` | `discord.Role` |
| `GUILD` | `guild_id` | `discord.Guild` |
| `STRING`, `INTEGER`, `BOOLEAN` | same name | the scalar |
| `SNOWFLAKE` | same name (string) | an id you handle yourself — *not* resolved |
| `INTERNAL` | never on the wire | only object-based callers pass it |

### Lifecycle

You do not call the registry yourself. `LiterallyBot.add_cog` /
`remove_cog` (in `bot.py`) wrap the discord.py cog lifecycle:

- **On load** — `registry.register_cog_ops(cog)` scans the instance's bound
  methods for specs and registers the batch **all-or-none**. A duplicate name or
  a malformed declaration registers *zero* ops and ejects the cog, so a loaded
  cog never has half its ops missing.
- **On unload** — `registry.unregister_owner(cog)` drops the batch, in a
  `finally`, so a cog whose own teardown raises still leaves no orphaned ops
  (an op whose owner is gone would fail confusingly at call time and keep its
  name reserved against the reload).
- **On `!reload`** — discord.py's reload is atomic: it tears the old cog down
  (dropping its ops) before adding the new one, and **restores the old cog if
  the new one fails to load**, which re-registers the old batch through the same
  path. A broken edit leaves the previous ops working.

Ops are bound methods, so they see cog state (`self.bot`, caches, config
helpers) exactly as your commands do.

### Config keeps your op's name across an unload

Every frontend queries the registry **live** — never a snapshot taken at import
— so your ops appear in the panel the moment the cog loads.

When a cog is unloaded, its op names stay in the stored `bot_tools_enabled` /
`mcp_tools_enabled` config lists and are simply filtered out of the *effective*
set. Reloading the cog restores the guild's choice instead of silently losing
it. Don't write code that prunes unknown names out of stored config.

One caveat: the **MCP** tool surface is built once at server start, so a cog
loaded afterwards contributes its ops to MCP only on the next bot restart. The
in-chat agent loop and the panel pick them up immediately.

### Checklist

- [ ] Logic lives in a service method; command and op both call it
- [ ] Op impl is `async`, returns a JSON-serializable dict, sends nothing
- [ ] Name is unique registry-wide and reads as a tool name
- [ ] Description written for a model, including what it does *not* do
- [ ] `permission` is the tier you'd require of a human running it
- [ ] `scope=OpScope.GUILD` only if it genuinely cannot act outside the guild
- [ ] `group` exists in `OP_GROUPS` (add it there if you need a new one)
- [ ] `!reload <cog>` twice in a row still works (no duplicate-name error)

Live examples: `cogs/optional/setrole.py` (two ops sharing the slash commands'
services) and `cogs/optional/danbooru.py` (one op sharing the prefix command's
search service).

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

## Disabling Cogs Per Deployment
- `!cogs` (superadmin panel) or `!disable my_cog` / `!enable my_cog` maintain
  the global `disabled_cogs` config list. A disabled cog stays on disk but
  is skipped by startup, `!reload`, and `!load` until re-enabled.
- This is how downstream forks of this codebase carry upstream cogs without
  running them — disable, don't delete, so upstream merges stay clean.
- `cogs/core/` can't be disabled; it is the means of re-enabling everything
  else. The filter tests `group != CORE_COG_GROUP`, so any future cog group
  is disableable by default rather than silently inheriting that immunity.
