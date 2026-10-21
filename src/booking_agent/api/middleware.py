"""ASGI and HTTP middlewares for request tracing and payload bounds."""

import json
import re
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Context variable holding the correlation ID for the current async execution flow.
request_id_ctx: ContextVar[str | None] = ContextVar("request_id_ctx", default=None)

_SAFE_REQUEST_ID_PATTERN: re.Pattern[str] = re.compile(r"^[a-zA-Z0-9_\-\.]{1,128}$")


def get_current_request_id() -> str | None:
    """Return the current correlation ID from contextvars, if set."""
    return request_id_ctx.get()


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Correlate requests using an incoming or generated X-Request-ID header."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Bind correlation ID into contextvars, invoke downstream, and set response header."""
        incoming_id = request.headers.get("X-Request-ID", "").strip()
        if incoming_id and _SAFE_REQUEST_ID_PATTERN.match(incoming_id):
            request_id = incoming_id
        else:
            request_id = str(uuid.uuid4())

        token = request_id_ctx.set(request_id)
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            request_id_ctx.reset(token)


class PayloadTooLargeError(Exception):
    """Internal sentinel exception raised when streaming request body exceeds max bytes."""


class BodySizeLimitMiddleware:
    """Enforce strict payload size limits at the ASGI boundary, returning HTTP 413."""

    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        """Initialize middleware with target ASGI application and byte limit."""
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Inspect Content-Length header or enforce streaming byte cap."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 1. Check Content-Length header if present
        for raw_key, raw_val in scope.get("headers", []):
            if raw_key.lower() == b"content-length":
                try:
                    content_length = int(raw_val)
                    if content_length > self.max_body_bytes:
                        await self._send_413(send)
                        return
                except ValueError:
                    pass

        # 2. Check streaming body chunks
        received_bytes = 0

        async def limited_receive() -> Message:
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                chunk = message.get("body", b"")
                received_bytes += len(chunk)
                if received_bytes > self.max_body_bytes:
                    raise PayloadTooLargeError()
            return message

        try:
            await self.app(scope, limited_receive, send)
        except PayloadTooLargeError:
            await self._send_413(send)

    async def _send_413(self, send: Send) -> None:
        """Send HTTP 413 Payload Too Large response."""
        body = json.dumps({"detail": "Payload exceeds maximum allowed size"}).encode("utf-8")
        headers: list[tuple[bytes, bytes]] = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
        ]
        # Attach correlation ID if set
        current_req_id = request_id_ctx.get()
        if current_req_id:
            headers.append((b"x-request-id", current_req_id.encode("ascii")))

        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": headers,
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": body,
                "more_body": False,
            }
        )
