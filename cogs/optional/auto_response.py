"""Config-driven auto-responses: canned replies (and optional auto-delete)
for message patterns — the meme graph and a lightweight keyword automod in
one table.

Guild config key `auto_responses` — list of entries:

    {"triggers": ["ping"],            # aliases, case-insensitive, CSV in UI
     "responses": ["pong"],           # one picked uniformly at random
     "match": "full",                 # full | contains | regex
     "auto_delete": false}            # true = delete the triggering message

`match` replaces the older boolean `full_match`, which is still read for
entries written before the third mode existed (true -> "full", false ->
"contains"). "regex" treats each trigger as a Python pattern, which is how
you get word boundaries: `\bthink\b` fires on "think" but not "rethinking".
An invalid pattern never matches and never raises.

Absent/empty key = inert in that guild (default OFF everywhere; manage with
!autoresponse). First matching entry wins, in config
order. Capped at 25 entries per guild — the panel dropdown's hard limit.

Loop safety: ALL bot-authored messages are ignored (message.author.bot),
not just our own — two bots both running this cog once replied to each
other's replies forever (the cope->seethe incident, 2026-08-07).
"""
from discord.ext import commands

import discord
from discord import app_commands
import random
import re

from core.utils import InvokerOnlyView, app_is_admin, is_admin

MAX_ENTRIES = 25  # Discord select-menu option cap

MATCH_FULL = "full"
MATCH_CONTAINS = "contains"
MATCH_REGEX = "regex"


def _response_texts(responses):
    """Plain response strings; tolerates the short-lived [text, weight]
    shape by taking the text and ignoring the weight."""
    out = []
    for r in responses or []:
        if isinstance(r, (list, tuple)) and r:
            out.append(str(r[0]))
        elif r is not None and str(r).strip():
            out.append(str(r))
    return out


def entry_match_mode(entry):
    """Match mode for an entry, honouring the legacy `full_match` bool."""
    mode = entry.get("match")
    if mode in (MATCH_FULL, MATCH_CONTAINS, MATCH_REGEX):
        return mode
    return MATCH_FULL if entry.get("full_match", True) else MATCH_CONTAINS


def find_response(entries, content):
    """(entry, response) for the first entry matching `content`, else None.

    full   — the whole (case-folded, stripped) message equals a trigger
    contains — a trigger appears anywhere in it
    regex  — a trigger is a case-insensitive pattern searched against it

    Pure function of (config entries, message text) so the rules stay
    unit-testable. A malformed regex is skipped rather than raised: a bad
    pattern in one guild's config must not break message handling."""
    text = content.strip().lower()
    for entry in entries:
        triggers = [str(t) for t in entry.get("triggers", [])]
        texts = _response_texts(entry.get("responses"))
        if not triggers or not texts:
            continue
        mode = entry_match_mode(entry)
        if mode == MATCH_REGEX:
            hit = False
            for pattern in triggers:
                try:
                    if re.search(pattern, content, re.IGNORECASE):
                        hit = True
                        break
                except re.error:
                    continue
        elif mode == MATCH_CONTAINS:
            hit = any(t.lower() in text for t in triggers)
        else:
            hit = text in [t.lower() for t in triggers]
        if hit:
            return entry, random.choice(texts)
    return None


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
        found = find_response(entries, message.content)
        if not found:
            return
        entry, response = found
        if entry.get("auto_delete"):
            try:
                await message.delete()
            except (discord.Forbidden, discord.HTTPException) as e:
                self.logger.warning(
                    f"auto_responses: couldn't delete message {message.id} "
                    f"in guild {message.guild.id}: {e}")
        await message.channel.send(response)

    # ---- admin UI -------------------------------------------------------

    @app_commands.command(name="autoresponse",
                          description="Open the auto-response panel")
    @app_commands.guild_only()
    @app_is_admin()
    async def autoresponse_slash(self, interaction: discord.Interaction):
        """Ephemeral twin of `!autoresponse` (#76). The gate is the bot's own
        admin concept, not Discord permissions, so it rides on @app_is_admin
        rather than default_permissions — that keeps the decorator
        authoritative for both enforcement and /help visibility. The body
        check stays as defense in depth and for the friendly denial."""
        if not is_admin(interaction):
            await interaction.response.send_message(
                "Requires admin.", ephemeral=True)
            return
        view = AutoResponseView(self, interaction.user, interaction.guild)
        await interaction.response.send_message(
            embed=view.render_embed(), view=view, ephemeral=True)
        view.message = None

    @commands.command(name="autoresponse", hidden=True)
    @commands.guild_only()
    @commands.check(is_admin)
    async def autoresponse_prefix(self, ctx):
        """Open the auto-response panel. Posts PUBLICLY — use
        /autoresponse for a private one (#76)."""
        view = AutoResponseView(self, ctx.author, ctx.guild)
        view.message = await ctx.send(embed=view.render_embed(), view=view)


class _EntryModal(discord.ui.Modal):
    """Add a new entry, or edit the one selected in the panel."""

    _MATCH_FULL = MATCH_FULL
    _MATCH_CONTAINS = MATCH_CONTAINS
    _MATCH_REGEX = MATCH_REGEX
    _ACT_REPLY = "reply"
    _ACT_DELETE = "reply_delete"

    def __init__(self, panel: "AutoResponseView", *, index=None):
        self._panel = panel
        self._index = index
        if index is None:
            super().__init__(title="Add auto-response")
            entry = {}
        else:
            entry = panel.entries()[index]
            super().__init__(title=f"Edit: {', '.join(entry.get('triggers', []))}"[:45])
        self.triggers = discord.ui.TextInput(
            label="Triggers (comma-separated)",
            required=True, max_length=200,
            default=", ".join(entry.get("triggers", [])),
            placeholder="word, phrase   (regex mode: \\bword\\b)")
        self.responses = discord.ui.TextInput(
            label="Responses (comma-separated, one is picked)",
            style=discord.TextStyle.paragraph,
            required=True, max_length=1000,
            default=", ".join(_response_texts(entry.get("responses"))),
            placeholder="reply text, another reply")
        mode = entry_match_mode(entry) if entry else self._MATCH_FULL
        self.match = discord.ui.Select(min_values=1, max_values=1, options=[
            discord.SelectOption(
                label="Full message", value=self._MATCH_FULL,
                description="Fires only when the whole message equals a trigger",
                default=mode == self._MATCH_FULL),
            discord.SelectOption(
                label="Contains", value=self._MATCH_CONTAINS,
                description="Fires when a trigger appears anywhere (keyword/automod)",
                default=mode == self._MATCH_CONTAINS),
            discord.SelectOption(
                label="Regex", value=self._MATCH_REGEX,
                description="Trigger is a pattern; \\bword\\b matches whole words only",
                default=mode == self._MATCH_REGEX),
        ])
        delete = bool(entry.get("auto_delete", False))
        self.action = discord.ui.Select(min_values=1, max_values=1, options=[
            discord.SelectOption(
                label="Reply", value=self._ACT_REPLY,
                description="Post the response, leave the message",
                default=not delete),
            discord.SelectOption(
                label="Reply + delete their message", value=self._ACT_DELETE,
                description="Needs the bot to have Manage Messages",
                default=delete),
        ])
        self.add_item(self.triggers)
        self.add_item(self.responses)
        self.add_item(discord.ui.Label(text="Matching", component=self.match))
        self.add_item(discord.ui.Label(text="On match", component=self.action))

    async def on_submit(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return
        mode = (self.match.values[0] if self.match.values else self._MATCH_FULL)
        # Regex triggers keep their case: matching is case-insensitive at search
        # time, but lowercasing here would mangle patterns like \bT\b or [A-Z].
        trigger_list = [t.strip() if mode == self._MATCH_REGEX else t.strip().lower()
                        for t in str(self.triggers.value).split(",") if t.strip()]
        if mode == self._MATCH_REGEX:
            bad = []
            for pattern in trigger_list:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    bad.append(f"`{pattern}` ({exc.msg})")
            if bad:
                self._panel.flash("⚠ Invalid regex — nothing saved: " + ", ".join(bad))
                await self._panel.rerender(interaction)
                return
        response_list = [r.strip()
                         for r in str(self.responses.value).split(",") if r.strip()]
        if not trigger_list or not response_list:
            self._panel.flash("⚠ Need at least one trigger and one response — nothing saved.")
            await self._panel.rerender(interaction)
            return
        entry = {
            "triggers": trigger_list,
            "responses": response_list,
            "match": mode,
            "auto_delete": (self.action.values[0] if self.action.values
                            else self._ACT_REPLY) == self._ACT_DELETE,
        }
        entries = self._panel.entries()
        if self._index is None:
            if len(entries) >= MAX_ENTRIES:
                self._panel.flash(f"⚠ Entry cap ({MAX_ENTRIES}) reached — remove one first.")
                await self._panel.rerender(interaction)
                return
            entries.append(entry)
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
                description=", ".join(_response_texts(e.get("responses")))[:100],
                default=(i == panel.selected),
            )
            for i, e in enumerate(entries[:MAX_ENTRIES])
        ]
        if not options:
            options = [discord.SelectOption(label="(no entries)", value="_none")]
        super().__init__(placeholder="Select an entry to edit / remove",
                         min_values=0, max_values=1, options=options,
                         disabled=not entries, row=0)

    async def callback(self, interaction: discord.Interaction):
        self._panel.selected = int(self.values[0]) if self.values else None
        await self._panel.rerender(interaction)


class AutoResponseView(InvokerOnlyView, discord.ui.View):
    """Single-invoker panel: entry select + Add/Edit/Remove."""

    panel_command = "!autoresponse"

    def __init__(self, cog: AutoResponse, user, guild):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild = guild
        self.invoker_id = user.id
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
            if len(self.entries()) >= MAX_ENTRIES:
                await interaction.response.send_message(
                    f"Entry cap ({MAX_ENTRIES}) reached — remove one first.",
                    ephemeral=True)
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
            description=(f"Triggers for **{self.guild.name}** — full-message "
                         "or contains matching, optional auto-delete."),
            color=discord.Color.blurple(),
        )
        if entries:
            lines = []
            for en in entries:
                tags = []
                mode = entry_match_mode(en)
                if mode != MATCH_FULL:
                    tags.append(mode)
                if en.get("auto_delete"):
                    tags.append("deletes")
                tag_str = f" ({', '.join(tags)})" if tags else ""
                lines.append(
                    f"**{', '.join(en.get('triggers', []))}**{tag_str} → "
                    f"{', '.join(_response_texts(en.get('responses')))[:80]}")
            body = "\n".join(lines)
            e.add_field(name=f"Entries ({len(entries)}/{MAX_ENTRIES})",
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


async def setup(bot):
    """Every cog needs a setup function like this."""
    await bot.add_cog(AutoResponse(bot))
