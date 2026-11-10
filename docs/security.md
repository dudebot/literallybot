# Security Model

Reference for the bot's permission model, the agentic/ops execution path, the
MCP ops server, and secret handling. Read alongside `docs/config-system.md`
(the JSON store that backs every permission list below).

## Permission Model

Commands have three access levels, decided by the bot (not by Discord roles
alone) in `core/permissions.py`. Prefix commands (`commands.Context`) and slash
commands (`discord.Interaction`) use the same `is_admin` / `is_superadmin`
predicates.

| Tier | Granted by | Stored in | Checked by |
|------|-----------|-----------|-----------|
| **Superadmin** | `!claimsuper` (first-come, once) or `!addsuperadmin` (by an existing superadmin) | `global.json` → `superadmins` (list of int user ids) | `is_superadmin(ctx)` |
| **Bot admin** | `!claimadmin` (needs Discord Administrator / guild owner, only if no admins exist yet) or `!addadmin` (by a superadmin or existing admin) | `<GUILD_ID>.json` → `admins` (list of int user ids) | `is_admin(ctx)` |
| **Everyone** | default | — | no check |

`is_admin` returns true for: superadmins, ids in the guild's `admins` list, a
member with the Discord **Administrator** permission, or the guild owner.
`is_superadmin` is global and id-only: the bot's operator.

A cog gates a command with one decorator line: `@commands.check(is_admin)` /
`@commands.check(is_superadmin)` on prefix, `@app_commands.check(...)` on slash.
Ops are different: they declare no access level (see below).

### Command gates (prefix and slash)

One predicate, two discord.py wrappers — `@commands.check(is_admin)` on
prefix, `@app_commands.check(is_admin)` on slash. Same function; discord.py
has two command systems and they do not accept each other's decorator. Do
not invent a third wrapper.

Gated slash commands set `guild_only` (never in DMs) and `@panel_slash_pin()`
(`core/panels.py`), which hides them from members without Manage Messages in
Discord's picker. That is visibility, not authorization: the check is the
gate. A bot admin or superadmin without Manage Messages in that guild still
has the prefix twin.

`/help` lists a command only if the invoker could run it, by reading the
decorator. A body `if not is_admin` is defense in depth, not the gate.

Authoring examples: [cog-development.md](cog-development.md) → Permission
Management.

### Escalation guards (intentional)

- **The bot's own account is never admin.** Context-based `is_admin` and
  `is_superadmin` explicitly return false
  when the actor id equals the bot user id. Without this, the bot's own Discord
  Administrator role would pass the `guild_permissions.administrator` check, and a
  self-authored command (driven through the agent loop or MCP server) could
  escalate. See `bot.py`'s `on_message` allowlist shim, which relies on this.
- **Superadmin is a trusted (owner) tier.** Superadmin-gated commands include
  the global config editor (`!config`, which stores provider API keys), the
  `disabled_cogs` editor (`!cogs`, `!disable`/`!enable`), process restart
  (`!restart`), bulk delete (`!cleanup`), and channel migration. These are
  effectively owner-level and are RCE-adjacent by design — grant superadmin only to
  the operator.
- **Claim-once semantics.** `!claimsuper` no-ops if any superadmin already exists;
  `!claimadmin` no-ops for non-superadmins once a guild already has admins.

## Agentic / Ops Execution Model

The registry (`core/ops.py`) describes atomic actions, typed parameters,
scopes and results. Neither `@op` nor `@registry.op` declares a permission
level. Nothing about access is put in a tool description or sent to the model.

### Configured authorization

`/aisettings` is the authority for in-chat tool access:

| Setting | Effect | Default |
|---------|--------|---------|
| Global `agent_ops_whitelist` | Selects capabilities available for guild configuration | Empty |
| Guild `agent_ops_gate` | Off disables the tool; Admin authorizes bot admins; Everyone authorizes members | Off |
| Global `ai_self_tools_enabled` | Master switch for SELF tools | On |

Tool selection and dispatch use `core.agent_gate`.
`resolve_agent_tools` offers the model only the tools the invoking user may
call; the model calls them and gets Discord's own error if Discord refuses.
Dispatch re-reads the whitelist and guild choice before resolving Discord IDs
or spending action budget. Off and whitelist revocation apply to every actor,
including superadmins; there is no bypass. Registration never grants access or
writes defaults. Settings editors and ordinary prefix/slash commands keep their
`is_admin` / `is_superadmin` checks.

MCP is a separate authenticated operator surface, controlled by
`mcp_tools_enabled` and its bearer/host/origin checks. It has no additional
per-op actor tier. Direct registry callers are trusted application code and
must authorize their entrypoint before dispatch.

### `confirm_everyone` is a prompt, not a boundary

An op declared with `confirm_everyone=True` (role and channel-permission
writes, bans and kicks, webhooks, bulk deletes, `configure_honeypot`, and
others) makes the `/aisettings` Server tab ask the admin to type YES in a modal
before the op is first set to Everyone
(`_ConfirmEveryoneModal` in `cogs/gpt.py`). Dismissing the modal saves nothing.
The flag is never checked at call time and never sent to the model. Operators
may still open any op to everyone; the prompt only makes sure it is on purpose.

### Execution boundaries

- The actor is the invoking user. In-chat calls are confined to the invoking
  guild; DM and GLOBAL ops cannot enter a guild agent's tool universe.
- Guild-channel visibility and message-history checks still apply. Private
  thread reads require membership or Manage Threads, verified against Discord.
- SELF ownership and target-resolution rules still apply; see Invoker-only
  tools below. SELF tools are unavailable through MCP.
- Discord itself enforces the bot's permissions, hierarchy and API limits.
  A configured-access refusal is local and does not mean Discord rejected it.
- Sends suppress mentions. Bot-wide, `allowed_mentions` blocks @everyone and
  role pings unless a call says otherwise (`bot.py`).
- Host paths. A parameter declared `OpParam(..., host_path=True)` (attachment
  and emoji file paths) must, in chat, resolve inside the guild's media library
  (`media/<guild_id>/`) or the emoji download folder (`media/tmp/`)
  (`core.ops.chat_host_path_error`, enforced in `core/agent_loop.py`). Anything
  else is refused with one fixed message before the file is touched. File type,
  size and existence checks still apply. Configured access says who may run an
  op, not which host files it may read. MCP is not confined.
- The action budget defaults to 8 (Server config offers 4/8/12/16; stored
  values are clamped to 1–16). The final three results carry a countdown;
  calls after exhaustion are refused in-band. The hard stop is a model-request
  limit of budget + 2, which falls back to one tool-less model answer.
- `stay_quiet()` is local terminal output, outside the action budget and
  registry. It ends the run without an automatic final reply; prior actions
  remain. Empty model output is an error.
- Op calls are logged with status and timings. SELF exchanges omit arguments
  and results from logs. Cog ops join and leave the registry with their owning
  cog; stored configuration for unloaded ops is preserved.

## MCP Ops Server

`core/mcp_server.py` exposes a subset of the ops registry over HTTP, in-process
with the bot, so it sees cog-registered ops. The server stays off unless
enabled. Every request still has to pass bearer auth
and the Host/Origin allowlist for the bind.

- **Off by default.** Refuses to start unless the `mcp_ops_enabled` global
  config boolean is true (toggled from `!aisettings` → MCP tab; binds on
  restart).
- **Bind.** Default `127.0.0.1`. `MCP_OPS_HOST`, when set, is an explicit
  opt-in: an IP address, `localhost`, or a wildcard (`0.0.0.0` or `::`).
  Unset or empty stays on loopback. Anything else refuses startup. A
  concrete LAN address needs no further setting. Bearer auth stays
  mandatory either way.
- **DNS-rebinding protection stays on.** FastMCP turns the Host/Origin
  check off for a non-loopback `host` unless `transport_security` is passed.
  This server always passes one. A concrete bind allowlists that address
  plus loopback (`127.0.0.1`, `localhost`, `[::1]`), any port. A wildcard
  refuses to start unless `MCP_OPS_TRUSTED_HOSTS` lists the IPs or
  hostnames clients actually send in `Host` (comma-separated). `0.0.0.0`
  is not a Host value. No deployment address is built in. A missing
  `Origin` is accepted (native clients omit it). A present `Origin` must
  be `http://` plus an allowlisted host. A bad `Host` is 421 and a bad
  `Origin` is 403, bearer or not. The allowlist is not a firewall and not
  TLS; a wildcard still listens on every interface the OS accepts.
- **Bearer token required.** Every request must carry
  `Authorization: Bearer <token>`; the token is compared with
  `hmac.compare_digest` (constant-time) before the request reaches FastMCP.
  The token resolves config-first
  (`mcp_ops_token` global key) with an `MCP_OPS_TOKEN` env fallback; when an
  operator enables the server with neither set, one is generated
  (`secrets.token_urlsafe(32)`) and written to global config. Generating is
  still fail-closed — the server is never reachable without a secret — and the
  token value is never logged, only its storage location. The bearer is one
  shared secret, not a per-user credential. Anyone who can reach the port
  and present it is a full operator of the exposed tools.
- **Mentions suppressed** on `send_message`, same as the agent loop.
- **Full guild reach by design.** MCP tools act as raw
  primitives: every guild the bot account is in is addressable, and access
  control belongs upstream in the MCP caller. DM channels are still refused
  on id-based calls — DMs flow only through the user-keyed DM ops (keyed by
  user_id, never a channel id, matching the DM API; Discord refuses bot DMs
  to strangers; `delete_dm` retracts and `edit_dm` edits only bot-authored
  messages, and `remove_dm_reaction` can only remove the bot's own
  reactions). DM and global tools follow the authenticated MCP operator
  surface's enabled-tool list. The one
  guild-confined surface in the system is the in-bot `!gpt` agent loop,
  which passes exactly `{ctx.guild.id}` to the registry.

### The bearer is the credential; `actor_id` is not authorization

The bearer token is full operator access to every tool `mcp_tools_enabled`
exposed at startup. There is one shared secret, not a per-user credential.

Every MCP tool also takes an `actor_id`. It is caller-supplied and not
credential-bound, and it grants nothing. When it resolves to a member of the
target guild, that member's channel visibility and private-thread access
apply to the call. Otherwise a bare id is used and those checks are skipped.
A caller can therefore pick any id, or an unknown one, to change which
visibility applies; the bearer already allows everything the exposed tools do.

The streamable-HTTP session id is a per-connection capability minted by the
MCP library. An unknown session id is rejected. This server does not use MCP
OAuth, so the library does not bind the session to a distinct user: any
request that already passed the shared bearer can use a live session.

That is acceptable when the operator treats the bearer as a host credential.
It is the same risk on loopback and on an explicit LAN or wildcard bind.
`serve()` logs it when the bind is not loopback. The Host/Origin allowlist
does not change it. Do not publish the port or front it with a public proxy.

## API Keys and Secrets

- **Discord token** resolves through a fixed chain (`core/bootstrap.py`, #83):
  the **`DISCORD_TOKEN` env var** first, then the **`discord_token` global
  config key**, then an **interactive `getpass` prompt** (no echo) when stdin is
  a TTY, and otherwise an immediate **nonzero exit** with per-platform
  instructions. Two properties carry the security weight:
  - **An env-supplied token is never persisted.** Copying a panel- or
    systemd-supplied secret into a file would surprise the operator (set in one
    place, living in two) and doubles the leak surface for a credential they
    rotate elsewhere.
  - **A prompted token is written only after discord.py confirms the login.**
    The verification boundary is `setup_hook`, which is reached only after the
    REST login succeeded. A `LoginFailure` writes nothing and exits 1 with a
    message naming the token's source; it never re-prompts. A typo'd token
    never reaches disk. (The prompt itself allows 3 tries for an *empty*
    entry.) `PrivilegedIntentsRequired` exits 1 with instructions for enabling
    the intents in the Developer Portal.

  Stored tokens live in `configs/global.json` as plaintext, protected by the
  store's 0600/0700 modes (below). **`.env` is deprecated** — still loaded when
  present so existing deployments keep working, no longer part of documented
  setup. `load_dotenv` only fills env vars that aren't already set, so a real
  `DISCORD_TOKEN` wins over a stale `.env` entry. `.env` and `.env.*` are
  gitignored.
- **Config store file permissions.** `configs/` is chmod 0700 at every startup
  and every config file lands 0600 — the temp file in the atomic tmp+rename is
  created 0600 (`os.open`) *and* `fchmod`'d on the open descriptor before any
  content is written, so a secret is never on disk under a looser mode. Both
  are required: O_CREAT's mode is umask-masked and ignored entirely when the
  temp file already exists, so a `.tmp` left by a previous crash would
  otherwise keep its old world-readable mode while the new secret is written
  into it. Best-effort: `OSError` is swallowed
  so a filesystem without POSIX modes (Windows, some volume mounts) still runs.
  This is the control that replaces a separate secrets file — there deliberately
  is none (owner decision, #83): provider keys already shared `global.json` as
  an accepted risk, and a second plaintext file is separation without a distinct
  threat model.
- **First-run superadmin.** On a first successful login with an empty/absent
  `superadmins` list, the Discord application owner (the *team owner* for
  team-owned apps, read from `Team.owner_id` — `application_info().owner` on a
  team app is a pseudo-user) is granted superadmin and the grant is logged
  plainly. The empty-list gate is the safety property: an established
  deployment's superadmin list is never rewritten, so this cannot be used to
  re-seat ownership. The gate is re-checked **after** the `application_info()`
  await, because cogs are loaded before `on_ready` and a `!claimsuper` racing
  that window must not be silently overwritten. `!claimsuper` remains the
  fallback when the application-info lookup fails.
- **Provider API keys** are entered through the `!aisettings` → Models &
  Providers key modal (superadmin-gated; a modal, never a slash parameter, so the
  key is never in a command log) and stored in `global.json` under
  `<PROVIDER>_API_KEY` — **plaintext on disk**. Environment variables of the same
  name are honored as a fallback. The store's 0700/0600 modes (above) are what
  protects them at rest.
- **The MCP bearer token** follows the same config-first-with-env-fallback
  pattern: the `mcp_ops_token` global config key, else `MCP_OPS_TOKEN`. If an operator enables the server with neither set, the bot
  **generates** one (`secrets.token_urlsafe(32)`) and persists it to
  `global.json`. Generating rather than refusing keeps the server fail-closed
  (it is never unauthenticated) without stranding an operator who has no UI for
  the secret. **The value is never logged** — only the fact that one was
  generated and which key holds it; read it out of `configs/global.json` to
  connect a client. It is plaintext on disk like the provider keys, and it is a
  full-privilege credential for the ops surface, protected at rest by the same
  0700/0600 modes as the Discord token.
- **`configs/` is gitignored in full**, so no per-guild data, admin
  lists, or stored keys are committed. Verified: `git ls-files configs/` is empty.
- **Keys never appear in-channel.** The key is typed into a Discord **modal**
  from the `!aisettings` → Models & Providers tab, which the panel answers
  ephemerally. It is deliberately not a slash-command parameter (a slash
  argument is visible while typing and lands in the interaction log), and the old
  prefix `!setapikey` — which posted the key into chat and then raced to delete
  it — has been removed.

## Prompt-Injection and Data Surfaces (`!gpt`)

- **Channel history is sent to a third-party LLM.** `!gpt` sends the last 15–30
  channel messages by default (the guild's history min/max) (plus referenced messages and their embeds/attachment URLs) and
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

## Known Accepted Risks (documented, not defects)

1. **The MCP bearer is full operator access** — `actor_id` is caller-supplied
   and only selects whose channel visibility applies. The HTTP boundary is the
   shared bearer plus the Host/Origin allowlist (see above).
2. **Superadmin tier is owner-level RCE-adjacent** — the global config editor,
   the `disabled_cogs` editor, restart, and bulk delete are intentionally
   available to superadmins. (Arbitrary cog load/reload and `git pull` were
   deleted in #86; deployment pulls happen in the shell that restarts the bot.)
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
- [x] **Restrict filesystem access to `configs/`** so plaintext API keys and admin
  lists aren't world-readable — `configs/` is chmod 0700 at every startup and
  every config file lands 0600; the atomic-save temp file is created 0600 and
  `fchmod`'d before content is written (#83). Best-effort on non-POSIX
  filesystems.
- [x] **Default the MCP server to loopback.** `MCP_OPS_HOST` is an explicit
  address or wildcard opt-in. DNS-rebinding Host/Origin checks stay on for
  that bind. Bearer auth stays mandatory. Do not publish the port or front it
  with a public proxy.
- [x] **Constrain media filenames** — add, lookup, post and delete validate
  literal basenames; glob/template syntax and path separators are rejected.
  Library enumeration excludes directories and symbolic links, and file actions
  recheck paths. Downloads and trims use a fresh staging directory before the
  finished file is moved into the guild library. Symlinked media roots and guild
  directories are rejected. These checks assume the host's library directories
  are not concurrently rewritten by an untrusted local process.
- [ ] **Audit media panel Add targets** — the `/media` panel's Add fetches an
  arbitrary URL server-side (bot-admin-gated); an admin could point it at
  internal/loopback or cloud metadata endpoints (SSRF). Consider an allowlist /
  private-IP block if the admin set is broad.
- [x] **No unauthenticated config-disclosure reads.** Provider, model and
  media configuration live inside the admin-gated `!aisettings` / `!media`
  panels, which answer ephemerally in their slash form. The public media
  inventory is available through the `list_media` op when exposed;
  `post_media` mirrors the public `!<name>` shortcut. Neither returns host
  filesystem paths.

### Invoker-only tools

`OpScope.SELF` binds ownership to the trusted invocation's `ctx.author`. A SELF op
may declare one optional `SELF_USER` parameter (wire `user_id`): omitted, it is the
invoker; naming someone else requires an admin invoker and a member of the current
guild. `Op.__call__` enforces this on both dispatch
paths before the op runs, so a model-chosen id cannot widen it.
Registration rejects identity/target selectors, Discord entity and snowflake
parameters, INTERNAL parameters and undeclared implementation arguments.
Both object and ID dispatch require a positive non-bot actor ID and reject
unexpected arguments. Payload parameters such as `text` remain model-fillable;
trusted session/revision tokens belong in runtime services, outside tool schemas.

SELF is excluded from MCP catalogs, schema-only builds and effective allowlists.
MCP wrapper creation and dispatch both refuse SELF before any actor/context
lookup, including stale wrappers and forged actor IDs. Bearer-to-user MCP
identity remains deferred; other scopes retain their existing MCP policy.

The shared agent policy requires a global whitelist entry and global
`ai_self_tools_enabled` (absent means true). Public guild SELF additionally
requires a non-off per-op gate (absent means off). All SELF switches, whitelist
entries and applicable gates are rechecked on each dispatch for every actor,
including superadmins.

SELF logs omit arguments, result bodies and error strings. They retain safe
operation, actor, surface, status and budget metadata. Provider exceptions from
a turn exposing SELF are replaced with a plain redacted error without the
original exception chain, since provider errors may contain tool exchanges.
The tool-name policy is included in history-window and retained-image keys;
cache separation supplements live authorization rather than replacing it.
