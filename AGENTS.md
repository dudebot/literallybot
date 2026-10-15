# Agent notes

Discord bot, Python 3.12+ / discord.py. Keep changes focused, reuse existing
patterns, and read the relevant guide before changing a subsystem.

## Where to look

- **Commands (`!`, `/`), `@` decorators, and panels:**
  [cog development](docs/cog-development.md) and [decorators](docs/decorators.md).
  Templates: [RNG](cogs/optional/rng.py) for a small cog,
  [log settings](cogs/optional/error_handler.py) for admin panels,
  [reaction roles](cogs/optional/setrole.py) for shared command/op behavior.
- **Config:** [API and key registry](docs/config-system.md). Use the central
  Config store, choose the correct scope, and document new keys there.
- **Auth, ops, MCP:** [security](docs/security.md) and
  [op authoring](docs/cog-development.md#registering-ops-from-a-cog).
  Reuse `core.utils.is_admin` / `is_superadmin`; register Discord actions as
  ops and share headless logic with commands. No parallel permission system.
- **Logging and test quality:** [error handling](docs/error-handling.md) and
  [test guidance](docs/error-handling.md#test-quality-and-development-checks).
  Use `self.bot.logger`; permission denials still log at WARNING.
- **Other systems and decisions:** [README](README.md#documentation) and
  [decision records](docs/decision-records.md).

## Working rules

- New cogs go in `cogs/optional/`, one per purpose. `cogs/core/` is the
  never-disableable recovery surface. Shared helpers live in `utils/`.
- Cogs are fixed at boot; disable via `disabled_cogs`, never delete or hot-reload.
  Follow the [restart instructions](docs/cog-development.md#testing--restarting-tips).
- Admin commands use the central checks, hidden prefix commands, and guild-only
  slash twins with `panel_slash_pin()`. Guard guild-only prefix commands too.
- Reuse `core.utils.recursive_split` for Discord message splitting.
  Keep async handlers nonblocking and runtime data/credentials out of git.
- Downstream copies merge upstream as-is. Fix shared backbone here first;
  keep copy-only additions separate. No merge helpers or `merge=ours`.
  Never cherry-pick private-copy commits into a public repo; re-author here.

## Tests

Keep tests for plausible regressions whose failure would matter, not test slop.
Development self-checks are fine; delete temporary checks and unused fixtures
before committing or handing off, without waiting to be asked. No count target.
See the [qualitative policy](docs/error-handling.md#test-quality-and-development-checks).
Use real Config with temporary storage; run `venv/bin/python -m pytest tests/`.
