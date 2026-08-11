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
│   ├── core/           # Recovery surface — never disableable (control, admin)
│   └── optional/       # Everything else; deployment picks via disabled_cogs
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
- New cogs go in `cogs/optional/`. `cogs/core/` is only for the recovery
  surface — the commands that turn things back on (`control.py`, `admin.py`)
- Use `!reload cogname` for hot-reload during development
- See `docs/cog-development.md` for structure and examples

## Architecture Seams (from the 2026-07 seam-machine pass)

Where new code should land, so seams don't re-greed:

- **Auth checks**: always `core.utils.is_admin` / `is_superadmin`. Never hand-roll
  a gate — every hand-rolled copy found so far had drifted from policy.
- **Message splitting**: `core.utils.recursive_split` is the one Discord
  2000-char splitter. Don't write another accumulator/slicer.
- **Discord actions for agents/frontends**: register an op in `core/ops.py`;
  frontends (`core/agent_loop.py`, `core/mcp_server.py`) generate their surface
  from the registry and must stay thin. `call_ids` gates permissions before
  resolving ids — keep that ordering.
- **One cog per purpose** (owner standard, 2026-08): cogs of the same purpose
  are coupled into one file. gpt.py owns ALL AI surface — chat paths, `_do_*`
  helpers, and the `/aisettings` panel (the former ai_admin.py was merged
  back in). Remaining parked seam: memory capture could be its own cog.
- **Admin surfaces are panels behind hidden `!` commands** (`!aisettings`,
  `!autoresponse`, `!media`, `!cogs`, `!config`): single-invoker Views gated
  by `is_admin`/`is_superadmin` — the BOT's admin concept, independent of
  the invoker's Discord permissions in whichever server they stand in.
  Slash commands are reserved for parameterized one-liners where the typed
  arg UI earns its place (`/role`) and truly public commands (`/help`) — so
  the slash picker never advertises admin machinery to regular members.
  Superadmin-only controls are OMITTED from a panel's render for
  non-superadmins (dynamic panel), not merely disabled.
- Other parked (real-but-leave-it): error-handler module globals -> instance on
  bot; `LLMClient.has_api_key()` helper to dedupe key-presence checks.
- The 2026-07-21 seam audit's parked items (fold-in triggers + upheld
  non-goals) are tracked in issue #62 — check it before flagging a seam in
  the files it names.
- **Config keys**: every real key in `configs/*.json` is inventoried in the
  Key Registry section of `docs/config-system.md` — keep it current when
  adding keys. One data model per concept: never add a parallel key for an
  existing concept (reaction-role mappings are `emoji_role_toggles`, full stop).
- **Rate limiting** is the nested-window ladder in gpt.py
  (`cooldown_config`/`_check_cooldown`, tunable via `!aisettings` →
  Cooldowns). Don't reintroduce flat per-message cooldowns.

## Related

The `REDACTED` bot (REDACTED/REDACTED) uses a similar `core/config.py` derived from the same original implementation.
