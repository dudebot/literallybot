"""Vision inputs: trigger + replied-to images, bytes download, multimodal parts."""
from types import SimpleNamespace as NS

import pytest
from pydantic_ai.messages import BinaryContent

from cogs.optional import gpt
from cogs.optional.gpt import download_images, referenced_message, trigger_images
from core.llm.client import _to_pai_messages

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def att(name, ctype="image/png"):
    return NS(content_type=ctype, url=f"https://cdn.discordapp.com/attachments/1/{name}?ex=1")


def test_collects_reply_target_and_embed_images_but_not_foreign_hosts():
    trigger = NS(attachments=[att("a.png"), att("doc.pdf", "application/pdf")], embeds=[])
    bot_post = NS(attachments=[att("b.webp", "image/webp")],
                  embeds=[NS(image=NS(proxy_url="https://media.discordapp.net/c.png", url=None),
                             thumbnail=NS(proxy_url=None, url="https://evil.example/x.png"))])
    urls = trigger_images(trigger, bot_post)
    assert [u.split("/")[-1].split("?")[0] for u in urls] == ["a.png", "b.webp", "c.png"]


@pytest.mark.asyncio
async def test_referenced_message_prefers_resolved_then_cache():
    import discord
    target = NS(id=5)
    ctx = NS(message=NS(reference=NS(message_id=5, resolved=None)), channel=None)
    assert await referenced_message(ctx, {5: target}) is target
    assert await referenced_message(NS(message=NS(reference=None))) is None


class FakeResp:
    def __init__(self, status, ctype, data):
        self.status, self.headers, self._d = status, {"Content-Type": ctype}, data
        self.content_length = len(data)
        self.content = self
    async def read(self, n):
        return self._d[:n]
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
    def get(self, url):
        if "big" in url:
            return FakeResp(200, "image/png", b"x" * 100)
        if "html" in url:
            return FakeResp(200, "text/html", b"<html>")
        return FakeResp(200, "image/png; charset=binary", PNG)


@pytest.mark.asyncio
async def test_download_keeps_images_within_cap(monkeypatch):
    import aiohttp
    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)
    got = await download_images(["https://cdn.discordapp.com/ok.png",
                                 "https://cdn.discordapp.com/big.png",
                                 "https://cdn.discordapp.com/html"], max_bytes=64)
    assert got == [{"data": PNG, "media_type": "image/png"}]


def test_bytes_become_binary_content_parts():
    parts = _to_pai_messages([{"role": "user", "content": "what is this",
                               "images": [{"data": PNG, "media_type": "image/png"}]}])[0].parts[0].content
    assert parts[0] == "what is this"
    assert isinstance(parts[1], BinaryContent) and parts[1].data == PNG
