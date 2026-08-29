"""/help and !help — one embed of the bot's commands, grouped strictly by cog.

discord.py's stock help_command is set to None on load; both surfaces
render the same embed via build_help_embed. The old hand-curated category
map is gone — it went stale the moment cogs were reshaped. The ONLY
grouping mechanism is the cog a command came from; no category
abstraction to maintain. Disabling this cog (!cogs / disabled_cogs)
removes both surfaces with zero code changes.

Visibility rule, one sentence: THE DECORATOR DECIDES. A command is listed
only if the invoker could actually run it, and the answer comes from the
command's own checks — never from `hidden=True` alone (an authoring hint)
and never from a "(superadmin)" suffix in the description (a string that
goes stale silently). Both surfaces apply the same rule:

- with the matching context (a Context for !help, an Interaction for
  /help), the checks are EXECUTED and answer exactly;
- without it — !help cannot run an Interaction-typed predicate, /help
  cannot run a Context-typed one — the fallback reads the tier the check
  declares via the `__gate__` stamp on core.utils' is_admin /
  is_superadmin (the same functions `@commands.check` and
  `@app_commands.check` both wrap). A gate of unknown tier fails
  closed (admin-or-better), never open.

Two leaks this replaced: gating on `hidden` alone advertised an is_owner()
command to every DM user, and treating `default_permissions is None` as
"public" advertised /cogs, /config and /autoresponse to everyone, since
their gate is this bot's admin list rather than a Discord permission.
"""
import inspect

import discord
from discord import app_commands
from discord.ext import commands

from core.utils import (GATE_ADMIN, GATE_SUPERADMIN, gate_of, is_admin,
                        is_superadmin)

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


def _checks_of(cmd):
    """Check predicates attached to a command or group.

    Command stores them on `.checks`; Group stores them on
    `__discord_app_commands_checks__` (Group has no `.checks` attribute).
    A listing that only reads `.checks` treats every Group as ungated.
    """
    seen = []
    for seq in (getattr(cmd, "checks", None),
                getattr(cmd, "__discord_app_commands_checks__", None)):
        if not seq:
            continue
        for check in seq:
            if check not in seen:
                seen.append(check)
    return seen


def _is_gated(cmd, cog=None):
    """Whether anything at all restricts who may run this command.

    Deliberately structural: the presence of a check, a cog-wide cog_check,
    or Discord-level default_permissions. It never reads the description —
    a "(superadmin)" suffix in help text is a string that goes stale and
    must never be what decides visibility.
    """
    if _checks_of(cmd):
        return True
    if getattr(cmd, "default_permissions", None) is not None:
        return True
    parent = getattr(cmd, "parent", None)
    if parent is not None and _is_gated(parent):
        return True
    cog = cog if cog is not None else getattr(cmd, "cog", None)
    # A cog_check gates every command in the cog (how !errorlog is gated
    # without any per-command decorator).
    if cog is not None and type(cog).cog_check is not commands.Cog.cog_check:
        return True
    return False


def _gate_tier(cmd):
    """The strictest tier any of this command's checks declares, or None.

    Reads the __gate__ stamp that core.utils' is_admin/is_superadmin check
    factories leave on their predicates. This is what lets a surface that
    cannot EXECUTE a check still know who it was meant for."""
    tiers = set()
    node = cmd
    while node is not None:
        for check in _checks_of(node):
            tier = gate_of(check)
            if tier:
                tiers.add(tier)
        node = getattr(node, "parent", None)
    if GATE_SUPERADMIN in tiers:
        return GATE_SUPERADMIN
    if GATE_ADMIN in tiers:
        return GATE_ADMIN
    return None


def _passes_tier(cmd, invoker_is_admin, invoker_is_superadmin):
    """Static visibility for a gated command, used when the surface cannot
    run the check itself. Unknown tier is treated as admin-or-better, never
    as public — an untagged gate fails closed."""
    tier = _gate_tier(cmd)
    if tier == GATE_SUPERADMIN:
        return invoker_is_superadmin
    return invoker_is_admin


async def _visible_to(cmd, ctx, invoker_is_admin, invoker_is_superadmin):
    """Whether a prefix command belongs in this invoker's listing.

    `hidden` is an authoring hint, not an authorization boundary: a command
    can be gated by a check and still ship without hidden=True (that is how
    an is_owner() command once got advertised to every DM user, who then
    ran it and got "You do not own this bot"). So the real test is the
    command's own checks.

    With a Context, can_run answers exactly. Without one (the /help path),
    fall back to the structural test rather than assuming public — that
    assumption is what leaked cog_check-gated commands like !errorlog.
    """
    if cmd.hidden and not invoker_is_admin:
        return False
    if ctx is None:
        if _is_gated(cmd):
            return _passes_tier(cmd, invoker_is_admin, invoker_is_superadmin)
        return True
    try:
        return await cmd.can_run(ctx)
    except commands.CommandError:
        # Any check that raises (NotOwner, CheckFailure, ...) means "not
        # for this invoker" — same answer as a plain False.
        return False


async def _slash_visible_to(ac, interaction, invoker_is_admin,
                            invoker_is_superadmin):
    """Whether a slash command belongs in this invoker's listing.

    The mirror of _visible_to. `default_permissions is None` used to stand
    in for "public", which advertised every admin panel to everyone —
    Discord's picker pin is not this bot's admin list. Ask the decorator
    instead (`is_admin` / `is_superadmin` on the command's checks).

    With an Interaction the checks run for real; !help has only a Context,
    which an Interaction-typed predicate cannot accept, so it falls back to
    the declared tier.
    """
    if not _is_gated(ac):
        return True
    checks = _checks_of(ac)
    # No runnable predicate (a Group gated only by default_permissions,
    # or a listing without an Interaction) — the declared tier, never open.
    if interaction is None or not checks:
        return _passes_tier(ac, invoker_is_admin, invoker_is_superadmin)
    for check in checks:
        try:
            result = check(interaction)
            if inspect.isawaitable(result):
                result = await result
            if not result:
                return False
        except app_commands.AppCommandError:
            return False
    return True


async def build_help_embed(bot, invoker_is_admin, ctx=None,
                           invoker_is_superadmin=False, interaction=None):
    """One monolithic embed: a field per cog, commands listed inside."""
    # Slash-command ownership: which cog registered each top-level command.
    slash_by_cog = {}
    for cog in bot.cogs.values():
        for ac in cog.get_app_commands():
            if await _slash_visible_to(ac, interaction, invoker_is_admin,
                                       invoker_is_superadmin):
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
            if not await _visible_to(cmd, ctx, invoker_is_admin,
                                     invoker_is_superadmin):
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
        embed = await build_help_embed(
            self.bot, is_admin(interaction),
            invoker_is_superadmin=is_superadmin(interaction),
            interaction=interaction)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @commands.command(name="help")
    async def help_prefix(self, ctx):
        """Overview of the bot's commands."""
        # Same invoker-based rendering as /help — but a channel message is
        # public, so an admin running !help in a busy channel does print
        # the admin command names where everyone can read them. /help
        # (ephemeral) is the discreet variant.
        embed = await build_help_embed(
            self.bot, is_admin(ctx), ctx,
            invoker_is_superadmin=is_superadmin(ctx))
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Help(bot))
