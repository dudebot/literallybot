import asyncio
import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Union
from urllib.parse import urlsplit

import aiohttp
import discord
from discord.ext import commands

from core.utils import is_superadmin


ChannelTarget = Union[discord.TextChannel, discord.Thread]
MessageableTarget = discord.abc.Messageable
BundleDict = Dict[str, Any]
MessageEntry = Dict[str, Any]


class ChannelMigrator(commands.Cog):
    """Tools for exporting and replaying channel history bundles."""

    CHAR_LIMIT = 1900  # Leave headroom for metadata
    POST_DELAY = 0.3
    ASSET_DELAY = 1.0
    STORAGE_DIR = Path("backups") / "channel_exports"
    JSONL_SUFFIX = ".jsonl"
    JSON_SUFFIX = ".json"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.logger = getattr(bot, "logger", None)
        self.storage_dir = self.STORAGE_DIR
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._no_mentions = discord.AllowedMentions.none()
        self._http_session = aiohttp.ClientSession()

    @commands.command(name="backupchannel")
    @commands.check(is_superadmin)
    async def backup_channel(
        self,
        ctx: commands.Context,
        channel: Optional[Union[ChannelTarget, str]] = None,
        guild_id: Optional[int] = None,
    ):
        """Export the current (or specified) channel into a local bundle."""
        target_channel = self._resolve_backup_channel(ctx, channel, guild_id)
        if not self._is_text_channel(target_channel):
            await ctx.send("Only text channels or threads can be exported.")
            return

        safe_name = self._generate_bundle_name(target_channel)
        bundle_path = self._bundle_path(safe_name)
        if bundle_path.exists():
            safe_name = self._dedupe_bundle_name(safe_name)
            bundle_path = self._bundle_path(safe_name)

        await ctx.send(
            f"Starting export of {target_channel.mention} into bundle `{safe_name}`. "
            "This may take a while for large histories."
        )

        message_count = 0
        try:
            with bundle_path.open("w", encoding="utf-8") as fp:
                header = self._build_bundle_header(ctx, target_channel, safe_name)
                fp.write(json.dumps(header) + "\n")
                async for message in target_channel.history(limit=None, oldest_first=True):
                    fp.write(json.dumps(self._wrap_message(message)) + "\n")
                    message_count += 1
                fp.write(json.dumps({"type": "summary", "message_count": message_count}) + "\n")
        except discord.Forbidden:
            await ctx.send("I cannot read that channel's history.")
            return
        except discord.HTTPException as exc:
            await ctx.send(f"Failed while reading history: {exc}")
            return

        await ctx.send(
            f"Export complete. Stored {message_count} messages at `{bundle_path}`.\n"
            f"Use `!migratehere {safe_name}` in the destination channel to replay."
        )

    @commands.command(name="migratehere")
    @commands.check(is_superadmin)
    async def migrate_here(
        self,
        ctx: commands.Context,
        bundle_name: str,
    ):
        """Replay a stored bundle into the current channel."""
        try:
            safe_name = self._sanitize_bundle_name(bundle_name)
        except commands.BadArgument as exc:
            await ctx.send(str(exc))
            return

        bundle_path = self._resolve_bundle_path(safe_name)
        if not bundle_path:
            await ctx.send(f"Bundle `{safe_name}` not found in {self.storage_dir}.")
            return

        target_channel = ctx.channel if self._is_text_channel(ctx.channel) else None
        if not target_channel:
            await ctx.send("Can only migrate into text channels or threads.")
            return

        total_entries = self._count_bundle_messages(bundle_path)
        await ctx.send(
            f"Replaying {total_entries} entries from `{safe_name}` into {target_channel.mention}. "
            "Attachments will be re-uploaded when possible (fallback to links if too large or unavailable)."
        )

        sent_entries = 0
        for entry in self._iter_bundle_messages(bundle_path):
            await self._replay_entry(target_channel, entry)
            sent_entries += 1

        await ctx.send(
            f"Migration complete. Replayed {sent_entries} entries from `{safe_name}` into {target_channel.mention}."
        )

    @commands.command(name="downloadbundleassets")
    @commands.check(is_superadmin)
    async def download_bundle_assets(
        self,
        ctx: commands.Context,
        bundle_name: Optional[str] = None,
    ):
        """Download all embed images and attachments from a bundle dump."""
        if bundle_name:
            try:
                safe_name = self._sanitize_bundle_name(bundle_name)
            except commands.BadArgument as exc:
                await ctx.send(str(exc))
                return
            bundle_path = self._resolve_bundle_path(safe_name)
        else:
            bundle_path = self._latest_bundle_path()
            safe_name = bundle_path.stem if bundle_path else None

        if not bundle_path:
            await ctx.send("No bundle found to download assets from.")
            return

        asset_dir = self.storage_dir / f"{safe_name}_assets"
        asset_dir.mkdir(parents=True, exist_ok=True)

        urls = self._collect_bundle_media_urls(bundle_path)
        if not urls:
            await ctx.send(f"No embed images or attachments found in `{safe_name}`.")
            return

        await ctx.send(f"Downloading {len(urls)} assets from `{safe_name}` into `{asset_dir}`.")

        downloaded = 0
        failed = 0
        for index, url in enumerate(sorted(urls)):
            filename = self._filename_from_url(url, index)
            payload = await self._download_with_retry(url, filename)
            if payload is None:
                failed += 1
                await asyncio.sleep(self.ASSET_DELAY)
                continue
            (asset_dir / filename).write_bytes(payload)
            downloaded += 1
            await asyncio.sleep(self.ASSET_DELAY)

        await ctx.send(
            f"Asset download complete. Saved {downloaded} files to `{asset_dir}` "
            f"({failed} failed)."
        )

    def cog_unload(self):
        if not self._http_session.closed:
            self.bot.loop.create_task(self._http_session.close())

    def _is_text_channel(self, channel: Any) -> bool:
        return isinstance(channel, (discord.TextChannel, discord.Thread))

    def _bundle_path(self, safe_name: str) -> Path:
        return self.storage_dir / f"{safe_name}{self.JSONL_SUFFIX}"

    def _resolve_bundle_path(self, safe_name: str) -> Optional[Path]:
        jsonl_path = self._bundle_path(safe_name)
        if jsonl_path.exists():
            return jsonl_path
        json_path = self.storage_dir / f"{safe_name}{self.JSON_SUFFIX}"
        if json_path.exists():
            return json_path
        return None

    def _latest_bundle_path(self) -> Optional[Path]:
        candidates = list(self.storage_dir.glob(f"*{self.JSONL_SUFFIX}")) + list(
            self.storage_dir.glob(f"*{self.JSON_SUFFIX}")
        )
        if not candidates:
            return None
        return max(candidates, key=lambda path: path.stat().st_mtime)

    def _generate_bundle_name(self, channel: ChannelTarget) -> str:
        guild_name = getattr(getattr(channel, "guild", None), "name", "guild")
        channel_name = getattr(channel, "name", str(channel.id))
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        base = f"{self._slugify(guild_name)}-{self._slugify(channel_name)}-{timestamp}"
        return self._sanitize_bundle_name(base)

    def _dedupe_bundle_name(self, base_name: str) -> str:
        counter = 1
        while True:
            candidate = self._sanitize_bundle_name(f"{base_name}-{counter}")
            if not self._bundle_path(candidate).exists():
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

    def _resolve_backup_channel(
        self,
        ctx: commands.Context,
        channel: Optional[Union[ChannelTarget, str]],
        guild_id: Optional[int],
    ) -> Optional[ChannelTarget]:
        if channel is None:
            return ctx.channel if self._is_text_channel(ctx.channel) else None
        if self._is_text_channel(channel):
            return channel  # type: ignore[return-value]

        channel_id = self._parse_channel_id(str(channel))
        if channel_id is None:
            return None

        guild = self.bot.get_guild(guild_id) if guild_id else ctx.guild
        if guild:
            resolved = guild.get_channel(channel_id)
        else:
            resolved = self.bot.get_channel(channel_id)
        return resolved if self._is_text_channel(resolved) else None

    def _parse_channel_id(self, raw: str) -> Optional[int]:
        match = re.match(r"^<#(\d+)>$", raw)
        if match:
            return int(match.group(1))
        if raw.isdigit():
            return int(raw)
        return None

    def _build_bundle_header(
        self,
        ctx: commands.Context,
        channel: ChannelTarget,
        bundle_name: str,
    ) -> BundleDict:
        return {
            "type": "bundle",
            "bundle_name": bundle_name,
            "exported_at": datetime.now(tz=timezone.utc).isoformat(),
            "exported_by": {
                "id": ctx.author.id,
                "name": str(ctx.author),
            },
            "source": {
                "guild_id": getattr(channel.guild, "id", None),
                "guild_name": getattr(channel.guild, "name", None),
                "channel_id": channel.id,
                "channel_name": getattr(channel, "name", str(channel.id)),
                "is_thread": isinstance(channel, discord.Thread),
            },
        }

    def _wrap_message(self, message: discord.Message) -> BundleDict:
        return {"type": "message", "message": self._serialize_message(message)}

    def _serialize_message(self, message: discord.Message) -> MessageEntry:
        return {
            "id": message.id,
            "created_at": message.created_at.replace(tzinfo=timezone.utc).isoformat(),
            "edited_at": message.edited_at.replace(tzinfo=timezone.utc).isoformat() if message.edited_at else None,
            "author": self._serialize_author(message.author),
            "content": message.content,
            "clean_content": message.clean_content,
            "attachments": [self._serialize_attachment(att) for att in message.attachments],
            "embeds": [embed.to_dict() for embed in message.embeds],
            "stickers": [self._serialize_sticker(sticker) for sticker in message.stickers],
            "jump_url": message.jump_url,
            "reference": self._serialize_reference(message.reference),
        }

    def _serialize_author(self, author: discord.abc.User) -> Dict[str, Any]:
        return {
            "id": author.id,
            "name": str(author),
            "display_name": getattr(author, "display_name", None),
        }

    def _serialize_attachment(self, attachment: discord.Attachment) -> Dict[str, Any]:
        return {
            "id": attachment.id,
            "filename": attachment.filename,
            "url": attachment.url,
            "content_type": attachment.content_type,
            "size": attachment.size,
        }

    def _serialize_sticker(self, sticker: discord.StickerItem) -> Dict[str, Any]:
        return {
            "id": sticker.id,
            "name": sticker.name,
            "format": getattr(sticker, "format", None),
        }

    def _serialize_reference(self, reference: Optional[discord.MessageReference]) -> Optional[Dict[str, Any]]:
        if not reference:
            return None
        resolved = reference.resolved
        resolved_author = None
        resolved_content = None
        if resolved and not isinstance(resolved, discord.DeletedReferencedMessage):
            resolved_author = str(getattr(resolved, "author", None)) if getattr(resolved, "author", None) else None
            resolved_content = getattr(resolved, "content", None)
        return {
            "message_id": reference.message_id,
            "channel_id": reference.channel_id,
            "guild_id": reference.guild_id,
            "resolved_author": resolved_author,
            "resolved_content": resolved_content,
        }

    def _count_bundle_messages(self, bundle_path: Path) -> int:
        if bundle_path.suffix == self.JSONL_SUFFIX:
            count = 0
            with bundle_path.open("r", encoding="utf-8") as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if payload.get("type") == "message":
                        count += 1
                    if payload.get("type") == "summary" and "message_count" in payload:
                        return int(payload["message_count"])
            return count
        bundle = self._read_bundle_json(bundle_path)
        return len(bundle.get("messages", []))

    def _iter_bundle_messages(self, bundle_path: Path) -> Iterable[MessageEntry]:
        if bundle_path.suffix == self.JSONL_SUFFIX:
            with bundle_path.open("r", encoding="utf-8") as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if payload.get("type") != "message":
                        continue
                    message = payload.get("message")
                    if isinstance(message, dict):
                        yield message
            return
        bundle = self._read_bundle_json(bundle_path)
        for entry in bundle.get("messages", []):
            if isinstance(entry, dict):
                yield entry

    def _read_bundle_json(self, path: Path) -> BundleDict:
        with path.open("r", encoding="utf-8") as fp:
            return json.load(fp)

    async def _replay_entry(self, channel: MessageableTarget, entry: MessageEntry):
        header = self._format_header(entry)
        content = entry.get("content") or ""
        chunks = self._chunk_text(content, self.CHAR_LIMIT) or [""]
        await self._send_chunks(channel, header, chunks)

        attachments = entry.get("attachments") or []
        if attachments:
            await self._handle_attachments(channel, attachments)

    def _format_header(self, entry: MessageEntry) -> str:
        timestamp = self._format_timestamp(entry.get("created_at"))
        author = entry.get("author") or {}
        author_name = author.get("display_name") or author.get("name") or "Unknown User"
        return f"{author_name} • {timestamp}"

    def _format_timestamp(self, raw: Optional[str]) -> str:
        timestamp_dt = self._parse_timestamp(raw)
        if timestamp_dt:
            return f"<t:{int(timestamp_dt.timestamp())}:R>"
        return raw or "unknown time"

    async def _send_chunks(self, channel: MessageableTarget, header: str, chunks: Iterable[str]) -> None:
        for index, chunk in enumerate(chunks):
            if index == 0:
                payload = f"**{header}**"
                if chunk:
                    payload = f"{payload}\n{chunk}"
            else:
                payload = chunk
            await channel.send(payload, allowed_mentions=self._no_mentions)
            await asyncio.sleep(self.POST_DELAY)

    def _parse_timestamp(self, raw: Optional[str]) -> Optional[datetime]:
        if not raw:
            return None
        normalized = raw.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized)
        except ValueError:
            return None

    async def _handle_attachments(self, channel: MessageableTarget, attachments: List[Dict[str, Any]]):
        filesize_limit = getattr(getattr(channel, "guild", None), "filesize_limit", 8 * 1024 * 1024)
        for attachment in attachments:
            url = attachment.get("url")
            filename = attachment.get("filename") or "file"
            size = attachment.get("size", 0)
            if not url:
                continue
            if size and size > filesize_limit:
                await channel.send(
                    (
                        f"[Attachment `{filename}` skipped: {size} bytes exceeds this guild's "
                        f"upload limit ({filesize_limit} bytes).]\n{url}"
                    ),
                    allowed_mentions=self._no_mentions,
                )
                await asyncio.sleep(self.POST_DELAY)
                continue
            payload = await self._download_with_retry(url, filename)
            if payload is None:
                await channel.send(
                    f"[Failed to download `{filename}` after retries. Linking original instead.]\n{url}",
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

    async def _download_with_retry(self, url: str, filename: str, attempts: int = 3) -> Optional[bytes]:
        for attempt in range(1, attempts + 1):
            try:
                async with self._http_session.get(url) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"HTTP {resp.status}")
                    return await resp.read()
            except Exception as exc:
                if attempt == attempts:
                    return None
                if self.logger:
                    self.logger.warning(
                        "Failed to download %s (attempt %d/%d): %s",
                        filename,
                        attempt,
                        attempts,
                        exc,
                    )
                await asyncio.sleep(self.POST_DELAY)
        return None

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

    def _collect_bundle_media_urls(self, bundle_path: Path) -> Set[str]:
        urls: Set[str] = set()
        for message in self._iter_bundle_messages(bundle_path):
            urls.update(self._extract_message_media_urls(message))
        return urls

    def _extract_message_media_urls(self, message: MessageEntry) -> Set[str]:
        urls: Set[str] = set()
        for attachment in message.get("attachments", []) or []:
            url = attachment.get("url")
            if isinstance(url, str) and url:
                urls.add(url)
        for embed in message.get("embeds", []) or []:
            if isinstance(embed, dict):
                urls.update(self._extract_embed_media_urls(embed))
        return urls

    def _extract_embed_media_urls(self, embed: Dict[str, Any]) -> Set[str]:
        urls: Set[str] = set()
        for key in ("thumbnail", "image", "images", "video"):
            urls.update(self._extract_urls_from_embed_section(embed.get(key)))
        return urls

    def _extract_urls_from_embed_section(
        self, section: Any, keys: Iterable[str] = ("url",)
    ) -> Set[str]:
        urls: Set[str] = set()
        if isinstance(section, dict):
            for key in keys:
                value = section.get(key)
                if isinstance(value, str) and value:
                    urls.add(value)
        elif isinstance(section, list):
            for item in section:
                urls.update(self._extract_urls_from_embed_section(item, keys=keys))
        elif isinstance(section, str):
            urls.add(section)
        return urls

    def _filename_from_url(self, url: str, index: int) -> str:
        parsed = urlsplit(url)
        name = Path(parsed.path).name or "file"
        return f"{index}_{name}"


async def setup(bot: commands.Bot):
    await bot.add_cog(ChannelMigrator(bot))
