"""Config-driven auto-responses: canned replies to message patterns.

Replaces the hardcoded reddit-meme reply graph and absorbs the old
interrogative cog (!should-style coin flips) into one per-guild trigger
table. Guild config key `auto_responses` — list of entries:

    {"triggers": ["cope"],             # aliases, matched case-insensitively
     "match": "exact",                 # "exact" whole message | "command" !word
     "responses": ["yikes", "cringe"], # one chosen uniformly at random
     "chance": 1.0}                    # optional reply probability, default 1

Absent/empty key = inert in that guild (default OFF everywhere; enable by
adding entries with /autoresponse add). First matching entry wins, in
config order.

Loop safety: ALL bot-authored messages are ignored (message.author.bot),
not just our own — two bots both running this cog once replied to each
other's replies forever (the cope->seethe incident, 2026-08-07). Keep that
guard; a chance below 1.0 is spam damping, not loop protection.

"command" entries answer prefix-style invocations (e.g. "!should we?")
without registering real commands, so the trigger words stay per-guild
config. The cog registers an error-handler whitelist hook so those
messages don't log CommandNotFound noise.
"""
from discord.ext import commands
from discord import app_commands
import discord
import random

from core.utils import is_admin
from core.error_handler import (
    register_error_whitelist_hook, unregister_error_whitelist_hook
)

MATCH_KINDS = ("exact", "command")
COMMAND_PREFIX = "!"


def _command_word(text, prefix=COMMAND_PREFIX):
    """The lowercased bare command word of a prefixed message, else None."""
    text = text.strip()
    if not text.startswith(prefix) or len(text) <= len(prefix):
        return None
    return text[len(prefix):].split(None, 1)[0].lower()


def find_response(entries, content):
    """(entry, response) for the first entry matching `content`, else None.

    Pure function of (config entries, message text) so the matching rules
    stay unit-testable without a bot.
    """
    text = content.strip().lower()
    word = _command_word(content)
    for entry in entries:
        kind = entry.get("match", "exact")
        triggers = [str(t).lower() for t in entry.get("triggers", [])]
        responses = entry.get("responses") or []
        if not triggers or not responses:
            continue
        if kind == "exact":
            hit = text in triggers
        elif kind == "command":
            hit = word is not None and word in triggers
        else:
            continue
        if hit:
            return entry, random.choice(responses)
    return None


class AutoResponse(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.logger = bot.logger

    def _entries(self, guild_id):
        return self.bot.config.get(guild_id, "auto_responses", []) or []

    def _suppress_command_not_found(self, ctx, error):
        """Whitelist hook: a CommandNotFound whose word is one of this
        guild's command-type triggers is expected traffic, not an error."""
        if ctx.guild is None:
            return False
        word = _command_word(ctx.message.content)
        if word is None:
            return False
        for entry in self._entries(ctx.guild.id):
            if entry.get("match") == "command":
                if word in [str(t).lower() for t in entry.get("triggers", [])]:
                    return True
        return False

    async def cog_load(self):
        register_error_whitelist_hook(self._suppress_command_not_found)

    async def cog_unload(self):
        unregister_error_whitelist_hook(self._suppress_command_not_found)

    @commands.Cog.listener()
    async def on_message(self, message):
        # ANY bot author, not just self — the guard that makes a two-bot
        # reply loop impossible. Do not weaken to `== self.bot.user`.
        if message.author.bot:
            return
        if message.guild is None:
            return
        entries = self._entries(message.guild.id)
        if not entries:
            return
        found = find_response(entries, message.content)
        if not found:
            return
        entry, response = found
        try:
            chance = float(entry.get("chance", 1.0))
        except (TypeError, ValueError):
            chance = 1.0
        if random.random() > chance:
            return
        await message.channel.send(response)

    # ---- admin UI --------------------------------------------------------

    autoresponse = app_commands.Group(
        name="autoresponse",
        description="Per-guild auto-response triggers (admin)",
        guild_only=True,
    )

    @autoresponse.command(name="list", description="Show this server's auto-response triggers")
    async def ar_list(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return
        entries = self._entries(interaction.guild.id)
        if not entries:
            await interaction.response.send_message(
                "No auto-responses configured. Add one with /autoresponse add.",
                ephemeral=True)
            return
        lines = []
        for i, e in enumerate(entries):
            chance = e.get("chance", 1.0)
            chance_note = f", {int(float(chance) * 100)}%" if float(chance) < 1.0 else ""
            responses = ", ".join(str(r) for r in e.get("responses", []))
            if len(responses) > 120:
                responses = responses[:117] + "..."
            lines.append(
                f"`{i}` [{e.get('match', 'exact')}{chance_note}] "
                f"{', '.join(e.get('triggers', []))} → {responses}")
        await interaction.response.send_message("\n".join(lines)[:2000], ephemeral=True)

    @autoresponse.command(name="add", description="Add an auto-response trigger")
    @app_commands.describe(
        triggers="Trigger words/phrases, comma-separated (aliases)",
        responses="Possible replies, separated by | (one picked at random)",
        match="exact: whole message equals a trigger; command: !word invocation",
        chance="Probability of replying, 0.0-1.0 (default 1.0)",
    )
    @app_commands.choices(match=[
        app_commands.Choice(name=k, value=k) for k in MATCH_KINDS])
    async def ar_add(self, interaction: discord.Interaction, triggers: str,
                     responses: str, match: str = "exact",
                     chance: float = 1.0):
        if not is_admin(interaction):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return
        trigger_list = [t.strip().lower() for t in triggers.split(",") if t.strip()]
        # command words are single tokens by construction — a phrase can
        # never match, so reject it here instead of dying silently later
        if match == "command":
            bad = [t for t in trigger_list if " " in t]
            if bad:
                await interaction.response.send_message(
                    f"Command triggers must be single words: {', '.join(bad)}",
                    ephemeral=True)
                return
        response_list = [r.strip() for r in responses.split("|") if r.strip()]
        if not trigger_list or not response_list:
            await interaction.response.send_message(
                "Need at least one trigger and one response.", ephemeral=True)
            return
        if not (0.0 < chance <= 1.0):
            await interaction.response.send_message(
                "chance must be within (0, 1].", ephemeral=True)
            return
        entry = {"triggers": trigger_list, "match": match,
                 "responses": response_list}
        if chance < 1.0:
            entry["chance"] = chance
        entries = self._entries(interaction.guild.id)
        entries.append(entry)
        self.bot.config.set(interaction.guild.id, "auto_responses", entries)
        self.logger.info(
            f"{interaction.user} added auto-response {entry} in guild {interaction.guild.id}")
        await interaction.response.send_message(
            f"Added: {', '.join(trigger_list)} → {len(response_list)} response(s).",
            ephemeral=True)

    @autoresponse.command(name="remove", description="Remove a trigger by its /autoresponse list index")
    @app_commands.describe(index="Entry number from /autoresponse list")
    async def ar_remove(self, interaction: discord.Interaction, index: int):
        if not is_admin(interaction):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return
        entries = self._entries(interaction.guild.id)
        if not (0 <= index < len(entries)):
            await interaction.response.send_message(
                f"No entry {index} — see /autoresponse list.", ephemeral=True)
            return
        removed = entries.pop(index)
        self.bot.config.set(interaction.guild.id, "auto_responses", entries)
        self.logger.info(
            f"{interaction.user} removed auto-response {removed} in guild {interaction.guild.id}")
        await interaction.response.send_message(
            f"Removed: {', '.join(removed.get('triggers', []))}", ephemeral=True)


async def setup(bot):
    """Every cog needs a setup function like this."""
    await bot.add_cog(AutoResponse(bot))
