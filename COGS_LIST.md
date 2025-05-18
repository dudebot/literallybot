\
# LiterallyBot Cogs

This document provides an overview of the cogs available in LiterallyBot. Cogs are modules that contain commands and listeners, allowing for a modular and extensible bot.

## Cog Categories

Cogs are organized into two main categories:

*   **Static Cogs (`cogs/static`):** These are essential for the bot's core operation, security, administration, and development. They are generally always loaded.
*   **Dynamic Cogs (`cogs/dynamic`):** These provide user-facing features, integrations, or entertainment. They can be loaded or unloaded as needed.

## Static Cogs

### 1. Admin (`cogs/static/admin.py`)
*   **Description:** Provides essential administrative commands for managing the bot, including loading/unloading cogs, managing permissions, updating the bot, and viewing diagnostic information.
*   **Key Commands:** `!load`, `!unload`, `!reload`, `!reloadall`, `!pull`, `!update`, `!kys`, `!config`, `!claimsuper`, `!claimadmin`, `!addadmin`, `!deladmin`, `!addmod`, `!delmod`, `!deletemsg`.

### 2. Dev (`cogs/static/dev.py`)
*   **Description:** Contains commands useful for development and debugging, typically restricted to the bot owner.
*   **Key Commands:** `!eval`, `!debug`, `!testexception`, `!ip`, `!shell`.

### 3. REPL (`cogs/static/repl.py`)
*   **Description:** Implements a Read-Eval-Print Loop (REPL) for interacting with the bot's environment directly through Discord.
*   **Key Commands:** `!repl`.

## Dynamic Cogs

### 1. Auto Response (`cogs/dynamic/auto_response.py`)
*   **Description:** Allows configuring automatic responses to specific trigger phrases or words.
*   **Key Commands:** `!addresponse`, `!delresponse`, `!listresponses`.

### 2. Danbooru (`cogs/dynamic/danbooru.py`)
*   **Description:** Integrates with Danbooru (an image board) to fetch and display images based on tags. Requires API key setup.
*   **Key Commands:** `!danbooru`, `!db`.

### 3. GPT (`cogs/dynamic/gpt.py`)
*   **Description:** Provides access to GPT-based language models for chat, completions, and other AI-powered interactions. Requires API key setup.
*   **Key Commands:** `!gpt`, `!ask`, `!instruct`, `!setpersonality`, `!setbotnickname`.

### 4. Interrogative (`cogs/dynamic/interrogative.py`)
*   **Description:** Handles interrogative sentences and provides yes or no answers. Responds to various forms like `!should`, `!is`, `!are`, `!was`, `!will`, `!can`, `!do`, `!did`, `!has`, `!had`, `!may`, `!might`, `!would`, `!could`, etc.
*   **Key Commands:** `!should` (and its many aliases like `!is`, `!are`, `!was`, etc. The bot typically responds to these when used at the start of a question).

### 5. Logging (`cogs/dynamic/logging.py`)
*   **Description:** Manages logging of bot events, errors, and command usage to files and/or Discord channels.
*   **Key Commands:** (Primarily backend, may have commands to view/manage logs)

### 6. Media (`cogs/dynamic/media.py`)
*   **Description:** Commands for playing short, pre-loaded sound effects or media clips. Also allows adding new media.
*   **Key Commands:** `!addmedia`, `!playeffect <effect_name>` (or similar, depends on specific registered effects like `!dingdingdoo`, `!poggers`).

### 7. Memes (`cogs/dynamic/memes.py`)
*   **Description:** Generates or fetches memes.
*   **Key Commands:** `!meme`, `!quoteme`.

### 8. Player (`cogs/dynamic/player.py`)
*   **Description:** A music player cog that allows users to play audio from YouTube, Spotify (via YouTube search), and direct URLs in voice channels.
*   **Key Commands:** `!play`, `!pause`, `!resume`, `!stop`, `!skip`, `!queue`, `!nowplaying`, `!volume`, `!loop`, `!shuffle`, `!seek`, `!forceskip`.

### 9. Reminders (`cogs/dynamic/reminders.py`)
*   **Description:** Allows users to set reminders for themselves. The bot will notify them at the specified time.
*   **Key Commands:** `!remindme`, `!reminders`.

### 10. RNG (`cogs/dynamic/rng.py`)
*   **Description:** Provides various random number generation and chance-based commands.
*   **Key Commands:** `!random`, `!roll`, `!dice`, `!coinflip`, `!choose`, `!order`.

### 11. SetRole (`cogs/dynamic/setrole.py`)
*   **Description:** Allows users to assign or remove pre-approved roles from themselves. Server admins can configure which roles are self-assignable.
*   **Key Commands:** `!setrole`, `!removerole`, `!roles`, `!whitelistrole`.

### 12. Signal (`cogs/dynamic/signal.py`)
*   **Description:** Integration with Signal messenger (details would depend on the specific implementation, potentially for notifications or cross-platform communication).
*   **Key Commands:** (Depends on implementation)

### 13. Tools (`cogs/dynamic/tools.py`)
*   **Description:** A collection of utility commands, such as fetching quotes, weather, dictionary definitions, etc.
*   **Key Commands:** `!quote`, `!weather`, `!define`, `!urban`, `!echo`, `!info`, `!ping`.

---

*This list is based on the file structure provided. For the most accurate and detailed command information, please use the `!help` command within Discord or check the source code of each cog.*
