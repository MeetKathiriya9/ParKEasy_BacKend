"""A hard ceiling on request body size.

Starlette streams a multipart file part to a spooled temp file *before* any
route code runs, so a single request can otherwise fill the disk. `Content-Length`
is checked first because that covers every normal client -- browsers and axios
always set it for a `FormData` upload -- and the streaming count catches a
`Transfer-Encoding: chunked` body that omits it.

The streamed case cannot report a precise `413`: it is detected inside the
request body stream, after Starlette's multipart parser has taken over. Rather
than raise through framework internals, the body is ended early, which surfaces
as the parser's usual `400`. That still stops the transfer at the limit, which
is the part that matters for availability.

Kept as pure ASGI middleware so it runs before routing and costs nothing when
`Content-Length` is honest.
"""

from __future__ import annotations

import json

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send


def _too_large_body(max_bytes: int) -> bytes:
    """The same `{"error": {...}}` envelope every other failure uses."""
    return json.dumps(
        {
            "error": {
                "code": "REQUEST_TOO_LARGE",
                "message": (
                    f"Request body is larger than the {max_bytes // (1024 * 1024)} MB limit"
                ),
                "details": {"max_bytes": max_bytes},
            }
        }
    ).encode("utf-8")


class RequestBodyLimitMiddleware:
    """Reject oversized request bodies before they are read."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be >= 1")
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = self._declared_length(scope)
        if declared is not None and declared > self.max_bytes:
            await self._reject(send)
            return

        seen = 0

        async def limited_receive() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    # End the stream instead of forwarding more; the parser
                    # reports the truncated body as a 400.
                    return {"type": "http.request", "body": b"", "more_body": False}
            return message

        await self.app(scope, limited_receive, send)

    @staticmethod
    def _declared_length(scope: Scope) -> int | None:
        raw = Headers(scope=scope).get("content-length")
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError:
            # Malformed header: fall through to the streaming count.
            return None

    async def _reject(self, send: Send) -> None:
        body = _too_large_body(self.max_bytes)
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


__all__ = ["RequestBodyLimitMiddleware"]
