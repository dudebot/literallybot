"""Hidden-unicode / lookalike punctuation on model-originated Discord sends."""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from core.ops import OpContext, registry
from core.utils import sanitize_outbound_text


def test_sanitize_folds_lookalike_backticks_and_quotes():
    # U+FF40 fullwidth grave (NFKC), U+02CB modifier grave, curly quotes.
    raw = "\uff40tags\uff40 is the *post\u2019s* \u201ctag_string\u201d \u02cbid:1\u02cb"
    assert sanitize_outbound_text(raw) == "`tags` is the *post's* \"tag_string\" `id:1`"


def test_sanitize_strips_hidden_payloads_and_odd_spaces():
    raw = "hello\u200b\u200c\ufeffworld\u00a0\u2003there\u202ehid"
    assert sanitize_outbound_text(raw) == "helloworld  therehid"


def test_sanitize_keeps_emoji_zwj_and_ascii():
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"  # 👨‍👩‍👧
    assert sanitize_outbound_text(f"`ok` {family}") == f"`ok` {family}"


@pytest.mark.asyncio
async def test_send_message_sanitizes_before_discord(config):
    guild = NS(id=7, owner=None)
    channel = NS(id=10, guild=guild, send=AsyncMock(return_value=NS(id=22, attachments=[])))
    ctx = OpContext(bot=NS(config=config, user=NS(id=999)), author=NS(id=1), guild=guild)
    result = await registry.call(
        "send_message", ctx, channel=channel,
        content="see \uff40foo\uff40\u200b now")
    assert result.ok
    assert channel.send.call_args.args[0] == "see `foo` now"
