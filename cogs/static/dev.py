from discord.ext import commands
import discord
from sys import version_info as sysv
from os import listdir
import subprocess
from datetime import datetime
from utils import smart_split
import sys

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
        self.logger = bot.logger

    @commands.Cog.listener()
    #This is the decorator for events (inside of cogs).
    async def on_ready(self):
        self.logger.info(f'Python {sysv.major}.{sysv.minor}.{sysv.micro} - Discord.py {discord.__version__}')

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
        cog_lower = cog.lower()
        # If already properly formatted, return as-is
        if cog_lower.startswith('cogs.'):
            return cog_lower
        # Check if this is a vibe cog
        if cog_lower.startswith('vibes.') or cog_lower.startswith('vibe_'):
            return f'cogs.vibes.{cog_lower.replace("vibes.", "")}'
        # Default to dynamic cogs
        return f'cogs.dynamic.{cog_lower}'

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
        self.logger.info(f"{ctx.author} (ID: {ctx.author.id}) invoked load on {cog}")
        message = await ctx.send('Loading...')
        await ctx.message.delete()
        try:
            await self.bot.load_extension(self.check_cog(cog))
        except Exception as exc:
            self.logger.error(f"Error loading {cog} by {ctx.author}", exc_info=True)
            await message.edit(content=f'An error has occurred: {exc}', delete_after=20)
        else:
            self.logger.info(f"Loaded {cog} successfully by {ctx.author}")
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
		
        self.logger.info(f"{ctx.author} (ID: {ctx.author.id}) invoked unload on {cog}")
        message = await ctx.send('Unloading...')
        await ctx.message.delete()
        try:
            await self.bot.unload_extension(self.check_cog(cog))
        except Exception as exc:
            self.logger.error(f"Error unloading {cog} by {ctx.author}", exc_info=True)
            await message.edit(content=f'An error has occurred: {exc}', delete_after=20)
        else:
            self.logger.info(f"Unloaded {cog} successfully by {ctx.author}")
            await message.edit(content=f'{self.check_cog(cog)} has been unloaded.', delete_after=20)
            
    @commands.command(name='reload', hidden=True)#This command is hidden from the help menu.
    @commands.is_owner()
    async def reload(self, ctx, cog=None):
        """This commands reloads a specific cog or all cogs in the `./cogs/dynamic` folder.
        
        Note:
            This command can be used only from the bot owner.
            This command is hidden from the help menu.
            This command deletes its messages after 20 seconds."""

        self.logger.info(f"{ctx.author} (ID: {ctx.author.id}) invoked reload on {cog or 'all dynamic'}")
        await ctx.message.delete()
        
        if cog is None:
            cogs_to_unload = [c for c in self.bot.extensions if c.startswith("cogs.dynamic.")]
            cogs_to_load = [f'cogs.dynamic.{filename[:-3]}' for filename in listdir('./cogs/dynamic') if filename.endswith('.py')]
        else:
            cogs_to_unload = [self.check_cog(cog)]
            cogs_to_load = [self.check_cog(cog)]

        errors = []
        message = await ctx.send(f'Reloading...')
        for cog in cogs_to_unload:
            if cog not in self.bot.extensions:
                continue
            try:
                await self.bot.unload_extension(cog)
            except Exception as exc:
                self.logger.error(f"Error unloading {cog} during reload by {ctx.author}", exc_info=True)
                errors.append(f'Error unloading {cog}: {exc}')
        
        for cog in cogs_to_load:
            try:
                await self.bot.load_extension(cog)
            except Exception as exc:
                self.logger.error(f"Error loading {cog} during reload by {ctx.author}", exc_info=True)
                errors.append(f'Error loading {cog}: {exc}')
        
        if errors:
            formatted_errors = '\n'.join([f"- {error}" for error in errors])
            response = f'Errors occurred:\n{formatted_errors}'
        else:
            formatted_cogs = '\n'.join([f"- {cog}" for cog in cogs_to_load])
            response = f'All cogs reloaded successfully:\n{formatted_cogs}'

        await message.edit(content=response, delete_after=20)
        
    @commands.command(name='update', hidden=True)
    @commands.is_owner()
    async def update(self, ctx):
        """This command executes a git pull command in the current environment to update the code.
        
        Note:
            This command can be used only from the bot owner.
            This command is hidden from the help menu.
        """
        self.logger.info(f"{ctx.author} invoked update command")
        message = await ctx.send('Attempting to update code via git pull...')
        try:
            # Delete the command message if possible, but don't fail if it's already gone or permissions are an issue
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                self.logger.warning("Could not delete update command message, it might have been already deleted or permissions are missing.")

            # Execute git pull
            result = subprocess.run(['git', 'pull'], capture_output=True, text=True, check=False)

            stdout_output = result.stdout.strip() if result.stdout else ""
            stderr_output = result.stderr.strip() if result.stderr else ""

            if result.returncode == 0:
                self.logger.info(f"Git pull successful. Output: {stdout_output if stdout_output else 'No output.'}")
                
                commit_hash = "N/A"
                human_time = "N/A"
                try:
                    commit_info_result = subprocess.run(
                        ['git', 'log', '-1', '--format="%H %ct"'], 
                        capture_output=True, text=True, check=False
                    )
                    if commit_info_result.returncode == 0 and commit_info_result.stdout:
                        parsed_commit_hash, commit_timestamp_str = commit_info_result.stdout.replace("\"", "").strip().split()
                        commit_timestamp = int(commit_timestamp_str)
                        commit_hash = parsed_commit_hash
                        human_time = datetime.fromtimestamp(commit_timestamp).strftime("%Y-%m-%d %H:%M")
                    else:
                        self.logger.warning(f"Failed to get commit info after successful pull. Git log stderr: {commit_info_result.stderr.strip() if commit_info_result.stderr else 'None'}")
                except Exception as e_commit:
                    self.logger.warning(f"Error processing commit info after successful pull: {e_commit}")

                response_content = (
                    f'Code update pull completed successfully!\n'
                    f'Current Commit Hash: {commit_hash}\n'
                    f'Commit Timestamp: {human_time}\n\n'
                )
                if stdout_output:
                    response_content += f'Git Pull Output:\n```\n{stdout_output}\n```'
                else:
                    response_content += 'No specific output from git pull.'
                
                await message.edit(content=response_content, delete_after=60)
            
            else: # result.returncode != 0, git pull encountered issues
                log_message_parts = [f"Git pull command finished with return code {result.returncode}."]
                if stdout_output: log_message_parts.append(f"Stdout: {stdout_output}")
                if stderr_output: log_message_parts.append(f"Stderr: {stderr_output}")
                full_log_message = "\n".join(log_message_parts)

                user_message_content = f"Git pull finished with return code {result.returncode}.\n"
                if stdout_output:
                    user_message_content += f"Output:\n```\n{stdout_output}\n```\n"
                if stderr_output:
                    user_message_content += f"Errors:\n```\n{stderr_output}\n```\n"

                if "Permission denied" in stderr_output or "unable to unlink" in stderr_output or "failed to unlink" in stderr_output:
                    self.logger.warning(f"Git pull encountered permission issues. {full_log_message}")
                    user_message_content += ("\n**Some files may not have been updated due to permission issues** (e.g., unable to delete old files). "
                                             "The bot continues to run. You might need to resolve permissions manually. "
                                             "Consider reloading cogs if applicable after resolving.")
                else:
                    self.logger.error(f"Git pull failed. {full_log_message}")
                    user_message_content += ("\n**The code update may have failed or is incomplete.** "
                                             "The bot continues to run. Check the output above and bot logs for details.")
                
                await message.edit(content=user_message_content, delete_after=180) # Keep message much longer for review

        except Exception as exc:
            self.logger.error("Exception during update command execution", exc_info=True)
            try:
                await message.edit(content=f'An unexpected error occurred during the update command: {exc}\nThe bot continues to run.', delete_after=60)
            except discord.HTTPException: # If message itself is gone
                self.logger.error(f"Failed to send update error to Discord, message gone. Error: {exc}")

    @commands.command(name='list_cogs', hidden=True)
    @commands.is_owner()
    async def list_cogs(self, ctx):
        """This command lists all the cogs in the `cogs/dynamic` directory.
        
        Note:
            This command can be used only from the bot owner.
            This command is hidden from the help menu.
        """
        self.logger.info(f"{ctx.author} invoked list_cogs")
        message = await ctx.send('Listing all cogs...')
        await ctx.message.delete()
        try:
            cogs = [cog[:-3] for cog in listdir('./cogs/dynamic') if cog.endswith('.py')]
            await message.edit(content=f'Available cogs: {", ".join(cogs)}', delete_after=20)
        except Exception as exc:
            self.logger.error("Error listing cogs", exc_info=True)
            await message.edit(content=f'An error has occurred: {exc}', delete_after=20)
            
    @commands.command(name='restart', aliases=['kys', 'shutdown'], hidden=True)
    @commands.is_owner()
    async def restart(self, ctx):
        """This command restarts the bot.
        
        Note:
            This command can be used only from the bot owner.
            This command is hidden from the help menu.
        """
        self.logger.info(f"{ctx.author} invoked shutdown")
        message = await ctx.send('I am sudoku...')
        await ctx.message.delete()
        try:
            await self.bot.close()
            sys.exit()
        except Exception as exc:
            self.logger.error("Error during shutdown", exc_info=True)
            await message.edit(content=f'An error has occurred: {exc}', delete_after=20)
            
    @commands.command(name='sync', hidden=True)
    @commands.is_owner()
    async def sync(self, ctx):
        """This command syncs the bot's commands with Discord.
        
        Note:
            This command can be used only from the bot owner.
            This command is hidden from the help menu.
        """
        self.logger.info(f"{ctx.author} invoked sync for guild {ctx.guild.id}")
        message = await ctx.send('Syncing commands...')
        await ctx.message.delete()
        try:
            self.bot.tree.copy_global_to(guild=ctx.guild)
            await self.bot.tree.sync(guild=ctx.guild)
            await message.edit(content='Commands synced successfully.', delete_after=20)
        except Exception as exc:
            self.logger.error("Error syncing commands", exc_info=True)
            await message.edit(content=f'An error has occurred: {exc}', delete_after=20)

async def setup(bot):
    """Every cog needs a setup function like this."""
    await bot.add_cog(Dev(bot))
