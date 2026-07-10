"""AI settings panel — the interactive `/ai settings` surface.

This is where new AI-admin UX lands (per CLAUDE.md, gpt.py is a parked seam).
It owns:

- `AiSettingsView` + its Selects/Modals: a single ephemeral, tabbed panel that
  collapses the scattered per-server `/ai` controls (provider, model,
  personality, nickname) plus the two tool allowlists (per-guild bot tools and
  the global MCP tool set) into one place.
- The one-shot migration from the removed `gpt_agentic_enabled` flag to the new
  per-guild `bot_tools_enabled` allowlist.

Config model (see cogs/dynamic/gpt.py for the consuming side):
- `bot_tools_enabled`  (guild scope, list[str]) — which ops the in-bot agent
  may call. Empty/absent => plain chat. Subset of `AGENT_OPS`.
- `mcp_tools_enabled`  (global scope, list[str]) — which ops the MCP server
  exposes to external services. Absent => the full `_EXPOSED_OPS` universe.

Auth: opening the panel needs `is_admin`. The Server page (provider/model/
personality/nickname) is admin-editable; the Bot-tools and MCP-tools pages are
superadmin-only (they set guild/global policy), and every tool-editing callback
re-checks `is_superadmin` server-side — `disabled=True` is only cosmetic.
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from core.utils import is_admin, is_superadmin
from core.agent_loop import AGENT_OPS
from mcp_ops.server import _EXPOSED_OPS
from core.llm.usage import _PRICING_USD_PER_MTOK
from cogs.dynamic.gpt import cooldown_tier_for_cost

# Output-token prices ($/Mtok) for models the usage.py table doesn't cover, so
# the one-time cost-seed can tier them correctly instead of defaulting to
# pricy. Local models are free -> cheap tier.
_EXTRA_OUTPUT_PRICES = {
    "xai": {"grok-4.5": 6.0},            # verified 2026-07 (x.ai): $2 in / $6 out
    "ollama": {"__all__": 0.0},          # local, free -> cheap tier
}


def _known_output_price(provider_id, model_name):
    """Best-effort $/Mtok output for a model, or None if unknown.

    Prefers an explicit extras entry, then a longest-prefix match in the
    shared usage.py pricing table (same matching estimate_cost uses)."""
    extras = _EXTRA_OUTPUT_PRICES.get(provider_id, {})
    if "__all__" in extras:
        return extras["__all__"]
    if model_name in extras:
        return extras[model_name]
    table = _PRICING_USD_PER_MTOK.get(provider_id, {})
    if model_name in table:
        return table[model_name][1]
    matches = [(k, v) for k, v in table.items() if model_name.startswith(k)]
    if matches:
        return max(matches, key=lambda kv: len(kv[0]))[1][1]
    return None

# send_message is deliberately NOT default-on for the in-bot agent (it can post
# into arbitrary channels); it stays available to the MCP surface. This only
# affects the migration seed and the panel's "read-only preset" button.
_BOT_READONLY_OPS = ("add_reaction", "search_history", "list_channels", "list_members")
# What an old `gpt_agentic_enabled=True` guild is migrated to: everything the
# agent loop offers EXCEPT send_message (matches the pre-migration behavior
# minus the never-defaulted broadcast tool).
AGENT_OPS_DEFAULT_ON = tuple(op for op in AGENT_OPS if op != "send_message")

PANEL_TIMEOUT = 180


class _ToolSelect(discord.ui.Select):
    """A multi-select over a fixed op universe, wired to save on change.

    `universe` is the full list of selectable op names; `current` is the
    subset currently enabled. `on_save(interaction, selected)` persists the
    new list and is expected to re-render the panel.
    """

    def __init__(self, universe, current, on_save, *, row=1):
        self._on_save = on_save
        current_set = set(current)
        options = [
            discord.SelectOption(label=name, value=name, default=(name in current_set))
            for name in universe
        ]
        super().__init__(
            placeholder="Select enabled tools (none = off)",
            min_values=0,
            max_values=len(options),
            options=options,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        await self._on_save(interaction, list(self.values))


class _ProviderSelect(discord.ui.Select):
    def __init__(self, view: "AiSettingsView", *, row=1):
        self._panel = view
        providers = view.gpt.llm.get_all_providers()
        current = view.provider
        options = [
            discord.SelectOption(
                label=info.get("name", pid), value=pid, description=pid,
                default=(pid == current),
            )
            for pid, info in list(providers.items())[:25]
        ]
        super().__init__(placeholder="AI provider", min_values=1, max_values=1,
                         options=options, row=row)

    async def callback(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message("Requires admin.", ephemeral=True)
            return
        # _do_setprovider resets the model to the provider default.
        self._panel.gpt._do_setprovider(interaction, self.values[0])
        self._panel.refresh_state()
        await self._panel.rerender(interaction)


class _ModelSelect(discord.ui.Select):
    def __init__(self, view: "AiSettingsView", *, row=2):
        self._panel = view
        providers = view.gpt.llm.get_all_providers()
        info = providers.get(view.provider, {})
        models = list(info.get("models", {}).keys())
        current = view.model
        if models:
            options = [
                discord.SelectOption(label=m, value=m, default=(m == current))
                for m in models[:25]
            ]
            disabled = False
        else:
            options = [discord.SelectOption(label="(no models)", value="_none")]
            disabled = True
        super().__init__(placeholder="Model", min_values=1, max_values=1,
                         options=options, row=row, disabled=disabled)

    async def callback(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message("Requires admin.", ephemeral=True)
            return
        self._panel.gpt._do_setmodel(interaction, self.values[0])
        self._panel.refresh_state()
        await self._panel.rerender(interaction)


class _PersonalityModal(discord.ui.Modal, title="Set AI personality"):
    def __init__(self, view: "AiSettingsView"):
        super().__init__()
        self._panel = view
        self.prompt = discord.ui.TextInput(
            label="Personality prompt",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=2000,
            default=view.current_personality() or "",
        )
        self.add_item(self.prompt)

    async def on_submit(self, interaction: discord.Interaction):
        self._panel.gpt._do_setpersonality(interaction, str(self.prompt.value))
        self._panel.refresh_state()
        await self._panel.rerender(interaction)


class _NicknameModal(discord.ui.Modal, title="Set bot nickname"):
    def __init__(self, view: "AiSettingsView"):
        super().__init__()
        self._panel = view
        self.nickname = discord.ui.TextInput(
            label="New nickname", required=True, max_length=32)
        self.add_item(self.nickname)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await interaction.guild.me.edit(nick=str(self.nickname.value))
        except discord.Forbidden:
            await interaction.response.send_message(
                "I don't have permission to change my nickname here.", ephemeral=True)
            return
        await self._panel.rerender(interaction)


class AiSettingsView(discord.ui.View):
    """Tabbed, ephemeral, single-invoker settings panel.

    Pages: "server" (admin), "bot" tools (superadmin), "mcp" tools (superadmin).
    Row 0 is always the page tabs; rows 1+ are rebuilt per page on each render.
    """

    def __init__(self, gpt_cog, interaction: discord.Interaction):
        super().__init__(timeout=PANEL_TIMEOUT)
        self.gpt = gpt_cog
        self.bot = gpt_cog.bot
        self.invoker_id = interaction.user.id
        self.guild = interaction.guild
        self.page = "server"
        self.message = None
        self.refresh_state()
        self._build()

    # --- state -----------------------------------------------------------
    def refresh_state(self):
        pc = self.gpt.get_provider_config(self._cfg_ctx())
        self.provider = pc["provider"]
        self.model = pc["model"]

    def _cfg_ctx(self):
        """Config resolves guild scope from a bare guild id (int)."""
        return self.guild.id if self.guild else None

    def current_personality(self):
        data = self.bot.config.get(self._cfg_ctx(), "gpt_personality_data")
        if isinstance(data, dict):
            return data.get("prompt")
        return None

    def _bot_tools(self):
        raw = self.bot.config.get(self._cfg_ctx(), "bot_tools_enabled") or []
        return [n for n in raw if n in AGENT_OPS]

    def _mcp_tools(self):
        raw = self.bot.config.get_global("mcp_tools_enabled")
        if raw is None:
            return list(_EXPOSED_OPS)
        return [n for n in raw if n in _EXPOSED_OPS]

    # --- rendering -------------------------------------------------------
    def _tab_button(self, label, page):
        style = (discord.ButtonStyle.primary if self.page == page
                 else discord.ButtonStyle.secondary)
        btn = discord.ui.Button(label=label, style=style, row=0)

        async def cb(interaction: discord.Interaction, _page=page):
            self.page = _page
            self._build()
            await self.rerender(interaction)

        btn.callback = cb
        return btn

    def _build(self):
        self.clear_items()
        self.add_item(self._tab_button("⚙ Server", "server"))
        self.add_item(self._tab_button("🤖 Bot tools", "bot"))
        self.add_item(self._tab_button("🌐 MCP tools", "mcp"))

        if self.page == "server":
            self.add_item(_ProviderSelect(self, row=1))
            self.add_item(_ModelSelect(self, row=2))
            self.add_item(self._personality_button())
            self.add_item(self._nickname_button())
        elif self.page == "bot":
            self.add_item(_ToolSelect(list(AGENT_OPS), self._bot_tools(),
                                      self._save_bot_tools, row=1))
            self.add_item(self._preset_button("Clear all (plain chat)",
                                              self._save_bot_tools, []))
            self.add_item(self._preset_button("Enable read-only set",
                                              self._save_bot_tools,
                                              list(_BOT_READONLY_OPS)))
        elif self.page == "mcp":
            self.add_item(_ToolSelect(list(_EXPOSED_OPS), self._mcp_tools(),
                                      self._save_mcp_tools, row=1))
            self.add_item(self._preset_button("Clear all", self._save_mcp_tools, []))
            self.add_item(self._preset_button("Enable read-only set",
                                              self._save_mcp_tools,
                                              [o for o in _EXPOSED_OPS
                                               if o.startswith(("search", "list"))]))

    def _personality_button(self):
        btn = discord.ui.Button(label="✏ Personality",
                                style=discord.ButtonStyle.secondary, row=3)

        async def cb(interaction: discord.Interaction):
            if not is_admin(interaction):
                await interaction.response.send_message("Requires admin.", ephemeral=True)
                return
            await interaction.response.send_modal(_PersonalityModal(self))

        btn.callback = cb
        return btn

    def _nickname_button(self):
        btn = discord.ui.Button(label="🏷 Nickname",
                                style=discord.ButtonStyle.secondary, row=3)

        async def cb(interaction: discord.Interaction):
            if not is_admin(interaction):
                await interaction.response.send_message("Requires admin.", ephemeral=True)
                return
            if not self.guild:
                await interaction.response.send_message(
                    "Nickname can only be set in a server.", ephemeral=True)
                return
            await interaction.response.send_modal(_NicknameModal(self))

        btn.callback = cb
        return btn

    def _preset_button(self, label, saver, value):
        btn = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, row=2)

        async def cb(interaction: discord.Interaction):
            await saver(interaction, value)

        btn.callback = cb
        return btn

    # --- saves (superadmin-gated) ----------------------------------------
    async def _save_bot_tools(self, interaction: discord.Interaction, selected):
        if not is_superadmin(interaction):
            await interaction.response.send_message(
                "Requires superadmin.", ephemeral=True)
            return
        cleaned = [n for n in selected if n in AGENT_OPS]
        self.bot.config.set(self._cfg_ctx(), "bot_tools_enabled", cleaned)
        await self.rerender(interaction)

    async def _save_mcp_tools(self, interaction: discord.Interaction, selected):
        if not is_superadmin(interaction):
            await interaction.response.send_message(
                "Requires superadmin.", ephemeral=True)
            return
        cleaned = [n for n in selected if n in _EXPOSED_OPS]
        self.bot.config.set_global("mcp_tools_enabled", cleaned)
        # Rebuild so the Select's checkmarks reflect the saved set, and note
        # that MCP changes only bind on the next bot restart.
        self._build()
        await interaction.response.edit_message(embed=self._embed(mcp_note=True),
                                                 view=self)

    # --- embed -----------------------------------------------------------
    def _embed(self, mcp_note=False):
        model_info = self.gpt._current_model_info(self._cfg_ctx())
        tier, cd = cooldown_tier_for_cost(model_info.get("cost_per_mtok_output"))
        bot_tools = self._bot_tools()
        mcp_tools = self._mcp_tools()
        e = discord.Embed(
            title="AI settings",
            description=f"Server: **{self.guild.name}**" if self.guild else "DM",
            color=discord.Color.blurple(),
        )
        e.add_field(name="Provider / Model",
                    value=f"{self.provider} / **{self.model}**", inline=True)
        e.add_field(name="Cooldown", value=f"{cd}s per msg ({tier})", inline=True)
        e.add_field(
            name="Bot tools (this server)",
            value=(", ".join(bot_tools) if bot_tools else "*none — plain chat*"),
            inline=False,
        )
        e.add_field(
            name="MCP tools (global)",
            value=(", ".join(mcp_tools) if mcp_tools else "*none*"),
            inline=False,
        )
        if mcp_note:
            e.set_footer(text="MCP changes take effect on next bot restart.")
        else:
            e.set_footer(text="Panel expires after 3 minutes of inactivity.")
        return e

    async def rerender(self, interaction: discord.Interaction):
        self._build()
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=self._embed(), view=self)
        else:
            await interaction.response.edit_message(embed=self._embed(), view=self)

    # --- lifecycle -------------------------------------------------------
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "This panel isn't yours — run `/ai settings` to open your own.",
                ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(
                    content="Panel expired — run `/ai settings` again.", view=self)
            except discord.HTTPException:
                pass


async def open_ai_settings(gpt_cog, interaction: discord.Interaction):
    """Entry point invoked by the `/ai settings` command in the Gpt cog."""
    if not is_admin(interaction):
        await interaction.response.send_message(
            "You do not have permission to use this command.", ephemeral=True)
        return
    view = AiSettingsView(gpt_cog, interaction)
    await interaction.response.send_message(embed=view._embed(), view=view,
                                            ephemeral=True)
    view.message = await interaction.original_response()


class AiAdmin(commands.Cog):
    """Holds the `gpt_agentic_enabled` -> `bot_tools_enabled` migration.

    The panel View classes live at module scope (imported by the Gpt cog's
    `/ai settings` command); this cog exists to run the one-shot migration on
    load. The migration lives here, NOT in core/config.py, because that config
    module is shared with the conditioner bot and must stay bot-agnostic.
    """

    def __init__(self, bot):
        self.bot = bot
        self.logger = bot.logger

    async def cog_load(self):
        self._migrate_agentic_flag()
        self._seed_model_costs()

    def _migrate_agentic_flag(self):
        """Map the removed per-guild agentic flag onto the tool allowlist.

        Idempotent: guarded on `bot_tools_enabled` already existing, and the
        old key is removed either way, so a second run is a no-op.
        """
        config = self.bot.config
        migrated = 0
        for cfg_id in list(config._configs.keys()):
            if not cfg_id.isdigit():          # guild files are "<id>.json"
                continue
            cfg = config._configs[cfg_id]
            if "gpt_agentic_enabled" not in cfg:
                continue
            gid = int(cfg_id)
            if "bot_tools_enabled" not in cfg and cfg.get("gpt_agentic_enabled"):
                config.set(gid, "bot_tools_enabled", list(AGENT_OPS_DEFAULT_ON))
                migrated += 1
            config.rem(gid, "gpt_agentic_enabled")
        if migrated or config._dirty_configs:
            config.flush()                    # beat the delayed-save timer
        if migrated:
            self.logger.info(
                "ai_admin: migrated %d guild(s) from gpt_agentic_enabled to "
                "bot_tools_enabled=%s", migrated, list(AGENT_OPS_DEFAULT_ON))

    def _seed_model_costs(self):
        """Backfill `cost_per_mtok_output` on existing models from known prices.

        Without this, every model configured before the cost field existed
        would fall through to the pricy (300s) default. Idempotent: only fills
        models that lack the field and whose price is known; unknown models are
        left unset (still pricy) for an operator to annotate via !addmodel."""
        config = self.bot.config
        providers = config.get_global("ai_providers")
        if not providers:
            return                            # running on built-in defaults
        seeded = 0
        for pid, pinfo in providers.items():
            for model_name, mcfg in pinfo.get("models", {}).items():
                if not isinstance(mcfg, dict) or "cost_per_mtok_output" in mcfg:
                    continue
                price = _known_output_price(pid, model_name)
                if price is not None:
                    mcfg["cost_per_mtok_output"] = price
                    seeded += 1
        if seeded:
            config.set_global("ai_providers", providers)
            config.flush()
            self.logger.info("ai_admin: seeded cost_per_mtok_output on %d model(s)",
                             seeded)


async def setup(bot):
    await bot.add_cog(AiAdmin(bot))
