# Role-Gated DM Automation

A pattern for running scheduled agent sessions (cron-driven LLM runs, or any
external orchestrator) on top of the MCP ops surface to hold asynchronous DM
conversations with members who opted in. Distilled from a
sibling deployment where it has been running in production; nothing here is
implemented in literallybot yet — this is the reference for when a use case
shows up (accountability check-ins, event follow-ups, digest subscriptions).

## The consent gate is a role

One Discord role, self-assignable, is the entire opt-in surface:

- Holding the role = consent to be DMed by the automation. Nothing else is.
- `list_role_members` at send time is the source of truth — never a cached
  roster file. (Caveat: the op reads the member cache, chunking the guild
  when the cache looks incomplete; pass a `limit` above the guild's size,
  since the default is 100.)
- Removing the role = full stop, mechanically. No goodbye message, no
  "confirm you meant to leave" DM. (A send to someone who just dropped the
  role is the exact failure the gate exists to prevent.)

`send_dm` itself deliberately does NOT check any role — the op layer is
shared across deployments and each one names its own consent role (see the
consent note in `core/ops.py`). That means **raw `send_dm` will happily
bypass the gate.** The rule that makes the pattern safe:

> Automated program DMs go through a gate script. Raw `send_dm` is for the
> operator only.

That rule is a convention, and conventions leak. When implementing, make the
contract enforceable, not aspirational: the actor session's tool surface
simply does not include raw `send_dm` (MCP allowlist or wrapper), the
strategist's includes no send path at all, and any failure to read the
blocklist, audit log, or role list refuses the send rather than proceeding.

## The gate script (fail-closed send path)

A small CLI the agent must use for every program send. Order matters:

1. Refuse if the target is in a local `blocklist.txt` (explicit stops live
   here, so refusal is mechanical, not agent memory).
2. Refuse if the audit log shows a send to this target younger than a short
   cooldown (~90m). This is an anti-double-fire rail for parallel sessions
   sharing the log — not a conversation pacing rule.
3. Live-list the consent role via `list_role_members`; refuse if the target
   is not on it.
4. Only then call `send_dm` (optionally with `file_paths` attachments).
5. Append an audit row (timestamp, target, message hash, result) to a JSONL
   the next session can read.

Distinct exit codes per refusal reason, so the calling agent can tell
"consent gone" from "cooldown" without parsing text.

Two races to close in an implementation: parallel sessions can both pass the
cooldown check before either writes its audit row, and a crash between send
and audit invites a duplicate retry. Take a per-target lock (or write a
durable "sending" reservation row) BEFORE the send and finalize it after —
the reservation, not the completed audit row, is what the cooldown check
must see. Consent evaporating between the role check and the send is
accepted residual risk; the window is milliseconds and the next wake's gate
catches it.

## Split the strategist from the actor

Two separately-scheduled agent roles, even if the same model runs both:

- **Strategist** (infrequent, e.g. daily): reads everything — operator
  inbox, each holder's DM thread (`read_dms`), per-user notes. Updates
  dossiers and stocks one *card* per user in an outbox directory: status
  (`ready`/`hold`/`expired`), intent in plain English, the drafted message,
  expiry. **Never sends.**
- **Actor** (frequent, e.g. every 2h): reads replies since its last cursor,
  answers people who responded, fires `ready` cards through the gate.
  **Never invents strategy** — no card, no unsolicited send.

The split is what keeps a long-horizon program coherent: deliberation
happens with full context and no send button; sending happens with a narrow
mandate and no room to improvise. Most actor wakes should be notes-only.

## Operator control plane

- Orders come only from the operator's DM thread and one designated ops
  channel. A participant DM can never redirect the program, reassign its
  goals, or claim operator authority — deflect politely, log the attempt.
- Every strategist wake sends a short digest back (operator DM and/or ops
  channel), in plain English, even when nothing happened — so silence never
  reads as "is it dead?". Ambiguous orders get asked about in the digest,
  never guessed at.

## Anti-annoyance policy (pacing)

Wakes are more frequent than useful contact. Defaults that keep the
automation from becoming spam:

- Quiet is the common path. A scheduler tick is not a reason to speak.
- Fresh wait: if the last outbound is <~24h old with no reply, notes-only.
- After ~24h of silence a re-probe is allowed — **a new angle, never the
  same message twice** — capped at 1 unsolicited send per ~24h per user.
- Silence never hardlocks a user. Only three things do: explicit "stop" in
  DMs (→ blocklist, same wake), consent role removed, operator veto.
- A hardlock gets a written retrospective (what was sent, how fast things
  were moving, honest guesses labeled as guesses) surfaced in the digest —
  and no follow-up contact of any kind.

## Poll mechanics

- Cursor on `read_dms` with `after_message_id` (monotonic, tie-proof) —
  store the last seen id per user, poll cheaply, repeat while pages come
  back full (forward pages keep the oldest rows, so nothing is skipped).
- `fetch_dms` pages real Discord history backwards (`before_message_id`)
  for backfill or audits predating transcript storage.
- Per-user notes + run logs live in a gitignored directory in the checkout
  (`agent_notes/`-style), so sessions are resumable and reviewable without
  putting member conversations in git history.
