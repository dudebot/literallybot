"""Log settings panel; reporting itself lives in core.error_handler."""

from copy import deepcopy

import discord
from discord import app_commands
from discord.ext import commands

from core.error_handler import ErrorCategory, ErrorSeverity, log_error_to_discord
from core.utils import InvokerOnlyView, is_admin, is_superadmin, panel_slash_pin


class ErrorLoggingAdmin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="logsettings", hidden=True)
    @commands.guild_only()
    @commands.check(is_admin)
    async def logsettings(self, ctx):
        """Configure log destinations and optional command-not-found reports."""
        view = LogSettingsView(self.bot, ctx.author, ctx.guild)
        view.message = await ctx.send(view=view)

    @app_commands.command(name="logsettings", description="Configure bot logging")
    @app_commands.guild_only()
    @panel_slash_pin()
    @app_commands.check(is_admin)
    async def logsettings_slash(self, interaction: discord.Interaction):
        view = LogSettingsView(self.bot, interaction.user, interaction.guild)
        await interaction.response.send_message(view=view, ephemeral=True)
        view.message = await interaction.original_response()


class _RateLimitModal(discord.ui.Modal, title="Duplicate alert interval"):
    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.minutes = discord.ui.TextInput(
            label="Minutes between identical alerts (1–60)", max_length=2,
            default=str(panel.config("global").get("rate_limit_minutes", 5)))
        self.add_item(self.minutes)

    async def on_submit(self, interaction):
        if not await self.panel.authorize(interaction, "global"):
            return
        try:
            minutes = int(self.minutes.value)
        except ValueError:
            minutes = 0
        if not 1 <= minutes <= 60:
            await interaction.response.send_message("Enter a whole number from 1 to 60.", ephemeral=True)
            return
        config = self.panel.config("global")
        config["rate_limit_minutes"] = minutes
        self.panel.save("global", config)
        await self.panel.rerender(interaction)

    async def on_error(self, interaction, error):
        await self.panel.on_error(interaction, error, None)


class LogSettingsView(InvokerOnlyView, discord.ui.LayoutView):
    panel_command = "`/logsettings`"
    expiry_text = None

    def __init__(self, bot, user, guild):
        super().__init__(timeout=300)
        self.bot = bot
        self.invoker_id = user.id
        self.guild = guild
        self.page = "server"
        self.route = "default_channel"
        self.build()

    @property
    def is_super(self):
        return is_superadmin(self.bot.config, self.invoker_id)

    def config(self, scope):
        # Work on a copy; a rejected edit must never mutate the store in place.
        config = (self.bot.config.get_global("error_logging", {}) if scope == "global"
                  else self.bot.config.get(self.guild.id, "error_logging", {}))
        return deepcopy(config)

    def save(self, scope, config):
        if scope == "global":
            self.bot.config.set_global("error_logging", config)
        else:
            self.bot.config.set(self.guild.id, "error_logging", config)

    async def authorize(self, interaction, scope):
        if not await super().interaction_check(interaction):
            return False
        if interaction.guild_id != self.guild.id or not is_admin(interaction):
            await interaction.response.send_message("Requires admin in this server.", ephemeral=True)
            return False
        if scope == "global" and not is_superadmin(interaction):
            await interaction.response.send_message("Requires superadmin.", ephemeral=True)
            return False
        return True

    async def interaction_check(self, interaction):
        return await self.authorize(interaction, self.page)

    async def on_error(self, interaction, error, item):
        self.bot.logger.error("Log settings interaction failed", exc_info=(type(error), error, error.__traceback__))
        if not interaction.response.is_done():
            await interaction.response.send_message("Unable to update log settings. The error has been logged.", ephemeral=True)
        await log_error_to_discord(self.bot, error, "logsettings", guild_id=self.guild.id)

    def row(self, *items):
        row = discord.ui.ActionRow()
        for item in items:
            row.add_item(item)
        self.add_item(row)

    async def rerender(self, interaction):
        self.build()
        if interaction.response.is_done():
            if self.message:
                await self.message.edit(view=self)
        else:
            await interaction.response.edit_message(view=self)

    def build(self):
        if self.page == "global" and not self.is_super:
            self.page = "server"
        self.clear_items()
        tabs = []
        for scope in (["server", "global"] if self.is_super else ["server"]):
            button = discord.ui.Button(label=scope.title(), style=(
                discord.ButtonStyle.primary if scope == self.page else discord.ButtonStyle.secondary))

            async def switch(interaction, scope=scope):
                if await self.authorize(interaction, scope):
                    self.page = scope
                    self.route = "default_channel"
                    await self.rerender(interaction)
            button.callback = switch
            tabs.append(button)
        self.row(*tabs)

        scope, route = self.page, self.route
        config = self.config(scope)
        default = config.get("default_channel")
        unknown = config.get("log_unknown_commands", False)
        selected = (config.get(route) if route == "default_channel" else
                    config.get(route.split(":")[0], {}).get(route.split(":")[1]))
        interval = self.config("global").get("rate_limit_minutes", 5)
        route_destination = (f"<#{selected}>" if selected else
                             "Not configured" if route == "default_channel" else
                             "No override (uses routing fallback)")
        body = (
            f"## Log settings — {scope.title()}\n"
            + ("Reports from this server. Global reporting continues independently.\n" if scope == "server"
               else "Reports from every server and DMs. Only superadmins can change these settings.\n")
            + f"**Destination:** {f'<#{default}>' if default else 'Not configured'}\n"
            "**Unexpected exceptions:** always reported to configured destinations.\n"
            "**Command denied:** always reported, with required and user access levels.\n"
            f"**Command not found:** {'On' if unknown else 'Off'} — optional activity reports.\n"
            "Recognized dice and media shortcuts are excluded.\n"
            f"**Duplicate alert interval:** {interval} minutes.\n\n"
            f"**Selected route:** {route_destination}\n"
            "Category routes take priority over severity routes. A default destination is required."
        )
        self.add_item(discord.ui.TextDisplay(body))
        options = [discord.SelectOption(label="Default destination", value="default_channel")]
        options.extend(discord.SelectOption(label=("Commands (fallback)" if c == ErrorCategory.COMMAND_ERROR
                                                   else c.value.replace("_", " ").title()),
                                            value=f"category_channels:{c.value}") for c in ErrorCategory)
        options.extend(discord.SelectOption(label=f"Severity: {s.severity_name.upper()}",
                                            value=f"severity_channels:{s.severity_name}") for s in ErrorSeverity)
        for option in options:
            option.default = option.value == route
        routes = discord.ui.Select(placeholder="Choose a route", options=options)

        async def choose_route(interaction):
            if await self.authorize(interaction, scope):
                self.route = routes.values[0]
                await self.rerender(interaction)
        routes.callback = choose_route
        self.row(routes)
        channels = discord.ui.ChannelSelect(placeholder="Set channel for selected route",
                                             channel_types=[discord.ChannelType.text])

        async def choose_channel(interaction):
            await self.set_channel(interaction, scope, route, channels.values[0].id)
        channels.callback = choose_channel
        self.row(channels)
        toggle = discord.ui.Button(label=f"Command not found: {'On' if unknown else 'Off'}",
                                   style=discord.ButtonStyle.success if unknown else discord.ButtonStyle.secondary)

        async def toggle_unknown(interaction):
            if await self.authorize(interaction, scope):
                current = self.config(scope)
                current["log_unknown_commands"] = not current.get("log_unknown_commands", False)
                self.save(scope, current)
                await self.rerender(interaction)
        toggle.callback = toggle_unknown
        reset = discord.ui.Button(label=("Global destination required" if scope == "global"
                                          else "Remove server destination")
                                  if route == "default_channel" else "Reset route",
                                  disabled=not selected or (scope == "global" and route == "default_channel"))

        async def reset_route(interaction):
            if not await self.authorize(interaction, scope):
                return
            if scope == "global" and route == "default_channel":
                return  # Keep the global exception destination configured.
            current = self.config(scope)
            if route == "default_channel":
                current.pop(route, None)
            else:
                group, key = route.split(":")
                current.get(group, {}).pop(key, None)
            self.save(scope, current)
            await self.rerender(interaction)
        reset.callback = reset_route
        buttons = [toggle, reset]
        if scope == "global":
            interval_button = discord.ui.Button(label="Duplicate alert interval")

            async def edit_interval(interaction):
                if await self.authorize(interaction, "global"):
                    await interaction.response.send_modal(_RateLimitModal(self))
            interval_button.callback = edit_interval
            buttons.append(interval_button)
        self.row(*buttons)

    async def set_channel(self, interaction, scope, route, channel_id):
        if not await self.authorize(interaction, scope):
            return
        channel = self.bot.get_channel(channel_id)
        if channel is None or channel.guild.id != self.guild.id:
            await interaction.response.send_message("Choose a text channel in this server.", ephemeral=True)
            return
        perms = channel.permissions_for(self.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            await interaction.response.send_message(
                "I need View Channel, Send Messages, and Embed Links in that channel.", ephemeral=True)
            return
        config = self.config(scope)
        if route == "default_channel":
            config[route] = channel_id
        else:
            group, key = route.split(":")
            config.setdefault(group, {})[key] = channel_id
        self.save(scope, config)
        await self.rerender(interaction)


async def setup(bot):
    await bot.add_cog(ErrorLoggingAdmin(bot))
