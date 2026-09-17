# Decorator reference

A lookup chart for the `@...` declarations in LiterallyBot. Start with the
custom helpers and command checks; the remaining tables cover command creation,
events, tasks, UI, Python utilities, and tests.

**Inventory scope:** every custom decorator API defined by this repository and
every decorator form used in its Python source, including disabled optional cogs
and tests. Audited against the working tree on 2026-09-17 (discord.py 2.6.4).
Repeated uses and differently named instances of the same API share a row. The
underlying libraries offer additional decorators; their full API references are
linked below. This is the repository's inventory, not an inventory of every
installed dependency.

## Custom decorators

| Decorator | Import / owner | Apply to | Purpose and limits | Deep dive |
|---|---|---|---|---|
| `@op(name, description, permission, ...)` | `from core.ops import op` | Async cog method or headless module function | Declares a behavioral op. Cog loading registers methods; module functions require `registry.register_module_ops(module)` at startup. Declaration alone does not register or enforce permissions on a direct Python call. | [Declaring an op](cog-development.md#declaring-the-op), [execution and permissions](security.md#agentic--ops-execution-model) |
| `@registry.op(name, description, permission, ...)` | `from core.ops import registry` | Async function in `core/ops.py` | Immediately registers a raw Discord API primitive. Registration stamps `origin='core'`; the behavioral registration paths stamp `origin='cog'`. Neither decorator takes an `origin` argument. | [Ops architecture](cog-development.md#registering-ops-from-a-cog) |
| `@panel_slash_pin()` | `from core.utils import panel_slash_pin` | Slash command | Sets the Manage Messages default permission for picker visibility. Does **not** enforce bot-admin authorization; pair with a check and `guild_only`. Wraps `app_commands.default_permissions(manage_messages=True)`. | [Permission recipe](cog-development.md#permission-management), [command gates](security.md#command-gates-prefix-and-slash) |
| `@suppress_command_not_found(*patterns)` | `from core.error_handler import suppress_command_not_found` | Cog **class** | Accepts regex strings or compiled patterns. Full-matches prefix-stripped, trimmed text only for human-authored prefix `CommandNotFound`. Only loaded cogs participate; command/event exceptions and denials remain visible. | [Regex suppression](error-handling.md#suppressing-commandnotfound) |

`@op` supports `params`, `serialize`, `agent_guidance`, `scope`, `group`, and
`group_label`; `@registry.op` supports the same fields except `group_label`.
See the [field chart](cog-development.md#the-fields) and
[scope rules](cog-development.md#choosing-scope). Current examples are
[reaction-role ops](../cogs/optional/setrole.py), [module ops](../utils/points.py),
and [RNG suppression](../cogs/optional/rng.py).

## Authorization and server context

`is_admin` and `is_superadmin` are **predicates**, not decorators. Import them
from `core.utils` and pass them to the wrapper for the correct command system.
`gate_of()` reads their attached metadata for help visibility and denial details.

| Decorator | Command system | What it requires | Deep dive |
|---|---|---|---|
| `@commands.check(is_admin)` | Prefix (`!`) | The central bot-admin predicate must pass. | [Permission model](security.md#permission-model) |
| `@commands.check(is_superadmin)` | Prefix (`!`) | The invoking user must be a global superadmin. | [Permission model](security.md#permission-model) |
| `@app_commands.check(is_admin)` | Slash (`/`) | The same bot-admin predicate, with an Interaction. | [Command gates](security.md#command-gates-prefix-and-slash) |
| `@app_commands.check(is_superadmin)` | Slash (`/`) | The same global-superadmin predicate, with an Interaction. | [Command gates](security.md#command-gates-prefix-and-slash) |
| `@commands.guild_only()` | Prefix (`!`) | Invocation must be in a server; required before accessing `ctx.guild` attributes. | [Cog rules](cog-development.md#command-development), [denial reporting](error-handling.md#severity-and-command-activity) |
| `@app_commands.guild_only()` | Slash (`/`) | Server context; use on the top-level slash command/group as supported by Discord. | [Permission recipe](cog-development.md#permission-management) |

A check failure becomes **Command denied**, with required and actual access
levels. A name that cannot be resolved becomes **Command not found**. Those are
separate [logging categories](error-handling.md#severity-and-command-activity).
An untagged custom predicate can still be passed to either `check` API, but it
does not acquire admin metadata automatically.

## Commands and interface declarations

Imports: `from discord.ext import commands`, `from discord import app_commands`,
and `import discord`.

| Decorator | Apply to / current example | What it does | Deep dive |
|---|---|---|---|
| `@commands.command(...)` | Async cog method; `RNG.dice` | Creates a prefix command. `name`, `aliases`, and `hidden=True` are options; hiding a command does not authorize it. | [Command development](cog-development.md#command-development) |
| `@commands.group(...)` | Async cog method; `Cleanup.cleanup` | Creates a prefix command group. `invoke_without_command=True` lets its callback handle invocation without a subcommand. | [Command development](cog-development.md#command-development) |
| `@<prefix_group>.command(...)` | Async method; `@cleanup.command(...)` | Adds a subcommand to an existing prefix group. | [Command development](cog-development.md#command-development) |
| `@app_commands.command(...)` | Async cog method; `logsettings_slash` | Creates a top-level slash command. | [Permission recipe](cog-development.md#permission-management) |
| `@<slash_group>.command(...)` | Async method; `@role.command(...)` | Adds a slash subcommand to an `app_commands.Group`. This is a different API from the prefix group's decorator. | [Command development](cog-development.md#command-development), [role example](../cogs/optional/setrole.py) |
| `@app_commands.describe(parameter="help", ...)` | Slash callback; `/role add` and `/role delete` | Provides parameter descriptions in Discord. Does not validate input or grant access. | [Command development](cog-development.md#command-development) |
| `@discord.ui.button(...)` | Async `discord.ui.View` method; `_ConfirmEditView.confirm` | Declares a button and its callback. Authorization belongs in the view/callback; the button decorator supplies none. | [Panel components](cog-development.md#panel-components) |

For library options, see the official [prefix command API](https://discordpy.readthedocs.io/en/stable/ext/commands/api.html#decorators)
and [application-command API](https://discordpy.readthedocs.io/en/stable/interactions/api.html#decorators).
The live source examples are [cleanup](../cogs/optional/cleanup.py),
[role commands/buttons](../cogs/optional/setrole.py), and
[log settings](../cogs/optional/error_handler.py).

## Events, background tasks, and error entrypoints

Import task decorators with `from discord.ext import tasks`. Names such as
`change_status` below refer to the `Loop` object created by `@tasks.loop(...)`.

| Decorator | Apply to / current spellings | What it does | Deep dive |
|---|---|---|---|
| `@commands.Cog.listener()` | Async cog event method, usually `on_message`, `on_ready`, etc. | Adds a listener alongside other handlers. Handling a message here does not consume it for command dispatch. An explicit event name can be passed. | [Events and tasks](cog-development.md#advanced-features), [dynamic shortcut suppression](error-handling.md#suppressing-commandnotfound) |
| `@bot.event` | Async function in `bot.py` | Registers the bot-level handler by function name. The bot's `on_message` preserves prefix dispatch with `process_commands`. | [Error flow](error-handling.md#error-flow-overview), [entrypoint](../bot.py) |
| `@bot.tree.error` | Async function in `bot.py` | Registers the central slash-command error handler. | [Error flow](error-handling.md#error-flow-overview) |
| `@tasks.loop(...)` | Async cog method | Creates a scheduled task loop. Current users: reminder polling, status rotation, reaction-role startup sync. The decorator does not start the loop; lifecycle code does. | [Events and tasks](cog-development.md#advanced-features) |
| `@<loop>.before_loop` | `@change_status.before_loop`, `@check_reminders.before_loop`, `@startup_sync.before_loop` | Registers the preparation coroutine, commonly `wait_until_ready()`. | [Events and tasks](cog-development.md#advanced-features) |
| `@<loop>.error` | `@change_status.error` | Registers the loop's unhandled-error callback. Forward failures to central logging; this is distinct from a per-command error handler. | [Manual reporting](error-handling.md#logging-errors-manually), [status example](../cogs/optional/status.py) |

Loop interval and lifecycle options are documented in the official
[task API](https://discordpy.readthedocs.io/en/stable/ext/tasks/index.html).
`before_loop` and `error` take the decorated coroutine directly: use them without
parentheses. `@bot.event` and `@bot.tree.error` also have no parentheses.

## Python decorators used internally

These are Python features, not additional bot command or permission systems.

| Decorator | Import | Use in this tree | Reference |
|---|---|---|---|
| `@dataclass` / `@dataclass(frozen=True)` | `from dataclasses import dataclass` | Op declarations, LLM request/result/usage records. `frozen=True` prevents normal attribute assignment; it does not deeply freeze nested objects. | [Op fields](cog-development.md#the-fields), [Python dataclasses](https://docs.python.org/3.12/library/dataclasses.html) |
| `@property` | Built-in | Computes a read attribute, e.g. a settings panel's current superadmin status. | [Panel components](cog-development.md#panel-components), [Python property](https://docs.python.org/3.12/library/functions.html#property) |
| `@staticmethod` | Built-in | A class-associated helper that receives neither `self` nor `cls`, e.g. media's `_guild_dir`. | [Service functions](cog-development.md#the-service-function-pattern-required), [Python staticmethod](https://docs.python.org/3.12/library/functions.html#staticmethod) |
| `@classmethod` | Built-in | Receives `cls`; used by the reminder snooze view's factory. | [Reminder source](../cogs/optional/reminders.py), [Python classmethod](https://docs.python.org/3.12/library/functions.html#classmethod) |
| `@contextlib.contextmanager` | `import contextlib` | Builds a context manager from a generator. The embedded MCP server uses it to leave signal handling with the bot. | [MCP server](security.md#mcp-ops-server), [Python contextmanager](https://docs.python.org/3.12/library/contextlib.html#contextlib.contextmanager) |

## Test decorators

These are used only by tests, not by running cogs. Import `pytest`; async test
support comes from the `pytest-asyncio` development dependency.

| Decorator | Purpose | Reference |
|---|---|---|
| `@pytest.fixture` / `@pytest.fixture(...)` | Supplies reusable setup and teardown, such as an isolated real `Config` store. | [Testing](cog-development.md#testing--restarting-tips), [pytest fixtures](https://docs.pytest.org/en/stable/how-to/fixtures.html) |
| `@pytest.mark.parametrize(...)` | Runs the same test over multiple argument sets. | [Testing](cog-development.md#testing--restarting-tips), [pytest parametrization](https://docs.pytest.org/en/stable/how-to/parametrize.html) |
| `@pytest.mark.asyncio` | Runs an async test under pytest-asyncio. | [Logging tests](../tests/test_error_logging.py), [pytest-asyncio marker](https://pytest-asyncio.readthedocs.io/en/stable/reference/markers/index.html) |
| `@pytest.mark.skipif(condition, reason=...)` | Skips tests when a stated prerequisite is absent. | [Testing](cog-development.md#testing--restarting-tips), [pytest skipping](https://docs.pytest.org/en/stable/how-to/skipping.html) |

Tests also use the runtime decorators above. `@_op(...)` is an imported alias of
`@op(...)`; `@reg.op(...)` is the same `OpRegistry.op` API on a test registry.
They are not extra decorator implementations.

## Standard recipes

A hidden admin prefix command and its slash twin:

```python
import discord
from discord import app_commands
from discord.ext import commands
from core.utils import is_admin, panel_slash_pin

class Settings(commands.Cog):
    @commands.command(name="settings", hidden=True)
    @commands.guild_only()
    @commands.check(is_admin)
    async def settings(self, ctx):
        ...

    @app_commands.command(name="settings", description="Open settings")
    @app_commands.guild_only()
    @panel_slash_pin()
    @app_commands.check(is_admin)
    async def settings_slash(self, interaction: discord.Interaction):
        ...
```

For an owner-only panel, replace `is_admin` with `is_superadmin` on both twins.
These decorators guard initial invocation. Recheck authorization when a user
clicks a control or submits a modal, since their permissions can change while
it is open. See the [panel pattern](cog-development.md#panel-components).

Regex suppression uses a **class** decorator, outside the command/listener stack:

```python
import re
from discord.ext import commands
from core.error_handler import suppress_command_not_found

DICE_PATTERN = re.compile(r"(?P<rolls>\d+)?d(?P<sides>\d+)", re.IGNORECASE)

@suppress_command_not_found(DICE_PATTERN)
class RNG(commands.Cog):
    ...  # Reuse DICE_PATTERN.fullmatch(text) in the listener.
```

Python applies stacked decorators from the bottom upward. Follow the existing
command/check order above. An op adapter is a separate async method calling the
shared service; do not stack `@op` onto a command or interaction handler.

## Available in libraries, but not the repo pattern

| Tempting spelling | Use here instead | Why / deep dive |
|---|---|---|
| `@is_admin`, `@is_superadmin` | `@commands.check(...)` or `@app_commands.check(...)` | These helpers are predicates. [Command gates](security.md#command-gates-prefix-and-slash). |
| `@commands.has_permissions(administrator=True)` | The shared `is_admin` predicate | Discord Administrator alone is not the complete bot-admin model. [Permission management](cog-development.md#permission-management). |
| `@app_commands.default_permissions(...)` | `@panel_slash_pin()` for gated panels | The underlying library decorator is available and appears in older doc examples; the wrapper keeps picker permissions consistent. It still needs an authorization check. [Permission recipe](cog-development.md#permission-management). |
| `@some_command.error` | Validate expected input in the body; let central handlers report unexpected errors | Local command error handlers can coexist with global dispatch and create confusing behavior. This restriction does not prohibit `@bot.tree.error` or `@loop.error`. [Error-handler guidance](error-handling.md#dont-use-local-error-handlers). |
| `@commands.cooldown(...)` for GPT rate limiting | The existing nested-window ladder in `gpt.py` | The old cooldown decorator was removed; do not add a second limiter. [Rate-limit decision](decision-records.md), [GPT source](../cogs/optional/gpt.py). |

`register_error_whitelist_hook(...)`, `cog_load`, `cog_unload`,
`interaction_check`, and `InvokerOnlyView` are also useful extension points, but
they are registration functions, lifecycle methods, or a mixin—not additional
`@` decorator APIs. Dynamic panels such as `/aisettings` and `/logsettings`
construct controls and assign callbacks directly.

When adding a custom decorator or introducing a new decorator family, update
this chart and its linked subsystem guide together.
