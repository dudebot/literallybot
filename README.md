# LiterallyBot

A modular Discord bot built with discord.py that's designed to be a "jack of all trades" - from music playback and AI chat to utilities and entertainment. Built with developer experience in mind, featuring a flexible cog system and comprehensive configuration management.

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
1. Invite bot to your Discord server
2. Run `!claimsuper` in any channel to become a superadmin

That's it! Your bot is now running with all core features available.

## ✨ Key Features

- **🧩 Modular Cog System** - Load/unload features dynamically without restarts
- **🎵 Music Player** - YouTube, Spotify support with queue management  
- **🤖 AI Integration** - GPT chat with memory and personality system
- **⚙️ Smart Configuration** - Per-server, per-user, and global settings with automatic persistence and modification through cogs
- **🎲 Utilities & Fun** - Dice rolling, random choices, reminders, auto-responses
- **🛠️ Developer Friendly** - Hot-reload cogs, built-in REPL, comprehensive logging

## 🔧 Optional Integrations

### AI Chat (GPT)
Store AI credentials in `configs/global.json`. Example keys:
```json
"XAI_API_KEY": "xai-XXXX",
"OPENAI_API_KEY": "sk-XXXX"
```
Once the keys are present, use the GPT commands to manage behaviour:
- `!aiinfo` – list available providers/models and confirm keys are detected
- `!setprovider <provider>` – switch between configured providers (e.g., `xai`, `openai`, `anthropic`)
- `!setmodel <model>` – choose a model for the current provider
- `!setpersonality` – update the bot’s response personality

### Image Search (Danbooru)
Add to your `.env` file:
```bash
DANBOORU_API_KEY=your_danbooru_key
DANBOORU_LOGIN=your_danbooru_username
```

### Smart Lights (Philips Hue)
Press the button on your Hue Bridge, then run:
```
!sethuebridgeip [IP of your Hue Bridge]
```

## 📋 Available Commands

**Core Features:**
- `!help` - Show all commands
- `!load <cog>` / `!unload <cog>` - Manage bot features
- `!update` - Pull latest code changes

**Music Player:**
- `!play <song>` - Play from YouTube/Spotify
- `!queue` - Show music queue
- `!skip` / `!pause` / `!resume`

**AI & Utilities:**
- `!gpt <message>` - Chat with AI (if configured)
- `!roll <dice>` - Roll dice
- `!remind <time> <message>` - Set reminders
- `!setrole <role>` - Self-assign roles

## 🏗️ Development & Extension

### Creating Custom Cogs
Add new features by creating cogs in `cogs/dynamic/`:

```python
# cogs/dynamic/my_feature.py
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

Load with `!load my_feature` - no restart needed!

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

Lists are just Python lists—get, mutate, then `set` the updated list. Call `self.bot.config.flush()` before shutdown if you need to force writes immediately.

For deeper walkthroughs, see:
- `docs/cog-development.md` – building cogs end-to-end
- `docs/config-system.md` – advanced config usage and patterns

### Administrative Commands
- `!claimsuper` - Become a bot superadmin (first time only)
- `!addsuperadmin @user` - Promote an additional bot superadmin
- `!load <cog>` / `!unload <cog>` - Manage features
- `!reload <cog>` - Hot-reload code changes
- `!update` - Pull latest changes from git
- `!kys` - Graceful shutdown (useful with systemd)

### Error Logging (optional)
- `!errorlog setchannel #channel` — Set error logging channel for this guild
- `!errorlog setglobal #channel` — Set global error channel (superadmin only)
- `!errorlog status` — View current error logging configuration
- `!errorlog disable` — Disable error logging

Notes:
- Supports per-guild and global error logging with category/severity routing
- Errors are rate-limited globally to avoid spam (configurable by superadmin)

### MCP Ops Server

`mcp_ops/` exposes the bot's ops registry (`core/ops.py` — permission-checked,
typed Discord actions like `send_message`, `search_history`, `add_reaction`)
over [MCP](https://modelcontextprotocol.io/) so an external agent
can drive the bot the same way an in-bot command would, without either
frontend re-implementing Discord plumbing or permission logic.

**This is OFF by default.** Two ways to run it, sharing the same guardrails:

1. **In-process with the bot** — `bot.py` starts it automatically after
   ready when `MCP_OPS_ENABLED=1` (tools then act through the live bot).
   When the env gates are unset, running the normal bot never starts it.
2. **Standalone** — `python3 -m mcp_ops.run_mcp_server` runs a separate
   process with a minimal cog-less Discord client on the same token.

**Security model (all gates fail closed):**
- **Off by default** — refuses to start unless `MCP_OPS_ENABLED=1` is set.
- **Auth required** — refuses to start unless `MCP_OPS_TOKEN` is set to a
  non-empty shared secret. Every request must send
  `Authorization: Bearer <token>`; requests without a matching token get a
  `401`.
- **Guild allowlist required** — refuses to start unless
  `MCP_OPS_GUILD_ALLOWLIST` (comma-separated guild ids) is set. Every tool
  call verifies its resolved channel belongs to an allowlisted guild; DMs
  and channels in other guilds are refused.
- **Loopback only** — binds to `127.0.0.1`, no host override. Every tool
  call is a live, authenticated Discord bot action; do not tunnel this port
  off-host casually.
- `send_message` always sends with `allowed_mentions` = none (no pings);
  `search_history` clamps `limit` to 200.
- **Accepted risk:** `actor_id` is caller-supplied and not bound to the
  bearer token, so any token-holder can act as any user id for permission
  purposes. Fine for localhost self-use; add real actor auth before any
  wider exposure.

**Run it (standalone):**
```bash
# in your .env or exported in the shell:
export MCP_OPS_ENABLED=1
export MCP_OPS_TOKEN=$(openssl rand -hex 32)   # generate a real secret
export MCP_OPS_GUILD_ALLOWLIST=your_guild_id
export DISCORD_TOKEN=your_bot_token_here        # same token the bot uses

python3 -m mcp_ops.run_mcp_server
# -> serves streamable-HTTP MCP at http://127.0.0.1:8765/mcp
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

**Exposed tools:**
| Tool | Permission | Args |
|------|-----------|------|
| `send_message` | EVERYONE | `channel_id`, `content`, `actor_id` |
| `search_history` | EVERYONE | `channel_id`, `actor_id`, `limit?`, `author_id?`, `contains?` |
| `add_reaction` | EVERYONE | `channel_id`, `message_id`, `emoji`, `actor_id` |

`actor_id` is the Discord user id the call is made on behalf of — the
registry runs the same permission check it would for an in-bot command,
against live bot config via `core.utils.is_admin`/`is_superadmin`.

### Production Deployment
For Linux servers, use the provided service template in `scripts/` and the `install_service.sh` script.

## Contributing

Contributions are welcome! Please open an issue or submit a pull request for any changes or improvements.

## License

This project is licensed under the MIT License.
