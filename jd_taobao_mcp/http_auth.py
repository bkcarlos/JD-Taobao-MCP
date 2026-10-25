from __future__ import annotations

import secrets
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class BearerAuth:
    """Protect every HTTP endpoint of the single-user MCP browser session."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        if len(token) < 32 or any(not 33 <= ord(c) <= 126 for c in token):
            raise ValueError("MCP_HTTP_TOKEN must be an independent random token of at least 32 ASCII characters.")
        self.app = app
        self.expected = ("Bearer " + token).encode("ascii")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] == "http":
            values = [v for k, v in scope.get("headers", []) if k.lower() == b"authorization"]
            if len(values) != 1 or not secrets.compare_digest(values[0], self.expected):
                response = JSONResponse(
                    {"error": "Unauthorized"}, status_code=401,
                    headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
