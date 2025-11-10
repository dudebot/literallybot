from discord.ext import commands
from core.utils import is_superadmin, is_admin, get_superadmins
import discord

class Admin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.logger = bot.logger

    @commands.command(name="claimsuper", aliases=["claimsuperadmin"])
    async def claimsuper(self, ctx):
        """Claim superadmin privileges (first come, first served)."""
        superadmins = get_superadmins(self.bot.config)
        if ctx.author.id not in superadmins:
            superadmins.append(ctx.author.id)
            self.bot.config.set_global("superadmins", superadmins)
            self.logger.info(f"Superadmin claimed by {ctx.author} (ID: {ctx.author.id})")
            await ctx.send("Superadmin claimed.")
        else:
            self.logger.warning(f"Failed superadmin claim by {ctx.author} (ID: {ctx.author.id}); already superadmin")
            await ctx.send("You are already a bot superadmin.")

    @commands.command(name="addsuperadmin")
    @commands.check(lambda ctx: is_superadmin(ctx.bot.config, ctx.author.id))
    async def addsuperadmin(self, ctx, member: discord.Member = None):
        """Add a user as a bot superadmin (owner only)."""
        if not member:
            await ctx.send("Please specify a user to add as bot superadmin.")
            self.logger.warning(f"addsuperadmin called without member by {ctx.author} (ID: {ctx.author.id})")
            return

        superadmins = get_superadmins(self.bot.config)
        if member.id in superadmins:
            self.logger.info(f"{member} (ID: {member.id}) already superadmin")
            await ctx.send(f"{member} is already a bot superadmin.")
        else:
            superadmins.append(member.id)
            self.bot.config.set_global("superadmins", superadmins)
            self.logger.info(f"{member} (ID: {member.id}) added as superadmin by {ctx.author} (ID: {ctx.author.id})")
            await ctx.send(f"{member} has been added as a bot superadmin.")

    @commands.command(name="claimadmin")
    async def claimadmin(self, ctx):
        """Claim admin status if you have Administrator permissions."""
        if ctx.guild is None:
            await ctx.send("This command cannot be used in direct messages.")
            self.logger.warning(f"claimadmin attempted in DMs by {ctx.author} (ID: {ctx.author.id})")
            return

        superadmins = self.bot.config.get_global("superadmins", [])
        if not (ctx.author.id in superadmins or ctx.author.guild_permissions.administrator or ctx.author == ctx.guild.owner):
            self.logger.warning(f"Unauthorized claimadmin attempt by {ctx.author} (ID: {ctx.author.id}) in guild {ctx.guild.id}")
            await ctx.send("You lack admin privileges on this server.")
            return
        # use per-guild config for admins
        admins = self.bot.config.get(ctx, "admins", [])
        if ctx.author.id not in superadmins and admins: # user is not a superadmin and there are already admins
            await ctx.send("There are already admins for this server. You must be added by one of the admins with !addadmin @you.")
            return
        if ctx.author.id in admins:
            self.logger.info(f"{ctx.author} (ID: {ctx.author.id}) already admin in guild {ctx.guild.id}")
            await ctx.send("You are already a bot admin.")
        else:
            admins.append(ctx.author.id)
            self.bot.config.set(ctx, "admins", admins)
            self.logger.info(f"{ctx.author} (ID: {ctx.author.id}) granted admin in guild {ctx.guild.id}")
            await ctx.send("You are now a bot admin.")

    @commands.command(name="addadmin")
    @commands.check(is_admin)
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
        admins = self.bot.config.get(ctx, "admins", [])
        superadmins = self.bot.config.get_global("superadmins", [])
        if ctx.author.id not in superadmins and not (
            ctx.author.guild_permissions.administrator or ctx.author == ctx.guild.owner or
            ctx.author.id in admins
        ):
            self.logger.warning(f"Unauthorized addadmin attempt by {ctx.author} (ID: {ctx.author.id}) to add {member} (ID: {member.id}) in guild {ctx.guild.id}")
            await ctx.send("You don't have permission to add bot admins.")
            return
        if member.id in admins:
            self.logger.info(f"{member} (ID: {member.id}) already admin in guild {ctx.guild.id}")
            await ctx.send(f"{member} is already a bot admin.")
        else:
            admins.append(member.id)
            self.bot.config.set(ctx, "admins", admins)
            self.logger.info(f"{member} (ID: {member.id}) added as admin by {ctx.author} (ID: {ctx.author.id}) in guild {ctx.guild.id}")
            await ctx.send(f"{member} has been added as a bot admin.")


async def setup(bot):
    await bot.add_cog(Admin(bot))
