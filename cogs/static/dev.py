from discord.ext import commands
import discord
from discord import app_commands
from sys import version_info as sysv
import subprocess
from datetime import datetime
import sys
from core.utils import InvokerOnlyView, is_superadmin, safe_delete, list_cog_modules

class Dev(commands.Cog):
    """Superadmin-only maintenance commands: cog load/unload/reload, git
    update, restart, and slash-command sync."""
    def __init__(self, bot):
        self.bot = bot
        self.logger = bot.logger

    @commands.Cog.listener()
    #This is the decorator for events (inside of cogs).
    async def on_ready(self):
        self.logger.info(f'Python {sysv.major}.{sysv.minor}.{sysv.micro} - Discord.py {discord.__version__}')

    def check_cog(self, cog):
        """Returns the name of the cog in the correct format.
        Args:
            self
            cog (str): The cogname to check

        Returns:
            cog if cog starts with `cogs.`, otherwise an fstring with this format`cogs.{cog}`_.
        Note:
            All cognames are made lowercase with `.lower()`_.
        """
        if (cog.lower()).startswith('cogs.dynamic.') == True:
            return cog.lower()
        return f'cogs.dynamic.{cog.lower()}'

    def disabled_cogs(self):
        """Bare lowercase names from the global `disabled_cogs` list."""
        return {str(name).lower()
                for name in (self.bot.config.get_global("disabled_cogs", []) or [])}

    @commands.command(name='load', hidden=True)
    @commands.check(is_superadmin)
    async def load(self, ctx, *, cog: str):
        """This commands loads the selected cog, as long as that cog is in the `./cogs` folder.

        Args:
            cog (str): The name of the cog to load. The name is checked with `.check_cog(cog)`_.

        Note:
            This command can be used only from the bot owner.
            This command is hidden from the help menu.
            This command deletes its messages after 20 seconds.
        """
        self.logger.info(f"{ctx.author} (ID: {ctx.author.id}) invoked load on {cog}")
        message = await ctx.send('Loading...')
        await safe_delete(ctx, self.logger)
        bare = self.check_cog(cog).rsplit('.', 1)[-1]
        if bare in self.disabled_cogs():
            await message.edit(
                content=f'{bare} is disabled via the global `disabled_cogs` config. '
                        f'Use `!enable {bare}` first.', delete_after=20)
            return
        try:
            await self.bot.load_extension(self.check_cog(cog))
        except Exception as exc:
            self.logger.error(f"Error loading {cog} by {ctx.author}", exc_info=True)
            await message.edit(content=f'An error has occurred: {exc}', delete_after=20)
        else:
            self.logger.info(f"Loaded {cog} successfully by {ctx.author}")
            await message.edit(content=f'{self.check_cog(cog)} has been loaded.', delete_after=20)

    @commands.command(name='unload', hidden=True)
    @commands.check(is_superadmin)
    async def unload(self, ctx, *, cog: str):
        """This commands unloads the selected cog, as long as that cog is in the `./cogs` folder.

        Args:
            cog (str): The name of the cog to unload. The name is checked with `.check_cog(cog)`_.
        Note:
            This command can be used only from the bot owner.
            This command is hidden from the help menu.
            This command deletes its messages after 20 seconds.
        """

        self.logger.info(f"{ctx.author} (ID: {ctx.author.id}) invoked unload on {cog}")
        message = await ctx.send('Unloading...')
        await safe_delete(ctx, self.logger)
        try:
            await self.bot.unload_extension(self.check_cog(cog))
        except Exception as exc:
            self.logger.error(f"Error unloading {cog} by {ctx.author}", exc_info=True)
            await message.edit(content=f'An error has occurred: {exc}', delete_after=20)
        else:
            self.logger.info(f"Unloaded {cog} successfully by {ctx.author}")
            await message.edit(content=f'{self.check_cog(cog)} has been unloaded.', delete_after=20)

    @commands.command(name='reload', hidden=True)#This command is hidden from the help menu.
    @commands.check(is_superadmin)
    async def reload(self, ctx, cog=None):
        """This commands reloads a specific cog or all cogs in the `./cogs/dynamic` folder.

        Note:
            This command can be used only from the bot owner.
            This command is hidden from the help menu.
            This command deletes its messages after 20 seconds."""

        self.logger.info(f"{ctx.author} (ID: {ctx.author.id}) invoked reload on {cog or 'all dynamic'}")
        await safe_delete(ctx, self.logger)

        if cog is None:
            # Filtering by config here means a reload-all also sheds cogs
            # that were disabled while loaded: unload sweeps everything,
            # load only brings back the enabled set.
            cogs_to_unload = [c for c in self.bot.extensions if c.startswith("cogs.dynamic.")]
            cogs_to_load = list_cog_modules('dynamic', self.bot.config)
        else:
            bare = self.check_cog(cog).rsplit('.', 1)[-1]
            if bare in self.disabled_cogs():
                await ctx.send(
                    f'{bare} is disabled via the global `disabled_cogs` config. '
                    f'Use `!enable {bare}` first.', delete_after=20)
                return
            cogs_to_unload = [self.check_cog(cog)]
            cogs_to_load = [self.check_cog(cog)]

        errors = []
        message = await ctx.send(f'Reloading...')
        for cog in cogs_to_unload:
            if cog not in self.bot.extensions:
                continue
            try:
                await self.bot.unload_extension(cog)
            except Exception as exc:
                self.logger.error(f"Error unloading {cog} during reload by {ctx.author}", exc_info=True)
                errors.append(f'Error unloading {cog}: {exc}')

        for cog in cogs_to_load:
            try:
                await self.bot.load_extension(cog)
            except Exception as exc:
                self.logger.error(f"Error loading {cog} during reload by {ctx.author}", exc_info=True)
                errors.append(f'Error loading {cog}: {exc}')

        if errors:
            formatted_errors = '\n'.join([f"- {error}" for error in errors])
            response = f'Errors occurred:\n{formatted_errors}'
        else:
            formatted_cogs = '\n'.join([f"- {cog}" for cog in cogs_to_load])
            response = f'All cogs reloaded successfully:\n{formatted_cogs}'

        await message.edit(content=response, delete_after=20)

    @commands.command(name='update', hidden=True)
    @commands.check(is_superadmin)
    async def update(self, ctx):
        """This command executes a git pull command in the current environment to update the code.

        Note:
            This command can be used only from the bot owner.
            This command is hidden from the help menu.
        """
        self.logger.info(f"{ctx.author} invoked update command")
        message = await ctx.send('Attempting to update code via git pull...')
        try:
            # Delete the command message if possible, but don't fail if it's already gone or permissions are an issue
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                self.logger.warning("Could not delete update command message, it might have been already deleted or permissions are missing.")

            # Execute git pull
            result = subprocess.run(['git', 'pull'], capture_output=True, text=True, check=False)

            stdout_output = result.stdout.strip() if result.stdout else ""
            stderr_output = result.stderr.strip() if result.stderr else ""

            if result.returncode == 0:
                self.logger.info(f"Git pull successful. Output: {stdout_output if stdout_output else 'No output.'}")

                commit_hash = "N/A"
                human_time = "N/A"
                try:
                    commit_info_result = subprocess.run(
                        ['git', 'log', '-1', '--format="%H %ct"'],
                        capture_output=True, text=True, check=False
                    )
                    if commit_info_result.returncode == 0 and commit_info_result.stdout:
                        parsed_commit_hash, commit_timestamp_str = commit_info_result.stdout.replace("\"", "").strip().split()
                        commit_timestamp = int(commit_timestamp_str)
                        commit_hash = parsed_commit_hash
                        human_time = datetime.fromtimestamp(commit_timestamp).strftime("%Y-%m-%d %H:%M")
                    else:
                        self.logger.warning(f"Failed to get commit info after successful pull. Git log stderr: {commit_info_result.stderr.strip() if commit_info_result.stderr else 'None'}")
                except Exception as e_commit:
                    self.logger.warning(f"Error processing commit info after successful pull: {e_commit}")

                response_content = (
                    f'Code update pull completed successfully!\n'
                    f'Current Commit Hash: {commit_hash}\n'
                    f'Commit Timestamp: {human_time}\n\n'
                )
                if stdout_output:
                    response_content += f'Git Pull Output:\n```\n{stdout_output}\n```'
                else:
                    response_content += 'No specific output from git pull.'

                await message.edit(content=response_content, delete_after=60)

            else: # result.returncode != 0, git pull encountered issues
                log_message_parts = [f"Git pull command finished with return code {result.returncode}."]
                if stdout_output: log_message_parts.append(f"Stdout: {stdout_output}")
                if stderr_output: log_message_parts.append(f"Stderr: {stderr_output}")
                full_log_message = "\n".join(log_message_parts)

                user_message_content = f"Git pull finished with return code {result.returncode}.\n"
                if stdout_output:
                    user_message_content += f"Output:\n```\n{stdout_output}\n```\n"
                if stderr_output:
                    user_message_content += f"Errors:\n```\n{stderr_output}\n```\n"

                if "Permission denied" in stderr_output or "unable to unlink" in stderr_output or "failed to unlink" in stderr_output:
                    self.logger.warning(f"Git pull encountered permission issues. {full_log_message}")
                    user_message_content += ("\n**Some files may not have been updated due to permission issues** (e.g., unable to delete old files). "
                                             "The bot continues to run. You might need to resolve permissions manually. "
                                             "Consider reloading cogs if applicable after resolving.")
                else:
                    self.logger.error(f"Git pull failed. {full_log_message}")
                    user_message_content += ("\n**The code update may have failed or is incomplete.** "
                                             "The bot continues to run. Check the output above and bot logs for details.")

                await message.edit(content=user_message_content, delete_after=180) # Keep message much longer for review

        except Exception as exc:
            self.logger.error("Exception during update command execution", exc_info=True)
            try:
                await message.edit(content=f'An unexpected error occurred during the update command: {exc}\nThe bot continues to run.', delete_after=60)
            except discord.HTTPException: # If message itself is gone
                self.logger.error(f"Failed to send update error to Discord, message gone. Error: {exc}")

    @commands.command(name='list_cogs', hidden=True)
    @commands.check(is_superadmin)
    async def list_cogs(self, ctx):
        """This command lists all the cogs in the `cogs/dynamic` directory.

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
            cogs = [mod.rsplit('.', 1)[-1] for mod in list_cog_modules('dynamic')]
            names = [f'{c} (disabled)' if c.lower() in disabled else c for c in cogs]
            await message.edit(content=f'Available cogs: {", ".join(names)}', delete_after=20)
        except Exception as exc:
            self.logger.error("Error listing cogs", exc_info=True)
            await message.edit(content=f'An error has occurred: {exc}', delete_after=20)

    @commands.command(name='disable', hidden=True)
    @commands.check(is_superadmin)
    async def disable(self, ctx, *, cog: str):
        """Add a dynamic cog to the global `disabled_cogs` list and unload it.

        The deployment-level off switch: a disabled cog stays on disk but is
        skipped by startup, !reload, and !load until re-enabled. Downstream
        forks use this to carry upstream cogs without running them.

        Note:
            This command can be used only from superadmins.
            This command is hidden from the help menu.
        """
        self.logger.info(f"{ctx.author} (ID: {ctx.author.id}) invoked disable on {cog}")
        message = await ctx.send('Disabling...')
        await safe_delete(ctx, self.logger)
        bare = self.check_cog(cog).rsplit('.', 1)[-1]
        known = {mod.rsplit('.', 1)[-1] for mod in list_cog_modules('dynamic')}
        if bare not in known:
            await message.edit(content=f'No dynamic cog named {bare}.', delete_after=20)
            return
        disabled = sorted(self.disabled_cogs() | {bare})
        self.bot.config.set_global("disabled_cogs", disabled)
        module = f'cogs.dynamic.{bare}'
        unloaded = ''
        if module in self.bot.extensions:
            try:
                await self.bot.unload_extension(module)
                unloaded = ' and unloaded'
            except Exception as exc:
                self.logger.error(f"Error unloading {module} during disable", exc_info=True)
                unloaded = f' (unload failed: {exc})'
        await message.edit(content=f'{bare} disabled{unloaded}.', delete_after=20)

    @commands.command(name='enable', hidden=True)
    @commands.check(is_superadmin)
    async def enable(self, ctx, *, cog: str):
        """Remove a dynamic cog from the global `disabled_cogs` list and load it.

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
        module = f'cogs.dynamic.{bare}'
        loaded = ''
        if module not in self.bot.extensions:
            try:
                await self.bot.load_extension(module)
                loaded = ' and loaded'
            except Exception as exc:
                self.logger.error(f"Error loading {module} during enable", exc_info=True)
                loaded = f' (load failed: {exc})'
        await message.edit(content=f'{bare} enabled{loaded}.', delete_after=20)

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
            await self.bot.close()
            sys.exit()
        except Exception as exc:
            self.logger.error("Error during shutdown", exc_info=True)
            await message.edit(content=f'An error has occurred: {exc}', delete_after=20)

    @app_commands.command(name="cogs",
                          description="Open the cog-management panel (superadmin)")
    async def cogs_slash(self, interaction: discord.Interaction):
        """Ephemeral twin of `!cogs` (#76). No default_permissions — the gate
        is the bot's own superadmin list, not Discord permissions."""
        if not is_superadmin(interaction):
            await interaction.response.send_message(
                "Requires superadmin.", ephemeral=True)
            return
        view = CogsView(self, interaction.user)
        await interaction.response.send_message(
            embed=view.render_embed(), view=view, ephemeral=True)
        view.message = None

    @app_commands.command(name="config",
                          description="Open the global-config editor (superadmin)")
    async def config_slash(self, interaction: discord.Interaction):
        """Ephemeral twin of `!config` (#76).

        The strongest case for ephemeral of all the panels: this one
        ENUMERATES global config keys. Values that look secret are masked and
        never prefilled, but the key list itself is a map of the bot's
        configuration surface and does not belong in a public channel."""
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
        """Open the cog-management panel (enable/disable/reload). Posts
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
    async def sync(self, ctx):
        """Sync application commands with Discord (canonical)."""
        self.logger.info(f"{ctx.author} invoked sync for guild {getattr(ctx.guild, 'id', 'N/A')}")
        message = await ctx.send('Syncing commands...')
        await safe_delete(ctx, self.logger)
        try:
            self.bot.tree.copy_global_to(guild=ctx.guild)
            await self.bot.tree.sync(guild=ctx.guild)
            await message.edit(content='Application commands synced.', delete_after=20)
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
        super().__init__(placeholder="Select a dynamic cog", min_values=0,
                         max_values=1, options=options[:25], row=0)

    async def callback(self, interaction: discord.Interaction):
        self._panel.selected = self.values[0] if self.values else None
        await self._panel.rerender(interaction)


class CogsView(InvokerOnlyView, discord.ui.View):
    """Superadmin panel over the disabled_cogs machinery: the slash-native
    face of !disable/!enable/!reload. Single-invoker, ephemeral."""

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
        return sorted(mod.rsplit('.', 1)[-1] for mod in list_cog_modules('dynamic'))

    def cog_state(self, name):
        if name in self.dev.disabled_cogs():
            return "disabled"
        if f"cogs.dynamic.{name}" in self.bot.extensions:
            return "loaded"
        return "unloaded"

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
        reload_btn = discord.ui.Button(label="🔄 Reload", row=1,
                                       style=discord.ButtonStyle.secondary,
                                       disabled=state != "loaded")
        reload_all_btn = discord.ui.Button(label="🔄 Reload all", row=1,
                                           style=discord.ButtonStyle.secondary)

        async def toggle_cb(interaction: discord.Interaction):
            if not is_superadmin(interaction):
                await interaction.response.send_message("Superadmin only.", ephemeral=True)
                return
            name = self.selected
            if name is None:
                await interaction.response.send_message("Select a cog first.", ephemeral=True)
                return
            module = f"cogs.dynamic.{name}"
            disabled = self.dev.disabled_cogs()
            if name in disabled:
                self._set_disabled_list(disabled - {name})
                note = ""
                if module not in self.bot.extensions:
                    try:
                        await self.bot.load_extension(module)
                        note = " and loaded"
                    except Exception as e:
                        note = f" (load failed: {e})"
                self.flash(f"{name} enabled{note}.")
            else:
                self._set_disabled_list(disabled | {name})
                note = ""
                if module in self.bot.extensions:
                    try:
                        await self.bot.unload_extension(module)
                        note = " and unloaded"
                    except Exception as e:
                        note = f" (unload failed: {e})"
                self.flash(f"{name} disabled{note}.")
            await self.rerender(interaction)

        async def reload_cb(interaction: discord.Interaction):
            if not is_superadmin(interaction):
                await interaction.response.send_message("Superadmin only.", ephemeral=True)
                return
            name = self.selected
            if name is None:
                await interaction.response.send_message("Select a cog first.", ephemeral=True)
                return
            module = f"cogs.dynamic.{name}"
            try:
                await self.bot.reload_extension(module)
                self.flash(f"{name} reloaded.")
            except Exception as e:
                self.flash(f"Reload of {name} failed: {e}")
            await self.rerender(interaction)

        async def reload_all_cb(interaction: discord.Interaction):
            if not is_superadmin(interaction):
                await interaction.response.send_message("Superadmin only.", ephemeral=True)
                return
            # Same semantics as !reload with no argument: unload every loaded
            # dynamic cog, load the enabled set — sheds newly disabled cogs.
            errors = []
            for module in [c for c in list(self.bot.extensions)
                           if c.startswith("cogs.dynamic.")]:
                try:
                    await self.bot.unload_extension(module)
                except Exception as e:
                    errors.append(f"unload {module}: {e}")
            for module in list_cog_modules('dynamic', self.bot.config):
                try:
                    await self.bot.load_extension(module)
                except Exception as e:
                    errors.append(f"load {module}: {e}")
            self.flash("Reloaded all dynamic cogs."
                       if not errors else "Errors: " + "; ".join(errors)[:900])
            await self.rerender(interaction)

        toggle_btn.callback = toggle_cb
        reload_btn.callback = reload_cb
        reload_all_btn.callback = reload_all_cb
        self.add_item(toggle_btn)
        self.add_item(reload_btn)
        self.add_item(reload_all_btn)

    def render_embed(self):
        marks = {"loaded": "✅", "unloaded": "⬜", "disabled": "🚫"}
        e = discord.Embed(
            title="Dynamic cogs",
            description="🚫 disabled cogs stay on disk but are skipped by "
                        "startup and reloads (global `disabled_cogs`).",
            color=discord.Color.blurple(),
        )
        lines = [f"{marks[self.cog_state(n)]} {n}" for n in self.cog_names()]
        e.add_field(name="Cogs", value="\n".join(lines)[:1024] or "*none*", inline=False)
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
    await bot.add_cog(Dev(bot))
