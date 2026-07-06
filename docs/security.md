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
`!gpt` when agentic mode is on) and the MCP server (`mcp_ops/server.py`).

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
- **Tool budget.** The agentic `!gpt` loop is capped at 8 tool calls per run
  (`max_tool_calls=8`); exceeding it returns a fixed message rather than looping.
- **Agentic mode is opt-in and superadmin-gated.** `!setagentic on|off` is
  superadmin-only and per-guild (`gpt_agentic_enabled`, default off). With the flag
  off, `!gpt` is plain chat with no tools.
- **Every executed op is logged** at INFO (op name, params, actor id, ok/error).

### Ordering caveat (exposed-op selection)

`Op.__call__` resolves ids to live objects **before** the permission gate runs.
The agent loop and MCP server therefore only expose EVERYONE-tier ops on purpose:
exposing an ADMIN op would let a non-admin distinguish "invalid id" from "requires
admin" by the error text — an id-probing oracle. To expose an ADMIN op, move
resolution to *after* the gate first. This constraint is documented at the
`_EXPOSED_OPS` / `AGENT_OPS` lists.

## MCP Ops Server

`mcp_ops/server.py` exposes a subset of the ops registry over HTTP. All gates are
fail-closed and independently required (`mcp_ops/run_mcp_server.py`):

- **Off by default.** Refuses to start unless `MCP_OPS_ENABLED=1`.
- **Loopback-only bind.** Hard-coded `127.0.0.1`; there is no host parameter. A
  legacy `MCP_OPS_HOST` set to any non-loopback value refuses startup rather than
  rebinding.
- **Bearer token required.** Every request must carry
  `Authorization: Bearer <MCP_OPS_TOKEN>`; the token is compared with
  `hmac.compare_digest` (constant-time). No token configured → refuses to start.
- **Guild allowlist required.** `MCP_OPS_GUILD_ALLOWLIST` (comma-separated guild
  ids) must name at least one guild; an empty allowlist refuses to build the
  server. Every id-resolved target must belong to an allowlisted guild.
  `list_guilds` output is filtered to the allowlist.
- **Mentions suppressed** on `send_message`, same as the agent loop.

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
- **Provider API keys** are set with `!setapikey <provider> <key>` (admin-gated) or
  `/ai setapikey` (ephemeral response), and stored in `global.json` under
  `<PROVIDER>_API_KEY` — **plaintext on disk**. Environment variables of the same
  name are also honored as a fallback. Protect the `configs/` directory's
  filesystem permissions accordingly.
- **`configs/` is gitignored in full**, so no per-guild data, memories, admin
  lists, or stored keys are committed. Verified: `git ls-files configs/` is empty.
- **Prefix `!setapikey` deletes the invoking message** immediately so the key
  doesn't linger in-channel; if deletion fails it warns the user to delete manually.
  The slash variant never posts the key as a visible message and replies ephemerally.

## Prompt-Injection and Data Surfaces (`!gpt`)

- **Channel history is sent to a third-party LLM.** `!gpt` scrapes the last ~15
  channel messages (plus referenced messages and their embeds/attachment URLs) and
  sends them to the configured provider. This is a data-egress surface: whatever is
  visible in the channel can leave to the provider. Choose providers accordingly;
  a local provider (`requires_api_key: false`, e.g. a self-hosted model) keeps data
  on-box.
- **Persona is guild-admin-settable.** `!setpersonality` (admin-gated) sets the
  system persona for that guild's `!gpt`. Any guild admin can rewrite the bot's
  system prompt.
- **Output mention filter is narrow.** `check_message_compliance` blocks only the
  literal substrings `@everyone` / `@here` in the model's reply. It does **not**
  block role mentions (`<@&ROLE_ID>`) or user mentions, and the primary chat reply
  is sent with default `allowed_mentions` (see hardening checklist).
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

- [ ] **Set a global `allowed_mentions` default on the Bot** (or pass
  `allowed_mentions=AllowedMentions.none()` on the `!gpt` reply `ctx.send`). The
  ops/agentic send path already suppresses mentions, but the primary chat reply
  path uses Discord's default, so a model reply containing `<@&ROLE_ID>` can ping a
  role and `<@USER_ID>` can ping users — only `@everyone`/`@here` literals are
  filtered.
- [ ] **Reconsider unconditional memory capture.** Because capture runs on every
  message and directives like `you're to always …` are injected into the shared
  guild prompt, any member can plant a persistent instruction that steers `!gpt`
  for everyone. Consider capturing only from `!gpt` invocations, or excluding the
  `directive` type, or scoping memories to their author.
- [ ] **Restrict filesystem access to `configs/`** so plaintext API keys and admin
  lists aren't world-readable.
- [ ] **Keep the MCP server loopback-only.** Never front it with a reverse proxy or
  bind it publicly without first replacing caller-supplied `actor_id` with real
  actor authentication.
- [ ] **Audit `addmedia` targets** — it fetches arbitrary user-supplied URLs
  server-side (admin-gated); an admin could point it at internal/loopback or cloud
  metadata endpoints (SSRF). Consider an allowlist / private-IP block if the admin
  set is broad.
- [ ] **The console REPL cog** (`cogs/static/repl.py`) reads from the host stdin
  and can send messages as the bot to any channel with no auth — it is a
  local-console god-mode surface (and blocks the event loop). Remove it in
  production unless the host console is trusted and the bot is run interactively.
- [ ] **Review unauthenticated read commands** (`!aiinfo`, `!listmodels`,
  `!listmedia`) — they disclose configuration state (which providers have keys
  configured, model lists) to any user. Low impact; gate if that matters.
