from __future__ import annotations

import secrets
import re
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


def http_transport_security(extra_hosts: str = "") -> TransportSecuritySettings:
    """Allow exact service hosts while retaining loopback and Origin checks."""
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    if extra_hosts:
        for entry in extra_hosts.split(","):
            host = entry.strip()
            match = re.fullmatch(r"([a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?)(?::([0-9]{1,5}))?", host)
            if not match:
                raise ValueError("MCP_HTTP_EXTRA_HOSTS requires exact hostnames with optional ports.")
            name, port = match.groups()
            if len(name) > 253 or any(not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
                                      for label in name.split(".")) or (port and not 1 <= int(port) <= 65535):
                raise ValueError("Invalid hostname or port in MCP_HTTP_EXTRA_HOSTS.")
            if host not in hosts:
                hosts.append(host)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
    )


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
