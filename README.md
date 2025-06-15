# LiterallyBot

A modular Discord bot built with discord.py that's designed to be a "jack of all trades" - from music playback and AI chat to utilities and entertainment. Built with developer experience in mind, featuring a flexible cog system and comprehensive configuration management.

## 🚀 Quick Start

1. **Clone and install:**
   ```bash
   git clone https://github.com/dudebot/literallybot.git
   cd literallybot
   pip install -r requirements.txt
   ```

2. **Set up your bot token:**
   ```bash
   # Create .env file
   echo "DISCORD_TOKEN=your_discord_token_here" > .env
   ```

3. **Run the bot:**
   ```bash
   python bot.py
   ```

4. **Claim admin permissions:**
   - Invite bot to your Discord server
   - Run `!claimsuper` in any channel to become superadmin

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
Add to your `.env` file:
```bash
OPENAI_API_KEY=your_openai_key
OPENAI_MODEL=gpt-4o-mini  # optional
OPENAI_BASE_URL=https://api.x.ai/v1  # optional, for alternative providers
```

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

See [COGS_LIST.md](COGS_LIST.md) for complete command documentation.

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

### Documentation
- **[docs/cog-development.md](docs/cog-development.md)** - Complete cog development guide
- **[docs/config-system.md](docs/config-system.md)** - Configuration system guide
- **[COGS_LIST.md](COGS_LIST.md)** - Complete feature documentation  
- **[CLAUDE.md](CLAUDE.md)** - Development setup guide

### Administrative Commands
- `!claimsuper` - Become bot superadmin (first time only)
- `!load <cog>` / `!unload <cog>` - Manage features
- `!reload <cog>` - Hot-reload code changes
- `!update` - Pull latest changes from git
- `!kys` - Graceful shutdown (useful with systemd)

### Production Deployment
For Linux servers, use the provided `bot.service` systemd template and `install_service.sh` script.

## Contributing

Contributions are welcome! Please open an issue or submit a pull request for any changes or improvements.

## License

This project is licensed under the MIT License.
