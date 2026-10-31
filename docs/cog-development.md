# Cog Development Guide

This guide covers creating custom cogs for LiterallyBot, from basic commands to advanced features with configuration management.

## Cog Architecture Overview

### Feature ownership and recovery

All feature cogs live directly in `cogs/`. Each owns its prefix/slash commands,
listeners, background tasks, views, and behavioral `@op` adapters. Shared
headless implementations and state live in `core/`; a private helper used only
inside one cog may remain there.

`core/cog_discovery.py` owns discovery and recovery protection. Its
`PROTECTED_COGS` mapping names `control` (configuration, enable/disable and
restart) and `admin` (ownership recovery and administrator management), with
explicit reasons. Loader and operator controls consult the same policy. New
cogs are disableable by default; adding protection needs a recovery reason.
Protection does not depend on a folder, decorator, or imported cog class.

`list_cog_modules(config=None)` discovers public Python modules directly in
`cogs/`, with protected cogs first. With a Config it filters `disabled_cogs`
**before importing feature modules**, so disabled optional dependencies and
import side effects cannot interfere with startup. Without Config it includes
disabled modules for operator listings. Stable bare IDs survive folder moves;
`cog_name` also accepts current and historical qualified module names.

The optional `error_handler` cog owns `/logsettings`; mandatory error handling
is wired independently by `bot.py` through `core/error_handler.py`.

### Enforced ownership boundaries

`tests/test_architecture.py` parses runtime Python with the standard-library AST.
It rejects core-to-cog and cog-to-cog imports, including aliases and relative
imports. Cog classes, Discord command/listener/task declarations, and behavioral
`@op` adapters must live in `cogs/`; behavioral ops must decorate Cog methods.
Use a shared `core/` service when two features need the same behavior.

This does not ban Discord SDK imports in shared message or panel helpers, raw
`registry.op` API primitives, or composition-root event wiring in `bot.py`. The
checks cover static imports and declarations; runtime lookups and dynamically
constructed imports still need code review.

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

THE DECORATOR DECIDES. The gate is `core.permissions.is_admin` / `is_superadmin` —
the central predicates, not a separate Discord-only check. `is_admin` includes
superadmins, configured guild admins, Discord administrators and the guild
owner, with the bot-account exclusion described in [security.md](security.md).
discord.py has two command systems, so the same function takes two wrappers. Do not invent a
third.

```python
import discord
from discord import app_commands
from discord.ext import commands
from core.permissions import is_admin, is_superadmin
from core.panels import panel_slash_pin

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
it lives in a shared headless module in `core/`, and each cog owns its adapters
onto it. For example, `core/points.py` owns balance storage and plain get/add/set
helpers; `cogs/points.py` owns the commands and all three points ops. Keeping
balances available to other features does not keep Points tools registered when
Points is disabled. This is the service-function pattern one level up.

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
method on the owning cog. The decorator only *attaches* a spec to the method;
it does not touch the registry at import time. Shared `core/` services do not
register behavioral tools themselves.

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
| **scope** | `OpScope.GUILD` / `DM` / `GLOBAL` / `SELF`. See below; this is a safety boundary, not a label. |
| **group** | Kebab-case id for the panel section. Core primitives use an `OP_GROUPS` key in `core/ops.py`. Cog ops pass any id plus optional `group_label`; the live registry learns the group when the op registers. Do not add cog groups to `OP_GROUPS`. Each group must stay under Discord's 25-option select cap. |
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
- **`OpScope.SELF`** — acts only for `ctx.author`, the non-bot invoker.
  Declare payloads only (STRING, INTEGER, BOOLEAN, STRING_LIST); never a user,
  actor, owner, subject, target, entity, snowflake or INTERNAL parameter.
  Both dispatch paths reject missing/bot actors and unexpected arguments.
  MCP excludes/refuses this scope. Exposure requires the shared whitelist
  and SELF switches, plus the guild gate for public use.
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

The agent and settings panel query the registry **live**; MCP builds its tool
surface from the registry at server startup and keeps it until restart. Neither
uses an import-time snapshot that misses loaded cogs. Registration makes an op
eligible for exposure; frontend configuration selects exposure, and each call
still passes the op's authorization gate. MCP's unset allowlist exposes the non-SELF
registered universe; registering a cog must not write exposure settings.

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

Live examples: `cogs/setrole.py` (four ops sharing the slash commands'
services), `cogs/danbooru.py` (one op sharing the prefix command's
search service), `cogs/auto_response.py` (three ops sharing the admin
panel's services — the modal was carrying the validation until they were
factored out) and `cogs/media.py` (two ops: `list_media`, and
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

Media additions allow one download per guild and two across the bot. Direct
HTTP transfers use finite connect/read timeouts; streamed downloads and yt-dlp
staging enforce a source budget of twice the guild upload limit, with a 200 MiB
ceiling. Published files must fit the guild upload limit and a 100 MiB ceiling.
yt-dlp downloads one item with finite socket timeouts and retries; progress
checks enforce a two-minute deadline. This deadline is cooperative during
progress callbacks, not a hard process timeout across extraction and merging.
ffmpeg trimming has a 60-second process timeout. Failed downloads remove staging
files and release admission slots.

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

### GPT history and image inputs

`!gpt` always uses `LLMClient.run_agent`, including tool-less guilds and DM
command contexts. DMs expose zero bot tools; automatic mention/reply handling
remains guild-only. `stay_quiet` is a terminal output tool on every run.
The hard-budget fallback is another agent run with zero bot tools and the
budget note. Existing tool authorization and guild confinement still apply.

History turns always carry message IDs. Attachment images and embed images /
thumbnails belong to their source turn, including referenced messages and bot
posts; the closing request context has no pixels. Pydantic-ai response parts
cannot contain input `BinaryContent`, so the adapter places bot-posted pixels
in a labeled user part immediately after the corresponding assistant text.

Each render sends at most eight images. Trigger images have first priority,
then reply-target images, each in attachment/embed order. Remaining slots keep
retained pins; only new required trigger/reply images can evict the oldest pins
(by message ID and position). Vacancies fill from the newest other images.
A bounded per-conversation memo preserves these selections while they remain
in the window, so text-only follow-ups keep shared prefixes stable. Overflow
images get `[image: filename]` placeholders, including excess explicit inputs.
Failed downloads and images excluded by `vision: false` also get placeholders.
Image availability never changes the system prompt. The history cost estimate
includes a conservative 1,792 input tokens per image.

`core/ai_images.py` holds an LRU of normalized JPEG/PNG bytes keyed by attachment
ID (or URL without query/fragment), bounded at 64 entries and 64 MiB. Failed
loads are cached for 30 seconds. Up to four downloads/processes run concurrently;
Pillow processing runs off the event loop. Inputs retain the Discord host
allowlist, 8 MiB body cap, and 2048-pixel normalization; redirects are refused.
A signed CDN 403/404 refreshes the source message once per preparation, then
retries the same image identity. Eviction or failure expiry can cause a reload;
deterministic normalization keeps successful image bytes stable. Failed loads
log a WARNING with the URL query and fragment removed. History comparison
snapshots retain image fingerprints rather than pixels; expired snapshots are
pruned on successful commits.

## Registering event reports

Features that emit guild event reports use the headless shared
[event logging service](event-logging.md). Register stable categories in
`cog_load`, unregister the owner in `cog_unload`, and call `report` from the
producer. `/logsettings` discovers registrations; do not add feature listeners
or feature inventories to a central logging cog. Error and permission-denial
reporting continues through the independent mandatory error pipeline.

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

Use `InvokerOnlyView` from `core.panels` before the Discord view base in the
inheritance list. It provides invoker checks and timeout cleanup. It does not
replace `is_admin` / `is_superadmin`: recheck the relevant predicate on control
callbacks and modal submissions, including after the invoker has been demoted.

For a fixed `discord.ui.View`, `@discord.ui.button(...)` declares a button on an
async method with `(self, interaction, button)`; see `_ConfirmEditView` in
[setrole.py](../cogs/setrole.py). A callback decorator does not supply
permission checks.

For dynamic settings, follow [AiSettingsView](../cogs/gpt.py) or
[LogSettingsView](../cogs/error_handler.py): build a
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
- Only IDs in `core.cog_discovery.PROTECTED_COGS` are exempt: `control` and
  `admin`. Operator controls show the protection reason; manually listing these
  IDs in `disabled_cogs` cannot remove the recovery path.
- After restart, a disabled feature has no commands, listeners, tasks, setup-time
  views or behavioral ops. Settings and shared services remain; assigned roles,
  reactions and posted messages are not undone. Independent raw API primitives
  retain their own exposure and permission policy.
- The switch is global and deployment-wide. Live guild settings, such as event
  report route enabled flags, control their behavior separately.

### Shared agent execution

`core.agent_loop.run_agent_turn` owns tool construction, terminal quiet,
narration nudge, usage logging and hard-limit fallback. GPT delegates to it;
other trusted callers supply their assembled messages, context, resolved tool
names, closing request, metadata and optional dispatch-validity hook. Persona,
history scraping, lifecycle checks and delivery remain with the caller.
Use `resolve_agent_tools(ctx, scopes)` before assembling history/guidance, and
include that same policy in the cache key. `build_agentic_guidance(names)`
supports ID-free guidance; guild callers retain the existing public guidance
by supplying guild/channel IDs.

Set `sequential=True` on an op whose writes must execute sequentially in the
provider loop; this metadata is carried by both registration paths into Tool.
This does not replace storage transactions or cancellation checks. The
synchronous `validity_check` hook runs immediately before dispatch; services
with stronger atomic lifecycle requirements must enforce them at mutation.
`LLMClient.run_agent(max_retries=0)` optionally disables SDK transport retries.
Omitting the override preserves SDK defaults; it does not change Agent
validation retries. The shared runner forwards it to main/nudge/fallback.
