from discord.ext import commands

class SetRole(commands.Cog):
    """This is a cog with a set role command."""
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name='setrole', description='Adds or removes available roles.')
    async def setrole(self, ctx, action: str, *, rolename: str):
        """Adds or removes available roles."""
        role_option = action
        if role_option not in ["+", "-"]:
            await ctx.send("Use a + or - to add or remove the role (eg: !setrole +Kinography)")
        else:
            role = discord.utils.get(ctx.guild.roles, name=rolename)
            if role is not None:
                if role_option == "+":
                    await ctx.author.add_roles(role)
                    await ctx.send(f"Added to role: {role.name}")
                elif role_option == "-":
                    await ctx.author.remove_roles(role)
                    await ctx.send(f"Removed from role: {role.name}")
            else:
                await ctx.send(f"Could not find role: {rolename}")

async def setup(bot):
    """Every cog needs a setup function like this."""
    await bot.add_cog(SetRole(bot))
