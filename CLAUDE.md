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
- **`cogs/dynamic/gpt.py` is the historical landing zone.** The AI-admin UX
  (the `/ai settings` panel and all provider/model CRUD surfaces) lives in
  `ai_admin.py`; gpt.py keeps the `_do_*` core-logic helpers, the chat paths,
  and the slim `/ai` group (settings/setapikey/status). New AI-admin features
  land in ai_admin.py, not gpt.py. Remaining parked seam: memory capture
  could be its own cog.
- Other parked (real-but-leave-it): error-handler module globals -> instance on
  bot; `LLMClient.has_api_key()` helper to dedupe key-presence checks (must
  count `requires_api_key: false` providers as satisfied, or ollama's ✅
  regresses).
- Parked from the 2026-07-21 seam audit — fold in on the NEXT REAL TOUCH of
  the named file, not as standalone work:
  - `bot.py` cog-load-failure reporting in `on_ready` -> a fourth `handle_*`
    sibling in `core/error_handler.py` (when error_handler is next open).
  - `ai_admin.py` `_default_model_button` inline catalog mutation -> a
    `gpt._do_setdefaultmodel` helper, matching the other `_do_*` seams.
  - `core/utils.py` `smart_split` placement-by-provenance docstring clause ->
    delete when rng.py is next touched.
  - `core/ops.py` `_smoke_test` asserts -> `tests/test_ops_registry.py` when
    the queued pytest+CI lane ships (keep a slim `python -m core.ops` printer).
  - `send_message`'s agent_guidance sentence states a gpt-loop fact in the ops
    layer — acceptable while gpt.py is the sole consumer; revisit only if a
    second agent_guidance consumer appears.
  - usage.py config-backed `estimate_cost` leg — only if per-call cost
    reporting ever grows a real consumer beyond the two debug log lines.
  - Multi-prefix support: not a goal. If it ever becomes one, use
    `bot.get_prefix`/`ctx.prefix` (discord.py's API), no new helper.
- **Config keys**: every real key in `configs/*.json` is inventoried in the
  Key Registry section of `docs/config-system.md` — keep it current when
  adding keys. One data model per concept: never add a parallel key for an
  existing concept (reaction-role mappings are `emoji_role_toggles`, full stop).
- **Rate limiting** is the nested-window ladder in gpt.py
  (`cooldown_config`/`_check_cooldown`, tunable via `/ai settings` →
  Cooldowns). Don't reintroduce flat per-message cooldowns.

## Related

The `conditioner` bot (EcstasyEngineer/conditioner) uses a similar `core/config.py` derived from the same original implementation.
