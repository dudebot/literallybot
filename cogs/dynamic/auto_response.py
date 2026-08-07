"""Config-driven auto-responses: weighted canned replies to exact messages.

Replaces the hardcoded reddit-meme reply graph with a per-guild trigger
table. Guild config key `auto_responses` — list of entries:

    {"triggers": ["cope"],                  # aliases, case-insensitive
     "responses": ["yikes", ["rare", 2]]}   # str = weight 1, [text, weight]
                                            # = relative weight

One response is chosen by relative weight — e.g. Yes 49 / No 49 /
"those are bad answers" 2. Absent/empty key = inert in that guild
(default OFF everywhere; enable with /autoresponse add). First matching
entry wins, in config order. Matching is whole-message exact only — this
cog never answers prefixed commands (that's interrogative/media territory).

Loop safety: ALL bot-authored messages are ignored (message.author.bot),
not just our own — two bots both running this cog once replied to each
other's replies forever (the cope->seethe incident, 2026-08-07).
"""
from discord.ext import commands
from discord import app_commands
import discord
import random

from core.utils import is_admin


def _weighted(responses):
    """(texts, weights) from mixed str / [text, weight] response entries.
    Malformed weights fall back to 1 rather than killing the entry."""
    texts, weights = [], []
    for r in responses:
        if isinstance(r, (list, tuple)) and r:
            text = str(r[0])
            try:
                weight = float(r[1]) if len(r) > 1 else 1.0
            except (TypeError, ValueError):
                weight = 1.0
        else:
            text, weight = str(r), 1.0
        if weight > 0:
            texts.append(text)
            weights.append(weight)
    return texts, weights


def find_response(entries, content):
    """The weighted-random response for the first entry matching `content`,
    else None. Pure function of (config entries, message text) so the
    matching rules stay unit-testable without a bot."""
    text = content.strip().lower()
    for entry in entries:
        triggers = [str(t).lower() for t in entry.get("triggers", [])]
        texts, weights = _weighted(entry.get("responses") or [])
        if not triggers or not texts:
            continue
        if text in triggers:
            return random.choices(texts, weights=weights)[0]
    return None


def _format_responses(responses, limit=120):
    parts = []
    for r in responses:
        if isinstance(r, (list, tuple)) and r:
            parts.append(f"{r[0]}×{r[1]}" if len(r) > 1 else str(r[0]))
        else:
            parts.append(str(r))
    out = ", ".join(parts)
    return out[:limit - 3] + "..." if len(out) > limit else out


def parse_responses(raw):
    """'Yes::49 | No::49 | bad::2' -> config response entries.
    `::weight` suffix optional; plain text = weight 1."""
    out = []
    for part in raw.split("|"):
        part = part.strip()
        if not part:
            continue
        if "::" in part:
            text, _, tail = part.rpartition("::")
            text = text.strip()
            try:
                weight = float(tail.strip())
            except ValueError:
                text, weight = part, None
            if text and weight is not None:
                out.append([text, weight] if weight != 1 else text)
                continue
        out.append(part)
    return out


class AutoResponse(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.logger = bot.logger

    def _entries(self, guild_id):
        return self.bot.config.get(guild_id, "auto_responses", []) or []

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
        response = find_response(entries, message.content)
        if response is not None:
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
            lines.append(
                f"`{i}` {', '.join(e.get('triggers', []))} → "
                f"{_format_responses(e.get('responses', []))}")
        await interaction.response.send_message("\n".join(lines)[:2000], ephemeral=True)

    @autoresponse.command(name="add", description="Add an auto-response trigger")
    @app_commands.describe(
        triggers="Trigger messages, comma-separated (matched as the whole message)",
        responses="Replies separated by | — append ::weight for odds, e.g. Yes::49 | No::49 | maybe::2",
    )
    async def ar_add(self, interaction: discord.Interaction, triggers: str,
                     responses: str):
        if not is_admin(interaction):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return
        trigger_list = [t.strip().lower() for t in triggers.split(",") if t.strip()]
        response_list = parse_responses(responses)
        if not trigger_list or not response_list:
            await interaction.response.send_message(
                "Need at least one trigger and one response.", ephemeral=True)
            return
        entry = {"triggers": trigger_list, "responses": response_list}
        entries = self._entries(interaction.guild.id)
        entries.append(entry)
        self.bot.config.set(interaction.guild.id, "auto_responses", entries)
        self.logger.info(
            f"{interaction.user} added auto-response {entry} in guild {interaction.guild.id}")
        await interaction.response.send_message(
            f"Added: {', '.join(trigger_list)} → {_format_responses(response_list)}",
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
