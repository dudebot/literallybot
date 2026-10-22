from discord.ext import commands
import discord
from core.permissions import is_admin
from core.ops import OpParam, OpScope, ParamKind, PermissionLevel, op
from core.points import get_points, add_points, set_points


def _payload(user, total: int) -> dict:
    return {"user_id": str(getattr(user, "id", user)), "points": total}


class Points(commands.Cog):
    """Per-user points: balance, leaderboard, admin adjust."""

    def __init__(self, bot):
        self.bot = bot
        self.logger = bot.logger if hasattr(bot, "logger") else None

    @commands.command(name="points", aliases=["balance", "score"])
    async def check_points(self, ctx, member: discord.Member = None):
        """Check your point balance or another user's balance."""
        target = member if member else ctx.author
        points = get_points(self.bot, target)
        if target == ctx.author:
            await ctx.send(f"You have **{points:,}** points")
        else:
            await ctx.send(f"{target.mention} has **{points:,}** points")

    @commands.command(name="leaderboard", aliases=["top", "rankings"])
    @commands.guild_only()
    async def leaderboard(self, ctx, limit: int = 10):
        """Show the points leaderboard for this server."""
        # Guild-only because the board walks ctx.guild.members. Points are
        # stored per-user, not per-guild, so a DM has no membership to scope.
        if limit > 20:
            limit = 20
        elif limit < 1:
            limit = 10

        guild_members = []
        for member in ctx.guild.members:
            if not member.bot:
                points = get_points(self.bot, member)
                if points > 0:
                    guild_members.append((member, points))
        guild_members.sort(key=lambda x: x[1], reverse=True)

        if not guild_members:
            await ctx.send("No one has any points yet!")
            return

        description = ""
        for i, (member, points) in enumerate(guild_members[:limit], 1):
            if i == 1:
                emoji = "🥇"
            elif i == 2:
                emoji = "🥈"
            elif i == 3:
                emoji = "🥉"
            else:
                emoji = f"{i}."
            description += f"{emoji} {member.display_name} - **{points:,}** points\n"

        embed = discord.Embed(
            title="Points Leaderboard",
            description=description,
            color=0x9932CC,
        )
        user_rank = None
        for i, (member, _points) in enumerate(guild_members, 1):
            if member == ctx.author:
                user_rank = i
                break
        if user_rank and user_rank > limit:
            user_points = get_points(self.bot, ctx.author)
            embed.set_footer(text=f"Your rank: #{user_rank} with {user_points:,} points")
        await ctx.send(embed=embed)

    @commands.command(name="add_points", aliases=["addpoints"], hidden=True)
    @commands.check(is_admin)
    async def admin_add_points(self, ctx, member: discord.Member, amount: int = 10):
        """Add points to a user. Amount can be negative to subtract."""
        new_total = add_points(self.bot, member, amount)
        if amount > 0:
            await ctx.send(
                f"Added {amount:,} points to {member.mention}. New balance: {new_total:,}")
        else:
            await ctx.send(
                f"Removed {abs(amount):,} points from {member.mention}. New balance: {new_total:,}")

    @commands.command(name="set_points", aliases=["setpoints"], hidden=True)
    @commands.check(is_admin)
    async def admin_set_points(self, ctx, member: discord.Member, amount: int):
        """Set a user's points to a specific amount."""
        old_balance = get_points(self.bot, member)
        set_points(self.bot, member, amount)
        await ctx.send(
            f"Set {member.mention}'s points to {amount:,} (was {old_balance:,})")


    @op(
        "get_user_points",
        "Read a user's point balance. Does not change anything.",
        PermissionLevel.EVERYONE,
        params=[
            OpParam("user", ParamKind.MEMBER, "Member whose balance to read."),
        ],
        serialize=lambda p: p,
        scope=OpScope.GUILD,
        group="points",
        group_label="Points",
    )
    async def op_get_user_points(self, ctx, user) -> dict:
        return _payload(user, get_points(self.bot, user))


    @op(
        "add_user_points",
        "Add points to a user (negative amount subtracts). Negative balances are allowed.",
        PermissionLevel.ADMIN,
        params=[
            OpParam("user", ParamKind.MEMBER, "Member to credit or debit."),
            OpParam("amount", ParamKind.INTEGER, "Points to add; negative subtracts."),
        ],
        serialize=lambda p: p,
        scope=OpScope.GUILD,
        group="points",
        group_label="Points",
    )
    async def op_add_user_points(self, ctx, user, amount: int) -> dict:
        return _payload(user, add_points(self.bot, user, int(amount)))


    @op(
        "set_user_points",
        "Set a user's point balance to an exact signed amount.",
        PermissionLevel.ADMIN,
        params=[
            OpParam("user", ParamKind.MEMBER, "Member whose balance to set."),
            OpParam("amount", ParamKind.INTEGER, "New balance (negative balances allowed)."),
        ],
        serialize=lambda p: p,
        scope=OpScope.GUILD,
        group="points",
        group_label="Points",
    )
    async def op_set_user_points(self, ctx, user, amount: int) -> dict:
        return _payload(user, set_points(self.bot, user, int(amount)))


async def setup(bot):
    await bot.add_cog(Points(bot))
