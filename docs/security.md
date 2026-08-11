# Security Model

Reference for the bot's permission model, the agentic/ops execution path, the
MCP ops server, and secret handling. Read alongside `docs/config-system.md`
(the JSON store that backs every permission list below).

## Permission Model

Three tiers, checked by `core/utils.py`. Both prefix commands (`commands.Context`)
and slash commands (`discord.Interaction`) route through the same `is_admin` /
`is_superadmin` helpers — there is one auth gate, not two.

| Tier | Granted by | Stored in | Checked by |
|------|-----------|-----------|-----------|
| **Superadmin** | `!claimsuper` (first-come, once) or `!addsuperadmin` (by an existing superadmin) | `global.json` → `superadmins` (list of int user ids) | `is_superadmin(ctx)` |
| **Guild admin** | `!claimadmin` (needs Discord Administrator / guild owner, only if no admins exist yet) or `!addadmin` (by a superadmin or existing admin) | `<GUILD_ID>.json` → `admins` (list of int user ids) | `is_admin(ctx)` |
| **Everyone** | default | — | no check |

`is_admin` returns true for: superadmins, ids in the guild's `admins` list, a
member with the Discord **Administrator** permission, or the guild owner.
`is_superadmin` is global and id-only.

### Escalation guards (intentional)

- **The bot's own account is never admin.** `is_admin` explicitly returns false
  when the actor id equals the bot user id. Without this, the bot's own Discord
  Administrator role would pass the `guild_permissions.administrator` check, and a
  self-authored command (driven through the agent loop or MCP server) could
  escalate. See `bot.py`'s `on_message` allowlist shim, which relies on this.
- **Superadmin is a trusted (owner) tier.** Superadmin-gated commands include
  arbitrary cog load/reload (`!load`, `!reload`), `git pull` (`!update`), process
  restart (`!restart`), bulk delete (`!cleanup`), and channel migration. These are
  effectively owner-level and are RCE-adjacent by design — grant superadmin only to
  the operator.
- **Claim-once semantics.** `!claimsuper` no-ops if any superadmin already exists;
  `!claimadmin` no-ops for non-superadmins once a guild already has admins.

## Agentic / Ops Execution Model

The ops registry (`core/ops.py`) is the single place every atomic Discord action
is defined, each with a declared `PermissionLevel` (EVERYONE / ADMIN / SUPERADMIN).
Two frontends call into it: the in-bot agent loop (`core/agent_loop.py`, used by
`!gpt` when agentic mode is on) and the MCP server (`core/mcp_server.py`).

Security properties enforced centrally, so no frontend can skip them:

- **Actor = the invoking user.** In the agent loop the live `commands.Context`
  passes straight through as the `OpContext`, so every op's permission gate
  evaluates the *invoking user's* real Member in their real guild — never the
  bot, never `guild.me`. An op the user can't authorize fails closed and the
  denial is returned into the loop as a tool error.
- **In-guild confinement.** `allowed_guild_ids` for the agent loop is exactly
  `{ctx.guild.id}`. Every id-resolved target (channel/message/member/role/guild)
  is checked to belong to an allowed guild (`check_guild_allowed`); a target in
  another guild the bot happens to be in, or in a DM, is refused
  (`GuildNotAllowedError`).
- **Channel-visibility gate.** When the actor is a real Member, ops whose target
  channel the actor cannot read (`permissions_for(actor).read_messages`) are
  refused — the bot sees more channels than any one user, and this stops it from
  leaking history/members from channels the caller can't see, or posting into them.
- **Mentions suppressed.** Both frontends force `allowed_mentions=none` on
  `send_message`, so tool-driven sends never ping.
- **Tool budget.** The agentic `!gpt` loop has a SOFT budget of 8 tool calls per
  run (`core/agent_loop.AGENT_TOOL_BUDGET`), shared across the run and any
  narration-nudge retry: the last 3 results carry a `tool_calls_remaining`
  countdown, and calls past the budget are refused with an answer-now error so
  the model always authors its own final reply. pydantic-ai's hard cap sits at
  2x (16) as a runaway backstop; even that path degrades to a model-authored
  plain-chat answer, never a canned failure string.
- **Scope is a structural boundary.** Every op declares an `OpScope`, and the
  in-guild agent's tool universe is *derived* as exactly the `GUILD`-scoped ops
  (`registry.guild_agent_names()`), queried live. DM-scoped ops (`send_dm`,
  `read_dms`, `fetch_dms`) and global ops (`list_guilds`) are therefore never
  offerable to a guild's agent surface at all — not by config, not by mistake.
  There is no hand-maintained "agent tools" list to drift (the former
  `agent=True` flag was removed in the 2026-08 refactor).
- **Agentic mode is opt-in and per-tool.** Each guild has a `bot_tools_enabled`
  allowlist (default empty, meaning `!gpt` is plain chat with no tools), managed
  from the `!aisettings` panel. The MCP server consumes its own global
  `mcp_tools_enabled` allowlist at build time.
- **Every executed op is logged** at INFO (op name, params, actor id, ok/error).

### Allowlists are exposure filters, not authorization

This distinction is the whole security model of the ops layer, and it explains
why the defaults look permissive:

| | Decides | Set by | Default |
|---|---------|--------|---------|
| **`PermissionLevel`** (per op, in code) | **Whether a given actor may run it** | The op's declaration; not configurable at runtime | enforced always |
| **`bot_tools_enabled`** (per guild) | Whether the guild's agent is *offered* it | any bot admin of that guild | empty ⇒ plain chat |
| **`mcp_tools_enabled`** (global) | Whether the MCP server *serves* it | superadmin | absent ⇒ **whole registry** |

Enabling an op grants **exposure**, never **authorization**. Every call still
passes the op's own `PermissionLevel` gate against the invoking actor, before any
Discord id is resolved. So an admin who enables `delete_message` for their guild
has not granted anyone the ability to delete messages — non-admin users who try
get the same permission error they would have gotten anyway.

That is why **`mcp_tools_enabled` fails open to the full registry** rather than
closed to nothing. It is a deliberate owner decision, not an oversight: the MCP
surface's actual security boundary is the set of gates that are *not*
configurable — loopback-only bind, mandatory bearer token, and the per-call
permission checks. A config list that an operator must populate before the
server is useful would add friction without adding a boundary. The guild-side
default is the opposite (empty ⇒ no tools) because there the exposure decision
is also a *behavioral* one: a guild that hasn't opted in should get plain chat.

**`bot_tools_enabled` is guild-admin-savable** (relaxed from superadmin in
2026-08). Not an escalation path, for the structural reason above plus scope:
the universe a guild admin picks from is guild-scoped ops only, so there is
nothing in it that reaches outside the guild they already administer.

**Cog-registered ops** enter these universes the moment their cog loads and
leave when it unloads, carrying the same declared `PermissionLevel` as any core
op — the registration path stamps `origin='cog'`, which a cog cannot forge, but
origin affects only how the panel renders it, never what it may do. A stored
allowlist name whose op is currently unregistered is retained in config and
dropped only from the effective set, so an unloaded cog cannot cause a silent
permission change on the next panel save.

### Ordering (exposed-op selection)

`registry.call_ids` checks the permission gate **before** resolving any ids to
live Discord objects, so a caller who fails the gate learns nothing about
whether a guessed id exists — no id-probing oracle. That is what makes
ADMIN-tier ops (e.g. `delete_message`) safely exposable on both frontends: a
non-admin gets the same permission error regardless of target validity.
`Op.__call__`'s own permission check is belt-and-suspenders for object-based
callers that bypass `call_ids` and resolved nothing through the registry.

## MCP Ops Server

`core/mcp_server.py` exposes a subset of the ops registry over HTTP, in-process
with the bot (the standalone runner was deleted in 2026-08 — it opened a second
Discord session on the same account and could not see cog-registered ops). All
gates are fail-closed and independently required:

- **Off by default.** Refuses to start unless the `mcp_ops_enabled` global
  config boolean is true (toggled from `!aisettings` → MCP tab; moved out of
  `.env` 2026-08 so it's operable without shell access; binds on restart).
- **Loopback-only bind.** Hard-coded `127.0.0.1`; there is no host parameter. A
  legacy `MCP_OPS_HOST` set to any non-loopback value refuses startup rather than
  rebinding.
- **Bearer token required.** Every request must carry
  `Authorization: Bearer <token>`; the token is compared with
  `hmac.compare_digest` (constant-time). The token resolves config-first
  (`mcp_ops_token` global key) with an `MCP_OPS_TOKEN` env fallback; when an
  operator enables the server with neither set, one is generated
  (`secrets.token_urlsafe(32)`) and written to global config. Generating is
  still fail-closed — the server is never reachable without a secret — and the
  token value is never logged, only its storage location.
- **Mentions suppressed** on `send_message`, same as the agent loop.
- **Full guild reach by design** (owner decision 2026-08; the former
  `MCP_OPS_GUILD_ALLOWLIST` gate was removed). MCP tools act as raw
  primitives: every guild the bot account is in is addressable, and access
  control belongs upstream in the MCP caller. DM channels are still refused
  on id-based calls — DMs flow only through the user-keyed DM ops (user_id
  only, matching the DM API; Discord refuses bot DMs to strangers). The one
  guild-confined surface in the system is the in-bot `!gpt` agent loop,
  which passes exactly `{ctx.guild.id}` to the registry.

### Accepted risk: caller-supplied `actor_id`

The MCP frontend takes `actor_id` as a plain tool parameter — it is **not**
credential-bound. A client that already holds the bearer token can present any
user id (including a superadmin's) for permission purposes. This is acceptable
**only** for localhost self-use, which the loopback bind + token enforce. Do not
expose this server beyond loopback without adding real actor authentication.
When `actor_id` doesn't match a real guild Member, a bare id-holder is used, which
the permission helpers treat as an ordinary non-admin unless the id is in a config
admin/superadmin list.

## API Keys and Secrets

- **Discord token** comes from `.env` (`DISCORD_TOKEN`), loaded via `dotenv`.
  `.env` and `.env.*` are gitignored.
- **Provider API keys** are entered through the `!aisettings` → Models &
  Providers key modal (superadmin-gated; a modal, never a slash parameter, so the
  key is never in a command log) and stored in `global.json` under
  `<PROVIDER>_API_KEY` — **plaintext on disk**. Environment variables of the same
  name are honored as a fallback. Protect the `configs/` directory's filesystem
  permissions accordingly.
- **The MCP bearer token** follows the same config-first-with-env-fallback
  pattern: the `mcp_ops_token` global config key, else `MCP_OPS_TOKEN`. It moved
  out of env-only into config in 2026-08 so the server is operable without shell
  access. If an operator enables the server with neither set, the bot
  **generates** one (`secrets.token_urlsafe(32)`) and persists it to
  `global.json`. Generating rather than refusing keeps the server fail-closed
  (it is never unauthenticated) without stranding an operator who has no UI for
  the secret. **The value is never logged** — only the fact that one was
  generated and which key holds it; read it out of `configs/global.json` to
  connect a client. It is plaintext on disk like the provider keys, and it is a
  full-privilege credential for the ops surface: treat `configs/` accordingly.
- **`configs/` is gitignored in full**, so no per-guild data, memories, admin
  lists, or stored keys are committed. Verified: `git ls-files configs/` is empty.
- **Keys never appear in-channel.** The key is typed into a Discord **modal**
  from the `!aisettings` → Models & Providers tab, which the panel answers
  ephemerally. It is deliberately not a slash-command parameter (a slash
  argument is visible while typing and lands in the interaction log), and the old
  prefix `!setapikey` — which posted the key into chat and then raced to delete
  it — has been removed.

## Prompt-Injection and Data Surfaces (`!gpt`)

- **Channel history is sent to a third-party LLM.** `!gpt` scrapes the last ~15
  channel messages (plus referenced messages and their embeds/attachment URLs) and
  sends them to the configured provider. This is a data-egress surface: whatever is
  visible in the channel can leave to the provider. Choose providers accordingly;
  a local provider (`requires_api_key: false`, e.g. a self-hosted model) keeps data
  on-box.
- **Persona is guild-admin-settable.** The personality editor in the
  `!aisettings` panel (admin-gated) sets the system persona for that guild's
  `!gpt`. Any guild admin can rewrite the bot's system prompt.
- **Output mention filter is narrow, backed by AllowedMentions.**
  `check_message_compliance` blocks only the literal substrings `@everyone` /
  `@here` in the model's reply, but every reply chunk is sent with
  `AllowedMentions(users=True, roles=False, everyone=False)` — user pings are an
  intended feature; role/everyone pings cannot fire even if the substring filter
  is bypassed.
- **Memory capture runs on every message.** Regexes in
  `capture_and_store_memories` extract statements ("my name is …", "you're to
  always …", etc.) from *all* messages — not just `!gpt` invocations — and persist
  them per-guild (`gpt_memories`), later injecting them into the system prompt for
  future `!gpt` calls. See hardening checklist for the stored-injection implication.

## Known Accepted Risks (documented, not defects)

1. **MCP `actor_id` is caller-supplied** — acceptable for localhost self-use only
   (see above).
2. **Superadmin tier is owner-level RCE-adjacent** — cog load/reload, `git pull`,
   restart, and bulk delete are intentionally available to superadmins.
3. **Provider API keys are stored plaintext** in `global.json` (outside git). Rely
   on filesystem permissions.
4. **Channel history egress to the LLM provider** is inherent to the `!gpt`
   feature.

## Hardening Checklist

- [x] **Mention suppression on the chat reply path** — `!gpt` replies pass
  `AllowedMentions(users=True, roles=False, everyone=False)`: user pings are an
  intended feature, but model output can no longer ping roles or everyone. The
  ops `send_message` op additionally suppresses ALL mentions by default at the
  registry level (a caller must pass an explicit `allowed_mentions` to ping).
- [x] **Directive memory capture is admin-gated** — `you're to always …`
  directives are only persisted when the author passes the shared `is_admin`
  gate; other members' directive-shaped messages are ignored. Non-directive
  memory types (names, preferences, reminders) are still captured from anyone,
  which only affects how the bot talks *about* that user.
- [ ] **Restrict filesystem access to `configs/`** so plaintext API keys and admin
  lists aren't world-readable.
- [ ] **Keep the MCP server loopback-only.** Never front it with a reverse proxy or
  bind it publicly without first replacing caller-supplied `actor_id` with real
  actor authentication.
- [ ] **Audit `addmedia` targets** — it fetches arbitrary user-supplied URLs
  server-side (admin-gated); an admin could point it at internal/loopback or cloud
  metadata endpoints (SSRF). Consider an allowlist / private-IP block if the admin
  set is broad.
- [x] **The console REPL cog was removed** (2026-07-05) — it read host stdin
  and could send messages as the bot to any channel with no auth, and was dead
  under systemd anyway (blocking `input()` on a non-tty).
- [x] **Unauthenticated config-disclosure reads are gone.** The former `/ai
  status` and `!listmedia` — which showed any member which providers had keys
  configured, the model catalog, and the media inventory — no longer exist. That
  state now lives only inside the admin-gated `!aisettings` / `!media` panels,
  which additionally answer ephemerally in their slash form, so a bystander
  cannot even read a panel someone else opened.

Fixed in the 2026-07-05 consistency sweep (see git history for details):
`!echo` is admin-gated (was open bot-impersonation), `!sethuebridgeip` is
superadmin-gated (was an ungated global-config write), `!addmedia` and the
`!errorlog` group route through the shared `is_admin` gate (each previously
used a divergent hand-rolled check), and the global-mutating provider commands
(`setapikey`/`addmodel`/`removemodel`/`addprovider`) were made superadmin-only
because they alter configuration shared by every guild — their prefix variants
have since been removed entirely in favor of the `/ai` surfaces, which keep the
superadmin gate.
`delete_message` is exposed to the agent loop and MCP — it remains ADMIN-gated
per-call, and `call_ids` now checks permissions *before* resolving any Discord
targets.
