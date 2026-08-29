# LiterallyBot - Claude Development Guide

## Project Structure

```
literallybot/
├── bot.py              # Main entry point, event handlers
├── core/
│   ├── bootstrap.py    # Token resolution chain + first-run superadmin (#83)
│   ├── config.py       # JSON config system (read docs/config-system.md)
│   ├── ops.py          # Ops registry — every Discord action, declared once
│   ├── agent_gate.py   # Two-tier agent exposure: global whitelist x per-guild gate
│   ├── agent_loop.py   # Frontend: in-chat agent tools (guild-scoped ops)
│   ├── mcp_server.py   # Frontend: MCP over loopback HTTP (whole registry)
│   ├── error_handler.py # Error logging to Discord channels
│   ├── dm_log.py       # DM transcript JSONL store
│   ├── llm/            # Provider-agnostic LLM client (pydantic-ai)
│   └── utils.py        # Permission helpers (is_admin, is_superadmin)
├── cogs/
│   ├── core/           # Recovery surface — never disableable (control, admin)
│   └── optional/       # Everything else; deployment picks via disabled_cogs
├── configs/            # Runtime JSON storage (guild, user, global)
├── docs/               # Developer documentation — the canonical per-file
│                       # index is README.md § Documentation (six files)
├── utils/              # Headless helpers shared by cogs (not Discord-aware)
│   └── points.py       # Per-user points store
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
- **The cog set is fixed at boot (#86).** There is no live load/unload/reload
  surface. Restart the bot to pick up a code change or a `disabled_cogs` edit
  (`!restart`, or the `!cogs` panel's Restart button — same exit path).
- See `docs/cog-development.md` for structure and examples

## Architecture Seams (from the 2026-07 seam-machine pass)

Where new code should land, so seams don't re-greed:

- **Auth checks**: always `core.utils.is_admin` / `is_superadmin`. Never hand-roll
  a gate — every hand-rolled copy found so far had drifted from policy.
- **Message splitting**: `core.utils.recursive_split` is the one Discord
  2000-char splitter. Don't write another accumulator/slicer.
- **Discord actions for agents/frontends**: register an op — two paths, same
  registry, and the owner's terms for the two kinds are load-bearing: an **API
  primitive** (raw Discord action) goes inline in `core/ops.py` via
  `@registry.op(...)` (stamped `origin='core'`); a **behavioral primitive** (a
  capability with the bot's own intelligence) goes on a cog method via the
  module-level `@op(...)` decorator from `core.ops` (stamped `origin='cog'` by
  `bot.py`'s `add_cog`/`remove_cog` wiring); factor the logic into a headless
  service method both the command and the op call, and never expose an
  interaction handler as an op. Origin is
  stamped by the registration PATH, never a decorator argument. Frontends
  (`core/agent_loop.py`, `core/mcp_server.py`, the `!aisettings` panel) generate
  their surface from the registry and must stay thin. See
  `docs/cog-development.md` → "Registering ops from a cog".
- **No code-level op subsets.** There is no `agent=True` flag; an op declares
  `scope` (GUILD/DM/GLOBAL) and the in-guild agent universe IS the guild-scoped
  set. Query the registry LIVE (`guild_agent_names()`, `ops()`, `grouped()`) —
  never freeze a module-level tuple, because cog ops arrive with their cog at
  boot and leave at shutdown, and which cogs those are is a config decision
  (`disabled_cogs`), not an import-time constant. `call_ids` gates permissions
  before resolving ids — keep that
  ordering. Admin config is an EXPOSURE filter; authorization is the per-call
  `PermissionLevel` gate.
- **One cog per purpose** (owner standard, 2026-08): cogs of the same purpose
  are coupled into one file. gpt.py owns ALL AI surface — chat paths, `_do_*`
  helpers, and the `/aisettings` panel (the former ai_admin.py was merged
  back in). Remaining parked seam: memory capture could be its own cog.
- **Admin surfaces are panels behind hidden `!` commands** (`!aisettings`,
  `!autoresponse`, `!media`, `!cogs`, `!config`): single-invoker Views gated
  by `is_admin`/`is_superadmin` — the BOT's admin concept, independent of
  the invoker's Discord permissions in whichever server they stand in.
  Prefix uses `@commands.check(is_admin)`; slash twins use
  `@app_commands.check(is_admin)` (same predicate) plus a picker pin
  (`guild_only` + `default_permissions(administrator=True)`) so Discord's
  picker does not advertise them to ordinary members. Public slash is
  `/help`; parameterized one-liners (`/role`) still earn the typed-arg UI.
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
  (`cooldown_config`/`_check_cooldown`). No panel surface since the 2026-08 UX
  pass — tuned by hand via the global config keys `cooldown_tier_bases` /
  `cooldown_windows`. Don't reintroduce flat per-message cooldowns, and don't
  document a Cooldowns tab that doesn't exist.

## Restarting the Deployed Bot

The bot runs as a systemd service (`literallybot.service`, `Restart=always`,
`RestartSec=3`, `User=dudebot`). To restart after a deploy or config change:
**kill the exact PID — no sudo needed** (the process is ours):

```bash
kill $(systemctl show literallybot.service -p MainPID --value)
```

systemd relaunches it in ~3s. Never `pkill -f` (matches its own command line
and has killed the invoking shell on this box); never `sudo systemctl restart`
(prompts for a password the session can't supply). Remember the doctrine:
config edits bind at restart — cogs, MCP tools, MCP enablement alike.

