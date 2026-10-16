"""!moshimoshi gate and MCP client push registry."""
from types import SimpleNamespace as NS

import pytest

from cogs.optional.moshimoshi import allowed, spawn_argv
from core.mcp_clients import ClientRegistry, ClientTrackingMiddleware, STALE_AFTER

DUDEBOT = 125839498150936576


def test_gate_defaults_to_dudebot_only(config):
    assert allowed(config, DUDEBOT)
    assert not allowed(config, 1)
    config.set_global("superadmins", [1])
    assert not allowed(config, 1)  # superadmin alone is not enough


def test_gate_respects_disable_and_allowlist(config):
    config.set_global("moshimoshi_allowed_users", [str(DUDEBOT), 2])
    assert allowed(config, 2)
    config.set_global("moshimoshi_enabled", False)
    assert not allowed(config, DUDEBOT)


def test_spawn_targets_are_fixed():
    assert spawn_argv("grok", "/p")[0].endswith("/.local/bin/grok")
    assert "exec" in spawn_argv("codex", "/p")
    with pytest.raises(ValueError):
        spawn_argv("sh", "/p")


class FakeConn:
    def __init__(self, sid, name, stream=True):
        self.session_id = sid
        self.client_params = NS(client_info=NS(name=name, version="1"))
        self.has_standalone_channel = stream
        self.sent = []

    async def notify(self, method, params, opts=None):
        self.sent.append((method, params))


@pytest.mark.asyncio
async def test_middleware_tracks_and_push_reports_delivery():
    reg = ClientRegistry()
    mw = ClientTrackingMiddleware(reg)
    a, b = FakeConn("a" * 32, "codex"), FakeConn("b" * 32, "claude", stream=False)

    async def nxt(ctx):
        return "ok"
    for conn in (a, b):
        assert await mw(NS(session=NS(_connection=conn),
                           protocol_version="2025-11-25"), nxt) == "ok"
    reg.touch("c" * 32, FakeConn("c" * 32, "new"), "2026-07-28")
    status = {c.name: s for c, s in await reg.push("hi", "me")}
    assert status == {"codex": "sent", "claude": "no-stream",
                      "new": "modern-protocol"}
    method, params = a.sent[0]
    assert method == "notifications/message"
    assert params["data"] == {"from": "me", "message": "hi"}


def test_stale_sessions_expire():
    reg = ClientRegistry()
    reg.touch("s", FakeConn("s", "x"), "2025-11-25", now=0)
    assert reg.live(now=STALE_AFTER + 1) == []
