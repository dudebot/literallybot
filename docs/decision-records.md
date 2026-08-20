# Decision Records

Durable records of architecture decisions that otherwise live only in GitHub
issue threads. Each entry names the issue that serves as the primary record
and captures amendments verified against deployed code.

## #58 — Core/LLM extraction + ops registry sprint (epic)

Issue #58 itself is the decision record for the sprint (there was no separate
file until this one). Its lanes: core/llm extraction, ops registry, MCP ops
server, in-bot agent loop, hygiene (pytest + CI).

### Amendment: pydantic-ai drift correction (2026-07-04)

The sprint ratified pydantic-ai as the LLM framework, but the merged client
(`667959f`, 2026-07-03) was hand-rolled on the raw openai SDK. The drift was
closed the next morning by `04c9555` ("feat(llm): migrate core/llm client to
pydantic-ai") and amended on #58 (comment of 2026-07-04T09:25:20Z).

### Amendment: post-migration status sweep (2026-07-07)

The 2026-07-04 pydantic-ai correction (`04c9555`) is verified in deployed
code: `core/llm/client.py` is pydantic-ai 2.5.0 throughout; the raw openai
SDK survives only in `discover_models` (pydantic-ai has no model-listing
API). Since then:

- `chat_stream` was removed outright on main rather than kept fixed
  (`0e63bd0` — no live caller ever existed). Where a `chat_stream` exists
  (older lineages), it must resolve keys via `_resolve_api_key` exactly like
  `chat()`; `tests/test_llm_keyless.py` locks that invariant by method name
  so a resurrected streaming path is covered the day it exists.
- The deferred in-bot agent loop lane shipped (`c498ebf`/`54888e7`), gated
  behind `gpt_agentic_enabled` + `!setagentic`.
- The MCP ops server is wired to the real registry with mention suppression
  and history clamps (`057674d`, `96bccf3`) and is config-gated OFF
  (`mcp_ops_enabled` global bool; formerly the `MCP_OPS_ENABLED` env var).
  2026-08: the original guild allowlist was REMOVED by owner decision — MCP
  tools are raw one-to-one API primitives with full guild reach; access
  control belongs upstream in the MCP caller. The only guild-confined
  surface is the in-bot agent loop.
- Codex authz finding #4 (caller-supplied `actor_id` not credential-bound)
  was consciously downgraded to a documented accepted risk for loopback
  self-use (`core/mcp_server.py`). Reopen it only if the server is ever
  exposed beyond localhost.
- Sole unshipped lane from the epic body: pytest scaffolding + CI smoke
  workflow. `tests/test_llm_keyless.py` is the first step of that lane; a CI
  workflow is still owed. Epic body checkboxes should be updated (or the
  epic closed against #59 plus a hygiene issue) — all five lanes have
  merged.

### Amendment: AI-surface rework (2026-07-10)

`70ad081` reworked the surfaces the sweep above described; where they
conflict, this entry wins:

- `gpt_agentic_enabled` + `!setagentic`/`/ai setagentic` are GONE. Agentic
  mode is per-tool: a per-guild `bot_tools_enabled` allowlist (empty/default
  ⇒ plain-chat path, guaranteed single model turn) plus a global
  `mcp_tools_enabled` allowlist consumed by `core/mcp_server.py` at build
  time (restart-gated). Both are edited via the `!aisettings` panel
  (`cogs/optional/ai_admin.py`), which also runs the one-shot flag→allowlist
  migration on cog load.
- The narration-guard regex + retry nudge (`NARRATED_ACTION_RE`) was removed:
  its word-list fired on ordinary replies and caused duplicate answers. The
  agentic prompt now encourages rather than forces tool use; the only hard
  rule kept is "never claim a tool ran when it didn't."
- Per-model cooldown is driven by `cost_per_mtok_output` ($/1M output
  tokens) → three tiers (cheap <$1 = 10s, standard <$5 = 45s, pricy = 300s),
  enforced by a manual per-guild min-interval check. `timeout_multiplier`
  survives in the schema but is no longer read; the old flat
  `@commands.cooldown` decorator (240s, display-only multiplier) is gone.
- UsageTracker stays deleted (reaffirming `0e63bd0`'s YAGNI call). The
  branch `fix/post-sprint-seams` wiring that revived it was NOT merged; only
  its tracker-independent keyless tests were salvaged into `tests/`. Per-call
  cost visibility is the INFO-level `est_cost_usd` log line on agentic runs.

### Amendment: narrated-call backstop returns, redesigned (2026-07-21)

Supersedes the 2026-07-10 sentence "the narration-guard regex + retry nudge
was removed" — a narrated-call backstop exists again, but it is NOT the
word-list regex (that stays dead). Trigger: a live incident where grok-4.5
posted "run tool search_history with channel_id is ..." verbatim with zero
tool calls. The redesign (`cogs/optional/gpt.py`):

- Detector fires ONLY on a verbatim enabled-tool snake_case name or explicit
  "run tool" phrasing in a zero-tool-call reply — never a generic word list.
- One corrective re-run max (`NUDGE_PROMPT`). A false alarm is self-cleared
  by the model answering with the bare `OK` sentinel, in which case the
  ORIGINAL reply posts unchanged — the channel sees exactly one message in
  every path, so false positives are invisible (the 2026-07-10 duplicate-
  answer failure cannot recur).
- The primary defense is prompt-side: `build_agentic_guidance` states the
  function-calling loop contract explicitly (text-only response ends the
  run and is posted verbatim); the backstop exists for the residual case.

### Correction: `/ai settings` never existed as written (2026-08-11)

Several entries above (and CLAUDE.md, until this refactor) refer to `/ai
settings` and `/ai setapikey`. There is no `/ai` command group in deployed
code. The real surfaces are the panel commands `!aisettings` and its ephemeral
slash twin `/aisettings`; API keys are entered through that panel's modal, never
as a slash parameter. Read every historical `/ai settings` reference in this file
as `!aisettings`.

### Correction: cooldown tier numbers (2026-08-11)

The 2026-07-10 amendment records "cheap <$1 = 10s, standard <$5 = 45s, pricy =
300s" against a flat per-guild min-interval. Both the numbers and the mechanism
are superseded by the nested-window ladder. Live values (`cogs/optional/gpt.py`
`COOLDOWN_TIERS` / `DEFAULT_COOLDOWN_WINDOWS`): base periods **2s / 8s / 20s**
for cheap / standard / pricy, with the shared window ladder `1 per 1x · 10 per
15x · 100 per 150x · 300 per 4320x` — a message must have room in *every*
window. The cost thresholds (<$1, <$5, catch-all) are unchanged. Tunable via the
global keys `cooldown_tier_bases` / `cooldown_windows`; there is no panel
surface for them since the 2026-08 UX pass.

## Ops-registry refactor: derived frontends + cog-registered ops (2026-08-11)

Closes the loop opened by #58's "world pattern". Issues #64, #65, #77, #78 are
the primary records; this entry captures what deployed code now does. Commits
`14ef07c`, `b3c375c`, `242cd74`, `f51989a`, `8ffb49c`.

### The `agent` flag is gone — scope replaces it

**Decision: no code-level op subsets.** `Op.agent` (a bool) and
`registry.agent_names()` are deleted. Every op now declares `scope`
(`OpScope.GUILD` / `DM` / `GLOBAL`), and the in-guild agent universe is
*derived*: exactly the guild-scoped ops, queried **live** via
`registry.guild_agent_names()`.

Rationale: the flag was a second, hand-maintained answer to a question the op
already answers structurally. An op is safe for a guild-confined, user-actored
loop precisely when it acts on a guild — that is what `scope` means. WHICH of
those a guild enables is per-guild config, not a constant in `core/ops.py`.

Assignments: `send_dm`/`read_dms`/`fetch_dms` = DM; `list_guilds` = GLOBAL; the
other 22 core ops = GUILD. (2026-08: `delete_dm` — user-keyed retract of
bot-authored DMs, #90 — later joined the DM set.)

**Live queries are mandatory, not stylistic.** Cog ops appear and disappear with
cog load/unload, so any import-time tuple is stale after the first `!reload`.
`agent_ops()` and `exposed_ops()` are functions that hit the registry per call.
The one deliberate exception is the MCP tool surface (below).

Also new: `group` (kebab-case id + display label, from `OP_GROUPS`) driving
panel sections, and `origin` (`'core'` / `'cog'`). **Origin is stamped by the
registration path, never accepted as a decorator argument** — a cog cannot claim
to be a core primitive.

### Cogs register ops, mirroring discord.py's cog lifecycle

A module-level `@op(...)` decorator attaches an immutable spec to a cog method
and **does not touch the registry at import time**.
`registry.register_cog_ops(cog)` scans bound methods, preflights the batch
(duplicates within the batch and against the registry, validation) and registers
**all-or-none** with `owner=cog`; `registry.unregister_owner(cog)` removes that
owner's batch by identity (`is`, so two instances of one class can't evict each
other) and never raises for an owner that registered nothing.

Wiring lives in `bot.py`'s `add_cog`/`remove_cog` overrides rather than in
`core/ops.py`, which keeps the registry frontend-agnostic (it never imports
`discord.ext.commands`; cogs are opaque owner objects). Consequences that made
this the right seam: a failed `!reload` restores the old cog, which
re-registers its old batch through the same path — discord.py's reload is
atomic, so we inherit atomicity for free. Unregistration runs in a `finally` so
a cog whose teardown raises leaves no orphaned ops. A registration failure
ejects the cog, so discord.py never holds a loaded cog whose ops silently
aren't there.

First cog ops shipped: `add_emoji_role_toggle` + `sync_emoji_role_toggles`
(setrole) and `search_danbooru` (danbooru). Each follows #64's prescription —
logic factored into a headless **service function** both the command and the op
call; interaction handlers are never exposed as ops.

**Stored config is never rewritten to match the live registry.** A name whose op
is temporarily unregistered stays in `bot_tools_enabled` / `mcp_tools_enabled`
and is dropped only from the *effective* set (`resolve_bot_tools` /
`resolve_mcp_tools` intersect with the live registry; the panel's `_merge_stored`
carries offline names through verbatim). Pruning would silently destroy a guild's
choice on the next panel save and never restore it when the cog returned.

### `mcp_ops/` dissolved into `core/mcp_server.py`

The package (server.py + auth.py) is replaced by one module beside its sibling
frontend `core/agent_loop.py`, with the in-bot start logic folded in.

**The standalone runner was deleted outright, not fixed.** `main` / `_run` /
`_make_discord_client` logged a *second* Discord client into the same bot
account, and — decisively — could not see cog-registered ops at all, which the
cog-op lane makes a correctness failure rather than an inefficiency. There is
now exactly one way to run the server: in-process via `maybe_start_in_bot(bot)`,
gated on `mcp_ops_enabled`.

Settings became **config-first with env fallback**, matching the
`<PROVIDER>_API_KEY` pattern: `mcp_ops_token` → `MCP_OPS_TOKEN`, and
`mcp_ops_port` → `MCP_OPS_PORT` → `8765`. If the server is enabled with no token
anywhere, one is **generated** (`secrets.token_urlsafe(32)`) and persisted to
global config, logging that it did so and where — never the value. Generating
rather than refusing is still fail-closed (the server is never unauthenticated);
refusing would strand an operator who has no UI to set the secret. Host stays
hard-coded `127.0.0.1`, and a non-loopback legacy `MCP_OPS_HOST` refuses startup.

**The MCP tool surface stays restart-bound, deliberately.** It is built once at
server start from a live registry query. FastMCP on mcp 1.x cannot reliably
broadcast `notifications/tools/list_changed` to already-connected
streamable-HTTP sessions, so a mid-flight surface change would leave clients
holding a stale tool list with no way to learn better. `requirements.txt` pins
`mcp>=1.28,<2`; a 2.x migration is deferred.

### Panel doctrine: derived, grouped, admin-savable

The `!aisettings` Server tab renders the **live** guild-scoped universe as one
select per group, visibly split into **API primitives** (core/ops.py) vs
**behavioral primitives** (cog-registered) — the owner's terms for the two
kinds, used across README, docs and the panel headings. The
MCP tab (superadmin, unchanged) renders the whole live registry the same way.
Both re-query the registry at render time, so a cog load/unload changes the
panel on the next rerender without a restart.

- **Deviation from #78: ONE config key, not two.** #78 proposed splitting core
  and cog ops into parallel `bot_tools_enabled` / `bot_cog_tools_enabled` lists.
  Rejected: op names are unique registry-wide, so the split buys no
  disambiguation and costs a second list to keep consistent. `origin` is a
  *rendering* concern; the panel groups by it visually while storing one flat
  list of names.
- **`_save_bot_tools` gate relaxed from superadmin to `is_admin`.** Guild admins
  configure their own guild's agent surface. Anti-escalation holds structurally:
  that universe is guild-scoped ops only, and every op re-checks its own
  `PermissionLevel` against the invoking user at call time — enabling an op
  grants exposure, never authorization.
- **The prefix-heuristic "read-only preset" button was dropped** (not
  reimplemented). A `startswith("list_")` guess at which ops are safe is exactly
  the hand-maintained-subset pattern this refactor kills; in an allowlist editor
  it silently mis-classifies the moment an op is named off-pattern. Dead
  `_BOT_READONLY_OPS` went with it.
- The `_ToolSelect` cross-chunk merge machinery is **kept** and generalized
  across group-partitioned selects: a Discord select only reports its own
  options, so without merging, saving one select would clear every other one's
  choices.
- `AGENT_OPS_DEFAULT_ON` and the `gpt_agentic_enabled` → allowlist migration
  are **deleted entirely** (same day, owner decision). The migration was one-shot
  and had completed: no live config carried `gpt_agentic_enabled` any more, so
  the seed and the machinery served no remaining purpose. A migration that has
  finished migrating is dead code, not heritage — if the legacy key ever
  resurfaces from a restored backup, the guild simply lands in the modern
  default (empty allowlist ⇒ plain chat) and gets configured via the panel.

### Exposure vs authorization (the doctrine both frontends encode)

Worth stating once, because it is the reason the defaults look permissive:
`mcp_tools_enabled` absent ⇒ the full registry is served, and `bot_tools_enabled`
empty ⇒ plain chat. Neither list is a security boundary. **Admin config is an
exposure filter; authorization is the per-call `PermissionLevel` gate**, which
`registry.call_ids` evaluates against the invoking user *before* resolving any
Discord id (so a failed gate is not an id-probing oracle). Fail-open-to-full on
the MCP side is safe only in combination with the gates that are not
configurable: loopback bind, mandatory bearer token, and per-op permission
checks.

## #86 — The cog set is fixed at boot (2026-08-11)

**Decision (owner):** hot cog manipulation had functionally never been used.
The dynamic surface is deleted; the capability survives through restart.

**Deleted outright:**

- `!load`, `!unload`, `!reload` (including bare `!reload` = reload-all) in
  `cogs/core/control.py`.
- `!update` — **entirely**, not just its reload step. Its purpose belonged to
  the desktop-dev era: develop and commit elsewhere, avoid logging into the
  server to pull. Development now happens on the server itself, so deployment
  pulls happen in the same shell that restarts the bot. A `git pull` command
  that a superadmin can fire from Discord is RCE-adjacent surface bought for
  nothing.
- The `!cogs` panel's live callbacks: the 🔄 Reload and 🔄 Reload all buttons,
  and the load/unload half of the enable/disable toggle. `!disable` / `!enable`
  likewise stopped unloading and loading.

**End state.** `disabled_cogs` is read once, by startup, through
`core.utils.list_cog_modules`. Every editor of that list — `!disable`,
`!enable`, and the `!cogs` panel — now writes config and nothing else. The
panel gained a **Restart** button that calls `Control.do_restart`, the *same*
exit path as `!restart`/`!kys` (`bot.close()` then `sys.exit()`, with systemd
restarting the process) rather than a second, parallel one. Its render marks
entries whose configured state disagrees with what is running as
*pending restart*, so an edit is never mistaken for an applied change.

**Consistency argument.** The MCP tool surface was already restart-bound by
deliberate decision (see the section above: FastMCP on mcp 1.x cannot
reliably broadcast `tools/list_changed` to connected sessions). Cogs churn
less often than tool lists. Rather than carry two lifetimes, the bot now has
one mental model: **config edits bind at restart.**

**Deliberately kept:**

- The `add_cog` / `remove_cog` overrides in `bot.py`. They are the boot-time
  registration path — the only thing stamping `origin='cog'` — and
  `remove_cog` still fires at shutdown teardown.
- The op-identity fail-closed guards in `core/agent_loop.py` and
  `core/mcp_server.py` (aeea912, 9be22f9), including the mid-await TOCTOU
  re-check, plus their regression tests. These are now **inert**: nothing in
  production re-registers a name mid-run. They stay because they are ~20
  tested lines and because they encode *why* name-based dispatch is sound —
  identity is re-checked, not assumed. If dynamism is ever reintroduced, the
  belt is already fastened.
- `registry.unregister_owner` and the register/unregister/register cycle
  tests, retargeted from "reload semantics" to registry machinery: a name
  left reserved by teardown would fail the *next* boot's registration, far
  from its cause.

**Correction to earlier records in this file.** Three passages above were
accurate when written and are left as written: the panel-doctrine section says
a cog load/unload "changes the panel on the next rerender without a restart";
the live-queries paragraph cites "the first `!reload`"; and the `add_cog`
seam rationale cites "a failed `!reload` restores the old cog" as a consequence
that made `bot.py` the right seam. That last one keeps its force — the seam is
still right, and the all-or-none/`finally`/eject properties it names are all
still live at boot and teardown — it simply no longer has a `!reload` to point
at. Read the others as: the
frontends still query the registry **live** rather than freezing an import-time
tuple — which remains mandatory, because *which* cogs register is a runtime
config decision resolved during startup — but the event that changes the answer
is a **restart**, not a reload.

**Docs retired with the surface:** the "hot-reload during development"
guidance in `CLAUDE.md` and `docs/cog-development.md`, the `!reload setrole`
apply-my-JSON-edits note in `cogs/optional/setrole.py` (`/role sync` already
covers that case, through the same `_sync_guild` service), the `disabled_cogs`
row in `docs/config-system.md`, the superadmin-tier inventory in
`docs/security.md`, and the README's hot-reload claims.

## #83 — Zero-friction bootstrap: token chain, .env deprecation (2026-08-11)

**Goal.** Clone → run → paste token → bot online, on every platform, without
disabling any hosting path. The old Quick Start required creating a `.env` by
hand before the first run and then discovering `!claimsuper` to get any admin
surface — two undocumentable steps for anyone who isn't already the author.

**The resolution chain is the design; the stdin prompt is sugar.** In
precedence order (`core/bootstrap.py`):

1. **`DISCORD_TOKEN` env var** — first-class forever. This is the contract for
   panel hosts (startup-variable field), Docker, and systemd. **Never persisted
   to disk**: copying a panel-supplied secret into a file surprises the operator
   (they set it in one place, it lives in two) and doubles the leak surface for
   a credential they rotate elsewhere.
2. **`discord_token` global config key** — the self-hosted steady state.
3. **Interactive `getpass` prompt** — ONLY when stdin is a TTY.
4. **Neither, and no TTY → exit nonzero with per-platform instructions.**

Point 4 is load-bearing and is the whole reason the chain beats "just prompt".
A prompt on a panel console or a systemd unit is a **hang**, not a question,
and a hung bot looks like a crashed bot. `isatty` is checked before anything is
read, and the check is wrapped: a detached/closed stdin raises from `isatty`
rather than returning False, and that must fail fast, not propagate an
unrelated `ValueError` out of startup.

**Verify before persisting.** A typo'd token written to `global.json` converts
a one-line fix into a "why does it fail *with a token configured*" debugging
session. The verification boundary is discord.py's own login: `bot.run()`
raises `LoginFailure` before the gateway connects, and reaches `setup_hook`
only after the REST login succeeded. So the candidate token is staged on the
bot and written from inside `setup_hook`, cleared after the write so a
reconnect can't re-persist. `LoginFailure` on a *prompted* token re-prompts (3
attempts) and writes nothing.

Retry is deliberately **prompt-only**. A rejected env or stored token exits
with a message naming which source failed, rather than falling through to a
prompt — silently prompting past a broken panel variable would hide the actual
misconfiguration and produce a bot that works once and fails on every restart.

**No separate `secrets.json`** (owner decision). Provider API keys already live
in `global.json` as a documented accepted risk; a second plaintext file is
separation without a distinct threat model — same process, same disk, same
blast radius. The hardening that actually does the work is filesystem
permissions: `configs/` 0700, every config file 0600. The temp file in the
existing atomic tmp+rename is *created* 0600 via `os.open` rather than chmod'd
after writing, so the content is never briefly on disk under the umask default.
Both chmods are best-effort — a filesystem that can't represent POSIX modes is
a downgrade in hardening, not a reason to refuse to start.

**First-run superadmin, empty-list-gated.** On a first successful login with
`superadmins` empty or absent, the application owner is granted it and the
grant is logged plainly. The empty-list gate is the safety property, not an
optimization: an established deployment always has a non-empty list, so this
can never re-seat ownership on one. It is re-checked *after* the
`application_info()` await — cogs load in `setup_hook`, so commands are already
dispatchable while that call is in flight, and a `!claimsuper` landing in that
window must win. A failed `application_info()` logs and falls back to
`!claimsuper` rather than taking startup down.

Team-owned apps read **`Team.owner_id`**. This was wrong in review: the code
first read `team.owner_user_id`, which is the raw JSON key discord.py parses
*from* (`discord/team.py`: `self.owner_id = _get_as_snowflake(data,
'owner_user_id')`), never an attribute on the object. The bug failed silently —
the `Team.owner` fallback is a `utils.get` over `members` and returns None when
the owner isn't in that list, so a team-owned app would have started with no
superadmin and no error. The regression test builds a **real** `discord.team.Team`
rather than a hand-made double, because a double would have cheerfully agreed
with the wrong attribute name.

**`.env` deprecated, not removed.** Still loaded when present so existing
deployments keep working untouched; dropped from documented setup. `load_dotenv`
only fills env vars that aren't already set, so a real `DISCORD_TOKEN` still
wins over a stale `.env` entry.

**`start.sh` / `start.bat`** build the venv, install requirements only when
`requirements.txt` is newer than a stamp file, and exec the bot. They are
deliberately dumb and readable: they're evangelism artifacts, and anyone should
be able to see exactly what they do to their machine before running them.
`start_bot.sh` was deleted rather than kept — it was a near-duplicate of
`start.sh` whose first act was to refuse to run without a `.env`, which is
precisely the friction this issue removes.

**Found in cold review (and fixed here), beyond the team-owner bug above:**

- **Secrets could be written under a loose mode.** The temp file in the atomic
  tmp+rename was created 0600 but chmod'd only after `json.dump`. O_CREAT's
  mode is ignored when the file already exists, so a `.tmp` left by a previous
  crash kept its old world-readable mode *while the new secret was written into
  it*. Now `fchmod`'d on the open descriptor before any content is written. The
  test samples the mode from inside `json.dump`; asserting the landed file's
  mode cannot catch this, since the post-rename chmod fixes the end state
  either way.
- **A crashed bot exited zero.** `bot.py`'s `except Exception` logged and fell
  through to a 0 exit (pre-existing on main). That is precisely the confusion
  this issue removes — a panel host shows a green "stopped normally" for a bot
  that crashed. Now exits 1.

**Correction to an earlier record.** The README's `!autoresponse` line claimed
"weighted responses". Responses are a uniform `random.choice`, and the
short-lived `[text, weight]` shape is stripped on read by `_response_texts`.
The line now says what the code does; weighting was not implemented.
