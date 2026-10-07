"""MCP bind allowlist on the real streamable-HTTP app.

A non-loopback bind must not turn FastMCP's Host check off, and a concrete
LAN address must work with only MCP_OPS_HOST. A wildcard must name the
client-facing hosts or refuse to start.
"""
import json
from types import SimpleNamespace as NS

import httpx
import pytest

from core import mcp_server
from core.ops import registry

LAN = "192.0.2.10"
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "lan-test", "version": "0"},
    },
}


def _bot(config):
    config.set_global("mcp_tools_enabled", [registry.names()[0]])
    return NS(config=config, user=None)


def _app(config, host):
    mcp = mcp_server.build_server(bot=_bot(config), host=host)
    app = mcp_server.wrap_with_auth(mcp.streamable_http_app(), "test-token")
    assert mcp.settings.transport_security.enable_dns_rebinding_protection
    return app


def _headers(token):
    headers = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
    }
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    return headers


async def _request(client, url, token):
    return await client.post(url, headers=_headers(token), content=json.dumps(INIT))


@pytest.mark.asyncio
async def test_concrete_lan_bind_allows_that_host_and_requires_bearer(config, monkeypatch):
    for name in (mcp_server.HOST_ENV_VAR, mcp_server.TRUSTED_HOSTS_ENV_VAR):
        monkeypatch.delenv(name, raising=False)
    assert mcp_server.resolve_host() == "127.0.0.1"
    app = _app(config, LAN)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=transport) as client:
        ok = await _request(client, f"http://{LAN}:8765/mcp", "test-token")
        assert ok.status_code == 200
        assert ok.headers.get("mcp-session-id")
        local = await _request(client, "http://127.0.0.1:8765/mcp", "test-token")
        assert local.status_code == 200
        denied = await _request(client, f"http://{LAN}:8765/mcp", None)
        assert denied.status_code == 401
        wrong = await _request(client, "http://evil.example:8765/mcp", "test-token")
        assert wrong.status_code == 421
        headers = _headers("test-token")
        headers["origin"] = "https://evil.example"
        bad_origin = await client.post(f"http://{LAN}:8765/mcp", headers=headers, json=INIT)
        assert bad_origin.status_code == 403


@pytest.mark.asyncio
async def test_wildcard_bind_requires_an_explicit_client_host_list(config, monkeypatch):
    monkeypatch.delenv(mcp_server.TRUSTED_HOSTS_ENV_VAR, raising=False)
    with pytest.raises(RuntimeError, match="MCP_OPS_TRUSTED_HOSTS"):
        mcp_server.transport_security_for("0.0.0.0")
    monkeypatch.setenv(mcp_server.TRUSTED_HOSTS_ENV_VAR, f"{LAN},ops.lan")
    app = _app(config, "0.0.0.0")
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=transport) as client:
        ok = await _request(client, f"http://{LAN}:8765/mcp", "test-token")
        assert ok.status_code == 200
        assert ok.headers.get("mcp-session-id")
        named = await _request(client, "http://ops.lan:8765/mcp", "test-token")
        assert named.status_code == 200
        wrong = await _request(client, "http://evil.example:8765/mcp", "test-token")
        assert wrong.status_code == 421
