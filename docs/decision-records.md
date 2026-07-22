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
- The MCP ops server is wired to the real registry with fail-closed guild
  allowlist, mention suppression, and history clamps (`057674d`, `96bccf3`)
  but remains env-gated OFF (`MCP_OPS_ENABLED`).
- Codex authz finding #4 (caller-supplied `actor_id` not credential-bound)
  was consciously downgraded to a documented accepted risk for loopback
  self-use (`mcp_ops/server.py`). Reopen it only if the server is ever
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
  `mcp_tools_enabled` allowlist consumed by `mcp_ops/server.py` at build
  time (restart-gated). Both are edited via the `/ai settings` panel
  (`cogs/dynamic/ai_admin.py`), which also runs the one-shot flag→allowlist
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
tool calls. The redesign (`cogs/dynamic/gpt.py`):

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
