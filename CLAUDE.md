# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

### Running the Bot
- `python bot.py` - Start the bot directly
- `./start_bot.sh` - Start bot using shell script (Linux/macOS)
- `start_bot.bat` - Start bot using batch file (Windows)

### Testing
- `pip install -r tests/requirements.txt` - Install test dependencies (pytest, pytest-cov)
- `pytest` - Run all tests
- `pytest tests/test_<specific>.py` - Run specific test file
- `pytest -v` - Run tests with verbose output
- `pytest --cov` - Run tests with coverage report

### VS Code Integration
VS Code launch configurations available:
- **"Run Bot"** - Start bot.py normally with integrated terminal
- **"Debug Bot"** - Start bot.py with full debugging support
- **"Run Tests"** - Execute all tests with pytest
- **"Debug Single Test"** - Debug currently open test file

VS Code tasks available (Ctrl+Shift+P → "Tasks: Run Task"):
- **"Run Bot"** - Quick bot startup
- **"Install Dependencies"** - Install main requirements.txt
- **"Run Tests"** - Execute test suite
- **"Install Test Dependencies"** - Install test requirements

### Dependencies
- `pip install -r requirements.txt` - Install main dependencies
- Dependencies include: discord.py, phue, python-dotenv, beautifulsoup4, openai, yt-dlp, requests

## Bot Architecture

### Core Components
- **bot.py**: Main bot entry point with event handlers, cog loading, and status cycling
- **core/config.py**: Configuration management system supporting per-guild JSON configs in `configs/` directory
- **utils.py**: Shared utility functions

### Cog System Architecture
The bot uses a modular cog system with two distinct categories:

#### Static Cogs (`cogs/static/`)
Essential cogs for core operation, always loaded:
- **admin.py**: Bot administration (load/unload cogs, permissions, updates, superadmin/admin system)
- **dev.py**: Development commands for bot owner (eval, debug, shell access)

#### Dynamic Cogs (`cogs/dynamic/`)
Feature cogs that can be loaded/unloaded:
- **gpt.py**: OpenAI integration with conversation history and message threading
- **player.py**: YouTube/Spotify music player with queue management
- **danbooru.py**: Image board integration
- **auto_response.py**: Configurable auto-responses to triggers
- **setrole.py**: Self-assignable role management
- **reminders.py**: User reminder system
- **media.py**: Pre-loaded sound effects player
- **rng.py**: Random number generation and chance commands
- **tools.py**: Utility commands (quotes, weather, definitions)
- **interrogative.py**: Yes/no question responses
- **memes.py**: Meme generation
- **logging.py**: Enhanced logging functionality
- **signal.py**: Signal messenger integration

### Configuration System
- **Global config**: `configs/global.json` for bot-wide settings
- **Per-guild config**: `configs/{guild_id}.json` for server-specific settings  
- **Per-user config**: `configs/user_{user_id}.json` for user-specific settings
- **Environment variables**: `.env` file for tokens and API keys
- **5-second write buffering**: Automatic batching of config writes to reduce filesystem I/O
- **Atomic writes**: Uses temporary files and rename for corruption-safe saves

#### Config API Usage
```python
# In cogs, access config via self.bot.config
config = self.bot.config

# Guild-specific (default scope)
config.get(ctx, key, default)
config.set(ctx, key, value)

# User-specific  
config.get_user(ctx, key, default)
config.set_user(ctx, key, value)
# OR: config.get(ctx, key, default, scope='user')

# Global
config.get_global(key, default)
config.set_global(key, value)
# OR: config.get(None, key, default, scope='global')

# Manual flush
config.flush()  # Force immediate save
```

**Note for cog development:** Always access config through `self.bot.config` within cog methods.

### Permission System
- **Superadmins**: Global bot owners (claim with `!claimsuper`, add others with `!addsuperadmin`)
- **Admins**: Per-guild administrators (claim with `!claimadmin` if Discord admin)
- **Moderators**: Server-specific moderation permissions
- Commands check permissions via config system

### Logging System
- **Rotating log files**: `logs/bot.log` (5MB max, 5 backups)
- **Console output**: Simultaneous logging to stdout
- **Event logging**: Commands, errors, admin actions tracked
- **Access logger**: `bot.logger` available in all cogs

## Environment Setup

Required environment variables in `.env`:
```
DISCORD_TOKEN=your_discord_token_here
OPENAI_API_KEY=your_openai_key (for GPT features)
OPENAI_MODEL=gpt-4o-mini (optional, for GPT features)
OPENAI_BASE_URL=https://api.x.ai/v1 (optional, for GPT features)
DANBOORU_API_KEY=your_danbooru_key (for Danbooru features)
DANBOORU_LOGIN=your_danbooru_username (for Danbooru features)
```

## Bot Management Commands

- `!claimsuper` - Claim superadmin (first use only)
- `!addsuperadmin @user` - Promote an additional superadmin
- `!claimadmin` - Claim guild admin (requires Discord admin)
- `!load <cog>` - Load cog (e.g., `!load dynamic.gpt`)
- `!unload <cog>` - Unload cog
- `!reload <cog>` - Reload cog
- `!reloadall` - Reload all cogs
- `!update` - Git pull latest changes
- `!kys` - Shutdown bot (useful with systemd auto-restart)

### Development
- `!eval <code>` - Execute Python code (owner only)
- `!shell <command>` - Execute shell commands (owner only)

## Cog Development

When creating new cogs:
- Place in `cogs/dynamic/` for features, `cogs/static/` for core functionality
- Follow existing patterns: inherit from `commands.Cog`, implement `__init__(self, bot)`
- Access config via `self.bot.config.get(ctx, key, default)`
- Access logger via `self.bot.logger` or `bot.logger`
- Include `async def setup(bot):` function for cog loading
- Use `@commands.command()` decorator for commands
- Load with `!load dynamic.cog_name` (filename without .py)

## Service Deployment

For Linux systemd deployment:
- Use provided `bot.service` template
- Run `install_service.sh` script (review and edit first)
- Bot supports `!kys` command for graceful shutdown with auto-restart
