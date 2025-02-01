from discord.ext import commands
import discord
from sys import version_info as sysv
from os import listdir
from config import Config
import subprocess

class Dev(commands.Cog):
	"""This is a cog with owner-only commands.
	Note:
		All cogs inherits from `commands.Cog`_.
		All cogs are classes, so they need self as first argument in their methods.
		All cogs use different decorators for commands and events (see example below).
		All cogs needs a setup function (see below).
    Documentation:
        https://discordpy.readthedocs.io/en/latest/ext/commands/cogs.html
	"""
	def __init__(self, bot):
		self.bot = bot


	@commands.Cog.listener()
	#This is the decorator for events (inside of cogs).
	async def on_ready(self):
		print(f'Python {sysv.major}.{sysv.minor}.{sysv.micro} - Disord.py {discord.__version__}\n')
		#Prints on the shell the version of Python and Discord.py installed in our computer.


	@commands.command(name='reload', hidden=True)#This command is hidden from the help menu.
	#This is the decorator for commands (inside of cogs).
	@commands.is_owner()
	#Only the owner (or owners) can use the commands decorated with this.
	async def reload(self, ctx, cog=None):
		"""This commands reloads all the cogs in the `./cogs` folder.
		
		Note:
			This command can be used only from the bot owner.
			This command is hidden from the help menu.
			This command deletes its messages after 20 seconds."""
   
		if cog is not None:
			cogs = [cog+".py"]
		else:
			cogs = listdir('./cogs/dynamic')

		message = await ctx.send('Reloading...')
		await ctx.message.delete()
		try:
			for cog in cogs:
				if cog.endswith('.py') == True:
					config = Config(ctx.guild.id)
					if cog[:-3] in config.config["cogs"]:
						self.bot.reload_extension(f'cogs.dynamic.{cog[:-3]}')
			ctx.send('All cogs have been reloaded.', delete_after=20)
		except Exception as exc:
			await message.edit(content=f'An error has occurred: {exc}', delete_after=20)
		else:
			await message.edit(content='All cogs have been reloaded.', delete_after=20)

	def check_cog(self, cog):
		"""Returns the name of the cog in the correct format.
		Args:
			self
			cog (str): The cogname to check
		
		Returns:
			cog if cog starts with `cogs.`, otherwise an fstring with this format`cogs.{cog}`_.
		Note:
			All cognames are made lowercase with `.lower()`_.
		"""
		if (cog.lower()).startswith('cogs.dynamic.') == True:
			return cog.lower()
		return f'cogs.dynamic.{cog.lower()}'

	@commands.command(name='load', hidden=True)
	@commands.is_owner()
	async def load(self, ctx, *, cog: str):
		"""This commands loads the selected cog, as long as that cog is in the `./cogs` folder.
				
		Args:
			cog (str): The name of the cog to load. The name is checked with `.check_cog(cog)`_.
		
		Note:
			This command can be used only from the bot owner.
			This command is hidden from the help menu.
			This command deletes its messages after 20 seconds.
		"""
		message = await ctx.send('Loading...')
		await ctx.message.delete()
		try:
			await self.bot.load_extension(self.check_cog(cog))
		except Exception as exc:
			await message.edit(content=f'An error has occurred: {exc}', delete_after=20)
		else:
			await message.edit(content=f'{self.check_cog(cog)} has been loaded.', delete_after=20)


	@commands.command(name='unload', hidden=True)
	@commands.is_owner()
	async def unload(self, ctx, *, cog: str):
		"""This commands unloads the selected cog, as long as that cog is in the `./cogs` folder.
		
		Args:
			cog (str): The name of the cog to unload. The name is checked with `.check_cog(cog)`_.
		Note:
			This command can be used only from the bot owner.
			This command is hidden from the help menu.
			This command deletes its messages after 20 seconds.
		"""
		message = await ctx.send('Unloading...')
		await ctx.message.delete()
		try:
			await self.bot.unload_extension(self.check_cog(cog))
		except Exception as exc:
			await message.edit(content=f'An error has occurred: {exc}', delete_after=20)
		else:
			await message.edit(content=f'{self.check_cog(cog)} has been unloaded.', delete_after=20)

	@commands.command(name='setbotoperator', hidden=True)
	@commands.has_permissions(administrator=True)
	async def set_bot_operator(self, ctx, user: discord.Member):
		"""This command sets the specified user as a bot operator if the command invoker has administrator permissions.
		
		Args:
			user (discord.Member): The user to set as a bot operator.
		Note:
			This command can be used only by server administrators.
			This command is hidden from the help menu.
		"""
		config = Config(ctx.guild.id)
		config.add_bot_operator(user.id)
		await ctx.send(f'{user.mention} has been set as a bot operator.')

	@commands.Cog.listener()
	async def on_ready(self):
		"""Python 3.x.x - Disord.py x.x.x"""
		pass

	@commands.command()
	async def load_cog(self, ctx, *, cog: str):
		await ctx.send('Loading...')
		await ctx.message.delete()
		self.bot.load_extension(f'cogs.{cog}')

	@commands.command()
	async def unload_cog(self, ctx, *, cog: str):
		await ctx.send('Unloading...')
		await ctx.message.delete()
		self.bot.unload_extension(f'cogs.{cog}')

	@commands.command()
	async def reload_cog(self, ctx, *, cog: str):
		await ctx.send('Reloading...')
		await ctx.message.delete()
		self.bot.reload_extension(f'cogs.{cog}')

	@commands.command()
	async def reload_all(self, ctx):
		await ctx.send('Reloading...')
		await ctx.message.delete()
		# ...logic to reload all cogs...

	@commands.command()
	async def set_bot_operator(self, ctx, *, user: discord.Member):
		# ...logic to set bot operator...
		await ctx.send(f'{user.mention} has been set as a bot operator.')

	@commands.command(name='update', hidden=True)
	@commands.is_owner()
	async def update(self, ctx):
		"""This command executes a git pull command in the current environment to update the code.
		
		Note:
			This command can be used only from the bot owner.
			This command is hidden from the help menu.
		"""
		message = await ctx.send('Updating code...')
		await ctx.message.delete()
		try:
			result = subprocess.run(['git', 'pull'], capture_output=True, text=True)
			if result.returncode == 0:
				await message.edit(content=f'Code updated successfully:\n{result.stdout}', delete_after=20)
			else:
				await message.edit(content=f'Error updating code:\n{result.stderr}', delete_after=20)
		except Exception as exc:
			await message.edit(content=f'An error has occurred: {exc}', delete_after=20)

async def setup(bot):
	"""Every cog needs a setup function like this."""
	await bot.add_cog(Dev(bot))
