# LiterallyBot - Claude Development Guide

## Project Structure

```
literallybot/
├── bot.py              # Main entry point, event handlers
├── core/
│   ├── config.py       # JSON config system (read docs/config-system.md)
│   ├── error_handler.py # Error logging to Discord channels
│   └── utils.py        # Permission helpers (is_admin, is_superadmin)
├── cogs/
│   ├── static/         # Always-loaded cogs (admin, dev, error_handler)
│   └── dynamic/        # Hot-reloadable feature cogs
├── configs/            # Runtime JSON storage (guild, user, global)
├── docs/               # Developer documentation
│   ├── cog-development.md
│   ├── config-system.md
│   └── error-handling.md
└── media/              # Audio/video files for !play and dynamic commands
```

## Before Making Changes

1. **Read the relevant docs/** file before modifying core systems
2. **Check existing patterns** in similar cogs before adding new features
3. **Don't over-engineer** - keep changes minimal and focused

## Key Patterns

### Config System
- `config.get()` is **read-only** - returns value or default, never writes
- Use explicit `config.set()` to persist values
- See `docs/config-system.md` for full API

### Error Handling
- Errors flow to global handler automatically - don't fight it
- For user input validation, parse args manually instead of relying on discord.py converters
- See `docs/error-handling.md` for patterns

### Cog Development
- Dynamic cogs go in `cogs/dynamic/`
- Use `!reload cogname` for hot-reload during development
- See `docs/cog-development.md` for structure and examples

## Sister Project

`../conditioner` shares the same `core/config.py` implementation. Changes to config behavior should be mirrored there.
