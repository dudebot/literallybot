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
  - Logs to bot.logger
  - Checks whitelist hooks (can suppress)
  - Rate-limits duplicate errors
  - Sends embed to Discord channels
        ↓
Discord error channels
  - Guild channel (if configured)
  - Global channel (always, for superadmin visibility)
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

If your cog creates dynamic "commands" (like media files that respond to `!filename`), register a whitelist hook to suppress the CommandNotFound spam:

```python
from core.error_handler import register_error_whitelist_hook, unregister_error_whitelist_hook

class Media(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        register_error_whitelist_hook(self._is_media_command)

    def cog_unload(self):
        unregister_error_whitelist_hook(self._is_media_command)

    def _is_media_command(self, ctx, error):
        """Return True to suppress this error from logging."""
        if not ctx.message.content.startswith('!'):
            return False

        cmd_name = ctx.message.content[1:].split()[0]
        # Check if this matches a media file
        return self._media_file_exists(cmd_name)
```

The hook receives `(ctx, error)` and returns `True` to suppress logging, `False` to let it through.

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

## Severity Levels

Severity is auto-determined based on error type, but here's what each level means:

| Severity | Color | Used For |
|----------|-------|----------|
| WARNING  | Gold  | CommandNotFound, MissingPermissions, CommandOnCooldown |
| ERROR    | Orange | Most other exceptions |
| CRITICAL | Red   | (Reserved for manual use) |

## Error Channel Configuration

Configured via `!errorlog` commands (see `!help errorlog`):

- **Guild channel**: `!errorlog setchannel #channel` - errors from this guild
- **Global channel**: `!errorlog setglobal #channel` - all errors (superadmin only)
- **Category routing**: `!errorlog setcategory command_error #channel`
- **Severity routing**: `!errorlog setseverity critical #channel`

## Rate Limiting

Duplicate errors are rate-limited (default 5 minutes). Same error won't spam the channel. Configurable via `!errorlog ratelimit <minutes>`.
