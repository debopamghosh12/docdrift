"""HTTP hardening for every response of the server: security headers (CSP with a per-request
nonce for the one inline script) and request body size limits (pure ASGI, so a large body is
refused before it is read into memory)."""

import secrets
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_FORM_BYTES = 64 * 1024  # web forms carry a ref, a memory rule or a URL
MAX_WEBHOOK_BYTES = 25 * 1024 * 1024  # GitHub caps webhook payloads at 25 MB
CDN_SCRIPTS = "https://cdn.tailwindcss.com https://unpkg.com"


def content_security_policy(nonce: str) -> str:
    return "; ".join([
        "default-src 'self'",
        f"script-src 'self' 'nonce-{nonce}' {CDN_SCRIPTS}",
        # Tailwind's CDN build and htmx inject <style> elements at runtime.
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        "font-src https://fonts.gstatic.com",
        "img-src 'self' data:",
        "connect-src 'self'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "object-src 'none'",
    ])


def security_headers(nonce: str) -> list[tuple[bytes, bytes]]:
    headers = {
        "content-security-policy": content_security_policy(nonce),
        "x-frame-options": "DENY",
        "x-content-type-options": "nosniff",
        "referrer-policy": "strict-origin-when-cross-origin",
        "strict-transport-security": "max-age=31536000; includeSubDomains",
        "cross-origin-opener-policy": "same-origin",
        "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=()",
    }
    return [(k.encode(), v.encode()) for k, v in headers.items()]


class BodyTooLarge(Exception):
    pass


class SecurityMiddleware:
    """Adds the headers above; answers 413 when a body exceeds its route's limit."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        nonce = secrets.token_urlsafe(16)
        scope.setdefault("state", {})["csp_nonce"] = nonce
        limit = MAX_WEBHOOK_BYTES if scope["path"] == "/webhook" else MAX_FORM_BYTES
        declared = _content_length(scope)
        if declared is not None and declared > limit:
            await _too_large(send, nonce)
            return
        received = 0
        started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise BodyTooLarge()
            return message

        async def send_with_headers(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                names = {name.lower() for name, _ in message.get("headers", [])}
                extra = [(k, v) for k, v in security_headers(nonce) if k not in names]
                message = {**message, "headers": [*message.get("headers", []), *extra]}
            await send(message)

        try:
            await self.app(scope, limited_receive, send_with_headers)
        except BodyTooLarge:
            if not started:
                await _too_large(send, nonce)


def _content_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


async def _too_large(send: Send, nonce: str) -> None:
    body = b"request body too large"
    headers: list[Any] = [(b"content-type", b"text/plain"), (b"content-length", str(len(body)).encode())]
    await send({"type": "http.response.start", "status": 413, "headers": [*headers, *security_headers(nonce)]})
    await send({"type": "http.response.body", "body": body})
