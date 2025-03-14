from discord.ext import commands
import discord
from config import Config

class Admin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="claimsuper", aliases=["claimsuperadmin"])
    async def claimsuper(self, ctx):
        """Claim the sole superadmin spot."""
        global_config = Config()
        if global_config.get("superadmin") is None:
            global_config.set("superadmin", ctx.author.id)
            await ctx.send("Superadmin claimed.")
        else:
            await ctx.send("There can only be one bot superadmin.")

    @commands.command(name="claimadmin")
    async def claimadmin(self, ctx):
        """Claim admin status if you have Administrator permissions."""
        if ctx.guild is None:
            await ctx.send("This command cannot be used in direct messages.")
            return
        
        global_superadmin = Config().get("superadmin")
        if not (ctx.author.id == global_superadmin or ctx.author.guild_permissions.administrator or ctx.author == ctx.guild.owner):
            await ctx.send("You lack admin privileges on this server.")
            return
        config = Config(ctx)
        admins = config.get("admins", [])
        if not ctx.author.id == global_superadmin and admins: # user is not a superadmin and there are already admins
            await ctx.send("There are already admins for this server. You must be added by one of the admins with !addadmin @you.")
            return
        if ctx.author.id in admins:
            await ctx.send("You are already a bot admin.")
        else:
            admins.append(ctx.author.id)
            config.set("admins", admins)
            await ctx.send("You are now a bot admin.")

    @commands.command(name="addadmin")
    async def addadmin(self, ctx, member: discord.Member = None):
        """Add a user as a server admin (if you're superadmin or already admin)."""
        if ctx.guild is None:
            await ctx.send("This command cannot be used in direct messages.")
            return
        if not member:
            await ctx.send("Please specify a user to add as bot admin.")
            return
        config = Config(ctx)
        admins = config.get("admins", [])
        if ctx.author.id != Config().get("superadmin") and not (
            ctx.author.guild_permissions.administrator or ctx.author == ctx.guild.owner or 
            ctx.author.id in admins
        ):
            await ctx.send("You don't have permission to add bot admins.")
            return
        if member.id in admins:
            await ctx.send(f"{member} is already an bot admin.")
        else:
            admins.append(member.id)
            config.set("admins", admins)
            await ctx.send(f"{member} has been added as an bot admin.")

async def setup(bot):
    await bot.add_cog(Admin(bot))
