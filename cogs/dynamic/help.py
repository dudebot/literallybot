"""/help and !help — one embed of the bot's commands, grouped strictly by cog.

discord.py's stock help_command is set to None on load; both surfaces
render the same embed via build_help_embed. The old hand-curated category
map is gone — it went stale the moment cogs were reshaped. The ONLY
grouping mechanism is the cog a command came from; no category
abstraction to maintain. Disabling this cog (!cogs / disabled_cogs)
removes both surfaces with zero code changes.

Visibility rules:
- prefix commands: `hidden=True` ones (the admin/superadmin surface,
  including every panel launcher) appear only for invokers passing
  `is_admin`; everything else shows for everyone
- slash commands: shown to everyone when they carry no default_permissions;
  gated ones appear only for admin invokers.
"""
import discord
from discord import app_commands
from discord.ext import commands

from core.utils import is_admin

MEDIA_NOTE = "Any file in this server's media library can be posted with !<name> — admins manage it via /media"

EMBED_COLOUR = discord.Colour.blurple()


def _truncate(text, limit):
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _slash_entries(cmd):
    """Yield (token, description) for a tree command, expanding Groups one level."""
    if isinstance(cmd, app_commands.Group):
        for child in cmd.commands:
            yield (f"/{cmd.name} {child.name}", child.description or "")
    else:
        yield (f"/{cmd.name}", cmd.description or "")


def build_help_embed(bot, invoker_is_admin):
    """One monolithic embed: a field per cog, commands listed inside."""
    # Slash-command ownership: which cog registered each top-level command.
    slash_by_cog = {}
    for cog in bot.cogs.values():
        for ac in cog.get_app_commands():
            if invoker_is_admin or ac.default_permissions is None:
                slash_by_cog.setdefault(cog.qualified_name, []).append(ac)

    embed = discord.Embed(
        title="Command Help",
        description="Prefix commands use `!`; slash commands use `/`.",
        colour=EMBED_COLOUR,
    )
    for cog_name in sorted(bot.cogs, key=str.lower):
        cog = bot.cogs[cog_name]
        entries = []
        for cmd in sorted(cog.get_commands(), key=lambda c: c.name):
            if cmd.hidden and not invoker_is_admin:
                continue
            entries.append((f"!{cmd.name}", cmd.short_doc or ""))
        for ac in slash_by_cog.get(cog_name, []):
            entries.extend(sorted(_slash_entries(ac)))
        if not entries and cog_name != "Media":
            continue
        lines = [f"`{token}` — {desc}" if desc else f"`{token}`"
                 for token, desc in entries]
        if cog_name == "Media":
            lines.insert(0, MEDIA_NOTE)
        embed.add_field(name=cog_name, value=_truncate("\n".join(lines), 1024),
                        inline=False)

    # Cog-less loose prefix commands, if any ever appear.
    loose = [c for c in bot.commands if c.cog is None and not c.hidden]
    if loose:
        lines = [f"`!{c.name}` — {c.short_doc}" if c.short_doc else f"`!{c.name}`"
                 for c in sorted(loose, key=lambda c: c.name)]
        embed.add_field(name="Other", value=_truncate("\n".join(lines), 1024),
                        inline=False)
    return embed


class Help(commands.Cog):
    """Interactive help: /help (the prefix !help is deliberately gone)."""

    def __init__(self, bot):
        self.bot = bot
        self._original_help_command = None

    async def cog_load(self):
        self._original_help_command = self.bot.help_command
        self.bot.help_command = None

    async def cog_unload(self):
        self.bot.help_command = self._original_help_command

    @app_commands.command(name="help", description="Overview of the bot's commands")
    async def slash_help(self, interaction: discord.Interaction):
        embed = build_help_embed(self.bot, is_admin(interaction))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @commands.command(name="help")
    async def help_prefix(self, ctx):
        """Overview of the bot's commands."""
        # Same invoker-based rendering as /help — but a channel message is
        # public, so an admin running !help in a busy channel does print
        # the admin command names where everyone can read them. /help
        # (ephemeral) is the discreet variant.
        embed = build_help_embed(self.bot, is_admin(ctx))
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Help(bot))
