# LiterallyBot

LiterallyBot is a versatile Discord bot built with discord.py, designed to be a "jack of all trades." It offers a wide array of features through its cog system, and server administrators can customize its behavior on a per-server basis via JSON configuration files (stored in `configs/`).

## Key Features

*   **Modular Cog System:** Easily enable, disable, or create new functionalities.
*   **Per-Server Configuration:** Customize bot behavior for each server.
*   **Extensive Command Set:** From moderation and utilities to entertainment and AI integrations.
*   **Easy to Extend:** Develop and integrate your own custom cogs.

## Installation

1. Clone the repository:
   ```sh
   git clone https://github.com/dudebot/literallybot.git
   cd literallybot
   ```

2. Install the dependencies:
   ```sh
   pip install -r requirements.txt
   ```

## Configuration

1. Create a `.env` file in the root directory of the project and add your Discord bot token:
   ```sh
   DISCORD_TOKEN=your_discord_token_here
   ```

2. (Optional) Configure additional global settings in `core/config.py` or per-server settings in the `configs/` directory (e.g., `configs/your_server_id.json`).

## GPT System Setup
To enable GPT-based commands, add these environment variables (examples) to your .env file:

```
OPENAI_API_KEY=[your OpenAI-compatible key]
OPENAI_MODEL=gpt-4o-mini
OPENAI_BASE_URL=https://api.x.ai/v1 (OPTIONAL)
```

## Danbooru System Setup
To enable Danbooru-based commands, add these environment variables (examples) to your .env file:

```
DANBOORU_API_KEY=[your Danbooru API key]
DANBOORU_LOGIN=[your Danbooru account name]
```

## Hue System Setup
To enable Hue-based commands, add these environment variables (examples) to your .env file:

```
press the button on the bridge
!sethuebridgeip [IP of your Hue Bridge]
```   

## Usage

1. Run the bot:
   ```sh
   python bot.py
   # Or use start_bot.bat (Windows) / start_bot.sh (Linux/macOS)
   ```

2. **Claim Superadmin:** Once the bot is running and has joined your server, use the `!claim_superadmin` command in any channel the bot can see. This will grant you the highest level of permissions for bot administration.

## Optional: Systemd Setup (Linux)

For running the bot as a service on Linux systems, you can use the provided `bot.service` file as a template. 
You can also use the `install_service.sh` script to help automate this process (review and edit the script to match your environment before running).

## Updating a Running Server
To pull the latest changes, call `!update` (which runs a git pull) and then `!reload` to reload all cogs. 
If you're running the bot using systemctl, you can use `!kys` to exit, letting systemctl restart the bot automatically.

## Administration

The bot includes a powerful administration system, primarily managed through the `Admin` cog (`cogs/static/admin.py`). After claiming superadmin privileges (`!claim_superadmin`), you can:

*   **Manage Cogs:** Load, unload, and reload cogs dynamically.
*   **Manage Permissions:** Control who can use which commands.
*   **Access Bot Internals:** View logs, bot status, and other diagnostic information.
*   **Update and Restart:** Pull the latest code changes from git and restart the bot.

Refer to the commands within the `Admin` cog (e.g., by using `!help Admin`) for a full list of administrative capabilities.

## Cog System Overview
The bot\'s commands and features are split into \"cogs\", each defined by a class in the cogs folder. 
You can easily expand functionality by creating additional cog files in the same directory, following the same structure and setup function.

### Cog Ontology
The cogs are organized into two main categories:

*   **Static Cogs (`cogs/static`):** These cogs are essential for the bot's fundamental operation, security, administration, and development lifecycle. They are expected to be always loaded and are critical for the bot's stability and manageability. Examples include `admin.py` (for bot administration) and `dev.py` (for owner-only development commands).
*   **Dynamic Cogs (`cogs/dynamic`):** These cogs provide specific user-facing features, integrations, or entertainment. They can be added, removed, or updated more frequently without destabilizing the core bot. They represent the primary way to extend the bot's functionality. Examples include `gpt.py` (for AI chat), `player.py` (for music playback), and `rng.py` (for dice rolls).

This distinction helps in managing the bot\'s architecture and allows for modular development.

For a detailed list and description of all available cogs, please see [COGS_LIST.md](COGS_LIST.md).

## Extending the Bot: Creating Your Own Cogs

You can easily extend LiterallyBot by creating your own cogs:

1.  **Create a Python File:** Add a new `.py` file in the `cogs/dynamic/` directory (e.g., `my_cog.py`).
2.  **Define Your Cog Class:** Create a class that inherits from `commands.Cog` from `discord.ext`.
    ```python
    from discord.ext import commands
    
    class MyCog(commands.Cog):
        def __init__(self, bot):
            self.bot = bot
     
        @commands.command()
        async def my_command(self, ctx):
            await ctx.send("Hello from MyCog!")
     
    async def setup(bot):
        await bot.add_cog(MyCog(bot))
    ```
3.  **Integrate Configuration (Optional):** If your cog requires configuration, you can leverage the bot's existing system (`core/config.py` and server-specific JSON files in `configs/`). Access configuration within your cog via `self.bot.config` or by passing relevant config sections during initialization.
4.  **Document Your Cog:** It's good practice to create a Markdown file (e.g., `docs/my_cog.md`) detailing your cog's commands and functionality. You can then link this in `COGS_LIST.md`.
5.  **Load Your Cog:** Use the `!load dynamic.my_cog_name` (filename without .py) command (if the Admin cog is loaded) or add it to the `initial_cogs` list in `bot.py` for automatic loading on startup.

## Commands

A general list of commands is provided below. For a more detailed and up-to-date list, use the `!help` command on your Discord server. For cog-specific commands, use `!help <CogName>`.

- `!greet`: Sends a greeting message.
- `!random <options>`: Picks a random item from a space-separated list.
- `!diceroll <sides>`: Rolls a dice with the specified number of sides.
- `!multidice <rolls> <sides>`: Rolls multiple dice with the specified number of sides.
- `!play <url>`: Plays a local mp3 file or an audio stream.
- `!stop`: Stops the audio stream.
- `!pause`: Pauses the audio stream.
- `!resume`: Resumes the audio stream.
- `!skip`: Skips the current audio stream.
- `!queue`: Shows the current audio stream queue.
- `!volume <volume>`: Sets the volume of the audio stream.
- `!loop`: Loops the current audio stream.
- `!shuffle`: Shuffles the current audio stream queue.
- `!quote`: Sends the quote of the day.
- `!setrole <+|-> <rolename>`: Adds or removes available roles.

## Contributing

Contributions are welcome! Please open an issue or submit a pull request for any changes or improvements.

## License

This project is licensed under the MIT License.
