from discord.ext import commands
import discord
from discord import app_commands
from sys import version_info as sysv
import sys
from core.utils import (InvokerOnlyView, is_superadmin, panel_slash_pin,
                        safe_delete, list_cog_modules)

class Control(commands.Cog):
    """The bot's runtime control plane, superadmin-only: the enable/disable
    switch over `disabled_cogs`, the global !config editor, restart, and
    command sync.

    The cog set is FIXED AT BOOT (#86): startup reads `disabled_cogs` and
    loads the rest, and there is no live load/unload/reload surface. Editing
    `disabled_cogs` — via `!enable`/`!disable` or the `!cogs` panel — writes
    config only; the change binds on the next restart. Same doctrine as the
    MCP tool surface: config edits bind at restart.

    This is why cogs/core/ is never filtered by disabled_cogs. Every
    in-Discord route to re-enable a cog runs through here, so disabling it
    leaves shell access as the only way back."""
    def __init__(self, bot):
        self.bot = bot
        self.logger = bot.logger

    @commands.Cog.listener()
    async def on_ready(self):
        self.logger.info(f'Python {sysv.major}.{sysv.minor}.{sysv.micro} - Discord.py {discord.__version__}')

    def check_cog(self, cog):
        """Prefix bare cog names with 'cogs.optional.'; lowercases input."""
        if cog.lower().startswith('cogs.optional.'):
            return cog.lower()
        return f'cogs.optional.{cog.lower()}'

    def disabled_cogs(self):
        """Bare lowercase names from the global `disabled_cogs` list."""
        return {str(name).lower()
                for name in (self.bot.config.get_global("disabled_cogs", []) or [])}

    @commands.command(name='list_cogs', hidden=True)
    @commands.check(is_superadmin)
    async def list_cogs(self, ctx):
        """This command lists all the cogs in the `cogs/optional` directory.

        Note:
            This command can be used only from the bot owner.
            This command is hidden from the help menu.
        """
        self.logger.info(f"{ctx.author} invoked list_cogs")
        message = await ctx.send('Listing all cogs...')
        await safe_delete(ctx, self.logger)
        try:
            # Display keeps the bare cog names (module path stripped).
            # Unfiltered listing so disabled cogs stay visible, marked.
            disabled = self.disabled_cogs()
            cogs = [mod.rsplit('.', 1)[-1] for mod in list_cog_modules('optional')]
            names = [f'{c} (disabled)' if c.lower() in disabled else c for c in cogs]
            await message.edit(content=f'Available cogs: {", ".join(names)}', delete_after=20)
        except Exception as exc:
            self.logger.error("Error listing cogs", exc_info=True)
            await message.edit(content=f'An error has occurred: {exc}', delete_after=20)

    @commands.command(name='disable', hidden=True)
    @commands.check(is_superadmin)
    async def disable(self, ctx, *, cog: str):
        """Add an optional cog to the global `disabled_cogs` list.

        The deployment-level off switch: a disabled cog stays on disk but is
        skipped by startup until re-enabled. Downstream forks use this to
        carry upstream cogs without running them.

        Writes config only — the cog set is fixed at boot (#86), so a
        currently-loaded cog keeps running until the bot restarts.

        Note:
            This command can be used only from superadmins.
            This command is hidden from the help menu.
        """
        self.logger.info(f"{ctx.author} (ID: {ctx.author.id}) invoked disable on {cog}")
        message = await ctx.send('Disabling...')
        await safe_delete(ctx, self.logger)
        bare = self.check_cog(cog).rsplit('.', 1)[-1]
        known = {mod.rsplit('.', 1)[-1] for mod in list_cog_modules('optional')}
        if bare not in known:
            await message.edit(content=f'No optional cog named {bare}.', delete_after=20)
            return
        disabled = sorted(self.disabled_cogs() | {bare})
        self.bot.config.set_global("disabled_cogs", disabled)
        await message.edit(
            content=f'{bare} disabled — takes effect on the next restart '
                    f'(`!restart`).', delete_after=20)

    @commands.command(name='enable', hidden=True)
    @commands.check(is_superadmin)
    async def enable(self, ctx, *, cog: str):
        """Remove an optional cog from the global `disabled_cogs` list.

        Writes config only — the cog set is fixed at boot (#86), so the cog
        starts running at the next restart.

        Note:
            This command can be used only from superadmins.
            This command is hidden from the help menu.
        """
        self.logger.info(f"{ctx.author} (ID: {ctx.author.id}) invoked enable on {cog}")
        message = await ctx.send('Enabling...')
        await safe_delete(ctx, self.logger)
        bare = self.check_cog(cog).rsplit('.', 1)[-1]
        disabled = self.disabled_cogs()
        if bare not in disabled:
            await message.edit(content=f'{bare} is not disabled.', delete_after=20)
            return
        self.bot.config.set_global("disabled_cogs", sorted(disabled - {bare}))
        await message.edit(
            content=f'{bare} enabled — takes effect on the next restart '
                    f'(`!restart`).', delete_after=20)

    async def do_restart(self):
        """Close the gateway and exit; systemd brings the process back.

        THE one exit path. `!restart`/`!kys`/`!shutdown` and the `!cogs`
        panel's Restart button both call this, so there is exactly one way
        the bot goes down deliberately — and, since the cog set is fixed at
        boot (#86), exactly one way a `disabled_cogs` edit takes effect.
        Raises whatever `bot.close()` raised; callers report it."""
        await self.bot.close()
        sys.exit()

    @commands.command(name='restart', aliases=['kys', 'shutdown'], hidden=True)
    @commands.check(is_superadmin)
    async def restart(self, ctx):
        """This command restarts the bot (expects systemctl auto-restart).

        Note:
            This command can be used by superadmins only.
            This command is hidden from the help menu.
            Use 'restart' alias for cleaner command.
        """
        self.logger.info(f"{ctx.author} invoked shutdown")

        # Use different message based on the command used
        if ctx.invoked_with == 'kys':
            restart_message = 'I am sudoku...'
        else:
            restart_message = 'Restarting...'

        # Send message first
        message = await ctx.send(restart_message)

        # Actually shut down
        try:
            await self.do_restart()
        except Exception as exc:
            self.logger.error("Error during shutdown", exc_info=True)
            await message.edit(content=f'An error has occurred: {exc}', delete_after=20)

    @app_commands.command(name="cogs",
                          description="Open the cog-management panel")
    @app_commands.guild_only()
    @panel_slash_pin()
    @app_commands.check(is_superadmin)
    async def cogs_slash(self, interaction: discord.Interaction):
        """Ephemeral twin of `!cogs` (#76).

        Authorization is `@app_commands.check(is_superadmin)`. The picker
        pin is Manage Messages so ordinary members do not see the command;
        a superadmin who is not a moderator in this guild still has `!cogs`."""
        if not is_superadmin(interaction):
            await interaction.response.send_message(
                "Requires superadmin.", ephemeral=True)
            return
        view = CogsView(self, interaction.user)
        await interaction.response.send_message(
            embed=view.render_embed(), view=view, ephemeral=True)
        view.message = None

    @app_commands.command(name="config",
                          description="Open the global-config editor")
    @app_commands.guild_only()
    @panel_slash_pin()
    @app_commands.check(is_superadmin)
    async def config_slash(self, interaction: discord.Interaction):
        """Ephemeral twin of `!config` (#76).

        This panel ENUMERATES global config keys (including API key names).
        Authorization is `@app_commands.check(is_superadmin)`. The picker
        pin is Manage Messages so ordinary members do not see the command;
        a superadmin who is not a moderator in this guild still has
        `!config`."""
        if not is_superadmin(interaction):
            await interaction.response.send_message(
                "Requires superadmin.", ephemeral=True)
            return
        view = ConfigView(self, interaction.user)
        await interaction.response.send_message(
            embed=view.render_embed(), view=view, ephemeral=True)
        view.message = None

    @commands.command(name="cogs", hidden=True)
    @commands.check(is_superadmin)
    async def cogs_panel(self, ctx):
        """Open the cog-management panel (the `disabled_cogs` editor). Posts
        PUBLICLY — use /cogs for a private one (#76)."""
        view = CogsView(self, ctx.author)
        view.message = await ctx.send(embed=view.render_embed(), view=view)

    @commands.command(name="config", hidden=True)
    @commands.check(is_superadmin)
    async def config_panel(self, ctx):
        """Open the global-config editor panel. Superadmin: global state must
        be reachable regardless of the invoker's Discord permissions in
        whatever server they happen to be standing in. Posts PUBLICLY and
        enumerates config KEY NAMES — prefer /config (#76)."""
        view = ConfigView(self, ctx.author)
        view.message = await ctx.send(embed=view.render_embed(), view=view)

    @commands.command(name='sync', hidden=True)
    @commands.check(is_superadmin)
    async def sync(self, ctx, scope: str = "global"):
        """Sync application commands with Discord.

        `!sync` (or `!sync global`) pushes the global tree — that's what
        DMs see. `!sync guild` copies the global tree onto this server
        and pushes that (faster to propagate; also the way to drop a
        stale guild copy of a deleted command)."""
        scope = (scope or "global").lower()
        self.logger.info(
            f"{ctx.author} invoked sync scope={scope} "
            f"guild={getattr(ctx.guild, 'id', 'N/A')}")
        message = await ctx.send('Syncing commands...')
        await safe_delete(ctx, self.logger)
        try:
            if scope == "guild":
                if ctx.guild is None:
                    await message.edit(
                        content="Guild sync needs to be run in a server.",
                        delete_after=20)
                    return
                self.bot.tree.copy_global_to(guild=ctx.guild)
                synced = await self.bot.tree.sync(guild=ctx.guild)
                where = "this server"
            else:
                synced = await self.bot.tree.sync()
                where = "globally"
            await message.edit(
                content=f"Synced {len(synced)} commands {where}.",
                delete_after=20)
        except Exception as exc:
            self.logger.error("Error during sync", exc_info=True)
            await message.edit(content=f'An error has occurred: {exc}', delete_after=20)

class _CogSelect(discord.ui.Select):
    def __init__(self, panel: "CogsView"):
        self._panel = panel
        options = []
        for name in panel.cog_names():
            state = panel.cog_state(name)
            options.append(discord.SelectOption(
                label=name, value=name, description=state,
                default=(name == panel.selected)))
        if not options:
            options = [discord.SelectOption(label="(no cogs)", value="_none")]
        super().__init__(placeholder="Select an optional cog", min_values=0,
                         max_values=1, options=options[:25], row=0)

    async def callback(self, interaction: discord.Interaction):
        self._panel.selected = self.values[0] if self.values else None
        await self._panel.rerender(interaction)


class CogsView(InvokerOnlyView, discord.ui.View):
    """Superadmin panel over the disabled_cogs machinery: the slash-native
    face of !disable/!enable. Single-invoker, ephemeral.

    Toggling writes the global `disabled_cogs` list and NOTHING else — the
    cog set is fixed at boot (#86). The Restart button is how a pending edit
    becomes reality, and it goes through Control.do_restart, the same exit
    path as `!restart`."""

    panel_command = "!cogs"

    def __init__(self, dev_cog, user):
        super().__init__(timeout=180)
        self.dev = dev_cog
        self.bot = dev_cog.bot
        self.invoker_id = user.id
        self.selected = None
        self.message = None
        self._flash = None
        self._build()

    def cog_names(self):
        return sorted(mod.rsplit('.', 1)[-1] for mod in list_cog_modules('optional'))

    def cog_state(self, name):
        """Configured state: what the NEXT boot will do with this cog."""
        if name in self.dev.disabled_cogs():
            return "disabled"
        return "enabled"

    def is_pending(self, name):
        """True when the configured state disagrees with what is running, so
        the panel can mark the edits a restart would apply."""
        running = f"cogs.optional.{name}" in self.bot.extensions
        return running is (self.cog_state(name) == "disabled")

    def has_pending(self):
        return any(self.is_pending(n) for n in self.cog_names())

    def flash(self, text):
        self._flash = text

    def _set_disabled_list(self, names):
        self.bot.config.set_global("disabled_cogs", sorted(names))

    def _build(self):
        self.clear_items()
        if self.selected not in self.cog_names():
            self.selected = None
        self.add_item(_CogSelect(self))
        state = self.cog_state(self.selected) if self.selected else None
        toggle_label = "▶ Enable" if state == "disabled" else "⏸ Disable"
        toggle_btn = discord.ui.Button(
            label=toggle_label,
            style=(discord.ButtonStyle.success if state == "disabled"
                   else discord.ButtonStyle.danger),
            row=1, disabled=state is None)
        # Relabelled once there is something to apply, so the operator can see
        # from the panel that their edit is still only config.
        restart_label = ("♻ Restart to apply" if self.has_pending()
                         else "♻ Restart bot")
        restart_btn = discord.ui.Button(
            label=restart_label, row=1,
            style=(discord.ButtonStyle.primary if self.has_pending()
                   else discord.ButtonStyle.secondary))

        async def toggle_cb(interaction: discord.Interaction):
            if not is_superadmin(interaction):
                await interaction.response.send_message("Superadmin only.", ephemeral=True)
                return
            name = self.selected
            if name is None:
                await interaction.response.send_message("Select a cog first.", ephemeral=True)
                return
            # Config only. The cog set is fixed at boot (#86): nothing is
            # loaded or unloaded here, by design.
            disabled = self.dev.disabled_cogs()
            if name in disabled:
                self._set_disabled_list(disabled - {name})
                self.flash(f"{name} enabled in config — restart to apply.")
            else:
                self._set_disabled_list(disabled | {name})
                self.flash(f"{name} disabled in config — restart to apply.")
            self.bot.logger.info(
                f"{interaction.user} toggled cog {name} via the !cogs panel")
            await self.rerender(interaction)

        async def restart_cb(interaction: discord.Interaction):
            if not is_superadmin(interaction):
                await interaction.response.send_message("Superadmin only.", ephemeral=True)
                return
            self.bot.logger.info(
                f"{interaction.user} invoked restart via the !cogs panel")
            # Answer the interaction BEFORE the process goes away — the
            # gateway close below means no later response could be sent.
            await interaction.response.edit_message(
                content="Restarting...", embed=None, view=None)
            try:
                await self.dev.do_restart()
            except Exception as e:
                self.bot.logger.error("Error during shutdown from !cogs panel",
                                      exc_info=True)
                await interaction.edit_original_response(
                    content=f"Restart failed: {e}")

        toggle_btn.callback = toggle_cb
        restart_btn.callback = restart_cb
        self.add_item(toggle_btn)
        self.add_item(restart_btn)

    def render_embed(self):
        marks = {"enabled": "✅", "disabled": "🚫"}
        e = discord.Embed(
            title="Cogs (bind at restart)",
            description="Editing the global `disabled_cogs` list. 🚫 disabled "
                        "cogs stay on disk but are skipped by startup. The "
                        "cog set is fixed at boot — toggles here change "
                        "config only and take effect on the next restart.",
            color=discord.Color.blurple(),
        )
        lines = [f"{marks[self.cog_state(n)]} {n}"
                 + (" ⏳ *pending restart*" if self.is_pending(n) else "")
                 for n in self.cog_names()]
        e.add_field(name="Cogs", value="\n".join(lines)[:1024] or "*none*", inline=False)
        if self.has_pending():
            e.add_field(
                name="Pending",
                value="⏳ entries differ from what is running — press "
                      "**♻ Restart to apply**.", inline=False)
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


# Key-name fragments whose global values are secrets: masked in every
# render and never prefilled into the edit modal.
_SECRET_KEY_FRAGMENTS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD")


def _is_secret_key(key):
    return any(fragment in key.upper() for fragment in _SECRET_KEY_FRAGMENTS)


class _ConfigValueModal(discord.ui.Modal):
    """Set a global config key. Values are JSON — strings need quotes."""

    def __init__(self, panel: "ConfigView", key=None):
        super().__init__(title=f"Edit {key}"[:45] if key else "Add global key")
        self._panel = panel
        import json
        if key is None:
            current = ""
        elif _is_secret_key(key):
            current = ""   # never surface a stored secret, even to its editor
        else:
            current = json.dumps(self._panel.bot.config.get_global(key), indent=None)
        self.key_input = discord.ui.TextInput(
            label="Key", required=True, max_length=100, default=key or "")
        self.value_input = discord.ui.TextInput(
            label='Value (JSON: "text", 42, true, [1,2], {...})',
            style=discord.TextStyle.paragraph, required=True, max_length=4000,
            default=current[:4000])
        self.add_item(self.key_input)
        self.add_item(self.value_input)

    async def on_submit(self, interaction: discord.Interaction):
        if not is_superadmin(interaction):
            await interaction.response.send_message("Superadmin only.", ephemeral=True)
            return
        import json
        key = str(self.key_input.value).strip()
        try:
            value = json.loads(str(self.value_input.value))
        except json.JSONDecodeError as e:
            self._panel.flash(f"⚠ Value is not valid JSON ({e}) — nothing saved. "
                              'Strings need quotes: "like this".')
            await self._panel.rerender(interaction)
            return
        self._panel.bot.config.set_global(key, value)
        self._panel.selected = key
        self._panel.flash(f"Saved `{key}`.")
        self._panel.bot.logger.info(
            f"{interaction.user} set global config key {key} via !config")
        await self._panel.rerender(interaction)


class _ConfigKeySelect(discord.ui.Select):
    def __init__(self, panel: "ConfigView"):
        self._panel = panel
        keys = panel.keys()
        options = [
            discord.SelectOption(label=k[:100], value=k,
                                 description=panel.preview(k)[:100],
                                 default=(k == panel.selected))
            for k in keys[:25]
        ]
        if not options:
            options = [discord.SelectOption(label="(no keys)", value="_none")]
        placeholder = "Select a key"
        if len(keys) > 25:
            placeholder += f" (first 25 of {len(keys)})"
        super().__init__(placeholder=placeholder, min_values=0, max_values=1,
                         options=options, disabled=not keys, row=0)

    async def callback(self, interaction: discord.Interaction):
        self._panel.selected = self.values[0] if self.values else None
        self._panel.confirming = False
        await self._panel.rerender(interaction)


class ConfigView(InvokerOnlyView, discord.ui.View):
    """Superadmin editor over configs/global.json. Values render as JSON;
    secret-looking keys are masked and never prefilled. Deleting requires a
    second, relabeled click."""

    panel_command = "!config"

    def __init__(self, dev_cog, user):
        super().__init__(timeout=180)
        self.dev = dev_cog
        self.bot = dev_cog.bot
        self.invoker_id = user.id
        self.selected = None
        self.confirming = False
        self.message = None
        self._flash = None
        self._build()

    def keys(self):
        return sorted(self.bot.config.global_keys())

    def preview(self, key):
        import json
        if _is_secret_key(key):
            return "••••••"
        try:
            return json.dumps(self.bot.config.get_global(key))
        except (TypeError, ValueError):
            return str(self.bot.config.get_global(key))

    def flash(self, text):
        self._flash = text

    def _build(self):
        self.clear_items()
        keys = self.keys()
        if self.selected not in keys:
            self.selected = None
            self.confirming = False
        self.add_item(_ConfigKeySelect(self))
        add_btn = discord.ui.Button(label="➕ Add", style=discord.ButtonStyle.primary, row=1)
        edit_btn = discord.ui.Button(label="✏ Edit", style=discord.ButtonStyle.secondary,
                                     row=1, disabled=self.selected is None)
        del_label = "⚠ Confirm delete" if self.confirming else "🗑 Delete"
        del_btn = discord.ui.Button(label=del_label, style=discord.ButtonStyle.danger,
                                    row=1, disabled=self.selected is None)

        async def add_cb(interaction):
            if not is_superadmin(interaction):
                await interaction.response.send_message("Superadmin only.", ephemeral=True)
                return
            await interaction.response.send_modal(_ConfigValueModal(self))

        async def edit_cb(interaction):
            if not is_superadmin(interaction):
                await interaction.response.send_message("Superadmin only.", ephemeral=True)
                return
            if self.selected is None:
                await interaction.response.send_message("Select a key first.", ephemeral=True)
                return
            await interaction.response.send_modal(_ConfigValueModal(self, key=self.selected))

        async def del_cb(interaction):
            if not is_superadmin(interaction):
                await interaction.response.send_message("Superadmin only.", ephemeral=True)
                return
            if self.selected is None:
                await interaction.response.send_message("Select a key first.", ephemeral=True)
                return
            if not self.confirming:
                self.confirming = True
                self.flash(f"Click **⚠ Confirm delete** to remove `{self.selected}`.")
                await self.rerender(interaction)
                return
            target = self.selected
            self.bot.config.rem(None, target, scope="global")
            self.bot.logger.info(
                f"{interaction.user} deleted global config key {target} via !config")
            self.selected = None
            self.confirming = False
            self.flash(f"Deleted `{target}`.")
            await self.rerender(interaction)

        add_btn.callback = add_cb
        edit_btn.callback = edit_cb
        del_btn.callback = del_cb
        self.add_item(add_btn)
        self.add_item(edit_btn)
        self.add_item(del_btn)

    def render_embed(self):
        keys = self.keys()
        e = discord.Embed(
            title="Global config",
            description="configs/global.json — every server this bot is in "
                        "reads these. Values are JSON; secret-looking keys "
                        "are masked.",
            color=discord.Color.blurple(),
        )
        if keys:
            lines = [f"`{k}` = {self.preview(k)[:80]}" for k in keys]
            body = "\n".join(lines)
            e.add_field(name=f"Keys ({len(keys)})",
                        value=body[:1010] + ("\n… (more)" if len(body) > 1010 else ""),
                        inline=False)
        else:
            e.add_field(name="Keys", value="*empty*", inline=False)
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
    await bot.add_cog(Control(bot))
