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
        if superadmins:
            self.logger.warning(f"Rejected superadmin claim by {ctx.author} (ID: {ctx.author.id}); superadmins already exist")
            await ctx.send("Superadmin already claimed. Use `!addsuperadmin` to add more.")
            return
        superadmins.append(ctx.author.id)
        self.bot.config.set_global("superadmins", superadmins)
        self.logger.info(f"Superadmin claimed by {ctx.author} (ID: {ctx.author.id})")
        await ctx.send("Superadmin claimed.")

    @commands.command(name="addsuperadmin")
    @commands.check(is_superadmin)
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

        # Route membership tests through the shared core.utils gate rather
        # than a hand-rolled copy of the superadmin list lookup.
        author_is_superadmin = is_superadmin(self.bot.config, ctx.author.id)
        if not (author_is_superadmin or ctx.author.guild_permissions.administrator or ctx.author == ctx.guild.owner):
            self.logger.warning(f"Unauthorized claimadmin attempt by {ctx.author} (ID: {ctx.author.id}) in guild {ctx.guild.id}")
            await ctx.send("You lack admin privileges on this server.")
            return
        # use per-guild config for admins
        admins = self.bot.config.get(ctx, "admins", [])
        if not author_is_superadmin and admins: # user is not a superadmin and there are already admins
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
        # Authorization is fully handled by @commands.check(is_admin) on the
        # decorator — the inline re-implementation this replaces was a second
        # copy of the same gate that could drift.
        admins = self.bot.config.get(ctx, "admins", [])
        if member.id in admins:
            self.logger.info(f"{member} (ID: {member.id}) already admin in guild {ctx.guild.id}")
            await ctx.send(f"{member} is already a bot admin.")
        else:
            admins.append(member.id)
            self.bot.config.set(ctx, "admins", admins)
            self.logger.info(f"{member} (ID: {member.id}) added as admin by {ctx.author} (ID: {ctx.author.id}) in guild {ctx.guild.id}")
            await ctx.send(f"{member} has been added as a bot admin.")

    @commands.command(name="removeadmin")
    @commands.check(is_admin)
    async def removeadmin(self, ctx, user: discord.User = None):
        """Remove a user from this server's bot admins (same gate as !addadmin)."""
        if ctx.guild is None:
            await ctx.send("This command cannot be used in direct messages.")
            self.logger.warning(f"removeadmin attempted in DMs by {ctx.author} (ID: {ctx.author.id})")
            return
        if not user:
            await ctx.send("Please specify a user to remove as bot admin.")
            self.logger.warning(f"removeadmin called without member by {ctx.author} (ID: {ctx.author.id})")
            return
        # discord.User (not Member) so admins who already left the guild can
        # still be removed by mention or id.
        admins = self.bot.config.get(ctx, "admins", [])
        if user.id not in admins:
            await ctx.send(f"{user} is not a bot admin.")
            return
        admins.remove(user.id)
        self.bot.config.set(ctx, "admins", admins)
        self.logger.info(f"{user} (ID: {user.id}) removed as admin by {ctx.author} (ID: {ctx.author.id}) in guild {ctx.guild.id}")
        await ctx.send(f"{user} has been removed as a bot admin.")

    @commands.command(name="removesuperadmin")
    @commands.check(is_superadmin)
    async def removesuperadmin(self, ctx, user: discord.User = None):
        """Remove a user from the global bot superadmins (superadmin only)."""
        if not user:
            await ctx.send("Please specify a user to remove as bot superadmin.")
            self.logger.warning(f"removesuperadmin called without member by {ctx.author} (ID: {ctx.author.id})")
            return
        superadmins = get_superadmins(self.bot.config)
        if user.id not in superadmins:
            await ctx.send(f"{user} is not a bot superadmin.")
            return
        if len(superadmins) == 1:
            await ctx.send("Refusing to remove the last superadmin — add another with `!addsuperadmin` first.")
            self.logger.warning(f"Refused removal of last superadmin {user} (ID: {user.id}) by {ctx.author} (ID: {ctx.author.id})")
            return
        superadmins.remove(user.id)
        self.bot.config.set_global("superadmins", superadmins)
        self.logger.info(f"{user} (ID: {user.id}) removed as superadmin by {ctx.author} (ID: {ctx.author.id})")
        await ctx.send(f"{user} has been removed as a bot superadmin.")

    @commands.command(name="listadmins")
    @commands.check(is_admin)
    async def listadmins(self, ctx):
        """List this server's bot admins and the global superadmins."""
        if ctx.guild is None:
            await ctx.send("This command cannot be used in direct messages.")
            return

        def format_ids(ids):
            if not ids:
                return "None"
            lines = []
            for uid in ids:
                user = self.bot.get_user(uid)
                lines.append(f"- {user} (ID: {uid})" if user else f"- ID: {uid}")
            return "\n".join(lines)

        admins = self.bot.config.get(ctx, "admins", [])
        superadmins = get_superadmins(self.bot.config)
        await ctx.send(
            f"**Server admins:**\n{format_ids(admins)}\n"
            f"**Global superadmins:**\n{format_ids(superadmins)}"
        )


async def setup(bot):
    await bot.add_cog(Admin(bot))
