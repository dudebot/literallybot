# Agent notes

Discord bot, Python 3.11+ / discord.py. Meant to stay small and hackable: keep
changes focused, copy an existing pattern, and read the linked guide before
changing a subsystem.

## Where to look

- **Writing a cog, commands, panels, ops:** [cog development](docs/cog-development.md)
  (has a full template) and [decorators](docs/decorators.md). Live examples:
  [RNG](cogs/rng.py) for a small cog, [log settings](cogs/error_handler.py)
  for an admin panel, [reaction roles](cogs/setrole.py) for a command and an
  op sharing one implementation.
- **Config:** [API and key registry](docs/config-system.md). Document new keys there.
- **Who may do what, the AI agent path, MCP:** [architecture](docs/architecture.md)
  and [security](docs/security.md).
- **Logging and tests:** [error handling](docs/error-handling.md) and the
  [test policy](docs/error-handling.md#test-quality-and-development-checks).
- **Why something is the way it is:** [decision records](docs/decision-records.md).
  The newest entry wins; older ones may be marked superseded.

## Access levels

"Admin" always means the bot's own notion, from `core.permissions`:

- **Superadmin** (`is_superadmin`): the bot's operator, listed in `superadmins`
  in `configs/global.json`.
- **Bot admin** (`is_admin`): in the server's `admins` list (`!claimadmin`,
  `!addadmin`), or a Discord Administrator, or the server owner, or a superadmin.
- **Everyone**: no check.

**Commands** declare their level with one decorator:
`@commands.check(is_admin)` on a prefix command, `@app_commands.check(is_admin)`
on a slash command (same for `is_superadmin`). An admin panel is a hidden
`!name` plus a `guild_only` `/name` with `@panel_slash_pin()`, which only hides
it from members without Manage Messages. Copy [log settings](cogs/error_handler.py).

**Ops** (AI and MCP tools) declare no level. Operators choose Off / Admin /
Everyone per server in `/aisettings`. So:

- Never write who may use a tool, or what it will refuse, into an op's
  description or guidance. The model is given only tools the user may call;
  if Discord refuses, it gets Discord's error.
- Never write `agent_ops_whitelist`, `agent_ops_gate` or `mcp_tools_enabled`
  from code. Registering an op enables nothing.
- `confirm_everyone=True` marks an op that can grant access or do lasting
  damage. It only makes the settings panel ask before opening it to Everyone.

## Working rules

- Features live in `cogs/`, one file per purpose, and own their commands,
  listeners, tasks, views and `@op` methods. Shared logic and state live in
  `core/`. A cog never imports another cog; `core/` never imports a cog.
- If the AI or an MCP client should be able to do something a command does,
  add an `@op` method on the same cog that calls the same function.
- No hot reload: cogs load at boot. To stop one in a deployment, `!disable`
  it (stored in `disabled_cogs`) and restart. Don't rename a cog file: its
  name is the stored ID. Only `control` and `admin` cannot be disabled.
- Use `self.bot.config` for settings, `self.bot.logger` for logging,
  `core.messages.recursive_split` to split long messages, `core.panels` for
  panel views.
- No blocking calls in async handlers (`asyncio.to_thread` for file, network
  and subprocess work). No runtime data or credentials in git.
- This repo is upstream of other deployments: fix shared code here and keep
  it deployment-neutral.

## Tests

`venv/bin/python -m pytest tests/`. Each test should name the specific failure
it catches; there is no count target. Use real `Config` with temporary storage
(see `tests/conftest.py`). Delete throwaway checks before handing off.
`tests/test_architecture.py` enforces the import and ownership rules above.
