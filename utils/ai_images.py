"""Bounded Discord image inputs. Cache normalized bytes, never signed URLs."""
import asyncio
import logging
from collections import OrderedDict
import time
from urllib.parse import urlparse, urlunparse

IMAGE_HOSTS = {"cdn.discordapp.com", "media.discordapp.net",
               "images-ext-1.discordapp.net", "images-ext-2.discordapp.net"}
MAX_IMAGES = 8
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def message_images(message):
    """Stable (identity, label, URL) order: attachments, embed image/thumbnail."""
    candidates = []
    for att in getattr(message, 'attachments', []):
        name = getattr(att, 'filename', '') or 'attachment'
        if (getattr(att, 'content_type', '') or '').startswith('image/') or name.lower().endswith(
                ('.png', '.jpg', '.jpeg', '.webp', '.gif', '.heic', '.bmp')):
            candidates.append((getattr(att, 'id', None), name, str(att.url)))
    for embed in getattr(message, 'embeds', []):
        for field in (getattr(embed, 'image', None), getattr(embed, 'thumbnail', None)):
            url = getattr(field, 'proxy_url', None) or getattr(field, 'url', None)
            if url:
                candidates.append((None, urlparse(str(url)).path.rsplit('/', 1)[-1] or 'embed', str(url)))
    out, seen = [], set()
    for attachment_id, name, url in candidates:
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.hostname not in IMAGE_HOSTS:
            continue
        key = ('attachment', attachment_id) if attachment_id else ('url', urlunparse(parsed._replace(query='', fragment='')))
        if key not in seen:
            out.append((key, name, url))
            seen.add(key)
    return out


class ImageCache:
    """LRU bounded by entries AND processed bytes; failures expire after 30s.

    The semaphore covers fetch + Pillow work, and per-key tasks coalesce requests
    from different channels. Pillow runs off the event loop.
    """
    def __init__(self, count=64, max_bytes=64 * 1024 * 1024, failure_ttl=30, *, logger=None):
        self.logger = logger if logger is not None else logging.getLogger(__name__)
        self.entries = OrderedDict()
        self.bytes = 0
        self.count, self.max_bytes, self.failure_ttl = count, max_bytes, failure_ttl
        self.pending = {}
        self.semaphore = asyncio.Semaphore(4)

    def put(self, key, image):
        old = self.entries.pop(key, None)
        if old:
            self.bytes -= len(old[0]['data']) if old[0] else 0
        size = len(image['data']) if image else 0
        if size > self.max_bytes:
            image, size = None, 0
        self.entries[key] = (image, time.monotonic())
        self.bytes += size
        while len(self.entries) > self.count or self.bytes > self.max_bytes:
            value, _ = self.entries.popitem(last=False)[1]
            self.bytes -= len(value['data']) if value else 0
        return image

    async def get(self, item, message, channel, refreshed):
        key = item[0]
        old = self.entries.get(key)
        if old and (old[0] is not None or time.monotonic() - old[1] < self.failure_ttl):
            self.entries.move_to_end(key)
            return old[0]
        if key not in self.pending:
            task = asyncio.create_task(self._load(item, message, channel, refreshed))
            self.pending[key] = task
            task.add_done_callback(lambda done: self.pending.pop(key, None))
        task = self.pending[key]
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                self.pending.pop(key, None)

    async def _load(self, item, message, channel, refreshed):
        key, _, url = item
        async with self.semaphore:
            status, image = await fetch_image(url)
            if status in (403, 404) and urlparse(url).hostname == 'cdn.discordapp.com' and urlparse(url).query:
                # One shared refresh per message per preparation, including failures.
                if message.id not in refreshed:
                    refreshed[message.id] = asyncio.create_task(channel.fetch_message(message.id))
                try:
                    fresh = await refreshed[message.id]
                    replacement = next((v for v in message_images(fresh) if v[0] == key), None)
                    if replacement:
                        _, image = await fetch_image(replacement[2])
                except Exception:
                    image = None
            if image is None:
                safe_url = urlunparse(urlparse(url)._replace(query='', fragment=''))
                self.logger.warning("Image download/normalization failed: %s", safe_url)
            return self.put(key, image)


async def fetch_image(url, *, max_bytes=MAX_IMAGE_BYTES, timeout=15):
    """No redirects to arbitrary origins; read the complete capped body."""
    import aiohttp
    if urlparse(url).scheme != 'https' or urlparse(url).hostname not in IMAGE_HOSTS:
        return 0, None
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.get(url, allow_redirects=False) as response:
                if response.status != 200:
                    return response.status, None
                if not response.headers.get('Content-Type', '').startswith('image/') or (response.content_length or 0) > max_bytes:
                    return 200, None
                body = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        return 200, None
        image = await asyncio.to_thread(normalize_image, bytes(body))
        if image and len(image['data']) > max_bytes:
            image = None
        return 200, image
    except Exception:
        return 0, None


MAX_IMAGE_SIDE = 2048
MAX_DECODE_PIXELS = 40_000_000
MAX_DECODE_SIDE = 12_000


def normalize_image(data, *, max_side=MAX_IMAGE_SIDE):
    """Decode with Pillow and re-encode as JPEG (or PNG when there is
    transparency), downscaled to max_side. Animated GIFs use their first
    frame. Returns {"data", "media_type"} or None for undecodable input,
    so providers only ever receive complete jpeg/png payloads."""
    import io
    import warnings
    from PIL import Image
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as img:
                width, height = img.size
                if width * height > MAX_DECODE_PIXELS or max(width, height) > MAX_DECODE_SIDE:
                    return None
                if img.format == "JPEG":
                    img.draft("RGB", (max_side, max_side))
                img.load()
                img.seek(0) if getattr(img, "is_animated", False) else None
                img.thumbnail((max_side, max_side))
                alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
                out = io.BytesIO()
                if alpha:
                    img.convert("RGBA").save(out, "PNG", optimize=True)
                    return {"data": out.getvalue(), "media_type": "image/png"}
                img.convert("RGB").save(out, "JPEG", quality=85)
                return {"data": out.getvalue(), "media_type": "image/jpeg"}
    except Exception:
        return None


