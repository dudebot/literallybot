"""Live MCP client registry and server-push for the ops MCP server.

A ServerMiddleware records every connected streamable-HTTP session
(session id, clientInfo name, protocol version, last seen) and keeps its
Connection so the bot can push a notification on the client\x27s standalone
stream. Used by `!moshimoshi`.

Delivery channel: `notifications/message` (MCP logging) on the standalone
GET stream. This is the only server-initiated channel that needs no extra
client capability on handshake-era (2025-xx) protocol versions. It is
best-effort: the SDK drops it silently when the client has no open stream,
and on 2026-07-28+ connections the standalone stream may not carry
`notifications/message` at all, so those are reported as not deliverable.
Whether a client shows the message to its model is up to the client.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

LOGGER_NAME = "moshimoshi"
STALE_AFTER = 30 * 60  # seconds; matches the SDK session idle timeout


@dataclass
class ClientInfo:
    session_id: str
    name: str
    version: str
    protocol_version: str
    first_seen: float
    last_seen: float
    connection: Any = field(repr=False, default=None)

    @property
    def modern(self) -> bool:
        return self.protocol_version >= "2026-07-28"


class ClientRegistry:
    def __init__(self) -> None:
        self._clients: Dict[str, ClientInfo] = {}

    def touch(self, session_id: Optional[str], connection: Any,
              protocol_version: str, now: Optional[float] = None) -> None:
        if not session_id:
            return
        now = time.time() if now is None else now
        params = getattr(connection, "client_params", None)
        info = getattr(params, "client_info", None) or getattr(params, "clientInfo", None)
        name = getattr(info, "name", None) or "unknown"
        version = getattr(info, "version", None) or ""
        known = self._clients.get(session_id)
        if known:
            known.last_seen = now
            known.connection = connection
            if name != "unknown":
                known.name, known.version = name, version
            return
        self._clients[session_id] = ClientInfo(
            session_id, name, version, protocol_version, now, now, connection)

    def live(self, now: Optional[float] = None) -> List[ClientInfo]:
        now = time.time() if now is None else now
        for sid in [s for s, c in self._clients.items()
                    if now - c.last_seen > STALE_AFTER]:
            del self._clients[sid]
        return sorted(self._clients.values(), key=lambda c: -c.last_seen)

    async def push(self, text: str, author: str) -> List[tuple]:
        """Push `text` to every live client. Returns (client, status) pairs,
        status one of: sent, no-stream, modern-protocol."""
        results = []
        for c in self.live():
            conn = c.connection
            if c.modern:
                results.append((c, "modern-protocol"))
                continue
            if conn is None or not getattr(conn, "has_standalone_channel", False):
                results.append((c, "no-stream"))
                continue
            await conn.notify("notifications/message", {
                "level": "notice",
                "logger": LOGGER_NAME,
                "data": {"from": author, "message": text},
            })
            results.append((c, "sent"))
        return results


registry = ClientRegistry()


class ClientTrackingMiddleware:
    """MCPServer middleware: record the session on every inbound message."""

    def __init__(self, reg: ClientRegistry = registry) -> None:
        self.reg = reg

    async def __call__(self, ctx, call_next):
        result = await call_next(ctx)
        try:
            # ServerSession has no public accessor for its Connection.
            conn = getattr(ctx.session, "_connection", None)
            self.reg.touch(getattr(conn, "session_id", None), conn,
                           ctx.protocol_version)
        except Exception:  # tracking must never break a request
            pass
        return result
