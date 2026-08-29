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

Ops: `list_autoresponses`, `add_autoresponse` and `remove_autoresponse` are
cog-provided behavioral primitives (see docs/cog-development.md). They call
the same services (`_list_entries` / `_add_entry` / `_remove_entry`) the
panel does — the panel holds presentation only, so an agent and an admin
clicking buttons write the identical `auto_responses` shape.
"""
from discord.ext import commands

import discord
from discord import app_commands
import random
import re

from core.ops import OpParam, OpScope, ParamKind, PermissionLevel, op
from core.utils import InvokerOnlyView, is_admin

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


def normalize_triggers(triggers, mode):
    """Trim and (outside regex mode) case-fold trigger strings.

    Regex triggers keep their case: matching is case-insensitive at search
    time, but lowercasing here would mangle patterns like `\\bT\\b` or
    `[A-Z]`. Shared by the panel modal and the add_autoresponse op so both
    write identically-shaped entries."""
    return [t.strip() if mode == MATCH_REGEX else t.strip().lower()
            for t in triggers if str(t).strip()]


def invalid_patterns(triggers):
    """['`pat` (reason)', ...] for triggers that aren't compilable regexes.

    Validation, not matching: `find_response` skips a bad pattern at runtime
    rather than raising, so without this an invalid regex would be stored and
    silently never fire."""
    bad = []
    for pattern in triggers:
        try:
            re.compile(pattern)
        except re.error as exc:
            bad.append(f"`{pattern}` ({exc.msg})")
    return bad


def _serialize_entry_list(result: dict) -> dict:
    """Wire payload for `list_autoresponses`. Without a serializer every
    frontend would see a bare {"ok": true} (Op.serialize_result), so the
    entries have to be copied out explicitly. `index` is positional, not an
    id, and stays an int — it is not a snowflake."""
    entries = list(result.get("entries") or [])
    return {"entries": entries, "count": len(entries)}


def _serialize_entry_change(result: dict) -> dict:
    """Wire payload for `add_autoresponse` / `remove_autoresponse`. `status`
    travels because the agent guidance branches on it, and `count` lets a
    caller see how close the guild is to the 25-entry cap."""
    entry = result.get("entry") or {}
    return {
        "status": result.get("status"),
        "index": result.get("index"),
        "count": result.get("count"),
        "entry": {
            "triggers": list(entry.get("triggers", [])),
            "responses": _response_texts(entry.get("responses")),
            "match": entry_match_mode(entry) if entry else None,
            "auto_delete": bool(entry.get("auto_delete", False)),
        },
    }


class AutoResponse(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.logger = bot.logger

    def _entries(self, guild_id):
        return self.bot.config.get(guild_id, "auto_responses", []) or []

    def _save(self, guild_id, entries):
        self.bot.config.set(guild_id, "auto_responses", entries)
        self.logger.info(
            f"auto_responses updated in guild {guild_id}: {len(entries)} entries")

    # --- services ---------------------------------------------------------
    #
    # Headless logic shared by the panel and the cog ops. Plain values in,
    # plain data out; they never touch an Interaction or send anything —
    # the caller presents the outcome.

    def _list_entries(self, guild_id):
        """Every configured entry, in config order (first match wins, so the
        order IS the precedence the caller needs to see)."""
        return [
            {
                "index": i,
                "triggers": [str(t) for t in entry.get("triggers", [])],
                "responses": _response_texts(entry.get("responses")),
                "match": entry_match_mode(entry),
                "auto_delete": bool(entry.get("auto_delete", False)),
            }
            for i, entry in enumerate(self._entries(guild_id))
        ]

    def _build_entry(self, triggers, responses, match, auto_delete):
        """Validate and normalize one entry. Raises ValueError with a
        caller-presentable message; returns the dict to store."""
        mode = str(match or MATCH_FULL).lower()
        if mode not in (MATCH_FULL, MATCH_CONTAINS, MATCH_REGEX):
            raise ValueError(
                f"match must be one of {MATCH_FULL}, {MATCH_CONTAINS}, {MATCH_REGEX}.")
        trigger_list = normalize_triggers(triggers, mode)
        response_list = [str(r).strip() for r in responses if str(r).strip()]
        if not trigger_list or not response_list:
            raise ValueError("Need at least one trigger and one response.")
        if mode == MATCH_REGEX:
            bad = invalid_patterns(trigger_list)
            if bad:
                raise ValueError("Invalid regex: " + ", ".join(bad))
        return {
            "triggers": trigger_list,
            "responses": response_list,
            "match": mode,
            "auto_delete": bool(auto_delete),
        }

    def _add_entry(self, guild_id, triggers, responses, match=MATCH_FULL,
                   auto_delete=False, index=None):
        """Append a new entry, or overwrite the one at `index`.

        Returns {"status": "added"|"updated", "index": int, "entry": {...},
        "count": int}. Raises ValueError on invalid input or a full table."""
        entry = self._build_entry(triggers, responses, match, auto_delete)
        entries = self._entries(guild_id)
        if index is None:
            if len(entries) >= MAX_ENTRIES:
                raise ValueError(
                    f"Entry cap ({MAX_ENTRIES}) reached — remove one first.")
            entries.append(entry)
            status, position = "added", len(entries) - 1
        else:
            if not 0 <= index < len(entries):
                raise ValueError(f"No entry at index {index}.")
            entries[index] = entry
            status, position = "updated", index
        self._save(guild_id, entries)
        return {"status": status, "index": position, "entry": entry,
                "count": len(entries)}

    def _remove_entry(self, guild_id, index):
        """Drop the entry at `index`. Returns
        {"status": "removed", "index": int, "entry": {...}, "count": int}.
        Raises ValueError when there is nothing at that index."""
        entries = self._entries(guild_id)
        if not 0 <= index < len(entries):
            raise ValueError(f"No entry at index {index}.")
        removed = entries.pop(index)
        self._save(guild_id, entries)
        return {"status": "removed", "index": index, "entry": removed,
                "count": len(entries)}

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
    @app_commands.default_permissions(administrator=True)
    @app_commands.check(is_admin)
    async def autoresponse_slash(self, interaction: discord.Interaction):
        """Ephemeral twin of `!autoresponse` (#76).

        Discord's picker cannot filter on the bot's admin list, so
        visibility is pinned guild-only + Administrator. The real gate is
        `@app_commands.check(is_admin)`. Bot admins without Discord
        Administrator still have `!autoresponse`. The body check stays as
        defense in depth and for the friendly denial."""
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

    # --- ops --------------------------------------------------------------
    #
    # Registered against the live cog instance by LiterallyBot.add_cog and
    # dropped on unload. They call the same services the panel does; neither
    # takes an Interaction.

    @op(
        "list_autoresponses",
        "List this guild's configured auto-responses in config order. The "
        "index of each entry is what remove_autoresponse takes, and the order "
        "is the precedence: the FIRST matching entry wins.",
        PermissionLevel.ADMIN,
        serialize=_serialize_entry_list,
        agent_guidance=(
            "Indexes shift when an entry is removed — list again before a "
            "second removal rather than reusing a stale index."),
        scope=OpScope.GUILD,
        group="auto-response",
    )
    async def op_list_autoresponses(self, ctx) -> dict:
        guild = getattr(ctx, "guild", None)
        if guild is None:
            raise ValueError("list_autoresponses must be called in a guild.")
        return {"entries": self._list_entries(guild.id)}

    @op(
        "add_autoresponse",
        "Add an auto-response to this guild: when a message matches one of "
        "the triggers, the bot replies with one of the responses picked at "
        "random. Appends a new entry; it never edits or replaces an existing "
        "one. Capped at 25 entries per guild.",
        PermissionLevel.ADMIN,
        params=[
            OpParam("triggers", ParamKind.STRING_LIST,
                    "Trigger strings (case-insensitive except in regex mode)."),
            OpParam("responses", ParamKind.STRING_LIST,
                    "Reply texts; one is picked at random per match."),
            OpParam("match", ParamKind.STRING,
                    "Match mode: 'full' (whole message equals a trigger), "
                    "'contains' (trigger appears anywhere), or 'regex' "
                    "(trigger is a case-insensitive Python pattern, e.g. "
                    r"'\bthink\b' for whole words).",
                    required=False, default=MATCH_FULL),
            OpParam("auto_delete", ParamKind.BOOLEAN,
                    "Delete the triggering message as well as replying.",
                    required=False, default=False),
        ],
        serialize=_serialize_entry_change,
        agent_guidance=(
            "'contains' fires on any message containing the trigger, so a "
            "short trigger becomes very noisy — prefer 'full', or 'regex' "
            r"with \bword\b, unless the user asked for substring "
            "matching. An invalid regex is rejected outright, nothing is "
            "stored."),
        scope=OpScope.GUILD,
        group="auto-response",
    )
    async def op_add_autoresponse(self, ctx, triggers, responses,
                                  match: str = MATCH_FULL,
                                  auto_delete: bool = False) -> dict:
        guild = getattr(ctx, "guild", None)
        if guild is None:
            raise ValueError("add_autoresponse must be called in a guild.")
        return self._add_entry(guild.id, list(triggers), list(responses),
                               match=match, auto_delete=bool(auto_delete))

    @op(
        "remove_autoresponse",
        "Remove one of this guild's auto-responses by its index, as reported "
        "by list_autoresponses. Removing shifts every later index down by one.",
        PermissionLevel.ADMIN,
        params=[
            OpParam("index", ParamKind.INTEGER,
                    "Zero-based index from list_autoresponses.", minimum=0),
        ],
        serialize=_serialize_entry_change,
        agent_guidance=(
            "Call list_autoresponses first and remove by the index you just "
            "read — indexes are positional, not stable ids."),
        scope=OpScope.GUILD,
        group="auto-response",
    )
    async def op_remove_autoresponse(self, ctx, index: int) -> dict:
        guild = getattr(ctx, "guild", None)
        if guild is None:
            raise ValueError("remove_autoresponse must be called in a guild.")
        return self._remove_entry(guild.id, int(index))


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
        auto_delete = (self.action.values[0] if self.action.values
                       else self._ACT_REPLY) == self._ACT_DELETE
        # Validation, normalization and the cap check all live in the service
        # the ops call, so the panel and an agent cannot drift apart on what
        # a valid entry is.
        try:
            result = self._panel.cog._add_entry(
                self._panel.guild.id,
                str(self.triggers.value).split(","),
                str(self.responses.value).split(","),
                match=mode, auto_delete=auto_delete, index=self._index)
        except ValueError as e:
            self._panel.flash(f"⚠ {e} — nothing saved.")
            await self._panel.rerender(interaction)
            return
        verb = "Added" if result["status"] == "added" else "Saved"
        self._panel.flash(f"{verb}: {', '.join(result['entry']['triggers'])}")
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
            if self.selected is None or self.selected >= len(self.entries()):
                await interaction.response.send_message("Select an entry first.", ephemeral=True)
                return
            try:
                result = self.cog._remove_entry(self.guild.id, self.selected)
            except ValueError as e:
                await interaction.response.send_message(str(e), ephemeral=True)
                return
            self.selected = None
            self.flash(f"Removed: {', '.join(result['entry'].get('triggers', []))}")
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
