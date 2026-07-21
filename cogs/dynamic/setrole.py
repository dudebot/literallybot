"""Reaction-role toggles: react to a configured message to get/drop a role.

Config schema (guild scope, unchanged):
- `emoji_role_toggles` ({message_id: {emoji_key: role_id}}) — reaction-role
  toggles driven by the raw reaction listeners.

Auth: configuring the toggles goes through `core.utils.is_admin`; the raw
reaction listeners act only on mappings admins configured.

`whitelist_roles` is a legacy guild-config key from the removed command/panel
claiming path — stored data is left in place, nothing reads it.
"""
import aiohttp
import discord
from discord.ext import commands

from core.utils import is_admin


class SetRole(commands.Cog):
    """Reaction-role toggles."""

    def __init__(self, bot):
        self.bot = bot
        self.logger = bot.logger

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
