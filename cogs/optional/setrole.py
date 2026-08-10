"""Reaction-role toggles: react to a configured message to get/drop a role.

Config schema (guild scope):
- `emoji_role_toggles` — flat list of quadruplets, one per toggle:
  [{"channel_id": int|None, "message_id": int, "emoji": str, "role_id": int}]
  "emoji" is the canonical string form (unicode char, or "<:name:id>" for
  custom). channel_id None only occurs on entries migrated from the legacy
  {message_id: {emoji_key: role_id}} dict shape — /role sync resolves them.
  The flat shape is deliberate: every entry is independently fetchable and
  enumerable, so agents can manage toggles by editing the guild JSON and
  running /role sync (or a bot restart) to reconcile reactions.

Auth: the /role group requires `core.utils.is_admin`; the raw reaction
listeners act only on mappings admins configured.

`whitelist_roles` is a legacy guild-config key from the removed command/panel
claiming path — stored data is left in place, nothing reads it.
"""
import asyncio
import time

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from core.utils import is_admin

TOGGLES_KEY = "emoji_role_toggles"
_PREVIEW_TTL = 300  # seconds a message-content preview stays cached for autocomplete
_AUTOCOMPLETE_BUDGET = 2.0  # seconds; past this, fall back to bare message ids


def _parse_emoji(raw: str) -> discord.PartialEmoji:
    return discord.PartialEmoji.from_str(raw.strip())


def _emoji_matches(stored: str, other: discord.PartialEmoji) -> bool:
    pe = _parse_emoji(stored)
    if pe.id:
        return other.id == pe.id
    return other.id is None and other.name == pe.name


class _ConfirmEditView(discord.ui.View):
    """Are-you-sure gate when /role add targets an emoji that already has a role."""

    def __init__(self, on_confirm):
        super().__init__(timeout=60)
        self._on_confirm = on_confirm

    @discord.ui.button(label="Change it", style=discord.ButtonStyle.primary)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._on_confirm(interaction)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Left unchanged.", view=None)
        self.stop()


class SetRole(commands.Cog):
    """Reaction-role toggles."""

    def __init__(self, bot):
        self.bot = bot
        self.logger = bot.logger
        self._preview_cache = {}  # (channel_id, message_id) -> (label, monotonic_ts)

    async def cog_load(self):
        self.startup_sync.start()

    def cog_unload(self):
        self.startup_sync.cancel()

    # --- storage --------------------------------------------------------------

    def _entries(self, guild_id: int) -> list:
        """Return the flat entry list, migrating the legacy dict shape in place."""
        stored = self.bot.config.get(guild_id, TOGGLES_KEY, [])
        if isinstance(stored, dict):
            migrated = []
            for msg_key, mapping in stored.items():
                for emoji_key, role_id in mapping.items():
                    # Legacy custom-emoji keys are the bare id; the real name is
                    # unknown until /role sync resolves it against the guild.
                    emoji = f"<:_legacy:{emoji_key}>" if emoji_key.isdigit() else emoji_key
                    migrated.append({
                        "channel_id": None,
                        "message_id": int(msg_key),
                        "emoji": emoji,
                        "role_id": role_id,
                    })
            self.bot.config.set(guild_id, TOGGLES_KEY, migrated)
            self.logger.info(f"Migrated {len(migrated)} legacy emoji_role_toggles entries for guild {guild_id}")
            return migrated
        return stored

    def _save(self, guild_id: int, entries: list):
        self.bot.config.set(guild_id, TOGGLES_KEY, entries)

    # --- /role command group --------------------------------------------------

    role = app_commands.Group(
        name="role",
        description="Manage reaction-role toggles",
        guild_only=True,
        default_permissions=discord.Permissions(manage_roles=True),
    )

    async def _message_choices(self, interaction: discord.Interaction, current: str):
        channel = getattr(interaction.namespace, "channel", None)
        channel_id = channel.id if channel else interaction.channel_id
        entries = self._entries(interaction.guild_id)
        # Snowflakes are chronological, so ascending id = post order in channel.
        message_ids = sorted({e["message_id"] for e in entries
                              if e["channel_id"] in (channel_id, None)})

        async def build(mid):
            label = await self._message_label(channel_id, mid)
            return app_commands.Choice(name=label[:100], value=str(mid))

        try:
            async with asyncio.timeout(_AUTOCOMPLETE_BUDGET):
                choices = list(await asyncio.gather(*(build(m) for m in message_ids)))
        except TimeoutError:
            choices = [app_commands.Choice(name=str(m), value=str(m)) for m in message_ids]
        if current:
            choices = [c for c in choices if current.lower() in c.name.lower() or current in c.value]
        return choices[:25]

    async def _message_label(self, channel_id, message_id) -> str:
        key = (channel_id, message_id)
        cached = self._preview_cache.get(key)
        if cached and time.monotonic() - cached[1] < _PREVIEW_TTL:
            return cached[0]
        label = str(message_id)
        channel = self.bot.get_channel(channel_id) if channel_id else None
        if channel:
            try:
                msg = await channel.fetch_message(message_id)
                first = next((ln for ln in (msg.content or "").splitlines() if ln.strip()), "")
                if not first and msg.embeds:
                    first = msg.embeds[0].title or ""
                if first:
                    label = f"{first[:70]} · {message_id}"
            except Exception:
                label = f"(unfetchable) · {message_id}"
        self._preview_cache[key] = (label, time.monotonic())
        return label

    async def _emoji_choices(self, interaction: discord.Interaction, current: str):
        try:
            message_id = int(getattr(interaction.namespace, "message", "") or 0)
        except ValueError:
            return []
        guild = interaction.guild
        choices = []
        for e in self._entries(interaction.guild_id):
            if e["message_id"] != message_id:
                continue
            role = guild.get_role(e["role_id"]) if guild else None
            role_name = role.name if role else f"deleted role {e['role_id']}"
            choices.append(app_commands.Choice(
                name=f"{e['emoji']} → @{role_name}"[:100], value=e["emoji"]))
        if current:
            choices = [c for c in choices if current.lower() in c.name.lower()]
        return choices[:25]

    @role.command(name="add", description="Add a reaction-role toggle (pick an existing emoji to edit its role)")
    @app_commands.describe(
        message="Target message: pick a configured one, or paste a message ID",
        emoji="Emoji to react with (pick an existing one to change its role)",
        target_role="Role toggled by the reaction",
        channel="Channel of the message (defaults to this channel)",
    )
    async def role_add(self, interaction: discord.Interaction, message: str, emoji: str,
                       target_role: discord.Role, channel: discord.TextChannel = None):
        # Ack immediately: emoji download/creation and message fetch below are
        # network round-trips that can blow past Discord's 3-second window.
        await interaction.response.defer(ephemeral=True)
        if not is_admin(interaction):
            await interaction.followup.send("You do not have permission to use this command.", ephemeral=True)
            return
        try:
            message_id = int(message.strip())
        except ValueError:
            await interaction.followup.send(f"Invalid message ID: {message}", ephemeral=True)
            return
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("This command must be used in a guild.", ephemeral=True)
            return
        partial_emoji = _parse_emoji(emoji)

        # For custom emoji: if the emoji isn't in the guild, fetch and add it.
        if partial_emoji.id and not guild.get_emoji(partial_emoji.id):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(partial_emoji.url) as resp:
                        if resp.status != 200:
                            await interaction.followup.send("Failed to fetch emoji image.", ephemeral=True)
                            return
                        image_data = await resp.read()
                partial_emoji = await guild.create_custom_emoji(
                    name=partial_emoji.name, image=image_data)
            except (discord.HTTPException, aiohttp.ClientError) as e:
                await interaction.followup.send(
                    f"Couldn't copy that custom emoji into this server "
                    f"(emoji slots full, bad image, or missing permission): {e}",
                    ephemeral=True)
                return

        target_channel = channel or interaction.channel
        emoji_str = str(partial_emoji)
        entries = self._entries(guild.id)
        existing = next((e for e in entries if e["message_id"] == message_id
                         and _emoji_matches(e["emoji"], partial_emoji)), None)

        if existing:
            old_role = guild.get_role(existing["role_id"])
            old_name = old_role.name if old_role else f"deleted role {existing['role_id']}"
            if existing["role_id"] == target_role.id:
                await interaction.followup.send(
                    f"{emoji_str} on that message already toggles @{old_name}.", ephemeral=True)
                return

            async def apply_edit(button_interaction: discord.Interaction):
                existing["role_id"] = target_role.id
                existing["channel_id"] = target_channel.id
                self._save(guild.id, entries)
                await button_interaction.response.edit_message(
                    content=f"Updated: {emoji_str} now toggles @{target_role.name} (was @{old_name}).",
                    view=None)

            await interaction.followup.send(
                f"{emoji_str} on that message currently toggles **@{old_name}**. "
                f"Change it to **@{target_role.name}**?",
                view=_ConfirmEditView(apply_edit), ephemeral=True)
            return

        # Pre-populate the reaction on the target message.
        try:
            target_message = await target_channel.fetch_message(message_id)
            await target_message.add_reaction(partial_emoji)
        except Exception as e:
            await interaction.followup.send(f"Failed to add reaction to the message: {e}", ephemeral=True)
            return

        entries.append({
            "channel_id": target_channel.id,
            "message_id": message_id,
            "emoji": emoji_str,
            "role_id": target_role.id,
        })
        self._save(guild.id, entries)
        await interaction.followup.send(
            f"Configured: {emoji_str} on {target_channel.mention}/{message_id} toggles @{target_role.name}.",
            ephemeral=True)

    @role.command(name="delete", description="Remove a reaction-role toggle")
    @app_commands.describe(
        message="Configured message (pick from the list)",
        emoji="Configured emoji on that message (pick from the list)",
    )
    async def role_delete(self, interaction: discord.Interaction, message: str, emoji: str):
        await interaction.response.defer(ephemeral=True)
        if not is_admin(interaction):
            await interaction.followup.send("You do not have permission to use this command.", ephemeral=True)
            return
        try:
            message_id = int(message.strip())
        except ValueError:
            await interaction.followup.send(f"Invalid message ID: {message}", ephemeral=True)
            return
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("This command must be used in a guild.", ephemeral=True)
            return
        partial_emoji = _parse_emoji(emoji)
        entries = self._entries(guild.id)
        entry = next((e for e in entries if e["message_id"] == message_id
                      and _emoji_matches(e["emoji"], partial_emoji)), None)
        if not entry:
            await interaction.followup.send("No toggle found for that message + emoji.", ephemeral=True)
            return
        entries.remove(entry)
        self._save(guild.id, entries)

        # Best-effort: clear the emoji off the message so stale reactions don't linger.
        cleanup = ""
        channel = guild.get_channel(entry["channel_id"]) if entry["channel_id"] else None
        if channel:
            try:
                target_message = await channel.fetch_message(message_id)
                await target_message.clear_reaction(_parse_emoji(entry["emoji"]))
                cleanup = " Reactions cleared from the message."
            except Exception as e:
                cleanup = f" (Could not clear reactions: {e})"
        await interaction.followup.send(f"Toggle {entry['emoji']} removed.{cleanup}", ephemeral=True)

    @role.command(name="sync", description="Reconcile configured toggles: re-add missing reactions, report broken entries")
    async def role_sync(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not is_admin(interaction):
            await interaction.followup.send("You do not have permission to use this command.", ephemeral=True)
            return
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("This command must be used in a guild.", ephemeral=True)
            return
        results = await self._sync_guild(guild)
        if not results:
            await interaction.followup.send("No reaction-role toggles configured.", ephemeral=True)
            return
        lines = []
        for entry, status in results:
            where = f"<#{entry['channel_id']}>/{entry['message_id']}" if entry["channel_id"] else str(entry["message_id"])
            lines.append(f"{entry['emoji']} {where} → <@&{entry['role_id']}>: {status}")
        report = "\n".join(lines)
        await interaction.followup.send(f"**Sync report**\n{report}"[:2000], ephemeral=True)

    role_add.autocomplete("message")(_message_choices)
    role_delete.autocomplete("message")(_message_choices)
    role_add.autocomplete("emoji")(_emoji_choices)
    role_delete.autocomplete("emoji")(_emoji_choices)

    # --- reconciliation -------------------------------------------------------

    async def _sync_guild(self, guild: discord.Guild) -> list:
        """Ensure the bot's reaction exists for every entry; normalize migrated
        entries (fill channel_id, recover custom-emoji names). Returns
        (entry, status) pairs. Never deletes entries — broken ones are reported
        so a human (or agent) decides."""
        entries = self._entries(guild.id)
        results = []
        changed = False
        for entry in entries:
            # Recover the real name for legacy custom-emoji entries.
            pe = _parse_emoji(entry["emoji"])
            if pe.id:
                guild_emoji = guild.get_emoji(pe.id)
                if guild_emoji and str(guild_emoji) != entry["emoji"]:
                    entry["emoji"] = str(guild_emoji)
                    pe = _parse_emoji(entry["emoji"])
                    changed = True

            message = None
            if entry["channel_id"]:
                channel = guild.get_channel(entry["channel_id"])
                if channel:
                    try:
                        message = await channel.fetch_message(entry["message_id"])
                    except discord.NotFound:
                        results.append((entry, "message deleted"))
                        continue
                    except Exception as e:
                        results.append((entry, f"fetch failed: {e}"))
                        continue
                else:
                    results.append((entry, "channel gone"))
                    continue
            else:
                # Legacy entry without a channel: locate the message once.
                for candidate in guild.text_channels:
                    try:
                        message = await candidate.fetch_message(entry["message_id"])
                        entry["channel_id"] = candidate.id
                        changed = True
                        break
                    except Exception:
                        continue
                if message is None:
                    results.append((entry, "message not found in any channel"))
                    continue

            status = []
            if not guild.get_role(entry["role_id"]):
                status.append("role missing")
            has_reaction = any(
                getattr(r, "me", False) and _emoji_matches(entry["emoji"], discord.PartialEmoji.from_str(str(r.emoji)))
                for r in message.reactions)
            if not has_reaction:
                try:
                    await message.add_reaction(pe)
                    status.append("reaction re-added")
                except Exception as e:
                    status.append(f"reaction failed: {e}")
            results.append((entry, ", ".join(status) or "ok"))
        if changed:
            self._save(guild.id, entries)
        return results

    @tasks.loop(count=1)
    async def startup_sync(self):
        """Reconcile every guild once on load. Because cog reload re-runs this,
        a reload doubles as the apply-my-JSON-edits refresh path."""
        for guild in self.bot.guilds:
            if not self._entries(guild.id):
                continue
            try:
                results = await self._sync_guild(guild)
                fixed = [s for _, s in results if s != "ok"]
                self.logger.info(
                    f"Reaction-role sync for {guild.id}: {len(results)} entries, "
                    f"{len(fixed)} needing attention{': ' + '; '.join(fixed) if fixed else ''}")
            except Exception as e:
                self.logger.error(f"Reaction-role sync failed for guild {guild.id}: {e}")

    @startup_sync.before_loop
    async def before_startup_sync(self):
        await self.bot.wait_until_ready()

    # --- reaction listeners ---------------------------------------------------

    async def _process_reaction_toggle(self, payload, add: bool):
        # Never react to the bot's own pre-populating reactions.
        if self.bot.user and payload.user_id == self.bot.user.id:
            return
        if not payload.guild_id:
            return
        entries = self._entries(payload.guild_id)
        entry = next((e for e in entries if e["message_id"] == payload.message_id
                      and _emoji_matches(e["emoji"], payload.emoji)), None)
        if not entry:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        member = guild.get_member(payload.user_id)
        if not member:
            return
        target_role = guild.get_role(entry["role_id"])
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
