from discord.ext import commands
import discord

class Admin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.logger = bot.logger

    @commands.command(name="claimsuper", aliases=["claimsuperadmin"])
    async def claimsuper(self, ctx):
        """Claim the sole superadmin spot."""
        # use global config for superadmin
        if self.bot.config.get(None, "superadmin") is None:
            self.bot.config.set(None, "superadmin", ctx.author.id)
            self.logger.info(f"Superadmin claimed by {ctx.author} (ID: {ctx.author.id})")
            await ctx.send("Superadmin claimed.")
        else:
            self.logger.warning(f"Failed superadmin claim by {ctx.author} (ID: {ctx.author.id}); already set")
            await ctx.send("There can only be one bot superadmin.")

    @commands.command(name="claimadmin")
    async def claimadmin(self, ctx):
        """Claim admin status if you have Administrator permissions."""
        if ctx.guild is None:
            await ctx.send("This command cannot be used in direct messages.")
            self.logger.warning(f"claimadmin attempted in DMs by {ctx.author} (ID: {ctx.author.id})")
            return
        
        global_superadmin = self.bot.config.get(None, "superadmin")
        if not (ctx.author.id == global_superadmin or ctx.author.guild_permissions.administrator or ctx.author == ctx.guild.owner):
            self.logger.warning(f"Unauthorized claimadmin attempt by {ctx.author} (ID: {ctx.author.id}) in guild {ctx.guild.id}")
            await ctx.send("You lack admin privileges on this server.")
            return
        # use per-guild config for admins
        config = self.bot.config
        admins = config.get(ctx, "admins", [])
        if not ctx.author.id == global_superadmin and admins: # user is not a superadmin and there are already admins
            await ctx.send("There are already admins for this server. You must be added by one of the admins with !addadmin @you.")
            return
        if ctx.author.id in admins:
            self.logger.info(f"{ctx.author} (ID: {ctx.author.id}) already admin in guild {ctx.guild.id}")
            await ctx.send("You are already a bot admin.")
        else:
            admins.append(ctx.author.id)
            config.set(ctx, "admins", admins)
            self.logger.info(f"{ctx.author} (ID: {ctx.author.id}) granted admin in guild {ctx.guild.id}")
            await ctx.send("You are now a bot admin.")

    @commands.command(name="addadmin")
    async def addadmin(self, ctx, member: discord.Member = None):
        """Add a user as a server admin (if you're superadmin or already admin)."""
        if ctx.guild is None:
            await ctx.send("This command cannot be used in direct messages.")
            self.logger.warning(f"addadmin attempted in DMs by {ctx.author} (ID: {ctx.author.id})")
            return
        if not member:
            await ctx.send("Please specify a user to add as bot admin.")
            self.logger.warning(f"addadmin called without member by {ctx.author} (ID: {ctx.author.id})")
            return
        config = self.bot.config
        admins = config.get(ctx, "admins", [])
        if ctx.author.id != self.bot.config.get(None, "superadmin") and not (
            ctx.author.guild_permissions.administrator or ctx.author == ctx.guild.owner or 
            ctx.author.id in admins
        ):
            self.logger.warning(f"Unauthorized addadmin attempt by {ctx.author} (ID: {ctx.author.id}) to add {member} (ID: {member.id}) in guild {ctx.guild.id}")
            await ctx.send("You don't have permission to add bot admins.")
            return
        if member.id in admins:
            self.logger.info(f"{member} (ID: {member.id}) already admin in guild {ctx.guild.id}")
            await ctx.send(f"{member} is already an bot admin.")
        else:
            admins.append(member.id)
            config.set(ctx, "admins", admins)
            self.logger.info(f"{member} (ID: {member.id}) added as admin by {ctx.author} (ID: {ctx.author.id}) in guild {ctx.guild.id}")
            await ctx.send(f"{member} has been added as an bot admin.")    
    
    @commands.command(name="deletemsg")
    @commands.is_owner()
    async def delete_message(self, ctx, guild_id: int, message_id: int, thread_id: int = None):
        """Delete a message using guild ID and message ID (Owner only).
        
        Args:
            guild_id: The ID of the server where the message is located
            message_id: The ID of the message to delete
            thread_id: Optional - The ID of the thread if the message is in a thread
        """
        try:
            # Get the guild object
            guild = self.bot.get_guild(guild_id)
            if not guild:
                await ctx.send(f"Could not find guild with ID {guild_id}")
                self.logger.warning(f"Failed to find guild with ID {guild_id} in delete_message command")
                return
            
            message = None
            
            # If thread_id is provided, try to find and delete the message directly from the thread
            if thread_id:
                thread = None
                # First check all text channels for this thread
                for channel in guild.text_channels:
                    thread = discord.utils.get(channel.threads, id=thread_id)
                    if thread:
                        break
                
                # If we found the thread, try to get the message from it
                if thread:
                    try:
                        message = await thread.fetch_message(message_id)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                        self.logger.warning(f"Failed to find message {message_id} in thread {thread_id}: {e}")
                else:
                    await ctx.send(f"Could not find thread with ID {thread_id} in guild {guild.name}")
                    self.logger.warning(f"Failed to find thread with ID {thread_id} in guild {guild_id}")
                    return
            else:
                # Search for the message in all text channels
                for channel in guild.text_channels:
                    try:
                        message = await channel.fetch_message(message_id)
                        if message:
                            break
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        continue
                    
                    # If not found in the main channel, check all threads in this channel
                    if not message:
                        try:
                            # Get active threads
                            await channel.fetch_active_threads()
                            # Check each thread
                            for thread in channel.threads:
                                try:
                                    message = await thread.fetch_message(message_id)
                                    if message:
                                        break
                                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                                    continue
                            
                            # If we found the message in a thread, break the outer loop
                            if message:
                                break
                        except (discord.Forbidden, discord.HTTPException):
                            continue
            
            if not message:
                await ctx.send(f"Could not find message with ID {message_id} in guild {guild.name}")
                self.logger.warning(f"Failed to find message with ID {message_id} in guild {guild_id}")
                return
            
            # Delete the message
            await message.delete()
            location = f"thread {message.channel.id}" if isinstance(message.channel, discord.Thread) else f"channel {message.channel.id}"
            self.logger.info(f"Message {message_id} deleted from {guild.name} ({location}) by {ctx.author} (ID: {ctx.author.id})")
            await ctx.send(f"Message {message_id} deleted from {guild.name} ({location})")
        except Exception as e:
            self.logger.error(f"Error deleting message: {e}", exc_info=True)
            await ctx.send(f"Error deleting message: {e}")

async def setup(bot):
    await bot.add_cog(Admin(bot))
