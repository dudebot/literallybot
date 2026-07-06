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

## Architecture Seams (from the 2026-07 seam-machine pass)

Where new code should land, so seams don't re-greed:

- **Auth checks**: always `core.utils.is_admin` / `is_superadmin`. Never hand-roll
  a gate — every hand-rolled copy found so far had drifted from policy.
- **Message splitting**: `core.utils.recursive_split` is the one Discord
  2000-char splitter. Don't write another accumulator/slicer.
- **Discord actions for agents/frontends**: register an op in `core/ops.py`;
  frontends (`core/agent_loop.py`, `mcp_ops/server.py`) generate their surface
  from the registry and must stay thin. `call_ids` gates permissions before
  resolving ids — keep that ordering.
- **`cogs/dynamic/gpt.py` is the historical landing zone** and still carries two
  parked seams (owner list): the ~700-line provider/model/key CRUD + `/ai` group
  belongs in its own `ai_admin.py` cog, and memory capture could be its own cog.
  New AI-admin features should NOT be added to gpt.py.
- Other parked (real-but-leave-it): error-handler module globals -> instance on
  bot; `LLMClient.has_api_key()` helper to dedupe key-presence checks; cooldown
  display shows base×multiplier but enforcement is flat `BASE_COOLDOWN_SECONDS`
  (product decision whether to enforce per-model).

## Related

The `conditioner` bot (EcstasyEngineer/conditioner) uses a similar `core/config.py` derived from the same original implementation.
