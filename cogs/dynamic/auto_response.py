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
            parts.append(f"{r[0]}×{r[1]:g}" if len(r) > 1 else str(r[0]))
        else:
            parts.append(str(r))
    out = ", ".join(parts)
    return out[:limit - 3] + "..." if len(out) > limit else out


def responses_to_syntax(responses):
    """Config response entries -> the modal's editable 'a | b::2' syntax
    (inverse of parse_responses)."""
    parts = []
    for r in responses:
        if isinstance(r, (list, tuple)) and r:
            parts.append(f"{r[0]}::{r[1]:g}" if len(r) > 1 else str(r[0]))
        else:
            parts.append(str(r))
    return " | ".join(parts)


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

    # ---- admin UI: single /autoresponse panel ---------------------------

    @app_commands.command(
        name="autoresponse",
        description="Manage this server's auto-response triggers (admin)")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.guild_only()
    async def autoresponse(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message(
                "You do not have permission to use this command.", ephemeral=True)
            return
        view = AutoResponseView(self, interaction)
        await interaction.response.send_message(
            embed=view.render_embed(), view=view, ephemeral=True)
        view.message = await interaction.original_response()


class _EntryModal(discord.ui.Modal):
    """Add a new entry, or edit the one selected in the panel."""

    def __init__(self, panel: "AutoResponseView", *, index=None):
        self._panel = panel
        self._index = index
        if index is None:
            super().__init__(title="Add auto-response")
            triggers_default = ""
            responses_default = ""
        else:
            entry = panel.entries()[index]
            super().__init__(title=f"Edit: {', '.join(entry.get('triggers', []))}"[:45])
            triggers_default = ", ".join(entry.get("triggers", []))
            responses_default = responses_to_syntax(entry.get("responses", []))
        self.triggers = discord.ui.TextInput(
            label="Triggers (comma-separated, whole message)",
            required=True, max_length=200, default=triggers_default,
            placeholder="cope, copes")
        self.responses = discord.ui.TextInput(
            label="Responses — a | b::weight (weight optional)",
            style=discord.TextStyle.paragraph,
            required=True, max_length=1000, default=responses_default,
            placeholder="Yes::49 | No::49 | those are bad answers::2")
        self.add_item(self.triggers)
        self.add_item(self.responses)

    async def on_submit(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return
        trigger_list = [t.strip().lower()
                        for t in str(self.triggers.value).split(",") if t.strip()]
        response_list = parse_responses(str(self.responses.value))
        if not trigger_list or not response_list:
            self._panel.flash("⚠ Need at least one trigger and one response — nothing saved.")
            await self._panel.rerender(interaction)
            return
        entry = {"triggers": trigger_list, "responses": response_list}
        entries = self._panel.entries()
        if self._index is None:
            entries.append(entry)
            self._panel.selected = len(entries) - 1
            self._panel.flash(f"Added: {', '.join(trigger_list)}")
        else:
            entries[self._index] = entry
            self._panel.flash(f"Saved: {', '.join(trigger_list)}")
        self._panel.save(entries)
        await self._panel.rerender(interaction)


class _EntrySelect(discord.ui.Select):
    def __init__(self, panel: "AutoResponseView"):
        self._panel = panel
        entries = panel.entries()
        options = [
            discord.SelectOption(
                label=", ".join(e.get("triggers", ["?"]))[:100],
                value=str(i),
                description=_format_responses(e.get("responses", []), limit=100),
                default=(i == panel.selected),
            )
            for i, e in enumerate(entries[:25])
        ]
        if not options:
            options = [discord.SelectOption(label="(no entries)", value="_none")]
        super().__init__(placeholder="Select an entry to edit / remove",
                         min_values=0, max_values=1, options=options,
                         disabled=not entries, row=0)

    async def callback(self, interaction: discord.Interaction):
        self._panel.selected = int(self.values[0]) if self.values else None
        await self._panel.rerender(interaction)


class AutoResponseView(discord.ui.View):
    """Single-invoker ephemeral panel: entry select + Add/Edit/Remove."""

    def __init__(self, cog: AutoResponse, interaction: discord.Interaction):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild = interaction.guild
        self.invoker_id = interaction.user.id
        self.selected = None
        self.message = None
        self._flash = None
        self._build()

    def entries(self):
        return self.cog._entries(self.guild.id)

    def save(self, entries):
        self.cog.bot.config.set(self.guild.id, "auto_responses", entries)
        self.cog.logger.info(
            f"auto_responses updated in guild {self.guild.id}: {len(entries)} entries")

    def flash(self, text):
        self._flash = text

    def _build(self):
        self.clear_items()
        has_sel = self.selected is not None and self.selected < len(self.entries())
        self.add_item(_EntrySelect(self))
        add_btn = discord.ui.Button(label="➕ Add", style=discord.ButtonStyle.primary, row=1)
        edit_btn = discord.ui.Button(label="✏ Edit", style=discord.ButtonStyle.secondary,
                                     row=1, disabled=not has_sel)
        remove_btn = discord.ui.Button(label="🗑 Remove", style=discord.ButtonStyle.danger,
                                       row=1, disabled=not has_sel)

        async def add_cb(interaction):
            if not is_admin(interaction):
                await interaction.response.send_message("Admins only.", ephemeral=True)
                return
            await interaction.response.send_modal(_EntryModal(self))

        async def edit_cb(interaction):
            if not is_admin(interaction):
                await interaction.response.send_message("Admins only.", ephemeral=True)
                return
            if self.selected is None or self.selected >= len(self.entries()):
                await interaction.response.send_message("Select an entry first.", ephemeral=True)
                return
            await interaction.response.send_modal(_EntryModal(self, index=self.selected))

        async def remove_cb(interaction):
            if not is_admin(interaction):
                await interaction.response.send_message("Admins only.", ephemeral=True)
                return
            entries = self.entries()
            if self.selected is None or self.selected >= len(entries):
                await interaction.response.send_message("Select an entry first.", ephemeral=True)
                return
            removed = entries.pop(self.selected)
            self.save(entries)
            self.selected = None
            self.flash(f"Removed: {', '.join(removed.get('triggers', []))}")
            await self.rerender(interaction)

        add_btn.callback = add_cb
        edit_btn.callback = edit_cb
        remove_btn.callback = remove_cb
        self.add_item(add_btn)
        self.add_item(edit_btn)
        self.add_item(remove_btn)

    def render_embed(self):
        entries = self.entries()
        e = discord.Embed(
            title="Auto-responses",
            description=(f"Exact whole-message triggers for **{self.guild.name}**. "
                         "Responses are picked by relative weight."),
            color=discord.Color.blurple(),
        )
        if entries:
            lines = [
                f"{'▸ ' if i == self.selected else ''}**{', '.join(en.get('triggers', []))}** → "
                f"{_format_responses(en.get('responses', []))}"
                for i, en in enumerate(entries)
            ]
            body = "\n".join(lines)
            e.add_field(name=f"Entries ({len(entries)})",
                        value=body[:1010] + ("\n… (more)" if len(body) > 1010 else ""),
                        inline=False)
        else:
            e.add_field(name="Entries", value="*none — this server has no auto-responses*",
                        inline=False)
        if self._flash:
            e.add_field(name="Last action", value=self._flash[:1024], inline=False)
            self._flash = None
        e.set_footer(text="Panel expires after 3 minutes of inactivity.")
        return e

    async def rerender(self, interaction: discord.Interaction):
        self._build()
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=self.render_embed(), view=self)
        else:
            await interaction.response.edit_message(embed=self.render_embed(), view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "This panel isn't yours — run /autoresponse to open your own.",
                ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(
                    content="Panel expired — run /autoresponse again.", view=self)
            except discord.HTTPException:
                pass


async def setup(bot):
    """Every cog needs a setup function like this."""
    await bot.add_cog(AutoResponse(bot))
