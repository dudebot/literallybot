from discord.ext import commands
import discord
from config import Config

class SetRole(commands.Cog):
    """This is a cog with role commands."""
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name='setrole', description='Adds or removes available roles. Optionally apply to a specified user.')
    async def setrole(self, ctx, role: str, member: discord.Member = None):
        config = Config(ctx)
        whitelist_roles = config.get("whitelist_roles", [])
        # Determine target; default to invoking user if no member provided.
        target = member if member else ctx.author
        # If the target is not the invoker, verify admin/superadmin permission.
        if member:
            admins = config.get("admins", [])
            if not (ctx.author.guild_permissions.administrator or ctx.author == ctx.guild.owner or
                    ctx.author.id in admins):
                await ctx.send("You don't have permission to modify roles for other users.")
                return
        role_option = role[0]
        rolename = role[1:].strip()
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.send("You need the manage roles permission to use this command.")
            return
        if role_option not in ["+", "-"]:
            await ctx.send("Use a + or - to add or remove the role (eg: !setrole +Events or !setrole +Events @member)")
            return
        elif rolename.lower() not in [r.lower() for r in whitelist_roles]:
            await ctx.send(f"Role not in whitelist: {rolename}")
            return
        role_obj = discord.utils.get(ctx.guild.roles, name=rolename)
        if role_obj is not None:
            if role_option == "+":
                await target.add_roles(role_obj)
                await ctx.send(f"Added role: {role_obj.name} to {target.mention}")
            elif role_option == "-":
                await target.remove_roles(role_obj)
                await ctx.send(f"Removed role: {role_obj.name} from {target.mention}")
        else:
            await ctx.send(f"Could not find role: {rolename}")

    @commands.command(name='whitelistrole', description='Adds a role to the whitelist roles.')
    async def whitelistrole(self, ctx, *, role: str):
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.send("You need the manage roles permission to use this command.")
            return
        config = Config(ctx)
        whitelist_roles = config.get("whitelist_roles", [])
        if role.lower() in [r.lower() for r in whitelist_roles]:
            await ctx.send(f"Role '{role}' is already whitelisted.")
        else:
            whitelist_roles.append(role)
            config.set("whitelist_roles", whitelist_roles)
            await ctx.send(f"Role '{role}' added to the whitelist.")

async def setup(bot):
    """Every cog needs a setup function like this."""
    await bot.add_cog(SetRole(bot))
