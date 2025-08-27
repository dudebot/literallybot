"""
This example bot is structured in multiple files and is made with the goal of showcasing commands, events and cogs.
Although this example is not intended as a complete bot, but as a reference aimed to give you a basic understanding for 
creating your bot, feel free to use these examples and point out any issue.
+ These examples are made with educational purpose and there are plenty of docstrings and explanation about most of the code.
+ This example is made with Python 3.8.5 and Discord.py 1.4.0a (rewrite).
Documentation:
+    Discord.py latest:    https://discordpy.readthedocs.io/en/latest/
+    Migration to rewrite:    https://discordpy.readthedocs.io/en/latest/migrating.html
+    Commands documentation:        https://discordpy.readthedocs.io/en/latest/ext/commands/commands.html
+    Cogs documentation:        https://discordpy.readthedocs.io/en/latest/ext/commands/cogs.html
+    Tasks documentation:    https://discordpy.readthedocs.io/en/latest/ext/tasks/index.html
The example files are organized in this directory structure:
...
    /discord
        -bot.py
        /cogs
            -dev.py
            -tools.py
            -quote.py
"""
from itertools import cycle
from discord.ext import commands, tasks
import discord
from os import listdir
from dotenv import load_dotenv
import os
from core.config import Config
# Logging setup
import logging
from logging.handlers import RotatingFileHandler
# Ensure logs directory exists
os.makedirs('logs', exist_ok=True)
# Configure logging
logging.basicConfig(level=logging.INFO,
    format='%(asctime)s %(levelname)s:%(name)s: %(message)s',
    handlers=[
        RotatingFileHandler('logs/bot.log', maxBytes=5*1024*1024, backupCount=5),
        logging.StreamHandler()
    ])
logger = logging.getLogger(__name__)

def get_prefix(bot, message):
    """This function returns a Prefix for our bot's commands.
    
    Args:
        bot (commands.Bot): The bot that is invoking this function.
        message (discord.Message): The message that is invoking.
        
    Returns:
        string or iterable conteining strings: A string containing prefix or an iterable containing prefixes
    Notes:
        Through a database (or even a json) this function can be modified to returns per server prefixes.
        This function should returns only strings or iterable containing strings.
        This function shouldn't returns numeric values (int, float, complex).
        Empty strings as the prefix always matches, and should be avoided, at least in guilds. 
    """
    if not isinstance(message.guild, discord.Guild):
        """Checks if the bot isn't inside of a guild. 
        Returns a prefix string if true, otherwise passes.
        """
        return '!'

    return ['!']

bot = commands.Bot(command_prefix=get_prefix, intents=discord.Intents.all())
# Attach central logger to bot for use in cogs
bot.logger = logger
bot.config = Config()

# Function to load all cogs from ./cogs/{static,dynamic,vibes}
async def load_cogs():
    for group in ("static", "dynamic", "vibes"):
        dir_path = f"./cogs/{group}"
        if not os.path.isdir(dir_path):
            logger.debug(f"Cog directory missing, skipping: {dir_path}")
            continue

        for filename in listdir(dir_path):
            # Skip non-python & dunder/hidden modules like __init__.py
            if not filename.endswith('.py') or filename.startswith('_'):
                continue

            cog_name = f"cogs.{group}.{filename[:-3]}"
            try:
                await bot.load_extension(cog_name)
                logger.info(f"Successfully loaded {cog_name}")
            except Exception as e:
                logger.error(f"Failed to load {cog_name}: {e}", exc_info=True)

@bot.event
#This is the decorator for events (outside of cogs).
async def on_ready():
    """This coroutine is called when the bot is connected to Discord.
    Note:
        `on_ready` doesn't take any arguments.
    
    Documentation:
    https://discordpy.readthedocs.io/en/latest/api.html#discord.on_ready
    """

    await load_cogs()
    
    logger.info(f'{bot.user.name} is online and ready!')
    #Prints a message with the bot name.

    if not change_status.is_running():
        change_status.start()
    #Starts the task `change_status`_.

    await bot.tree.sync()
    # Sync application commands with Discord

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    if isinstance(message.channel, discord.DMChannel):
        logger.info(f'Received DM from {message.author} (ID: {message.author.id}): {message.content}')
    await bot.process_commands(message)

#todo this doesn't show the actual error type nor line numbers. also i dont think we log slash commands
#exc_info=True also seems to spam "NoneType: None" somehow
@bot.event
async def on_command_error(ctx, error):
    logger.error(f'Error in command {ctx.command}: {error}', exc_info=True)

@bot.event
async def on_command(ctx):
    logger.info(f'Command {ctx.command} invoked by {ctx.author} (ID: {ctx.author.id}) args={ctx.args} kwargs={ctx.kwargs}')

@bot.event
async def on_command_completion(ctx):
    logger.info(f'Command {ctx.command} completed by {ctx.author} in {ctx.channel}')

@bot.event
async def on_error(event, *args, **kwargs):
    logger.exception(f'Unhandled exception in event {event}', exc_info=True)

statuslist = cycle([
        "01010101",
        "01110111",
        "01010101",
        "01111110"
    ])

@tasks.loop(seconds=200)
async def change_status():
    """This is a background task that loops every 16 seconds.
    The coroutine looped with this task will change status over time.
    The statuses used are in the cycle list called `statuslist`_.
    
    Documentation:
        https://discordpy.readthedocs.io/en/latest/ext/tasks/index.html
    """
    await bot.change_presence(activity=discord.Game(next(statuslist)))


if __name__ == "__main__":
    #Grab token from the token.txt file
    load_dotenv()
    TOKEN = os.getenv('DISCORD_TOKEN')

    try:
        bot.run(TOKEN)
    except Exception:
        logger.critical('Bot terminated unexpectedly', exc_info=True)
    finally:
        # Properly shutdown config system
        bot.config.shutdown()
        logger.info('Config system shutdown complete')
    #Runs the bot with its token. Don't put code below this command.
