# LiterallyBot

LiterallyBot is a Discord bot built with discord.py. This bot includes various commands and functionalities such as greeting users, rolling dice, playing music, and more.

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

2. (Optional) Configure additional settings in the `config.py` file.

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
HUE_BRIDGE_IP=[your Hue bridge IP]
```   

## Usage

1. Run the bot:
   ```sh
   python bot.py
   ```

2. Set superadmin permissions with !claim_superadmin

## Systemd Setup

Here's an example usage of how to install the bot on systemd. Replace [your install folder], [your user], and [your group] with your actual setup:

```
[Unit]
Description=Literally a Discord Bot
Wants=network-online.target
After=network.target

[Service]
User=[your user]
Group=[your group]
WorkingDirectory=[your install folder]/literallybot/
ExecStart=/usr/bin/python3 [your install folder]/literallybot/bot.py
ExecStop=pkill -9 -f bot.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

## Updating a Running Server
To pull the latest changes, call `!update` (which runs a git pull) and then `!reload` to reload all cogs. 
If you're running the bot using systemctl, you can use `!kys` to exit, letting systemctl restart the bot automatically.

## Cog System Overview
The bot's commands and features are split into "cogs", each defined by a class in the cogs folder. 
Each cog self-contains related commands and logic, keeping the code well-organized. 
You can easily expand functionality by creating additional cog files in the same directory, following the same structure and setup function.

## Commands

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
