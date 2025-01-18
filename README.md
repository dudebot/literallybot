# LiterallyBot

LiterallyBot is a Discord bot built with discord.py. This bot includes various commands and functionalities such as greeting users, rolling dice, playing music, and more.

## Installation

1. Clone the repository:
   ```sh
   git clone https://github.com/dudebot/literallybot.git
   cd literallybot
   ```

2. Set up a virtual environment:
   ```sh
   python -m venv venv
   source venv/bin/activate  # On Windows use `venv\Scripts\activate`
   ```

3. Install the dependencies:
   ```sh
   python -m pip install --upgrade pip
   pip install setuptools wheel
   pip install -r requirements.txt
   ```

## Configuration

1. Create a `.env` file in the root directory of the project and add your Discord bot token:
   ```sh
   DISCORD_TOKEN=your_discord_token_here
   ```

2. (Optional) Configure additional settings in the `config.py` file.

## Usage

1. Run the bot:
   ```sh
   python bot.py
   ```

2. The bot will be online and ready to use. You can interact with it using the defined commands.

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
