"""AI settings panel — the interactive `/ai settings` surface.

This is where new AI-admin UX lands (per CLAUDE.md, gpt.py is a parked seam).
It owns:

- `AiSettingsView` + its Selects/Modals: a single ephemeral, tabbed panel that
  collapses the scattered per-server `/ai` controls (provider, model,
  personality, nickname), the global provider/model catalog CRUD (add/edit/
  remove providers and models, API keys — the Providers tab), and the two tool
  allowlists (per-guild bot tools and the global MCP tool set) into one place.
- The one-shot migration from the removed `gpt_agentic_enabled` flag to the new
  per-guild `bot_tools_enabled` allowlist.

Config model (see cogs/dynamic/gpt.py for the consuming side):
- `bot_tools_enabled`  (guild scope, list[str]) — which ops the in-bot agent
  may call. Empty/absent => plain chat. Subset of `AGENT_OPS`.
- `mcp_tools_enabled`  (global scope, list[str]) — which ops the MCP server
  exposes to external services. Absent => the full `_EXPOSED_OPS` universe.

Auth: opening the panel needs `is_admin`. The Server page (provider/model/
personality/nickname) is admin-editable; the Providers, Bot-tools and MCP-tools
pages are superadmin-only for mutations (they set global/guild policy), and
every mutating callback re-checks `is_superadmin` server-side —
`disabled=True` is only cosmetic.
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from core.utils import is_admin, is_superadmin
from core.agent_loop import AGENT_OPS
from mcp_ops.server import _EXPOSED_OPS
from core.llm.usage import _PRICING_USD_PER_MTOK
from cogs.dynamic.gpt import cooldown_tier_for_cost, COOLDOWN_TIERS

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


class _MgmtProviderSelect(discord.ui.Select):
    """Providers-tab browse select: which provider is being managed. Distinct
    from the Server tab's _ProviderSelect, which switches the guild's ACTIVE
    provider — this one only changes what the CRUD controls point at."""

    def __init__(self, view: "AiSettingsView", *, row=1):
        self._panel = view
        providers = view.gpt.llm.get_all_providers()
        options = [
            discord.SelectOption(
                label=info.get("name", pid), value=pid, description=pid,
                default=(pid == view.mgmt_provider),
            )
            for pid, info in list(providers.items())[:25]
        ]
        super().__init__(placeholder="Provider to manage", min_values=1,
                         max_values=1, options=options, row=row)

    async def callback(self, interaction: discord.Interaction):
        self._panel.mgmt_provider = self.values[0]
        self._panel.mgmt_model = None
        await self._panel.rerender(interaction)


class _MgmtModelSelect(discord.ui.Select):
    """Providers-tab browse select: which model the edit/remove/default
    buttons target. min_values=0 so it can be deselected."""

    def __init__(self, view: "AiSettingsView", *, row=2):
        self._panel = view
        providers = view.gpt.llm.get_all_providers()
        models = list(providers.get(view.mgmt_provider, {}).get("models", {}).keys())
        if models:
            options = [
                discord.SelectOption(label=m, value=m,
                                     default=(m == view.mgmt_model))
                for m in models[:25]
            ]
            disabled = False
        else:
            options = [discord.SelectOption(label="(no models)", value="_none")]
            disabled = True
        super().__init__(placeholder="Model to edit / remove / make default",
                         min_values=0, max_values=1, options=options, row=row,
                         disabled=disabled)

    async def callback(self, interaction: discord.Interaction):
        self._panel.mgmt_model = self.values[0] if self.values else None
        await self._panel.rerender(interaction)


class _AddProviderModal(discord.ui.Modal, title="Add OpenAI-compatible provider"):
    def __init__(self, view: "AiSettingsView"):
        super().__init__()
        self._panel = view
        self.provider_id = discord.ui.TextInput(
            label="Provider id (short, e.g. groq)", required=True, max_length=32)
        self.base_url = discord.ui.TextInput(
            label="API base URL", required=True, max_length=200,
            placeholder="https://api.groq.com/openai/v1")
        self.default_model = discord.ui.TextInput(
            label="Default model id", required=True, max_length=100)
        self.display_name = discord.ui.TextInput(
            label="Display name (optional)", required=False, max_length=64)
        for item in (self.provider_id, self.base_url, self.default_model,
                     self.display_name):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        if not is_superadmin(interaction):
            await interaction.response.send_message("Requires superadmin.", ephemeral=True)
            return
        pid = str(self.provider_id.value).strip().lower()
        result = self._panel.gpt._do_addprovider(
            interaction, pid, str(self.base_url.value).strip(),
            str(self.default_model.value).strip(),
            str(self.display_name.value).strip() or None)
        if result.startswith("Added"):
            self._panel.mgmt_provider = pid
            self._panel.mgmt_model = None
        self._panel.flash(result)
        await self._panel.rerender(interaction)


class _ModelModal(discord.ui.Modal):
    """Add a model, or edit the cost/max_tokens of an existing one.

    Modals have no numeric fields, so cost/max_tokens are TextInputs parsed
    with try/except; blank clears the field (cost falls back to the pricy
    tier, max_tokens to the provider default)."""

    def __init__(self, view: "AiSettingsView", *, edit: bool):
        self._panel = view
        self._edit = edit
        if edit:
            super().__init__(title=f"Edit {view.mgmt_model}"[:45])
            providers = view.gpt.llm.get_all_providers()
            mcfg = providers.get(view.mgmt_provider, {}).get("models", {}).get(view.mgmt_model, {})
            if not isinstance(mcfg, dict):
                mcfg = {}
            cost_default = mcfg.get("cost_per_mtok_output")
            tokens_default = mcfg.get("max_completion_tokens")
        else:
            super().__init__(title=f"Add model to {view.mgmt_provider}"[:45])
            cost_default = None
            tokens_default = None
            self.model_name = discord.ui.TextInput(
                label="Model id (as the provider API expects)",
                required=True, max_length=100)
            self.add_item(self.model_name)
        self.cost = discord.ui.TextInput(
            label="Cost $/Mtok output (blank = pricy tier)", required=False,
            max_length=16, default="" if cost_default is None else f"{cost_default:g}")
        self.max_tokens = discord.ui.TextInput(
            label="Max completion tokens (blank = default)", required=False,
            max_length=16, default="" if tokens_default is None else str(tokens_default))
        self.add_item(self.cost)
        self.add_item(self.max_tokens)

    async def on_submit(self, interaction: discord.Interaction):
        if not is_superadmin(interaction):
            await interaction.response.send_message("Requires superadmin.", ephemeral=True)
            return
        try:
            cost = float(str(self.cost.value).strip()) if str(self.cost.value).strip() else None
            max_tokens = int(str(self.max_tokens.value).strip()) if str(self.max_tokens.value).strip() else None
        except ValueError:
            self._panel.flash("⚠ Cost must be a number and max tokens an integer — nothing saved.")
            await self._panel.rerender(interaction)
            return
        gpt = self._panel.gpt
        if self._edit:
            result = gpt._do_editmodel(self._panel.mgmt_model,
                                       self._panel.mgmt_provider, cost, max_tokens)
        else:
            name = str(self.model_name.value).strip()
            result = gpt._do_addmodel(interaction, name,
                                      self._panel.mgmt_provider, cost, max_tokens)
            if result.startswith("Added"):
                self._panel.mgmt_model = name
        self._panel.flash(result)
        await self._panel.rerender(interaction)


class _ApiKeyModal(discord.ui.Modal, title="Set provider API key"):
    """Key entry via modal — the value never appears in any channel, and the
    panel (like the whole flow) is ephemeral."""

    def __init__(self, view: "AiSettingsView"):
        super().__init__()
        self._panel = view
        self.api_key = discord.ui.TextInput(
            label=f"API key for {view.mgmt_provider}"[:45], required=True,
            max_length=400)
        self.add_item(self.api_key)

    async def on_submit(self, interaction: discord.Interaction):
        if not is_superadmin(interaction):
            await interaction.response.send_message("Requires superadmin.", ephemeral=True)
            return
        # Model discovery is a network call — defer (update-message style) so
        # the 3s interaction window can't expire under it.
        await interaction.response.defer()
        try:
            lines = await self._panel.gpt._do_setapikey(
                self._panel.mgmt_provider, str(self.api_key.value).strip())
        except ValueError as e:
            lines = [str(e)]
        self._panel.flash(" ".join(lines))
        await self._panel.rerender(interaction)


class _RemoveProviderModal(discord.ui.Modal, title="Remove provider"):
    """Typed-confirmation gate: dropping a provider discards every model under
    it plus its stored key, so a stray click must not be enough."""

    def __init__(self, view: "AiSettingsView"):
        super().__init__()
        self._panel = view
        self.confirm = discord.ui.TextInput(
            label=f"Type '{view.mgmt_provider}' to confirm"[:45],
            required=True, max_length=64)
        self.add_item(self.confirm)

    async def on_submit(self, interaction: discord.Interaction):
        if not is_superadmin(interaction):
            await interaction.response.send_message("Requires superadmin.", ephemeral=True)
            return
        target = self._panel.mgmt_provider
        if str(self.confirm.value).strip().lower() != target:
            self._panel.flash("Confirmation text didn't match — nothing removed.")
            await self._panel.rerender(interaction)
            return
        result = self._panel.gpt._do_removeprovider(interaction, target)
        if result.startswith("Removed"):
            self._panel.mgmt_provider = None  # refresh_state picks a survivor
            self._panel.mgmt_model = None
        self._panel.flash(result)
        await self._panel.rerender(interaction)


def _clip_1024(text: str) -> str:
    """Keep an embed field value under Discord's 1024-char cap."""
    return text if len(text) <= 1024 else text[:1010] + "\n… (more)"


def _fmt_secs(s: float) -> str:
    """Humanize a period: 45s, 7.5min, 2.1h."""
    if s < 120:
        return f"{s:g}s"
    if s < 7200:
        return f"{s / 60:g}min"
    return f"{s / 3600:.1f}h"


class _TierBasesModal(discord.ui.Modal, title="Cooldown tier base periods"):
    """One base period x per cost tier; the window ladder scales off it."""

    def __init__(self, view: "AiSettingsView"):
        super().__init__()
        self._panel = view
        bases, _ = view.gpt.cooldown_config()
        self.inputs = {}
        for label, _bound, _default in COOLDOWN_TIERS:
            ti = discord.ui.TextInput(
                label=f"{label} tier base seconds (x)",
                required=True, max_length=10, default=f"{bases[label]:g}")
            self.inputs[label] = ti
            self.add_item(ti)

    async def on_submit(self, interaction: discord.Interaction):
        if not is_superadmin(interaction):
            await interaction.response.send_message("Requires superadmin.", ephemeral=True)
            return
        try:
            new_bases = {label: float(str(ti.value).strip())
                         for label, ti in self.inputs.items()}
        except ValueError:
            self._panel.flash("⚠ Base periods must be numbers — nothing saved.")
            await self._panel.rerender(interaction)
            return
        if any(v < 0 for v in new_bases.values()):
            self._panel.flash("⚠ Base periods can't be negative — nothing saved.")
            await self._panel.rerender(interaction)
            return
        self._panel.bot.config.set(None, "cooldown_tier_bases", new_bases, scope="global")
        self._panel.flash("Tier base periods updated (0 disables rate limiting for a tier).")
        await self._panel.rerender(interaction)


class _WindowsModal(discord.ui.Modal, title="Cooldown window ladder"):
    """The stacked quotas, as `count:period_mult` pairs relative to the tier
    base x — e.g. `1:1, 10:15, 100:150` = 1 msg per x AND 10 per 15x AND
    100 per 150x. Every window must have room for a message to pass."""

    def __init__(self, view: "AiSettingsView"):
        super().__init__()
        self._panel = view
        _, windows = view.gpt.cooldown_config()
        self.spec = discord.ui.TextInput(
            label="count:mult pairs, comma-separated",
            required=True, max_length=100,
            default=", ".join(f"{c}:{m:g}" for c, m in windows))
        self.add_item(self.spec)

    async def on_submit(self, interaction: discord.Interaction):
        if not is_superadmin(interaction):
            await interaction.response.send_message("Requires superadmin.", ephemeral=True)
            return
        try:
            pairs = []
            for chunk in str(self.spec.value).split(","):
                c, m = chunk.strip().split(":")
                pairs.append((int(c), float(m)))
            pairs.sort()
        except ValueError:
            self._panel.flash("⚠ Couldn't parse — use `count:mult, count:mult` (e.g. `1:1, 10:15, 100:150`).")
            await self._panel.rerender(interaction)
            return
        if not (1 <= len(pairs) <= 5):
            self._panel.flash("⚠ 1–5 windows, please.")
            await self._panel.rerender(interaction)
            return
        counts = [c for c, _ in pairs]
        mults = [m for _, m in pairs]
        if (any(c < 1 for c in counts) or any(m <= 0 for m in mults)
                or len(set(counts)) != len(counts)
                or sorted(mults) != mults or len(set(mults)) != len(mults)):
            self._panel.flash("⚠ Counts and periods must both be strictly increasing and positive.")
            await self._panel.rerender(interaction)
            return
        self._panel.bot.config.set(None, "cooldown_windows",
                                   [[c, m] for c, m in pairs], scope="global")
        self._panel.flash("Window ladder updated.")
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
        self.mgmt_provider = None   # Providers tab browse state
        self.mgmt_model = None
        self._flash = None          # one-render status line (last action result)
        self.refresh_state()
        self._build()

    # --- state -----------------------------------------------------------
    def refresh_state(self):
        pc = self.gpt.get_provider_config(self._cfg_ctx())
        self.provider = pc["provider"]
        self.model = pc["model"]
        # Keep the Providers-tab browse state valid across CRUD ops: default
        # to the guild's active provider, fall back to any survivor after a
        # removal, and drop a model selection that no longer exists.
        all_providers = self.gpt.llm.get_all_providers()
        if self.mgmt_provider not in all_providers:
            self.mgmt_provider = self.provider if self.provider in all_providers \
                else next(iter(all_providers), None)
            self.mgmt_model = None
        if self.mgmt_model is not None:
            models = all_providers.get(self.mgmt_provider, {}).get("models", {})
            if self.mgmt_model not in models:
                self.mgmt_model = None

    def flash(self, text: str):
        """Queue a status line shown once in the next embed render."""
        self._flash = text

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
        self.add_item(self._tab_button("🧩 Providers", "providers"))
        self.add_item(self._tab_button("⏱ Cooldowns", "cooldowns"))
        self.add_item(self._tab_button("🤖 Bot tools", "bot"))
        self.add_item(self._tab_button("🌐 MCP tools", "mcp"))

        if self.page == "server":
            self.add_item(_ProviderSelect(self, row=1))
            self.add_item(_ModelSelect(self, row=2))
            self.add_item(self._personality_button())
            self.add_item(self._nickname_button())
        elif self.page == "providers":
            self.add_item(_MgmtProviderSelect(self, row=1))
            self.add_item(_MgmtModelSelect(self, row=2))
            has_model = self.mgmt_model is not None
            providers = self.gpt.llm.get_all_providers()
            is_default = has_model and providers.get(self.mgmt_provider, {}) \
                .get("default_model") == self.mgmt_model
            self.add_item(self._crud_button(
                "➕ Add model", row=3,
                opener=lambda: _ModelModal(self, edit=False)))
            self.add_item(self._crud_button(
                "✏ Edit model", row=3, disabled=not has_model,
                opener=lambda: _ModelModal(self, edit=True)))
            self.add_item(self._remove_model_button(disabled=not has_model))
            self.add_item(self._default_model_button(
                disabled=not has_model or is_default))
            self.add_item(self._crud_button(
                "➕ Add provider", row=4,
                opener=lambda: _AddProviderModal(self)))
            self.add_item(self._crud_button(
                "🔑 Set API key", row=4,
                opener=lambda: _ApiKeyModal(self)))
            self.add_item(self._crud_button(
                "🗑 Remove provider", row=4,
                style=discord.ButtonStyle.danger,
                opener=lambda: _RemoveProviderModal(self)))
        elif self.page == "cooldowns":
            self.add_item(self._crud_button(
                "✏ Tier base periods", row=1,
                opener=lambda: _TierBasesModal(self)))
            self.add_item(self._crud_button(
                "✏ Window ladder", row=1,
                opener=lambda: _WindowsModal(self)))
            self.add_item(self._reset_cooldowns_button())
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

    # --- Providers tab controls (superadmin: global catalog mutations) ----
    def _crud_button(self, label, *, row, opener, disabled=False,
                     style=discord.ButtonStyle.secondary):
        """Button that opens a modal — superadmin-gated at open (the modal's
        on_submit re-checks again; the button state is only cosmetic)."""
        btn = discord.ui.Button(label=label, style=style, row=row, disabled=disabled)

        async def cb(interaction: discord.Interaction):
            if not is_superadmin(interaction):
                await interaction.response.send_message(
                    "Requires superadmin (this edits global bot config).",
                    ephemeral=True)
                return
            await interaction.response.send_modal(opener())

        btn.callback = cb
        return btn

    def _remove_model_button(self, *, disabled):
        btn = discord.ui.Button(label="🗑 Remove model",
                                style=discord.ButtonStyle.danger, row=3,
                                disabled=disabled)

        async def cb(interaction: discord.Interaction):
            if not is_superadmin(interaction):
                await interaction.response.send_message(
                    "Requires superadmin (this edits global bot config).",
                    ephemeral=True)
                return
            # _do_removemodel guards the provider's default model itself, so
            # no extra confirm step: a misclick is always recoverable via
            # ➕ Add model.
            result = self.gpt._do_removemodel(interaction, self.mgmt_model,
                                              self.mgmt_provider)
            self.mgmt_model = None
            self.flash(result)
            await self.rerender(interaction)

        btn.callback = cb
        return btn

    def _reset_cooldowns_button(self):
        btn = discord.ui.Button(label="↺ Reset to defaults",
                                style=discord.ButtonStyle.secondary, row=1)

        async def cb(interaction: discord.Interaction):
            if not is_superadmin(interaction):
                await interaction.response.send_message(
                    "Requires superadmin (this edits global bot config).",
                    ephemeral=True)
                return
            self.bot.config.rem(None, "cooldown_tier_bases", scope="global")
            self.bot.config.rem(None, "cooldown_windows", scope="global")
            self.flash("Cooldown config reset to built-in defaults.")
            await self.rerender(interaction)

        btn.callback = cb
        return btn

    def _default_model_button(self, *, disabled):
        btn = discord.ui.Button(label="⭐ Make default",
                                style=discord.ButtonStyle.secondary, row=3,
                                disabled=disabled)

        async def cb(interaction: discord.Interaction):
            if not is_superadmin(interaction):
                await interaction.response.send_message(
                    "Requires superadmin (this edits global bot config).",
                    ephemeral=True)
                return
            all_providers = self.gpt.llm.get_all_providers()
            if self.mgmt_model not in all_providers.get(self.mgmt_provider, {}).get("models", {}):
                self.flash("That model no longer exists.")
            else:
                all_providers[self.mgmt_provider]["default_model"] = self.mgmt_model
                self.bot.config.set(None, "ai_providers", all_providers, scope="global")
                self.flash(f"Default model for {self.mgmt_provider} is now {self.mgmt_model}.")
            await self.rerender(interaction)

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
        if self.page == "providers":
            e = self._providers_embed()
        elif self.page == "cooldowns":
            e = self._cooldowns_embed()
        else:
            e = self._overview_embed()
        if self._flash:
            e.add_field(name="Last action", value=self._flash[:1024], inline=False)
            self._flash = None
        if mcp_note:
            e.set_footer(text="MCP changes take effect on next bot restart.")
        else:
            e.set_footer(text="Panel expires after 3 minutes of inactivity.")
        return e

    def _overview_embed(self):
        model_info = self.gpt._current_model_info(self._cfg_ctx())
        bases, windows = self.gpt.cooldown_config()
        tier, base = cooldown_tier_for_cost(
            model_info.get("cost_per_mtok_output"), bases)
        bot_tools = self._bot_tools()
        mcp_tools = self._mcp_tools()
        e = discord.Embed(
            title="AI settings",
            description=f"Server: **{self.guild.name}**" if self.guild else "DM",
            color=discord.Color.blurple(),
        )
        e.add_field(name="Provider / Model",
                    value=f"{self.provider} / **{self.model}**", inline=True)
        e.add_field(name="Rate limit",
                    value=" · ".join(f"{c}/{_fmt_secs(m * base)}"
                                     for c, m in windows) + f" ({tier})",
                    inline=True)
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
        return e

    def _cooldowns_embed(self):
        """The rate-limit picture: the window ladder, and every configured
        model bucketed into its cost tier with the resulting allowances."""
        bases, windows = self.gpt.cooldown_config()
        all_providers = self.gpt.llm.get_all_providers()
        e = discord.Embed(
            title="AI settings — Cooldowns",
            description="Stacked windows: a message needs room in EVERY "
                        "window (bursts are cheap, sustained spam hits the "
                        "outer quotas). Superadmins are immune; failed API "
                        "calls are refunded. Global config (superadmin).",
            color=discord.Color.blurple(),
        )
        e.add_field(
            name="Window ladder (per tier base x)",
            value=" · ".join(f"{c} msg{'s' if c > 1 else ''} / {m:g}x"
                             for c, m in windows),
            inline=False,
        )
        # Bucket every configured model into its tier.
        buckets = {label: [] for label, _b, _d in COOLDOWN_TIERS}
        for pid, pinfo in all_providers.items():
            for m, mcfg in pinfo.get("models", {}).items():
                cost = mcfg.get("cost_per_mtok_output") if isinstance(mcfg, dict) else None
                label, _ = cooldown_tier_for_cost(cost, bases)
                buckets[label].append(f"{pid}/{m}")
        for label, bound, _default in COOLDOWN_TIERS:
            base = bases[label]
            bound_str = "≥ $5" if bound == float("inf") else f"< ${bound:g}"
            if base <= 0:
                ladder = "*rate limiting disabled*"
            else:
                ladder = " · ".join(
                    f"{c}/{_fmt_secs(m * base)}" for c, m in windows)
            models = buckets[label]
            e.add_field(
                name=f"{label} ({bound_str}/Mtok out) — x = {base:g}s",
                value=f"{ladder}\n" + (_clip_1024(", ".join(models))
                                       if models else "*no models in this bucket*"),
                inline=False,
            )
        return e

    def _providers_embed(self):
        """The Providers tab doubles as the read surface that replaced
        /ai info and /ai listmodels: per-model cost/tier/cooldown for the
        managed provider, plus a key-status overview of every provider."""
        all_providers = self.gpt.llm.get_all_providers()
        pid = self.mgmt_provider
        info = all_providers.get(pid, {})
        e = discord.Embed(
            title="AI settings — Providers",
            description="Global model catalog — changes here affect every "
                        "server this bot is in (superadmin).",
            color=discord.Color.blurple(),
        )
        e.add_field(
            name=f"{info.get('name', pid)} ({pid})",
            value=f"{info.get('base_url', '*built-in*')}\n"
                  f"{self.gpt.provider_key_status(pid, info)}",
            inline=False,
        )
        default_model = info.get("default_model")
        bases, _windows = self.gpt.cooldown_config()
        lines = []
        for m, mcfg in info.get("models", {}).items():
            if not isinstance(mcfg, dict):
                mcfg = {}
            cost = mcfg.get("cost_per_mtok_output")
            tier, base = cooldown_tier_for_cost(cost, bases)
            cost_str = "cost unset" if cost is None else f"${cost:g}/Mtok"
            extras = f", max_tokens {mcfg['max_completion_tokens']}" \
                if "max_completion_tokens" in mcfg else ""
            marker = " ⭐" if m == default_model else ""
            lines.append(f"• **{m}**{marker} — {cost_str} → {tier} (x={base:g}s){extras}")
        models_text = "\n".join(lines) if lines else "*no models*"
        if len(models_text) > 1024:
            models_text = models_text[:1010] + "\n… (more)"
        e.add_field(name=f"Models ({len(lines)})", value=models_text, inline=False)
        overview = " · ".join(
            f"{p} {'✅' if self.gpt.provider_key_status(p, pi).startswith('✅') else '❌'}"
            f" ({len(pi.get('models', {}))})"
            for p, pi in all_providers.items()
        )
        e.add_field(name="All providers (key · models)",
                    value=overview[:1024] or "*none*", inline=False)
        return e

    async def rerender(self, interaction: discord.Interaction):
        self.refresh_state()
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
