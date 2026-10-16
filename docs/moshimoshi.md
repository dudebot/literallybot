# !moshimoshi (proof of concept)

Push a message from Discord to the MCP clients currently connected to the
ops MCP server (`core/mcp_server.py`).

## Usage

- `!moshimoshi` lists live MCP sessions: clientInfo name and version, session
  id prefix, protocol version, last seen.
- `!moshimoshi <message>` pushes the message to every live session and
  reports each one as `sent`, `no-stream` or `modern-protocol`.

Works in a guild channel or in a DM to the bot. Callers who aren't allowed get
no reply.

## How delivery works

`core/mcp_clients.py` registers a `ServerMiddleware` on the `MCPServer`. Every
inbound MCP message records its session (id, clientInfo, protocol version,
last seen) and keeps the SDK `Connection`. Sessions idle for 30 minutes are
dropped, which matches the SDK session idle timeout.

A push is a `notifications/message` (MCP logging, `level: notice`,
`logger: moshimoshi`, `data: {from, message}`) sent on the session's
standalone GET stream:

- `sent`: handed to the stream. This is best-effort; the SDK drops it
  silently if the stream has just closed.
- `no-stream`: the client has no standalone stream open.
- `modern-protocol`: on a 2026-07-28 session the standalone stream may not
  carry `notifications/message`, so nothing is sent.

**Limits.** Delivery is not the same as waking an agent. Current Codex, Claude
Code and Grok Build clients log or ignore server notifications. None of them
starts a model turn because of one. MCP has no server-to-client "wake"
primitive: the 2026-07-28 revision removed server-initiated requests such as
sampling, and the Tasks extension is client-polled.

## Optional headless fallback

Set global `moshimoshi_spawn_fallback` to `"grok"` or `"codex"`. When no
session took the push, the cog runs that agent once, headless, in
`moshimoshi_workdir` (default `~/moshimoshi-workspace`) with a 600 s timeout,
and posts the reply. A reply over 2000 characters is attached as a file.
Only one run happens at a time.

- grok runs `--agent evelyn --always-approve --permission-mode bypassPermissions --max-turns 20`.
- codex runs `exec --dangerously-bypass-approvals-and-sandbox`.

Both have full shell access as the bot user.

## Config (configs/global.json)

| Key | Default | Meaning |
|---|---|---|
| `moshimoshi_enabled` | `true` | Master switch |
| `moshimoshi_allowed_users` | `[125839498150936576]` | Discord ids allowed; superadmin alone is not enough |
| `moshimoshi_spawn_fallback` | unset | `"grok"` / `"codex"` / unset |
| `moshimoshi_workdir` | `~/moshimoshi-workspace` | cwd for the fallback |

## Security

Turning on the fallback gives Discord-triggered shell access through an agent
that has bypassed approvals. The allowlist is the only gate, so keep it to
the owner's id. Anyone who controls that Discord account controls the box.
