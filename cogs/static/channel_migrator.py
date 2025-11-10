import asyncio
import io
import json
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional, Union

import aiohttp
import discord
from discord.ext import commands

from core.utils import is_superadmin


def _superadmin_only(ctx: commands.Context) -> bool:
    """Shared permission check for this cog's commands."""
    return is_superadmin(ctx.bot.config, ctx.author.id)


class ChannelMigrator(commands.Cog):
    """Tools for exporting and replaying channel history bundles."""

    CHAR_LIMIT = 1900  # Leave headroom for metadata
    POST_DELAY = 0.3

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.logger = getattr(bot, "logger", None)
        self.storage_dir = os.path.join("backups", "channel_exports")
        os.makedirs(self.storage_dir, exist_ok=True)
        self._no_mentions = discord.AllowedMentions.none()
        self._http_session = aiohttp.ClientSession()

    @commands.command(name="backupchannel")
    @commands.check(_superadmin_only)
    async def backup_channel(
        self,
        ctx: commands.Context,
        channel: Optional[Union[discord.TextChannel, discord.Thread]] = None,
    ):
        """Export the current (or specified) channel into a local bundle."""
        target_channel = channel or ctx.channel

        if not isinstance(target_channel, (discord.TextChannel, discord.Thread)):
            await ctx.send("Only text channels or threads can be exported.")
            return

        safe_name = self._generate_bundle_name(target_channel)
        bundle_path = self._bundle_path(safe_name)

        if os.path.exists(bundle_path):
            safe_name = self._dedupe_bundle_name(safe_name)
            bundle_path = self._bundle_path(safe_name)

        await ctx.send(
            f"Starting export of {target_channel.mention} into bundle `{safe_name}`. "
            "This may take a while for large histories."
        )

        try:
            messages = []
            async for message in target_channel.history(limit=None, oldest_first=True):
                messages.append(self._serialize_message(message))
        except discord.Forbidden:
            await ctx.send("I cannot read that channel's history.")
            return
        except discord.HTTPException as exc:
            await ctx.send(f"Failed while reading history: {exc}")
            return

        bundle = {
            "bundle_name": safe_name,
            "exported_at": datetime.now(tz=timezone.utc).isoformat(),
            "exported_by": {
                "id": ctx.author.id,
                "name": str(ctx.author),
            },
            "source": {
                "guild_id": getattr(target_channel.guild, "id", None),
                "guild_name": getattr(target_channel.guild, "name", None),
                "channel_id": target_channel.id,
                "channel_name": target_channel.name if hasattr(target_channel, "name") else str(target_channel.id),
                "is_thread": isinstance(target_channel, discord.Thread),
            },
            "message_count": len(messages),
            "messages": messages,
        }

        with open(bundle_path, "w", encoding="utf-8") as fp:
            json.dump(bundle, fp, indent=2)

        await ctx.send(
            f"Export complete. Stored {len(messages)} messages at `{bundle_path}`.\n"
            f"Use `!migratehere {safe_name}` in the destination channel to replay."
        )

    @commands.command(name="migratehere")
    @commands.check(_superadmin_only)
    async def migrate_here(
        self,
        ctx: commands.Context,
        bundle_name: str,
        destination: Optional[Union[discord.TextChannel, discord.Thread]] = None,
    ):
        """Replay a stored bundle into the current (or specified) channel."""
        try:
            safe_name = self._sanitize_bundle_name(bundle_name)
        except commands.BadArgument as exc:
            await ctx.send(str(exc))
            return
        bundle_path = self._bundle_path(safe_name)

        if not os.path.exists(bundle_path):
            await ctx.send(f"Bundle `{safe_name}` not found in {self.storage_dir}.")
            return

        target_channel = destination or ctx.channel
        if not isinstance(target_channel, (discord.TextChannel, discord.Thread)):
            await ctx.send("Can only migrate into text channels or threads.")
            return

        try:
            with open(bundle_path, "r", encoding="utf-8") as fp:
                bundle = json.load(fp)
        except json.JSONDecodeError as exc:
            await ctx.send(f"Bundle `{safe_name}` is corrupt: {exc}")
            return

        total_entries = len(bundle.get("messages", []))
        await ctx.send(
            f"Replaying {total_entries} entries from `{safe_name}` into {target_channel.mention}. "
            "Attachments will be re-uploaded when possible (fallback to links if too large or unavailable)."
        )

        sent_entries = 0
        for entry in bundle.get("messages", []):
            await self._replay_entry(target_channel, entry)
            sent_entries += 1

        await ctx.send(
            f"Migration complete. Replayed {sent_entries} entries from `{safe_name}` into {target_channel.mention}."
        )

    def cog_unload(self):
        if not self._http_session.closed:
            self.bot.loop.create_task(self._http_session.close())

    def _bundle_path(self, safe_name: str) -> str:
        return os.path.join(self.storage_dir, f"{safe_name}.json")

    def _generate_bundle_name(self, channel: Union[discord.TextChannel, discord.Thread]) -> str:
        guild_name = getattr(getattr(channel, "guild", None), "name", "guild")
        channel_name = getattr(channel, "name", str(channel.id))
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        base = f"{self._slugify(guild_name)}-{self._slugify(channel_name)}-{timestamp}"
        return self._sanitize_bundle_name(base)

    def _dedupe_bundle_name(self, base_name: str) -> str:
        counter = 1
        while True:
            candidate = self._sanitize_bundle_name(f"{base_name}-{counter}")
            if not os.path.exists(self._bundle_path(candidate)):
                return candidate
            counter += 1

    def _sanitize_bundle_name(self, bundle_name: str) -> str:
        cleaned = "".join(ch for ch in bundle_name if ch.isalnum() or ch in ("-", "_")).strip("_-")
        if not cleaned:
            raise commands.BadArgument("Bundle name must contain letters, numbers, '-' or '_'")
        return cleaned.lower()

    def _slugify(self, value: str) -> str:
        normalized = "".join(ch.lower() if ch.isalnum() else "-" for ch in value)
        normalized = "-".join(filter(None, normalized.split("-")))
        return normalized or "channel"

    def _serialize_message(self, message: discord.Message) -> Dict:
        return {
            "id": message.id,
            "created_at": message.created_at.replace(tzinfo=timezone.utc).isoformat(),
            "edited_at": message.edited_at.replace(tzinfo=timezone.utc).isoformat() if message.edited_at else None,
            "author": {
                "id": message.author.id,
                "name": str(message.author),
                "display_name": getattr(message.author, "display_name", None),
            },
            "content": message.content,
            "clean_content": message.clean_content,
            "attachments": [
                {
                    "id": attachment.id,
                    "filename": attachment.filename,
                    "url": attachment.url,
                    "content_type": attachment.content_type,
                    "size": attachment.size,
                }
                for attachment in message.attachments
            ],
            "embeds": [embed.to_dict() for embed in message.embeds],
            "stickers": [
                {"id": sticker.id, "name": sticker.name, "format": getattr(sticker, "format", None)}
                for sticker in message.stickers
            ],
            "jump_url": message.jump_url,
            "reference": self._serialize_reference(message.reference),
        }

    def _serialize_reference(self, reference: Optional[discord.MessageReference]) -> Optional[Dict]:
        if not reference:
            return None
        resolved = reference.resolved
        return {
            "message_id": reference.message_id,
            "channel_id": reference.channel_id,
            "guild_id": reference.guild_id,
            "resolved_author": str(resolved.author) if resolved and resolved.author else None,
            "resolved_content": resolved.content if resolved else None,
        }

    async def _replay_entry(self, channel: discord.abc.Messageable, entry: Dict):
        timestamp_dt = self._parse_timestamp(entry.get("created_at"))
        if timestamp_dt:
            timestamp_repr = f"<t:{int(timestamp_dt.timestamp())}:R>"
        else:
            timestamp_repr = entry.get("created_at", "unknown time")
        author = entry.get("author", {})
        author_name = author.get("display_name") or author.get("name") or "Unknown User"
        header = f"{author_name} • {timestamp_repr}"

        content = entry.get("content") or ""
        chunks = self._chunk_text(content, self.CHAR_LIMIT) or [""]

        for index, chunk in enumerate(chunks):
            if index == 0:
                payload = f"**{header}**"
                if chunk:
                    payload = f"{payload}\n{chunk}"
            else:
                payload = chunk
            await channel.send(payload, allowed_mentions=self._no_mentions)
            await asyncio.sleep(self.POST_DELAY)

        attachments = entry.get("attachments") or []
        if attachments:
            await self._handle_attachments(channel, attachments)

        embeds = entry.get("embeds") or []
        if embeds:
            embed_notice = f"[Original message contained {len(embeds)} embed(s); re-post manually if needed.]"
            await channel.send(embed_notice, allowed_mentions=self._no_mentions)
            await asyncio.sleep(self.POST_DELAY)

    def _parse_timestamp(self, raw: Optional[str]) -> Optional[datetime]:
        if not raw:
            return None
        normalized = raw.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized)
        except ValueError:
            return None

    async def _handle_attachments(self, channel: discord.abc.Messageable, attachments: List[Dict]):
        filesize_limit = getattr(getattr(channel, "guild", None), "filesize_limit", 8 * 1024 * 1024)
        for attachment in attachments:
            url = attachment.get("url")
            filename = attachment.get("filename") or "file"
            size = attachment.get("size", 0)
            if not url:
                continue
            if size and size > filesize_limit:
                await channel.send(
                    f"[Attachment `{filename}` skipped: {size} bytes exceeds this guild's upload limit ({filesize_limit} bytes).]\n{url}",
                    allowed_mentions=self._no_mentions,
                )
                await asyncio.sleep(self.POST_DELAY)
                continue
            try:
                async with self._http_session.get(url) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"HTTP {resp.status}")
                    payload = await resp.read()
            except Exception as exc:
                await channel.send(
                    f"[Failed to download `{filename}` ({exc}). Linking original instead.]\n{url}",
                    allowed_mentions=self._no_mentions,
                )
                await asyncio.sleep(self.POST_DELAY)
                continue

            discord_file = discord.File(io.BytesIO(payload), filename=filename)
            try:
                await channel.send(file=discord_file, allowed_mentions=self._no_mentions)
            except discord.HTTPException as exc:
                await channel.send(
                    f"[Failed to upload `{filename}` ({exc}). Linking original instead.]\n{url}",
                    allowed_mentions=self._no_mentions,
                )
            await asyncio.sleep(self.POST_DELAY)

    def _chunk_text(self, text: str, limit: int) -> List[str]:
        if not text:
            return []
        chunks: List[str] = []
        remaining = text
        while remaining:
            if len(remaining) <= limit:
                chunks.append(remaining)
                break
            split_at = remaining.rfind("\n", 0, limit)
            if split_at == -1:
                split_at = remaining.rfind(" ", 0, limit)
            if split_at == -1:
                split_at = limit
            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:].lstrip("\n ")
        return chunks


async def setup(bot: commands.Bot):
    await bot.add_cog(ChannelMigrator(bot))
