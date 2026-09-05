# Agent notes

Discord bot (discord.py, Python 3.12+). Cog loader, JSON config, ops
registry. This file is the working agreement for anyone changing the
tree — origin repo or a downstream copy. Same file either way.

## Layout

```
bot.py                 # entry, event handlers
core/                  # config, ops registry, agent loop, MCP, errors, LLM
cogs/core/             # recovery surface (control, admin) — never disableable
cogs/optional/         # everything else; a deploy picks via disabled_cogs
configs/               # runtime JSON (global, guild, user) — not in git
docs/                  # per-system docs; README indexes them
utils/                 # headless helpers shared by cogs
```

`cogs/core/` exists so that if an optional cog is off, you can still turn
it — or anything else — back on from Discord. Adding a third file there
needs that justification.

## Downstream copies

This tree is meant to be merged as-is. Extra cogs, utils, tests, and docs
that exist only in a copy never conflict.

```bash
git fetch upstream
git merge upstream/main
```

No helper script. No `merge=ours`. Backbone (`bot.py`, `core/`, shared
cogs under `cogs/core/` and the shared files in `cogs/optional/`) is
edited **here first**. A copy that patches backbone will fight every
merge; put the patch here instead.

`disabled_cogs` (global config, bare cog names) is how a copy keeps
unused cogs on disk without running them. Disable, never delete.

If this tree is public, do not cherry-pick commits from a private copy
into it — that publishes the copy's author identity. Re-author the
change here.

## Before changing anything

1. Read the relevant `docs/` file for that subsystem.
2. Match existing cog/op patterns. Don't invent a second one.
3. Keep the diff to the failure mode you are fixing.

## Config

`docs/config-system.md` is the API and the key registry. Keep the
registry current when adding keys. One data model per concept.

```python
bot.config.get(ctx, "key")              # guild
bot.config.set(ctx, "key", value)
bot.config.get_user(user, "key")        # user_<id>.json
bot.config.set_user(user, "key", value)
bot.config.get_global("key")
bot.config.set_global("key", value)
bot.config.flush()                      # 5s write buffer otherwise
```

`get` / `get_user` / `get_global` are read-only: a missing key returns
the default and does not create a file.

Timestamps in config are naive local time (the host timezone). Discord
API times are UTC-aware — convert with
`.astimezone().replace(tzinfo=None)` before comparing to stored values.

## Seams

Where new code should land, so seams don't re-greed:

- **Auth**: `core.utils.is_admin` / `is_superadmin`. Never hand-roll a
  gate.
- **Message splitting**: `core.utils.recursive_split` is the Discord
  2000-char splitter.
- **Discord actions for agents/frontends**: register an op. An **API
  primitive** (raw Discord action) goes inline in `core/ops.py` via
  `@registry.op(...)` (`origin='core'`). A **behavioral primitive** goes
  on a cog method via `@op(...)` from `core.ops` (`origin='cog'`). Factor
  the logic into a headless service both the command and the op call.
  Never expose an interaction handler as an op. Origin is stamped by the
  registration path, never a decorator argument.
- **No code-level op subsets.** An op declares `scope` (GUILD/DM/GLOBAL).
  The in-chat agent universe IS the guild-scoped set, queried live.
- **One cog per purpose.** Same purpose = same file.
- **Admin surfaces** are panels behind hidden `!` commands, gated by
  `is_admin` / `is_superadmin`. Slash twins use the same predicate plus
  `guild_only` and `panel_slash_pin()` (Manage Messages — visibility, not
  auth). Public slash is `/help`.
- **CheckFailure is signal**, not noise: a user reaching for a command
  they shouldn't have still logs to the error channel at WARNING. The
  user-facing copy is a gate sentence, not "something went wrong".
- **Config keys**: one key per concept. Inventory in
  `docs/config-system.md`.
- **Rate limiting** is the nested-window ladder in `gpt.py`. No
  Cooldowns tab.

## Cogs

The set is fixed at boot. There is no live load/unload/reload. A code
change or a `disabled_cogs` edit binds on restart (`!restart`, or the
`!cogs` panel Restart button).

New cogs go in `cogs/optional/`. Use the central logger
(`self.bot.logger`). Prefix admin commands `hidden=True`; the check is
what actually gates them. Any command that touches `ctx.guild.<attr>`
needs `@commands.guild_only()` (in a DM `ctx.guild` is `None`).

`is_admin(ctx)` is False in DMs unconditionally. `!help` in a DM is a
public listing.

## Ops and MCP

`core/ops.py` is the registry. MCP (`core/mcp_server.py`) is a thin
frontend over it — loopback, bearer token, opt-in via `mcp_ops_enabled`.
Message reads (`read_history`, `search_history`, `get_message`) serialize
embed bodies, not just `content`.

## Restart

Config, cogs, and MCP bind at boot. After a deploy:

```bash
kill $(systemctl show "$UNIT".service -p MainPID --value)
```

`$UNIT` is whatever `scripts/install_service.sh` installed (defaults to
the directory name). systemd relaunches in ~3s. Never `pkill -f` (it
matches its own command line). Never `sudo systemctl restart` from a
session that cannot supply a password. With `Restart=always` you cannot
stop the service, only restart it.

## Tests

```bash
python -m pytest tests/
```

A test that fakes `Config` will not catch a missing method on the real
class. Hit `core.config.Config` for store API.
