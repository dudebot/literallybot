# Error Handling Guide

This guide covers how errors flow through LiterallyBot and how to handle them in your cogs.

## Error Flow Overview

```
Command/Event triggers error
        ↓
Global handler (bot.py)
  - on_command_error      → handle_command_error()
  - on_app_command_error  → handle_app_command_error()
  - on_error              → handle_event_error()
        ↓
core/error_handler.py
  - Suppresses recognized dynamic shortcuts (CommandNotFound only)
  - Classifies and logs to bot.logger
  - Rate-limits duplicate errors
  - Sends embed to Discord channels
        ↓
Discord error channels
  - Guild channel (if configured)
  - Global channel (independent; unknown commands opt-in)
```

All unhandled errors flow through this pipeline automatically. You don't need to do anything special for errors to be logged.

## Handling User Input Errors

When users provide bad input, **don't rely on discord.py's type converters** to catch it. Parse and validate input yourself so you can give friendly error messages.

### Bad: Relying on Converters

```python
@commands.command()
async def remindme(self, ctx, number: int, unit: str, *, text: str):
    # If user types "!remindme in 6 days", discord.py tries to convert
    # "in" to int, fails, and throws BadArgument to the global handler.
    # User gets no feedback, you get an error log.
    ...
```

### Good: Parse It Yourself

```python
@commands.command()
async def remindme(self, ctx, *, args: str = None):
    usage = "Usage: `!remindme <number> <unit> <message>`"

    if not args:
        await ctx.send(usage)
        return

    parts = args.split(maxsplit=2)
    if len(parts) < 3:
        await ctx.send(usage)
        return

    number_str, unit, text = parts

    try:
        number = int(number_str)
    except ValueError:
        await ctx.send(usage)
        return

    # Now proceed with valid input
    ...
```

This gives users immediate feedback without polluting your error logs.

## Don't Use Local Error Handlers

Avoid `@command.error` decorators:

```python
# Don't do this
@mycommand.error
async def mycommand_error(self, ctx, error):
    if isinstance(error, commands.BadArgument):
        await ctx.send("Bad input!")
    else:
        raise error  # This still goes to global handler
```

Problems with this pattern:
1. The global `on_command_error` fires regardless of whether local handler exists
2. You end up fighting discord.py's error dispatch order
3. Re-raising creates confusing control flow

Just validate input in the command body instead.

## Suppressing CommandNotFound

Dynamic shortcuts run alongside discord.py command dispatch. A successful
listener does not consume the message; the command dispatcher can still report
`CommandNotFound`. Recognize the same input in both places; do not track message
IDs or rely on listener execution order.

For regex-based shortcuts, decorate the **cog class**:

```python
import re
from core.error_handler import suppress_command_not_found

DICE_PATTERN = re.compile(r"(?P<rolls>\d+)?d(?P<sides>\d+)", re.IGNORECASE)

@suppress_command_not_found(DICE_PATTERN)
class RNG(commands.Cog):
    ...  # The listener uses DICE_PATTERN.fullmatch(text), too.
```

The decorator accepts one or more regex strings or compiled patterns. They are
compiled once and full-match the whole message after removing `ctx.prefix` and
trimming surrounding whitespace. Only loaded cogs participate. Matching is
restricted to human-authored prefix `CommandNotFound` errors; invocation failures,
event exceptions, permission denials, and stale slash-command registrations are
never suppressed. Regexes are code declarations, not user-configurable patterns.

Match recognized syntax even when the cog returns a validation message:
`!999d67` is handled by RNG's "too many dice" reply. `!2d20 extra` is not handled
and stays an unknown command. Keep prefix handling, case, and whitespace rules
identical between the listener and its suppression rule.

For dynamic lookups such as media filenames, the existing callback API remains:
register `register_error_whitelist_hook(self._is_media_command)` in `__init__`,
unregister it in `cog_unload`, and return a boolean from `(ctx, error)`. The media
hook and listener both check the entire filename. Hooks run only for prefix
`CommandNotFound`. A hook exception is reported as an unexpected error and cannot
silently suppress the original activity.

## Logging Errors Manually

For errors you catch yourself but still want logged to Discord:

```python
from core.error_handler import log_error_to_discord, ErrorCategory, ErrorSeverity
import asyncio

try:
    result = await some_api_call()
except Exception as e:
    self.logger.error(f"API call failed: {e}", exc_info=True)
    await ctx.send("Something went wrong.")

    # Also send to Discord error channels
    asyncio.create_task(log_error_to_discord(
        self.bot, e, 'my_command_api_call',
        category=ErrorCategory.COMMAND_ERROR,
        severity=ErrorSeverity.ERROR,
        extra_info=f"User: {ctx.author}",
        guild_id=ctx.guild.id if ctx.guild else None
    ))
```

## Severity and command activity

| Situation | Severity | Discord behavior |
|-----------|----------|------------------|
| Recognized dice/media shortcut | — | Suppressed before logging |
| Unknown prefix command | INFO | Local log always; Discord opt-in per destination |
| Permission or server-only refusal | WARNING | Always sent to configured destinations as **Command denied** |
| Cooldown or prefix input validation exception | WARNING | Command warning, without a traceback |
| Unexpected command/event exception, bot missing permissions, stale slash registration | ERROR | Always sent to configured destinations, with traceback |
| Explicit critical report | CRITICAL | Always sent to configured destinations, with traceback |

Unknown commands use category `command_not_found` and title **Command not found**.
Denials use `command_denied` and include the **required** and **user** access levels
in both the reply and log. Requirements come from `gate_of()` metadata on the
existing `is_admin` / `is_superadmin` check decorators (including parent groups).
The actual user tier is evaluated by those same helpers. Role/Discord-permission
checks name their requirement; an untagged custom check is labeled as such rather
than guessing an admin level. Bot permission failures are errors, not user denials.

Both command frontends unwrap invocation failures for useful tracebacks, without
mistaking an exception raised inside a command for a routine dispatcher refusal.
Unknown-command local logs include the attempted name, user, guild, channel, and
source message link. The Discord toggle affects only Discord delivery.

## Log settings

Open **`/logsettings`** (ephemeral) or **`!logsettings`** for the prefix fallback.
The panel follows `/aisettings`: invoker-only, guild-only, bot-admin-gated, and
Manage Messages pinned in the slash picker. Every interaction and modal submission
rechecks current permissions.

- **Server:** edit this server's destination and routes; optionally enable
  **Command not found** reports (default off).
- **Global:** superadmins only. Edit the destination for all servers and DMs,
  its independent unknown-command toggle, and the 1–60 minute duplicate-alert
  interval modal (default 5). The panel cannot remove the global destination.
- Select a route, then a text channel. **Reset route** removes an override.
  **Remove server destination** disables local delivery but leaves global
  reporting alone. The bot needs View Channel, Send Messages, and Embed Links.

Exceptions and denials have no off toggle. A destination must still be configured
and reachable. Selecting a channel in Global selects from the server where the
panel was opened; an existing destination in another server is retained until
explicitly changed. Stored settings are read on each report and take effect
immediately; new code and cog declarations require a restart.

Guild and global routing are **independent**, not fallback/override scopes.
An event can go to both; the same destination receives only one copy. Within each
scope: specific category → legacy `command_error` route for command activity →
severity → default channel. The panel labels the legacy route **Commands (fallback)**.
A default channel is required before any routes activate. Existing route maps are
preserved when another setting changes. See `error_logging` in
[config-system.md](config-system.md).

## Duplicate alerts

The global interval applies per destination, source guild, category, command/event,
exception type, and message. Unknown commands and denials also distinguish users,
so one user's activity cannot hide another's. Local logs are not rate-limited.
Failed sends are logged locally and do not consume the next retry's cooldown.

## Test quality and development checks

Keep regression tests whose failure would reveal a **plausible future mistake
with consequences that matter**. There is no test-count or coverage target.
A new feature does not automatically need a permanent test bundle.

- Name the mistake and its consequence: a dropped permission check, wrong config
  scope, lost write, suppressed real error, or broken library integration. A past
  incident is useful evidence, not a prerequisite. Merely inventing a mutation
  that makes an assertion fail does not establish a test's value.
- Assert outcomes through real local behavior. Use real Config in temporary
  storage and mock external I/O, not the behavior being proved. Never load a
  running bot's config or credentials. Reuse `tests/conftest.py` for Config.
- Extend an existing scenario when it protects the same failure mechanism.
  Separate implementations of the same privacy/auth policy can deserve separate
  tests; a helper test does not prove callers use it. Keep coherent scenarios,
  not per-op matrices or unrelated assertions combined to shrink the count.
- Remove copied schema/metadata inventories, trivial forwarding checks, cosmetic
  assertions, tests of the fake itself, and checks for absent features. Actual
  component-limit failures or leaked private data matter; button labels do not.
  Discord validates API arguments, but cannot enforce the invoking user's privacy
  on requests made with the bot's credentials.

**Self-checks are temporary.** Tests and probes can validate an implementation
while building it. Retain one only if it also meets the regression standard
above: a credible future mistake and an actionable failure. Having helped during
implementation, passing once, or being cheap to run is not enough.

Before committing or handing off, review the tests introduced or changed during
the work. Delete development-only checks and their unused fixtures without
waiting for the user to ask; do not leave them skipped or in a permanent scratch
suite. Explain retained additions and why existing tests do not protect that
failure. For removals, identify the replacement or explain why the check is not
worth keeping. Report collected count changes as context only, including
parameterized cases. Never prune or combine valuable tests to reach a number.
See the [historical audit](testing.md) for how the suite accumulated.
