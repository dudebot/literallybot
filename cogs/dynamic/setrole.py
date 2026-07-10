"""Role subsystem: whitelist self-service claiming + reaction-role toggles.

Two halves, one config schema (unchanged — 8 live guilds depend on it):
- `whitelist_roles` (guild scope, list[str] of role NAMES) — roles members may
  claim themselves via `!setrole` or `/roles claim`. Managed by admins through
  the `/roles settings` panel (which replaced the old `!whitelistrole`).
- `emoji_role_toggles` (guild scope, {message_id: {emoji_key: role_id}}) —
  reaction-role toggles driven by the raw reaction listeners.

Auth: the whitelist itself is the safety boundary for self-assignment, so
`!setrole` and `/roles claim` need no permission. Anything that changes policy
(the whitelist, the toggles) or touches another member goes through
`core.utils.is_admin`. Panel style mirrors cogs/dynamic/ai_admin.py.
"""
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from core.utils import is_admin

PANEL_TIMEOUT = 180


def _find_role_ci(guild: discord.Guild, name: str):
    """Case-insensitive role lookup by name (first match wins)."""
    lowered = name.lower()
    return discord.utils.find(lambda r: r.name.lower() == lowered, guild.roles)


def _resolve_whitelist(guild: discord.Guild, whitelist):
    """[(stored_name, Role-or-None), ...] for each whitelist entry."""
    return [(entry, _find_role_ci(guild, entry)) for entry in whitelist]


def _clip(text: str, limit: int = 1024) -> str:
    """Keep an embed field under Discord's 1024-char cap."""
    return text if len(text) <= limit else text[: limit - 2] + " …"


class _WhitelistRoleSelect(discord.ui.RoleSelect):
    """RoleSelect editing the whitelist in place: the selection IS the list.

    Save-on-change, same semantics as ai_admin's _ToolSelect. Same data model
    as always — role NAME strings under `whitelist_roles` in the guild json.
    """

    def __init__(self, panel: "RoleSettingsView"):
        self._panel = panel
        whitelist = panel.bot.config.get(panel.guild.id, "whitelist_roles", []) or []
        current = [role for _, role in _resolve_whitelist(panel.guild, whitelist) if role]
        super().__init__(
            placeholder="Claimable roles — what's selected here is the whitelist",
            min_values=0,
            max_values=25,
            default_values=current[:25],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message("Requires admin.", ephemeral=True)
            return
        names = [role.name for role in self.values]
        self._panel.bot.config.set(self._panel.guild.id, "whitelist_roles", names)
        await self._panel.rerender(interaction)


class RoleSettingsView(discord.ui.View):
    """Ephemeral, single-invoker admin panel for the role subsystem."""

    def __init__(self, bot, interaction: discord.Interaction):
        super().__init__(timeout=PANEL_TIMEOUT)
        self.bot = bot
        self.invoker_id = interaction.user.id
        self.guild = interaction.guild
        self.message = None
        self._build()

    def _build(self):
        self.clear_items()
        self.add_item(_WhitelistRoleSelect(self))

    # --- embed -------------------------------------------------------------
    def _whitelist_lines(self):
        whitelist = self.bot.config.get(self.guild.id, "whitelist_roles", []) or []
        lines = []
        for entry, role in _resolve_whitelist(self.guild, whitelist):
            if role:
                lines.append(f"• {role.mention}")
            else:
                lines.append(f"• `{entry}` ⚠ no matching role in this server")
        return lines

    def _toggle_lines(self):
        toggles = self.bot.config.get(self.guild.id, "emoji_role_toggles", {}) or {}
        lines = []
        for msg_id, mapping in toggles.items():
            for emoji_key, role_id in mapping.items():
                if str(emoji_key).isdigit():
                    e = self.guild.get_emoji(int(emoji_key))
                    emoji_disp = str(e) if e else f"(custom emoji {emoji_key})"
                else:
                    emoji_disp = emoji_key
                role = self.guild.get_role(role_id)
                role_disp = role.mention if role else f"(missing role {role_id})"
                lines.append(f"msg `{msg_id}` — {emoji_disp} → {role_disp}")
        return lines

    def _embed(self):
        e = discord.Embed(
            title="Role settings",
            description=f"Server: **{self.guild.name}**",
            color=discord.Color.blurple(),
        )
        wl = self._whitelist_lines()
        e.add_field(
            name="Claimable roles (whitelist)",
            value=_clip("\n".join(wl)) if wl else "*none configured*",
            inline=False,
        )
        tg = self._toggle_lines()
        e.add_field(
            name="Reaction-role toggles",
            value=_clip("\n".join(tg)) if tg else "*none configured*",
            inline=False,
        )
        e.set_footer(
            text="Edits save instantly — the selection above is the whitelist. "
                 "Discord selects cap at 25 roles. Panel expires after 3 minutes."
        )
        return e

    async def rerender(self, interaction: discord.Interaction):
        self._build()
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=self._embed(), view=self)
        else:
            await interaction.response.edit_message(embed=self._embed(), view=self)

    # --- lifecycle -----------------------------------------------------------
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "This panel isn't yours — run `/roles settings` to open your own.",
                ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(
                    content="Panel expired — run `/roles settings` again.", view=self)
            except discord.HTTPException:
                pass


class _ClaimSelect(discord.ui.Select):
    """Multi-select over the whitelisted roles; submit diffs against what the
    member currently has and adds/removes accordingly."""

    def __init__(self, panel: "RoleClaimView"):
        self._panel = panel
        member = panel.member
        member_role_ids = {r.id for r in member.roles}
        options = [
            discord.SelectOption(
                label=role.name,
                value=str(role.id),
                default=(role.id in member_role_ids),
            )
            for role in panel.claimable[:25]
        ]
        super().__init__(
            placeholder="Select the roles you want (deselect to remove)",
            min_values=0,
            max_values=len(options),
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        panel = self._panel
        member = interaction.guild.get_member(interaction.user.id) or interaction.user
        panel.member = member
        # Diff only against roles the select actually SHOWED: with a 26+ role
        # whitelist the select truncates to 25, and a held role the member
        # never saw must not be silently removed as "deselected".
        claimable_ids = {int(o.value) for o in self.options}
        selected_ids = {int(v) for v in self.values}
        current_ids = {r.id for r in member.roles} & claimable_ids

        changed, failed = [], []
        for role_id in selected_ids - current_ids:
            role = interaction.guild.get_role(role_id)
            if not role:
                continue
            try:
                await member.add_roles(role, reason="Self-service /roles claim")
                changed.append(f"+ {role.name}")
            except discord.Forbidden:
                failed.append(f"{role.name} (role is above me or I lack Manage Roles)")
        for role_id in current_ids - selected_ids:
            role = interaction.guild.get_role(role_id)
            if not role:
                continue
            try:
                await member.remove_roles(role, reason="Self-service /roles claim")
                changed.append(f"- {role.name}")
            except discord.Forbidden:
                failed.append(f"{role.name} (role is above me or I lack Manage Roles)")

        lines = []
        if changed:
            lines.append("Changed: " + ", ".join(changed))
        if failed:
            lines.append("Couldn't change: " + "; ".join(failed))
        if not lines:
            lines.append("No changes — your selection already matches your roles.")

        panel._build()
        await interaction.response.edit_message(
            content="\n".join(lines), view=panel)


class RoleClaimView(discord.ui.View):
    """Ephemeral, single-invoker claim panel over the whitelisted roles."""

    def __init__(self, interaction: discord.Interaction, claimable):
        super().__init__(timeout=PANEL_TIMEOUT)
        self.invoker_id = interaction.user.id
        self.member = interaction.user
        self.claimable = claimable
        self.message = None
        self._build()

    def _build(self):
        self.clear_items()
        self.add_item(_ClaimSelect(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "This panel isn't yours — run `/roles claim` to open your own.",
                ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(
                    content="Panel expired — run `/roles claim` again.", view=self)
            except discord.HTTPException:
                pass


class SetRole(commands.Cog):
    """Role whitelist claiming + reaction-role toggles."""

    def __init__(self, bot):
        self.bot = bot
        self.logger = bot.logger

    # --- /roles app command group -------------------------------------------
    roles_group = app_commands.Group(
        name="roles", description="Claim roles or configure the claimable set")

    @roles_group.command(
        name="settings",
        description="Manage claimable roles and view reaction toggles (admin)")
    async def roles_settings(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command must be used in a server.", ephemeral=True)
            return
        if not is_admin(interaction):
            await interaction.response.send_message(
                "You do not have permission to use this command.", ephemeral=True)
            return
        view = RoleSettingsView(self.bot, interaction)
        await interaction.response.send_message(
            embed=view._embed(), view=view, ephemeral=True)
        view.message = await interaction.original_response()

    @roles_group.command(
        name="claim", description="Pick which claimable roles you want")
    async def roles_claim(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command must be used in a server.", ephemeral=True)
            return
        whitelist = self.bot.config.get(
            interaction.guild.id, "whitelist_roles", []) or []
        claimable = [role for _, role in
                     _resolve_whitelist(interaction.guild, whitelist) if role]
        if not claimable:
            await interaction.response.send_message(
                "No claimable roles are configured on this server. "
                "An admin can set them up with `/roles settings`.", ephemeral=True)
            return
        view = RoleClaimView(interaction, claimable)
        await interaction.response.send_message(
            "Select the whitelisted roles you want. Roles you already have are "
            "pre-selected — deselect one to remove it.",
            view=view, ephemeral=True)
        view.message = await interaction.original_response()

    # --- prefix command -------------------------------------------------------
    @commands.command(name='setrole')
    @commands.guild_only()
    async def setrole(self, ctx, *, args: str):
        """Add or remove a whitelisted role: !setrole +Role Name [@member]

        Self-assignment is open to everyone — the whitelist is the safety
        boundary. Targeting another member requires bot admin.
        """
        parts = args.split()
        member = None
        # Only a trailing mention or bare user id counts as a target member, so
        # multi-word role names can't be misread as a member lookup.
        if len(parts) > 1:
            last = parts[-1]
            if last.startswith("<@") and last.endswith(">"):
                # Explicit mention: a resolution failure is a hard error.
                inner = last[2:-1].lstrip("!")
                if inner.isdigit():
                    member = ctx.guild.get_member(int(inner))
                    if member is None:
                        await ctx.send(f"Could not find member: {last}")
                        return
                    parts = parts[:-1]
            elif last.isdigit() and 15 <= len(last) <= 20:
                # Snowflake-shaped bare id. Short numbers stay part of the
                # role name ("Tier 2"), and even a snowflake that doesn't
                # resolve falls back to the role-name reading.
                candidate = ctx.guild.get_member(int(last))
                if candidate is not None:
                    member = candidate
                    parts = parts[:-1]
        role_arg = " ".join(parts)

        target = member if member else ctx.author
        if member and member != ctx.author and not is_admin(ctx):
            await ctx.send("You don't have permission to modify roles for other users.")
            return

        if not role_arg or role_arg[0] not in ("+", "-"):
            await ctx.send("Use a + or - to add or remove the role "
                           "(eg: !setrole +Events or !setrole +Events @member)")
            return
        role_option = role_arg[0]
        rolename = role_arg[1:].strip()

        whitelist_roles = self.bot.config.get(ctx, "whitelist_roles", []) or []
        # Resolve to the canonical whitelist entry, then look the role up
        # case-insensitively so casing never blocks a claim.
        canonical = next(
            (r for r in whitelist_roles if r.lower() == rolename.lower()), None)
        if canonical is None:
            await ctx.send(f"Role not in whitelist: {rolename}")
            return
        role_obj = _find_role_ci(ctx.guild, canonical)
        if role_obj is None:
            await ctx.send(f"Could not find role: {canonical}")
            return
        try:
            if role_option == "+":
                await target.add_roles(role_obj)
                await ctx.send(f"Added role: {role_obj.name} to {target.mention}")
            else:
                await target.remove_roles(role_obj)
                await ctx.send(f"Removed role: {role_obj.name} from {target.mention}")
        except discord.Forbidden:
            await ctx.send(f"I can't manage {role_obj.name} — it's above my top "
                           "role or I lack the Manage Roles permission.")

    # --- reaction-role toggles ------------------------------------------------
    @discord.app_commands.command(name="setemojiroletoggle", description="Configure a reaction role toggle (mod-only)")
    @discord.app_commands.default_permissions(manage_messages=True)
    async def setemojiroletoggle(self, interaction: discord.Interaction, message_id: str, emoji: str, role: discord.Role, channel: discord.TextChannel = None):
        if not is_admin(interaction):
            await interaction.response.send_message(
                "You do not have permission to use this command.", ephemeral=True)
            return
        try:
            msg_id_int = int(message_id)
        except Exception as e:
            await interaction.response.send_message(f"Invalid message ID provided: {e}", ephemeral=True)
            return
        # Convert emoji string to PartialEmoji
        try:
            partial_emoji = discord.PartialEmoji.from_str(emoji)
        except Exception as e:
            await interaction.response.send_message(f"Invalid emoji provided: {e}", ephemeral=True)
            return

        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("This command must be used in a guild.", ephemeral=True)
            return

        # For custom emoji: if the emoji isn't in the guild, fetch and add it.
        if partial_emoji.id and not guild.get_emoji(partial_emoji.id):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(partial_emoji.url) as resp:
                        if resp.status != 200:
                            await interaction.response.send_message(
                                "Failed to fetch emoji image.", ephemeral=True)
                            return
                        image_data = await resp.read()
                partial_emoji = await guild.create_custom_emoji(
                    name=partial_emoji.name, image=image_data)
            except (discord.HTTPException, aiohttp.ClientError) as e:
                await interaction.response.send_message(
                    f"Couldn't copy that custom emoji into this server "
                    f"(emoji slots full, bad image, or missing permission): {e}",
                    ephemeral=True)
                return

        # Pre-populate the reaction on the target message.
        target_channel = channel or interaction.channel
        try:
            message = await target_channel.fetch_message(msg_id_int)
            await message.add_reaction(partial_emoji)
        except Exception as e:
            await interaction.response.send_message(f"Failed to add reaction to the message: {e}", ephemeral=True)
            return

        # Store the emoji-role mapping; use emoji.id (as string) for custom emoji, else emoji.name.
        key = str(partial_emoji.id) if partial_emoji.id else partial_emoji.name
        toggles = self.bot.config.get(interaction, "emoji_role_toggles", {})
        msg_key = str(msg_id_int)
        if msg_key not in toggles:
            toggles[msg_key] = {}
        toggles[msg_key][key] = role.id
        self.bot.config.set(interaction, "emoji_role_toggles", toggles)

        await interaction.response.send_message("Emoji role toggle configured.", ephemeral=True)

    @discord.app_commands.command(name="removeemojiroletoggle", description="Remove a reaction role toggle (mod-only)")
    @discord.app_commands.default_permissions(manage_messages=True)
    async def removeemojiroletoggle(self, interaction: discord.Interaction, message_id: str, emoji: str):
        if not is_admin(interaction):
            await interaction.response.send_message(
                "You do not have permission to use this command.", ephemeral=True)
            return
        try:
            msg_id_int = int(message_id)
        except Exception as e:
            await interaction.response.send_message(f"Invalid message ID provided: {e}", ephemeral=True)
            return
        try:
            partial_emoji = discord.PartialEmoji.from_str(emoji)
        except Exception as e:
            await interaction.response.send_message(f"Invalid emoji provided: {e}", ephemeral=True)
            return
        key = str(partial_emoji.id) if partial_emoji.id else partial_emoji.name
        toggles = self.bot.config.get(interaction, "emoji_role_toggles", {})
        msg_key = str(msg_id_int)
        if msg_key in toggles and key in toggles[msg_key]:
            del toggles[msg_key][key]
            if not toggles[msg_key]:
                del toggles[msg_key]
            self.bot.config.set(interaction, "emoji_role_toggles", toggles)
            await interaction.response.send_message("Emoji role toggle removed.", ephemeral=True)
        else:
            await interaction.response.send_message("Emoji role toggle not found.", ephemeral=True)

    async def _process_reaction_toggle(self, payload, add: bool):
        # Never react to the bot's own pre-populating reactions.
        if self.bot.user and payload.user_id == self.bot.user.id:
            return
        toggles = self.bot.config.get(payload.guild_id, "emoji_role_toggles", {})
        msg_key = str(payload.message_id)
        if msg_key not in toggles:
            return
        mapping = toggles[msg_key]
        emoji_identifier = str(payload.emoji.id) if payload.emoji.id else payload.emoji.name
        if emoji_identifier not in mapping:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        member = guild.get_member(payload.user_id)
        if not member:
            return
        target_role = guild.get_role(mapping[emoji_identifier])
        if not target_role:
            return
        try:
            if add:
                await member.add_roles(target_role)
            else:
                await member.remove_roles(target_role)
        except Exception as e:
            self.logger.error(f"Error toggling role: {e}")

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        await self._process_reaction_toggle(payload, True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload):
        await self._process_reaction_toggle(payload, False)


async def setup(bot):
    """Every cog needs a setup function like this."""
    await bot.add_cog(SetRole(bot))
