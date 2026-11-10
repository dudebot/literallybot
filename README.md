# LiterallyBot

A modular Discord bot built with discord.py: AI chat that can act on your
server, moderation helpers, utilities and toys in one "jack of all trades".
It is meant to be read and hacked on: features are single-file cogs, settings
are JSON, and adding a command or an AI tool is a few lines.

- **AI chat with tools.** Mention the bot and it answers in character, sees
  images, and (if you allow it) performs Discord actions: rename someone,
  react, search history, post media. Works with xAI, OpenAI, Anthropic,
  OpenRouter's free models, or a local Ollama server.
- **You decide what the AI may do.** Every action is off until the bot's owner
  whitelists it and a server admin sets it to Admin or Everyone.
- **MCP server.** Drive the same actions from Claude Code or any MCP client.
- **The usual bot things.** Dice, reminders with snooze, auto-responses,
  per-server media clips, reaction roles, points, counting channels, a
  honeypot channel, bulk cleanup.
- **Cogs you can switch off** without deleting them, per deployment.

## Quick start

You need **Python 3.11 or newer**. `ffmpeg` is optional (trimming media clips).

1. Create the bot at
   [discord.com/developers/applications](https://discord.com/developers/applications):
   **New Application** → **Bot** → **Reset Token** and copy it.
2. On the same **Bot** page, enable all three **Privileged Gateway Intents**
   (Presence, Server Members, Message Content). The bot will not connect
   without them.
3. Start it:

   ```bash
   git clone https://github.com/dudebot/literallybot.git
   cd literallybot
   ./start.sh          # Windows: start.bat
   ```

   The script builds a virtualenv, installs dependencies and asks for the
   token (not echoed). The token is saved only after it logs in; if it is
   rejected, nothing is saved and you run the script again.
4. Invite the bot: **OAuth2 → URL Generator**, scopes `bot` and
   `applications.commands`, plus the permissions you want it to have. It can
   only do in Discord what its own role allows.
5. In your server, run `/aisettings` to add an AI provider key (or point it at
   Ollama or OpenRouter's free tier) and choose what the AI may do.

You are already the bot's superadmin: on first run it grants that to the
application owner.

## Hosting it somewhere else

Four paths, one precedence chain. The bot resolves its token as
**`DISCORD_TOKEN` env var → saved config → interactive prompt**, and if there's
no token *and* nowhere to prompt, it exits immediately with instructions rather
than hanging a console waiting for input that can't arrive.

| Where | What to do |
|-------|-----------|
| **Linux / macOS** | `./start.sh` and paste the token at the prompt. Saved to `configs/global.json` (mode 0600); later starts need nothing. |
| **Windows** | `start.bat` — same prompt, same one-time paste. Needs [Python 3](https://python.org) with "Add Python to PATH" checked at install. |
| **Game/hosting panel** (Pterodactyl, Pelican, most others) | Add a **`DISCORD_TOKEN` startup variable** in the panel's Startup tab, then run `./start.sh`. There's no terminal to prompt on a panel, so the env var *is* the setup — and the bot telling you exactly that when the variable is missing is the intended behavior, not an error to work around. |
| **Docker / systemd** | Pass the token as an environment variable: `docker run -e DISCORD_TOKEN=... `, or `Environment="DISCORD_TOKEN=..."` (or an `EnvironmentFile=` pointing at a 0600 file) in the unit. |

An env-supplied token is **never written to disk** — where you set it is where
it stays, so rotating it in your panel or unit file is the whole operation.

A `.env` file is still loaded if present (deprecated); a real environment
variable wins over it.


## Who can do what

The bot has its own three access levels. They are the bot's, not Discord's:

| Level | Who | Set by |
|-------|-----|--------|
| **Everyone** | Any member | |
| **Bot admin** | People in the server's `admins` list, plus anyone with Discord's Administrator permission and the server owner | `!claimadmin` (first Discord admin to run it), then `!addadmin @user` / `!removeadmin @user` |
| **Superadmin** | The bot's operator: the `superadmins` list in `configs/global.json` | Granted to the application owner on first run; `!addsuperadmin @user` / `!removesuperadmin @user` |

`!listadmins` shows both lists. Commands declare the level they need; `/help`
shows each person only the commands they can run. Admin panels also exist as
slash commands, hidden from members without Manage Messages.

## Commands

**Everyone**

- `/help` or `!help`: the commands you can run, grouped by feature
- **Chat with the AI**: mention the bot, reply to one of its messages, or `!gpt <question>` (servers only)
- `!dice 2d20`, `!random a b c`, `!order a b c`
- `!remindme 10 minutes Check the oven` (aliases `!r`, `!reminder`, `!setreminder`); reminders arrive by DM with snooze buttons
- `!should <question>` and friends (`!is`, `!are`, `!will`, …): a yes/no oracle
- `!points` (`!balance`, `!score`), `!leaderboard` (`!top`, `!rankings`)
- `!<name>`: post a clip from this server's media library
- `!danbooru <tags>` (`!db`): image search, forced to safe results outside NSFW channels
- `!ping`, `!info`
- Reaction roles: react to a configured message to toggle a role

**Bot admins** (each panel is `/name`, or `!name` if you cannot see the slash command)

| Command | What it does |
|---------|--------------|
| `/aisettings` | AI on/off, model, personality, history size, which actions the AI may take |
| `/autoresponse` | Trigger → response entries for this server |
| `/media` | This server's media library: add from a URL (YouTube or direct link, optional trim), delete, list |
| `/role add`, `/role delete`, `/role sync` | Reaction-role mappings |
| `/counter enable\|disable` | Make a channel a counting channel |
| `/honeypot` | A trap channel: posting there assigns a role and removes the poster's recent messages ([guide](docs/honeypot.md)) |
| `/logsettings` | Where errors and activity logs go ([guide](docs/error-handling.md)) |
| `!add_points`, `!set_points`, `!echo` | Adjust points; make the bot say something |

**Superadmin**

| Command | What it does |
|---------|--------------|
| `/aisettings` (extra tabs) | Providers, models, API keys, the AI action whitelist, the MCP server |
| `/cogs`, `!disable <cog>`, `!enable <cog>`, `!list_cogs` | Switch features off or on for this deployment (applies on restart) |
| `/config` | Edit global settings |
| `!restart` (`!kys`, `!shutdown`) | Exit cleanly; your service manager starts it again |
| `!sync` | Re-sync slash commands |
| `!cleanup …` | Bulk-delete messages (below) |
| `!backupchannel`, `!migratehere`, `!downloadbundleassets` | Export a channel and replay it elsewhere |

**Message cleanup** (superadmin, `!cleanup <subcommand>`) — scans text channels
the bot can read and moderate (threads and forum posts are not scanned):
- `!cleanup user <user_id>` — delete a user's messages
- `!cleanup gone` — delete messages by non-bot users who have left the server
- `!cleanup replies <user_id>` — delete replies to a user
- `!cleanup stale-replies` — delete replies to non-bot users who have left, plus replies whose referenced message Discord didn't resolve (already-deleted originals are skipped)
- `!cleanup full <user_id>` — `user` + `replies` for that user, plus `stale-replies`

## AI chat

Mention the bot, reply to it, or use `!gpt`. Chat works in servers only, has a
per-server off switch, and is rate limited per model price tier.

**Providers and keys.** `/aisettings` → Models & Providers. Keys are stored in
`configs/global.json` or read from environment variables named
`<PROVIDER>_API_KEY` (`XAI_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
`OPENROUTER_API_KEY`). Models can be added from the panel without code changes.

- **OpenRouter free tier**: only `:free` models are listed automatically. They
  cost nothing but share OpenRouter's free quota.
- **Local models (Ollama)**: a provider with
  `"base_url": "http://localhost:11434/v1"` and `"requires_api_key": false`.
  For a model that spends its whole budget thinking, set
  `"reasoning_effort": "none"` on it.

### Images

The AI can see images in the conversation:
- image **attachments** on the triggering message,
- images in the message being **replied to**,
- **link-preview / embed images** (embed image and thumbnail),
- **earlier images** in channel history, which remain visible on follow-up turns
  within the selected history and the per-request image budget.

Images are downloaded, normalized, and sent as bytes (never as signed URLs).
Normalized images are held in an **in-memory LRU cache** (64 images / 64 MB,
nothing written to disk), and history image selection favors keeping the same
images across turns to support provider prompt-cache reuse.

**Safety limits** (`core/ai_images.py`):
- **Discord-hosted images only** — `https` URLs on `cdn.discordapp.com`,
  `media.discordapp.net`, `images-ext-1.discordapp.net`, or `images-ext-2.discordapp.net`; anything else is ignored.
- **Max 8 images per request.**
- **Oversized images are rejected** — downloads over 8 MB, and images whose
  decoded dimensions exceed 40 megapixels or 12,000 px on a side, are dropped.
- **Pillow normalization** — every image is decoded and re-encoded as JPEG (PNG
  when it has transparency), downscaled to 2048 px max side; animated GIFs use
  their first frame; undecodable data is dropped.

Vision is on by default for every model; set `"vision": false` on a model's
entry in `ai_providers` to opt it out (it then gets text only).

### Letting the AI act

The AI can call **ops**: Discord actions such as sending a message, adding a
reaction, renaming a member, searching history, managing roles, plus whatever
your cogs add. Nothing is enabled by default.

1. The **superadmin** whitelists ops in `/aisettings` → In-chat ops. Each one
   costs prompt tokens on every message, so enable what you will use.
2. Each **server's bot admins** set every whitelisted op to **Off**, **Admin**
   or **Everyone** on the Server tab.

The bot acts with its own Discord permissions on behalf of whoever asked, only
inside the current server, and only on channels that person can see. If you
set "rename members" to Everyone, every member can rename anyone the bot can.
That is allowed; for ops that can grant access or do lasting damage, the panel
asks you to confirm first. Details: [architecture](docs/architecture.md),
[security](docs/security.md).

**Measuring it.** Every AI turn appends one line to
`logs/agent_tools/YYYY-MM-DD.jsonl`: which tools were called in what order,
whether they worked, how many model round trips the turn took, tokens and
estimated cost. Never arguments or results.
`venv/bin/python scripts/tool_usage.py --days 7` summarizes it per model and
per tool, so you can tell whether a prompt or tool change made turns cheaper.
For cost on a model the built-in price table does not know, set
`cost_per_mtok_input` and `cost_per_mtok_output` on the model.

## MCP server

The same ops, served to an external agent over
[MCP](https://modelcontextprotocol.io/). Off by default. As superadmin, turn it
on in `/aisettings` → MCP ops and restart. It listens on `127.0.0.1:8765`
(`mcp_ops_port` / `MCP_OPS_PORT` to change) and requires a bearer token; if
you did not set `mcp_ops_token` / `MCP_OPS_TOKEN`, one is generated into
`configs/global.json`.

**Connect to it** (e.g. from an MCP-capable client config — substitute your own
port if you set `mcp_ops_port`):
```json
{
  "mcpServers": {
    "literallybot-ops": {
      "url": "http://127.0.0.1:8765/mcp",
      "headers": {
        "Authorization": "Bearer <your mcp_ops_token value>"
      }
    }
  }
}
```

The token is full operator access to every server the bot is in. Which ops are
served is the `mcp_tools_enabled` list (the MCP ops tab); with no list, all of them
are. The `actor_id` argument is not authentication: it only selects whose
channel visibility applies. Keep the port on loopback unless you have read
[security](docs/security.md).

## Writing your own cog

Drop a file in `cogs/` and restart:

```python
# cogs/my_feature.py
from discord.ext import commands
from core.permissions import is_admin


class MyFeature(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def greet(self, ctx):
        """Anyone can run this."""
        greeting = self.bot.config.get(ctx, "greeting", "hello")
        await ctx.send(greeting)

    @commands.command(hidden=True)
    @commands.guild_only()
    @commands.check(is_admin)          # bot admins only; is_superadmin for the operator
    async def setgreeting(self, ctx, *, text: str):
        self.bot.config.set(ctx, "greeting", text)
        await ctx.send("Saved.")


async def setup(bot):
    await bot.add_cog(MyFeature(bot))
```

That is the whole pattern: one decorator line sets who may run a command, and
`self.bot.config` reads and writes per-server settings. The
[cog guide](docs/cog-development.md) has the full template, including slash
commands, admin panels and giving the AI a new tool with `@op`.

## Documentation

- [Cog development](docs/cog-development.md): template, commands, panels, ops
- [Decorator reference](docs/decorators.md)
- [Architecture](docs/architecture.md): ops, frontends, who can use what
- [Config system](docs/config-system.md): API and every config key
- [Security](docs/security.md): access levels, the agent path, MCP
- [Error handling and logging](docs/error-handling.md), [event logging](docs/event-logging.md)
- [AI history and prompt caching](docs/ai-caching.md)
- [Honeypot](docs/honeypot.md), [test policy](docs/testing.md), [decision records](docs/decision-records.md)
- `AGENTS.md`: working rules for coding agents and contributors

## Running it as a service

On Linux, `sudo ./scripts/install_service.sh [service_name]` installs a systemd
unit that runs `start.sh`, or start from
`scripts/literallybot.service.example`.

## Contributing

Issues and pull requests are welcome.

## License

[MIT](LICENSE).
