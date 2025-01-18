"""
This example bot is structured in multiple files and is made with the goal of showcasing commands, events and cogs.
Although this example is not intended as a complete bot, but as a reference aimed to give you a basic understanding for 
creating your bot, feel free to use these examples and point out any issue.
+ These examples are made with educational purpose and there are plenty of docstrings and explanation about most of the code.
+ This example is made with Python 3.8.5 and Discord.py 1.4.0a (rewrite).
Documentation:
+	Discord.py latest:	https://discordpy.readthedocs.io/en/latest/
+	Migration to rewrite:	https://discordpy.readthedocs.io/en/latest/migrating.html
+	Commands documentation:		https://discordpy.readthedocs.io/en/latest/ext/commands/commands.html
+	Cogs documentation:		https://discordpy.readthedocs.io/en/latest/ext/commands/cogs.html
+	Tasks documentation:	https://discordpy.readthedocs.io/en/latest/ext/tasks/index.html
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

	return ['!', '?', '>']


bot = commands.Bot(command_prefix=get_prefix, description='A simple example of bot made with Discord.py', intents=discord.Intents.all())

# Function to load all cogs in the './cogs' directory
async def load_cogs():
    for filename in listdir('./cogs'):
        if filename.endswith('.py'):
            cog_name = f'cogs.{filename[:-3]}'
            try:
                await bot.load_extension(cog_name)
                print(f'Successfully loaded {cog_name}')
            except Exception as e:
                print(f'Failed to load {cog_name}: {e}')

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

	print(f'{bot.user.name} is online and ready!')
	#Prints a message with the bot name.

	change_status.start()
	#Starts the task `change_status`_.


statuslist = cycle([
		"0101010",
		"1010101",
	])


@tasks.loop(seconds=16)
async def change_status():
	"""This is a background task that loops every 16 seconds.
	The coroutine looped with this task will change status over time.
	The statuses used are in the cycle list called `statuslist`_.
	
	Documentation:
		https://discordpy.readthedocs.io/en/latest/ext/tasks/index.html
	"""
	await bot.change_presence(activity=discord.Game(next(statuslist)))


# Command to dynamically reload cogs
@bot.command(name='reload', hidden=True)
@commands.is_owner()
async def reload_cog(ctx, *, cog: str):
    """This command reloads the selected cog, as long as that cog is in the `./cogs` folder.
    
    Args:
        cog (str): The name of the cog to reload.
    Note:
        This command can be used only by the bot owner.
        This command is hidden from the help menu.
        This command deletes its messages after 20 seconds.
    """
    message = await ctx.send('Reloading...')
    await ctx.message.delete()
    try:
        bot.reload_extension(f'cogs.{cog}')
    except Exception as exc:
        await message.edit(content=f'An error has occurred: {exc}', delete_after=20)
    else:
        await message.edit(content=f'{cog} has been reloaded.', delete_after=20)


#Grab token from the token.txt file
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

bot.run(TOKEN) #Runs the bot with its token. Don't put code below this command.
