# Cog Development Guide

This guide covers creating custom cogs for LiterallyBot, from basic commands to advanced features with configuration management.

## Cog Architecture Overview

### Cog Categories

The split is one question: **if this cog is off, can you still turn it — or
anything else — back on from Discord alone?**

- **Core Cogs (`cogs/core/`)** - The recovery surface. `control.py`
  (`!enable`/`!disable` over `disabled_cogs`, `!cogs`, `!config`, restart) and
  `admin.py` (the `claimsuper` bootstrap). Never filtered by
  `disabled_cogs`, because disabling them leaves shell access as the only
  way back. Adding a cog here should be rare and needs this justification.
- **Optional Cogs (`cogs/optional/`)** - Everything else, and the main
  extension point. "Optional" means the deployment chooses, not that the
  cog is unimportant — error handling's `/logsettings` surface lives here,
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

See the [decorator reference](decorators.md) for the complete `@...` inventory
and standard command/check stacks.

Command mechanics (arguments, aliases, converters) are discord.py's territory —
see the [discord.py commands docs](https://discordpy.readthedocs.io/en/stable/ext/commands/commands.html).
The one repo rule: unhandled errors are automatically logged to Discord channels
via the global error handler, and for user input validation you parse arguments
yourself rather than relying on discord.py converters — see
[error-handling.md](error-handling.md).

## Configuration Integration

### Basic Config Usage

Read settings where they are consumed; write settings in the authorized editor
that owns them. For example, inside an admin-checked, guild-only AI settings
callback (including a fresh permission check on submission):

```python
enabled = self.bot.config.get(interaction, "ai_enabled", True)
self.bot.config.set(interaction, "ai_enabled", not enabled)
```

The Config store does not authorize its callers. Do not add another public
setter for an existing panel's keys or enable your own ops during cog startup.
See [tool activation](config-system.md#enabling-cog-tools) for the stored JSON
and the operator workflow.

### Managing Lists and Arrays

For an illustrative guild-scoped list, inside its authorized settings handler:

```python
favorites = list(self.bot.config.get(ctx, "favorites", []))
if item not in favorites:
    favorites.append(item)
    self.bot.config.set(ctx, "favorites", favorites)
```

`favorites` is an example key, not a shipped feature. Require a guild before
guild-scoped writes: a DM context falls back to global storage. Copying a list
avoids mutating the stored value before `set`; a get/copy/set sequence is not
an atomic transaction across concurrent writers.

## Permission Management

THE DECORATOR DECIDES. The gate is `core.utils.is_admin` / `is_superadmin` —
the central predicates, not a separate Discord-only check. `is_admin` includes
superadmins, configured guild admins, Discord administrators and the guild
owner, with the bot-account exclusion described in [security.md](security.md).
discord.py has two command systems, so the same function takes two wrappers. Do not invent a
third.

```python
import discord
from discord import app_commands
from core.utils import is_admin, is_superadmin, panel_slash_pin

@commands.command(hidden=True)
@commands.guild_only()
@commands.check(is_admin)          # prefix
async def aisettings(self, ctx):
    ...

@app_commands.command(name="aisettings")
@app_commands.guild_only()
@panel_slash_pin()                 # picker pin, not authorization
@app_commands.check(is_admin)      # slash — same predicate
async def aisettings_slash(self, interaction: discord.Interaction):
    ...
```

- Prefix: `@commands.check(is_admin)` or `@commands.check(is_superadmin)`.
- Slash: `@app_commands.check(is_admin)` or `@app_commands.check(is_superadmin)`.
- Gated slash also sets `guild_only` (never in DMs) and pins
  `default_permissions(manage_messages=True)` so ordinary members do not
  see the command in Discord's picker. Manage Messages is the typical
  moderator bit — not Discord Administrator, which locked out bot admins
  who are not server admins. Authorization is still the check. A bot
  admin/superadmin without Manage Messages in that guild still has the
  prefix twin. `/help` lists a command only if the invoker could run it.
- `/help` lists a command only if the invoker could run it, by reading the
  decorator (`__gate__` on `is_admin` / `is_superadmin`). A body
  `if not is_admin(...)` is defense in depth, not the gate — `/help`
  cannot see it.
- Bootstrap only: `!claimsuper` / `!claimadmin` have no decorator
  (empty-list / Discord-admin-once). Everything else uses the decorator.
- Never `@commands.has_permissions(administrator=True)` as the bot's admin
  concept. That is Discord's bit, caused a real lockout, and is not
  `is_admin`.

The permission model itself (who is an admin, the escalation guards) lives
in [security.md](security.md).

## Registering ops from a cog

An **op** is one atomic Discord-affecting capability, declared once in the ops
registry (`core/ops.py`) with its permission requirement and typed parameters.
Every frontend — the in-chat agent loop, the MCP server, the `!aisettings`
panel — is *generated* from those declarations. Registering an op makes it
available for selection without frontend code; the configured exposure gates
decide whether a frontend actually offers it. Registration must not change
those settings.

Two kinds share the registry: **API primitives** (raw Discord actions, declared
inline in `core/ops.py`) and **behavioral primitives** (what YOU are writing
here — a capability with the bot's own intelligence behind it, like wiring up
an emoji role toggle). The declaration shape and calling convention are
identical; only the registration path differs.

Register an op when the capability is something an agent or an external
operator should be able to *invoke*. Keep it a plain command when it is a piece
of interactive UI (a panel, a modal flow, a paginated browser).

### The service-function pattern (required)

**Factor the logic so both the command and the op call it.** Never expose an
interaction handler as an op.

A command handler and an op have incompatible contracts: the command handler
owns presentation (it sends replies, edits panels, answers an
`Interaction` exactly once), while an op returns plain data describing its
declared action. Calling a command handler from an op couples the action to
that command's UI and may produce duplicate replies. An `Interaction`-typed
handler also requires an interaction that a headless caller does not have.

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
2. **Separate action from presentation.** No `ctx.send` confirmation or
   `interaction.response` in a shared service. If sending is the declared
   action (for example `post_media`), the service may send the requested
   attachment to the target channel and return the resulting message ID.
   The frontend decides whether to add a confirmation; it must not repeat
   the action.
3. **Failures are raises or status fields, not messages.** Raise
   `RuntimeError` with a caller-presentable message for a hard failure; use a
   `status` field for expected outcomes the caller must branch on (`"created"`
   vs `"exists"` vs `"unchanged"`). The registry turns a raise into
   `OpResult(ok=False, error=...)` for every frontend identically.

### Composing cogs

Cogs never consume each other. No cog imports another cog's module, and no cog
calls `bot.get_cog("Other")` to reach another cog's methods — the cog-to-cog
dependency graph is empty by construction, and keeping it empty is what makes
disabling any cog safe: `disabled_cogs` can drop any optional cog without
stranding a caller.

When two cogs need the same capability, the capability belongs to neither cog:
it lives in a shared headless module (`utils/` for feature helpers, as in
`utils/points.py`; `core/` for infrastructure), and each cog is a thin Discord
surface over it — the
service-function pattern above, one level up.

The one legitimate `get_cog()` call inside a cog is fetching **its own**
instance from a detached `View` callback, where discord.py hands the callback
no cog reference. Fetching a *different* cog is the line not to cross.

There is deliberately **no dependency system** behind the `!cogs` panel — no
"cog X requires cog Y" metadata, no load ordering. With zero edges in the
graph there is nothing for it to enforce, and the cog set is fixed at boot
(#86), so there is no runtime unload to protect against either. Don't build
one until a real cog-to-cog edge exists — and before creating that edge,
prefer moving the shared capability into a headless module instead.

### Declaring the op

Import the module-level `op` decorator from `core.ops` and decorate an **async**
method. The decorator only *attaches* a spec to the function — it does not touch
the registry at import time.

```python
from core.ops import OpParam, OpScope, ParamKind, PermissionLevel, op


def _serialize_toggle(result: dict) -> dict:
    """Adapt the result for the frontend. Snowflakes go out as STRINGS:
    ids exceed 2**53 and would round in JSON transit, reporting the wrong
    message or role."""
    payload = {"status": result["status"], "emoji": result["emoji"],
               "message_id": str(result["message_id"]),
               "role_id": str(result["role_id"])}
    if result.get("old_role_id") is not None:
        payload["old_role_id"] = str(result["old_role_id"])
    return payload


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
        serialize=_serialize_toggle,
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

> **`serialize` and `agent_guidance` are a pair.** Guidance that tells the
> model to branch on a field (`"'exists' means nothing was written"`) is
> unfollowable unless the serializer actually ships that field. If you write
> one, review the other. When a consequential regression warrants a test,
> assert the frontend result through `op.result_payload(...)`. Do not create
> a payload test for every op; follow the [test retention rules](testing.md).

#### The fields

| Field | What it does |
|-------|--------------|
| **name** | Unique registry-wide (core ops included). A collision fails the whole cog's batch — pick something specific. This is the literal tool name a model sees. |
| **description** | Written *for a model*, not a changelog. State what it does, what it returns, and what it does **not** do — and when the excluded action is another op's job, name that op instead of asserting nonexistence (descriptions claiming a sibling "is not an op" go stale the day it ships). |
| **permission** | `PermissionLevel.EVERYONE` / `ADMIN` / `SUPERADMIN`, checked against the **invoking user** on every call, before any Discord id is resolved. Config never overrides this. |
| **params** | Typed `OpParam`s. The registry generates the JSON schema *and* resolves Discord entities from ids — see the kinds below. |
| **scope** | `OpScope.GUILD` / `DM` / `GLOBAL`. See below; this is a safety boundary, not a label. |
| **group** | Kebab-case id for the panel section. Core primitives use an `OP_GROUPS` key in `core/ops.py`. Cog/util ops pass any id plus optional `group_label`; the live registry learns the group when the op registers. Do not add cog groups to `OP_GROUPS`. Each group must stay under Discord's 25-option select cap. |
| **agent_guidance** | Optional. Extra instruction injected for the agent loop — use it for non-obvious result semantics ("status 'exists' means nothing was written"). |
| **serialize** | Callable adapting a return value to a JSON-safe dict. Without one, dict results pass through unchanged; other results become `{}`. Use it when objects or IDs need conversion. |

There is deliberately **no `origin` parameter**. Origin is stamped by the
registration *path* (`'core'` for `core/ops.py`'s inline registrations,
`'cog'` for this one), so a cog cannot claim to be an API primitive.

#### Choosing `scope`

`scope` answers *where the op acts*, and it is what makes each frontend's
universe derivable instead of hand-listed:

- **`OpScope.GUILD`** — acts on or inside a guild (channels, members, roles,
  emojis). The in-guild agent universe is selected from this set, queried live,
  then narrowed by the configured exposure gates.
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
  name reserved against the next registration). In practice unload happens at
  shutdown teardown: the cog set is fixed at boot (#86).

Ops are bound methods, so they see cog state (`self.bot`, caches, config
helpers) exactly as your commands do.

### Config keeps your op's name while its cog is disabled

Every frontend queries the registry **live** — never a snapshot taken at import
— so your ops appear in the panel as soon as the cog is in the boot set.

When a cog is not loaded (disabled via `disabled_cogs`), its op names stay in
the stored `agent_ops_whitelist` / `mcp_tools_enabled` config lists (and the
per-guild `agent_ops_gate` entries survive the same way), simply filtered out
of the *effective* set. Re-enabling the cog restores the guild's choice instead
of silently losing it. Don't write code that prunes unknown names out of stored
config.

### Checklist

- [ ] Logic lives in a service method; command and op both call it
- [ ] Op impl is `async`, returns plain result data, and leaves UI replies to
      its frontend; declared send actions report what was sent
- [ ] Name is unique registry-wide and reads as a tool name
- [ ] Description written for a model, including what it does *not* do
- [ ] `permission` is the tier you'd require of a human running it
- [ ] `scope=OpScope.GUILD` only if it genuinely cannot act outside the guild
- [ ] `group` is a kebab-case id; new cog groups use `group_label=` on `@op`, not a line in `OP_GROUPS`
- [ ] The bot restarts cleanly twice in a row with the cog in the boot set
      (no duplicate-name error from the batch registering again)

Live examples: `cogs/optional/setrole.py` (two ops sharing the slash commands'
services), `cogs/optional/danbooru.py` (one op sharing the prefix command's
search service), `cogs/optional/auto_response.py` (three ops sharing the admin
panel's services — the modal was carrying the validation until they were
factored out) and `cogs/optional/media.py` (two ops: `list_media`, and
`post_media`, which ACTS rather than returning data, because the `!<name>`
surface it mirrors is public while a returned host path would be the
admin-gated attachment surface in disguise).

### Media tools and discovery

The media cog already supplies two behavioral primitives:

| Tool | Action | Permission floor |
|------|--------|------------------|
| `list_media(guild_id?)` | Return sorted filenames; MCP must select a guild, chat defaults to the invoking guild | Everyone |
| `post_media(channel_id, name)` | Send the matching library file to a visible guild channel | Everyone |

`post_media` accepts a filename or prefix of at least two characters, like
`!<name>`. Prefer a full filename returned by `list_media` when discovering
an item. A successful result contains `status`, the matched `name`, and
`message_id`; the attachment has already been sent. Use `stay_quiet` when
the in-chat request needs only the clip and no final reply. Paths are never
returned. Addition and deletion remain admin-panel actions (`/media`).
If exposed later, both should have ADMIN floors and share panel services;
deletion should take an exact filename, and download failures must produce
tool errors rather than successful human-readable error strings.

Activation uses the [global and guild gates](config-system.md#enabling-cog-tools).
The stable `@op` description becomes the Pydantic tool schema description
(`core/agent_loop.py`, `Tool.from_schema`). `agent_guidance` is separate
code-authored text appended by `build_agentic_guidance` only for enabled
tools. Neither field currently has a runtime config editor or catalog callback.
There is no lazy schema discovery in the in-chat loop: enabled schemas are
sent with the run; `list_media` discovers library data on demand.

#### Dynamic catalog proposal (not implemented)

Keep static schemas plus `list_media` as the default. For a future experiment,
build a sorted, bounded catalog snapshot from the invoking guild only, once
per run, and append it to the changing request context after reusable history.
Include it only when media tools are enabled. Encode filenames as untrusted
data, cap entries and bytes, and identify truncation with a `list_media`
fallback. Resolve the actual file again at execution: a snapshot can become
stale. Never mutate registry descriptions shared across guilds or put a
guild catalog in the server-wide MCP schema.

This saves a discovery call for vague requests but adds input tokens to every
media-enabled run. Catalog changes in a tool description can invalidate cache
reuse at or after that changed prefix; they do not inherently destroy every
cached token. A stable sorted catalog need not change on every request, but
the current history cost estimator does not model changing tool definitions.
Keeping optional catalog data late in request context preserves more stable
prefix material. Compare actual cached-input usage, total cost, latency and
correct clip selection before adopting automatic injection. For the current
small library, explicit listing keeps freshness and behavior simpler.

## Advanced Features

Event listeners (`@commands.Cog.listener()`) and background tasks
(`@tasks.loop`) are standard discord.py — see the
[discord.py docs](https://discordpy.readthedocs.io/en/stable/ext/tasks/).
Repo rules: an `on_message` listener must return early on
`message.author.bot`, and a background task started in `__init__` must be
cancelled in `cog_unload`.

Config works without a live Context — pass a guild id directly:
`self.bot.config.set(1234567890, "setting_name", True)`.

### Panel components

Use `InvokerOnlyView` from `core.utils` before the Discord view base in the
inheritance list. It provides invoker checks and timeout cleanup. It does not
replace `is_admin` / `is_superadmin`: recheck the relevant predicate on control
callbacks and modal submissions, including after the invoker has been demoted.

For a fixed `discord.ui.View`, `@discord.ui.button(...)` declares a button on an
async method with `(self, interaction, button)`; see `_ConfirmEditView` in
[setrole.py](../cogs/optional/setrole.py). A callback decorator does not supply
permission checks.

For dynamic settings, follow [AiSettingsView](../cogs/optional/gpt.py) or
[LogSettingsView](../cogs/optional/error_handler.py): build a
`discord.ui.LayoutView` from `TextDisplay`, `ActionRow`, buttons, and selectors,
assign callbacks, and rebuild it from current config after an edit. Use a
`discord.ui.Modal` for typed input. `on_submit` and `interaction_check` are
methods, not decorators. Keep slash panels ephemeral and store the panel message
when needed for edits and timeout cleanup. Components V2 panels set
`expiry_text = None` because their message has no separate content field.

## Testing & Restarting Tips

Follow [the qualitative test policy](error-handling.md#test-quality-and-development-checks). A new cog or op does not
require its own schema, metadata, forwarding, and payload test bundle.

- **The cog set is fixed at boot (#86)** — there is no hot-reload. To pick up a
  code change, restart the bot: `!restart` in Discord (or the `!cogs` panel's
  Restart button — same exit path), or restart the service from the shell,
  which is also where deployment `git pull`s happen.
- Run `pytest tests/` before restarting; a cog that fails to import is reported
  to Discord at startup but simply won't be there.
- Let unexpected failures reach the central error handler. Catch only when
  recovering or adding context; explicitly report handled failures through
  the [error pipeline](error-handling.md#logging-errors-manually).
- Keep commands async-friendly and avoid blocking calls

For a systemd deployment, restart the installed unit's process:

```bash
bot_pid=$(systemctl show "$UNIT".service -p MainPID --value)
if [[ "$bot_pid" =~ ^[1-9][0-9]*$ ]]; then
    kill -INT "$bot_pid"
else
    echo "No running bot process found for $UNIT.service" >&2
fi
```

`$UNIT` is the name chosen by `scripts/install_service.sh` (the directory name
by default). SIGINT lets the Python shutdown path flush buffered config;
plain `kill` sends SIGTERM and can skip that finalizer. Validate the PID:
`kill 0` would signal the caller's process group. systemd relaunches the bot
in about three seconds. Never use `pkill -f`
(it can match its own command), or `sudo systemctl restart` from a session that
cannot supply a password. `Restart=always` relaunches a killed process.

## Disabling Cogs Per Deployment
- `!cogs` (superadmin panel) or `!disable my_cog` / `!enable my_cog` maintain
  the global `disabled_cogs` config list. These write config only and bind at
  the next restart. A disabled cog stays on disk but is skipped by startup
  until re-enabled.
- This is how downstream forks of this codebase carry upstream cogs without
  running them — disable, don't delete, so upstream merges stay clean.
- `cogs/core/` can't be disabled; it is the means of re-enabling everything
  else. The filter tests `group != CORE_COG_GROUP`, so any future cog group
  is disableable by default rather than silently inheriting that immunity.
