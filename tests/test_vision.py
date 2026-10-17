"""Vision inputs: collection, full-body download, Pillow normalization, fallback."""
import io
from types import SimpleNamespace as NS

import pytest
from PIL import Image
from pydantic_ai.messages import BinaryContent

from cogs.optional.gpt import strip_images
from utils.ai_images import fetch_image, message_images, normalize_image
from core.llm.client import _to_pai_messages


def encode(img, fmt, **k):
    b = io.BytesIO(); img.save(b, fmt, **k); return b.getvalue()


JPEG = encode(Image.new("RGB", (64, 32), (0, 60, 255)), "JPEG")


def att(name, ctype="image/png"):
    return NS(content_type=ctype, filename=name,
              url=f"https://cdn.discordapp.com/attachments/1/{name}?ex=1")


def test_collects_reply_target_and_embed_images_but_not_foreign_hosts():
    trigger = NS(attachments=[att("a.png"), att("doc.pdf", "application/pdf"),
                              att("odd.jpg", None)], embeds=[])
    bot_post = NS(attachments=[att("b.webp", "image/webp")],
                  embeds=[NS(image=NS(proxy_url="https://media.discordapp.net/c.png", url=None),
                             thumbnail=NS(proxy_url=None, url="https://evil.example/x.png"))])
    urls = [item[2] for msg in (trigger, bot_post) for item in message_images(msg)]
    assert [u.split("/")[-1].split("?")[0] for u in urls] == ["a.png", "odd.jpg", "b.webp", "c.png"]


@pytest.mark.parametrize("fmt,mode,expect", [
    ("WEBP", "RGB", "image/jpeg"), ("PNG", "RGBA", "image/png"),
    ("GIF", "P", "image/jpeg"), ("JPEG", "RGB", "image/jpeg")])
def test_normalize_outputs_only_jpeg_or_png(fmt, mode, expect):
    out = normalize_image(encode(Image.new(mode, (40, 20)), fmt))
    assert out["media_type"] == expect
    assert Image.open(io.BytesIO(out["data"])).size == (40, 20)


def test_normalize_downscales_and_rejects_garbage():
    big = normalize_image(encode(Image.new("RGB", (5000, 2500)), "JPEG"), max_side=2048)
    assert Image.open(io.BytesIO(big["data"])).size == (2048, 1024)
    assert normalize_image(JPEG[:200]) is None  # truncated payload never reaches a provider


class FakeContent:
    def __init__(self, data):
        self.data = data
    async def iter_chunked(self, n):
        for i in range(0, len(self.data), 7):  # many small chunks, like a real socket
            yield self.data[i:i + 7]


class FakeResp:
    def __init__(self, ctype, data):
        self.status, self.headers = 200, {"Content-Type": ctype}
        self.content_length, self.content = len(data), FakeContent(data)
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False


class FakeSession:
    def __init__(self, *a, **k):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False
    def get(self, url, **kwargs):
        if "big" in url:
            return FakeResp("image/jpeg", b"x" * 10_000)
        if "html" in url:
            return FakeResp("text/html", b"<html>")
        return FakeResp("image/jpeg", JPEG)


@pytest.mark.asyncio
async def test_download_reads_whole_body_and_caps(monkeypatch):
    import aiohttp
    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)
    results = [await fetch_image(url, max_bytes=5_000) for url in
               ["https://cdn.discordapp.com/ok.jpg", "https://cdn.discordapp.com/big.jpg",
                "https://cdn.discordapp.com/html"]]
    got = [image for _, image in results if image]
    assert len(got) == 1 and got[0]["media_type"] == "image/jpeg"
    assert Image.open(io.BytesIO(got[0]["data"])).size == (64, 32)


def test_bytes_become_binary_content_and_fallback_strips_them():
    msgs = [{"role": "system", "content": "s"},
            {"role": "user", "content": "what is this",
             "images": [{"data": JPEG, "media_type": "image/jpeg"}]}]
    parts = _to_pai_messages(msgs)[0].parts[1].content
    assert isinstance(parts[1], BinaryContent) and parts[1].data == JPEG
    stripped = strip_images(msgs, "[note]")
    assert "images" not in stripped[1] and stripped[1]["content"].endswith("[note]")
    assert msgs[1]["images"]  # original untouched
