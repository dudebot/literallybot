"""Bearer-token auth for the MCP ops spike.

This is deliberately NOT the MCP SDK's full OAuth `AuthSettings` /
`TokenVerifier` machinery (that expects a standing OAuth issuer, which is
overkill for a proof-of-concept with a single shared secret). Instead this is
a small ASGI middleware that checks a static bearer token from the
`Authorization` header against an env var before any request reaches the
FastMCP app.

Security model:
- OFF by default: the server (mcp_ops/run_mcp_server.py) will refuse to start
  unless MCP_OPS_ENABLED=1 is set (see run_mcp_server.py).
- Auth required: every request must carry `Authorization: Bearer <token>`
  matching MCP_OPS_TOKEN. No token configured => server refuses to start
  (fail closed, not fail open).
- No token comparison shortcuts: uses hmac.compare_digest to avoid timing
  side-channels on the comparison.
"""
from __future__ import annotations

import hmac
import os

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

TOKEN_ENV_VAR = "MCP_OPS_TOKEN"


class BearerTokenMiddleware(BaseHTTPMiddleware):
    """Rejects any request that doesn't carry the configured bearer token."""

    def __init__(self, app, token: str):
        super().__init__(app)
        if not token:
            raise ValueError(
                f"BearerTokenMiddleware requires a non-empty token "
                f"(set {TOKEN_ENV_VAR} in the environment)."
            )
        self._token = token

    async def dispatch(self, request: Request, call_next):
        header = request.headers.get("authorization", "")
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer" or not presented:
            return JSONResponse(
                {"error": "missing or malformed Authorization header; expected 'Bearer <token>'"},
                status_code=401,
            )
        if not hmac.compare_digest(presented, self._token):
            return JSONResponse({"error": "invalid token"}, status_code=401)
        return await call_next(request)


def load_token_from_env() -> str:
    """Read the required auth token from the environment.

    Raises RuntimeError if unset/empty — the server must fail closed rather
    than silently run unauthenticated.
    """
    token = os.environ.get(TOKEN_ENV_VAR, "").strip()
    if not token:
        raise RuntimeError(
            f"{TOKEN_ENV_VAR} is not set. The MCP ops server requires an auth "
            f"token and refuses to start without one — see README's MCP section."
        )
    return token


def wrap_with_auth(app: Starlette, token: str) -> Starlette:
    """Wrap a Starlette app so every request must present the bearer token."""
    app.add_middleware(BearerTokenMiddleware, token=token)
    return app
