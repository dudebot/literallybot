# Agent notes

Discord bot, Python 3.12+ / discord.py. Keep changes focused, reuse existing
patterns, and read the relevant guide before changing a subsystem.

## Where to look

- **Commands (`!`, `/`), `@` decorators, and panels:**
  [cog development](docs/cog-development.md) and [decorators](docs/decorators.md).
  Templates: [RNG](cogs/rng.py) for a small cog,
  [log settings](cogs/error_handler.py) for admin panels,
  [reaction roles](cogs/setrole.py) for shared command/op behavior.
- **Config:** [API and key registry](docs/config-system.md). Use the central
  Config store, choose the correct scope, and document new keys there.
- **Auth, ops, MCP:** [security](docs/security.md) and
  [op authoring](docs/cog-development.md#registering-ops-from-a-cog).
  Reuse `core.permissions.is_admin` / `is_superadmin`; register Discord actions as
  ops and share headless logic with commands. No parallel permission system.
  For existing media tools and activation, see the
  [media tool guide](docs/cog-development.md#media-tools-and-discovery) and
  [two-tier config example](docs/config-system.md#enabling-cog-tools).
- **Logging and test quality:** [error handling](docs/error-handling.md) and
  [test guidance](docs/error-handling.md#test-quality-and-development-checks).
  Use `self.bot.logger`; permission denials still log at WARNING.
- **Other systems and decisions:** [README](README.md#documentation) and
  [decision records](docs/decision-records.md).

## Working rules

- All feature cogs live in `cogs/`, one per purpose. Cogs own prefix/slash
  commands, listeners, tasks, views, and behavioral `@op` adapters. Shared
  headless implementations and state live in `core/`; private helpers may stay
  with their cog. Never import or call another cog to reuse its behavior.
- `core/cog_discovery.py` owns discovery and protection. Only `control` and
  `admin` are protected, with reasons in `PROTECTED_COGS`. Filter disabled
  modules before import; never import them to discover metadata or infer
  protection from folders. Keep stable bare cog IDs.
- Behavioral ops belong to their owning cog even when services live in `core/`.
  Disabling withdraws feature entrypoints at next boot while preserving shared
  implementations, saved settings and existing effects. Independent raw Discord
  API primitives retain their own exposure and permission policy.
- Cogs are fixed at boot; disable via `disabled_cogs`, never delete or hot-reload.
  Follow the [restart instructions](docs/cog-development.md#testing--restarting-tips).
- Admin commands use the central checks, hidden prefix commands, and guild-only
  slash twins with `panel_slash_pin()`. Guard guild-only prefix commands too.
- Cogs declare capabilities; authorized operators configure their exposure.
  Never auto-enable a cog's own tools or grant access from registration or execution.
- Use `core.permissions` for authorization, `core.panels` for panel lifecycle
  and slash visibility, and `core.messages` for outbound text helpers.
- Reuse `core.messages.recursive_split` for Discord message splitting.
  Keep async handlers nonblocking and runtime data/credentials out of git.
- Downstream copies merge upstream as-is. Fix shared backbone here first;
  keep copy-only additions separate. No merge helpers or `merge=ours`.
  Never cherry-pick private-copy commits into a public repo; re-author here.

## Tests

Keep tests for plausible regressions whose failure would matter, not test slop.
Development self-checks are fine; delete temporary checks and unused fixtures
before committing or handing off, without waiting to be asked. No count target.
See the [qualitative policy](docs/error-handling.md#test-quality-and-development-checks).
Architecture ownership is enforced by `tests/test_architecture.py`: no core-to-cog
or cog-to-cog imports; Cog classes and Discord feature declarations live in
`cogs/`; behavioral `@op` declarations belong on owning Cog methods. Shared
Discord SDK helpers in `core/` and raw API `registry.op` operations remain valid.
These are static import/declaration checks, not a runtime dependency sandbox.
Use real Config with temporary storage; run `venv/bin/python -m pytest tests/`.
