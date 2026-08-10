# LiterallyBot

A modular Discord bot built with discord.py that's designed to be a "jack of all trades" — from AI chat to utilities and entertainment. Built with developer experience in mind: hot-reloadable cogs, a three-scope JSON config system, and a shared ops registry that lets both an in-bot agent loop and an external MCP client drive the same permission-checked Discord actions. Actively developed.

## 🚀 Quick Start

**Copy-paste these commands** (replace `your_bot_token_here` with your actual Discord bot token):

```bash
git clone https://github.com/dudebot/literallybot.git
cd literallybot
echo "DISCORD_TOKEN=your_bot_token_here" > .env
./start_bot.sh
```

The start script will:
- Create a virtual environment automatically
- Install all dependencies
- Start the bot

**First-time setup:**
1. Invite the bot to your Discord server
2. Run `!claimsuper` in any channel to become a superadmin

That's it! Your bot is now running with all core features available.

## ✨ Key Features

- **🧩 Modular Cog System** — load/unload/hot-reload features without restarts; disable cogs globally without deleting them
- **🤖 AI Integration** — provider-agnostic chat (xAI, OpenAI, Anthropic, local Ollama) with memory, personality, and an optional agentic mode that performs real Discord actions
- **⚙️ Smart Configuration** — per-server, per-user, and global JSON settings with write buffering, atomic saves, and live reload on external edits
- **🎲 Utilities & Fun** — dice rolling, random choices, reminders with snooze buttons, auto-responses, per-guild media libraries, reaction roles
- **🛠️ Developer Friendly** — hot-reload cogs, comprehensive logging, error routing to Discord channels

## 🧭 Command Surface Philosophy

The public slash picker is kept deliberately tiny: `/help` and `/role` are the only slash commands. Everything administrative is a **prefix-launched panel** (`!aisettings`, `!autoresponse`, `!media`, `!cogs`, `!config`) — single-invoker interactive Views gated by the bot's own admin concept (`is_admin` / `is_superadmin`), independent of the invoker's Discord permissions. Regular members never see admin machinery in the slash picker; `/help` and `!help` show each invoker exactly the commands they can use.

## 📋 Everyday Commands

- `/help` or `!help` — one embed of the bot's commands, grouped by cog, filtered to what you can actually run
- **Chat with the AI** — @mention the bot or reply to one of its messages (no command needed; guild channels only)
- `!dice <NdX>` — roll dice, e.g. `!dice 2d20`
- `!random <choices>` / `!order <choices>` — pick or shuffle
- `!remindme <duration> <message>` — e.g. `!remindme 10 minutes Check the oven`, `!remindme 1d12h Ship it` (aliases: `!r`, `!reminder`); deliveries come with snooze buttons scaled to the original duration
- `!should <question>` — yes/no oracle; answers most interrogatives (`!is`, `!are`, `!will`, `!shall`, …)
- `!ping` / `!info` — latency and bot info
- `!<name>` — post any file from this server's media library (see Media Libraries below)
- **Reaction roles** — react to a configured message to toggle a role; admins manage mappings with `/role add`, `/role delete`, `/role sync`

## 🤖 AI Chat

The AI layer lives in `core/llm/` (provider-agnostic client built on
[pydantic-ai](https://ai.pydantic.dev/)) and supports multiple providers behind
one interface: xAI (Grok), OpenAI, Anthropic (Claude), and any local
OpenAI-compatible server such as [Ollama](https://ollama.com/).

**Talking to it:** @mention the bot or reply to its messages. Chat is
guild-only, has a per-guild kill switch, and is rate-limited by a
nested-window cooldown ladder — all tunable from the panel.

**Configuring it:** `!aisettings` (admin-only, hidden from `/help` for
non-admins) opens the AI settings panel — pick provider/model, set API keys,
edit the personality, manage per-tool agentic allowlists, tune cooldowns,
and (superadmin) add/remove providers and models. Superadmin-only controls
are omitted from the panel entirely for non-superadmins.

**API keys** live in global config (`configs/global.json`) or environment
variables — set them from the panel, or directly:
```json
"XAI_API_KEY": "xai-XXXX",
"OPENAI_API_KEY": "sk-XXXX",
"ANTHROPIC_API_KEY": "sk-ant-XXXX"
```
Provider/model lists live under `ai_providers` in global config and are
managed at runtime through the panel — no code change to add a model.

**Local models (Ollama):** point a provider at a local server with
`"base_url": "http://localhost:11434/v1"` and `"requires_api_key": false` in its
`ai_providers` entry. Reasoning models that would otherwise spend their whole
token budget "thinking" can be tamed with `"reasoning_effort": "none"` per model.

### Agentic AI (experimental)

When enabled, the AI can **perform real Discord actions** — send/edit
messages, add reactions, search history, manage roles — instead of only
describing them. It runs a tool-calling loop over a shared **ops registry**
(`core/ops.py`), acting as the **invoking user** (never the bot), confined to
the current guild, with mentions suppressed and a per-run tool-call cap.

Agentic mode is per-tool: each guild has a tool allowlist (default empty, so
chat stays plain chat and nothing changes) managed from the `!aisettings`
panel. See `docs/security.md` for the full model.

## 🎬 Media Libraries

Each guild gets its own media library under `media/<guild_id>/` (runtime
data, not in git) — libraries never bleed across guilds. Any member posts a
clip with `!<name>`; admins manage the library through the `!media` panel —
add from a URL (YouTube or direct file link, with optional trim), delete,
and browse the listing with file sizes.

## 🛡️ Administration

**Admin hierarchy** (the bot's own concept, separate from Discord permissions):
- `!claimsuper` — become a bot superadmin (first time only)
- `!addsuperadmin @user` / `!removesuperadmin @user` — manage superadmins
- `!claimadmin` / `!addadmin @user` / `!removeadmin @user` — manage per-server bot admins
- `!listadmins` — show this server's bot admins and the global superadmins

**Panels** (single-invoker interactive Views):
- `!aisettings` — AI provider/model/keys/personality/agentic tools/cooldowns/MCP (admin; superadmin tabs hidden from admins)
- `!autoresponse` — per-guild trigger→response entries, weighted responses, automod-style deletion (admin)
- `!media` — this guild's media library (admin)
- `!cogs` — enable/disable/reload cogs bot-wide (superadmin)
- `!config` — global-config editor (superadmin)

**Cog & code management** (superadmin):
- `!load <cog>` / `!unload <cog>` / `!reload <cog>` — manage features live
- `!disable <cog>` / `!enable <cog>` — global disabled_cogs switch: carry a cog on disk without running it
- `!update` — pull latest changes from git
- `!sync` — sync slash commands
- `!restart` (alias `!kys`) — graceful shutdown (systemd restarts it)

### Error Logging (optional)
- `!errorlog setchannel #channel` — set the error channel for this guild
- `!errorlog setglobal #channel` — set the global error channel (superadmin)
- `!errorlog setcategory` / `!errorlog setseverity` — category/severity routing
- `!errorlog status` / `!errorlog disable` / `!errorlog ratelimit` — inspect, disable, tune

Errors are rate-limited globally to avoid spam. See `docs/error-handling.md`.

## 🔧 Optional Integrations

### Image Search (Danbooru)
`!danbooru <tags>` (alias `!db`). Keys go in global config or `.env`:
```bash
DANBOORU_API_KEY=your_danbooru_key
DANBOORU_LOGIN=your_danbooru_username
```

### Smart Lights (Philips Hue)
Press the button on your Hue Bridge, then run:
```
!sethuebridgeip [IP of your Hue Bridge]
```

## 🔌 MCP Ops Server

`mcp_ops/` exposes the bot's ops registry (`core/ops.py` — permission-checked,
typed Discord actions like `send_message`, `search_history`, `send_dm`,
`create_role`) over [MCP](https://modelcontextprotocol.io/) so an external
agent can drive the bot the same way an in-bot command would, without either
frontend re-implementing Discord plumbing or permission logic.

**This is OFF by default.** Two ways to run it, sharing the same guardrails:

1. **In-process with the bot** — `bot.py` starts it automatically after
   ready when the `mcp_ops_enabled` global config bool is true (toggle in
   `!aisettings` → MCP tab; binds on restart). Tools act through the live
   bot. When the flag is unset/false, running the normal bot never starts it.
2. **Standalone** — `python3 -m mcp_ops.run_mcp_server` runs a separate
   process with a minimal cog-less Discord client on the same token.

**Security model (all gates fail closed):**
- **Off by default** — refuses to start unless the `mcp_ops_enabled` global
  config boolean is true.
- **Auth required** — refuses to start unless `MCP_OPS_TOKEN` is set to a
  non-empty shared secret. Every request must send
  `Authorization: Bearer <token>`; requests without a matching token get a
  `401`.
- **Loopback only** — binds to `127.0.0.1`, no host override. Every tool
  call is a live, authenticated Discord bot action; do not tunnel this port
  off-host casually.
- `send_message` always sends with `allowed_mentions` = none (no pings);
  `search_history` clamps `limit` to 200.
- **Full guild reach by design** — tools act as raw primitives: every guild
  the bot account is in is addressable. DMs flow through the dedicated
  `send_dm`/`read_dms`/`fetch_dms` ops, never through id-based channel calls.
  Access control belongs upstream in the MCP caller; the only guild-confined
  surface is the in-bot agent loop (pinned to its invoking guild).
- **Accepted risk:** `actor_id` is caller-supplied and not bound to the
  bearer token, so any token-holder can act as any user id for permission
  purposes. Fine for localhost self-use; add real actor auth before any
  wider exposure.

**Run it (standalone):**
```bash
# enable the server in global config (or via !aisettings -> MCP tab):
python3 -c "import json;p='configs/global.json';d=json.load(open(p));d['mcp_ops_enabled']=True;json.dump(d,open(p,'w'),indent=4)"
# in your .env or exported in the shell:
export MCP_OPS_TOKEN=$(openssl rand -hex 32)   # generate a real secret
export DISCORD_TOKEN=your_bot_token_here        # same token the bot uses

python3 -m mcp_ops.run_mcp_server
# -> serves streamable-HTTP MCP at http://127.0.0.1:8765/mcp  (port: MCP_OPS_PORT)
```

**Connect to it** (e.g. from an MCP-capable client config):
```json
{
  "mcpServers": {
    "literallybot-ops": {
      "url": "http://127.0.0.1:8765/mcp",
      "headers": {
        "Authorization": "Bearer <your MCP_OPS_TOKEN value>"
      }
    }
  }
}
```

**Exposed tools:** by default the full ops registry — messaging
(`send_message`, `edit_message`, `delete_message`, `add_reaction`,
`remove_reaction`, `pin_message`, `create_thread`, `search_history`), DMs
(`send_dm`, `read_dms`, `fetch_dms`), roles (`add_role`, `remove_role`,
`create_role`, `edit_role`, `delete_role`), and introspection (`list_guilds`,
`list_channels`, `list_members`, `list_roles`, `list_role_members`,
`list_channel_overwrites`). The served subset is trimmable at runtime via the
`mcp_tools_enabled` global config list, managed from the `!aisettings` → MCP
tab — what the panel shows is what gets served.

Exact per-tool schemas are served live via MCP `tools/list`; offline, run
`python3 -m core.ops` to print the full ops registry.

`actor_id` is the Discord user id the call is made on behalf of — the
registry runs the same permission check it would for an in-bot command,
against live bot config via `core.utils.is_admin`/`is_superadmin`.

## 🏗️ Development & Extension

### Creating Custom Cogs
Add new features by creating cogs in `cogs/optional/`:

```python
# cogs/optional/my_feature.py
from discord.ext import commands

class MyFeature(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def my_command(self, ctx):
        # Access config system
        setting = self.bot.config.get(ctx, "my_setting", "default")
        await ctx.send(f"Setting: {setting}")

async def setup(bot):
    await bot.add_cog(MyFeature(bot))
```

Load with `!load my_feature` — no restart needed! It shows up in `/help`
automatically, grouped under its cog.

### Config Quick Reference
LiterallyBot's config helper is available as `self.bot.config` in every cog:

```python
# Per-guild (default scope)
prefix = self.bot.config.get(ctx, "prefix", "!")
self.bot.config.set(ctx, "prefix", "?")

# Per-user
timezone = self.bot.config.get_user(ctx, "timezone", "UTC")
self.bot.config.set_user(ctx, "timezone", "UTC")

# Global (bot-wide)
superadmins = self.bot.config.get_global("superadmins", [])
self.bot.config.set_global("maintenance_mode", True)
```

Lists are just Python lists — get, mutate, then `set` the updated list. Call
`self.bot.config.flush()` before shutdown if you need to force writes
immediately.

### Documentation
- `docs/cog-development.md` — building cogs end-to-end
- `docs/config-system.md` — config API, patterns, and the config Key Registry
- `docs/error-handling.md` — how errors flow and how to handle them
- `docs/security.md` — permission model and the agentic/ops execution path
- `docs/agent-automation.md` — role-gated DM automation pattern for scheduled agents
- `docs/decision-records.md` — durable architecture decision records

### Production Deployment
For Linux servers, run `./install_service.sh` from the repo root (interactive
systemd installer), or start from the unit template in
`scripts/literallybot.service.example`.

## Contributing

Contributions are welcome! Please open an issue or submit a pull request for any changes or improvements.

## License

This project is released under the [MIT License](LICENSE).
