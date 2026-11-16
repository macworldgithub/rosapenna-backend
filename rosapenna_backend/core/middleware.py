"""HTTP middleware for request size limits."""

import re

class RequestBodySizeLimitMiddleware:
    """Bound request bodies before FastAPI parses them."""

    def __init__(self, app, max_bytes: int = 1_048_576):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "").upper()
        path = scope.get("path", "")
        is_4k_capped = method == "POST" and bool(
            re.fullmatch(r"/staff/confirmations/[^/]{1,100}/retry", path)
            or re.fullmatch(r"/staff/pre-arrival/.*", path)
        )
        max_bytes = min(self.max_bytes, 4096) if is_4k_capped else self.max_bytes
        content_length = next(
            (value.decode("latin-1") for name, value in scope.get("headers", [])
             if name.lower() == b"content-length"),
            None,
        )
        if content_length is not None:
            try:
                if int(content_length) > max_bytes:
                    await self._reject(send, max_bytes, is_4k_capped)
                    return
            except ValueError:
                await self._reject(send, max_bytes, is_4k_capped)
                return

        chunks = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunks.extend(message.get("body", b""))
            if len(chunks) > max_bytes:
                await self._reject(send, max_bytes, is_4k_capped)
                return
            if not message.get("more_body", False):
                break

        body = bytes(chunks)
        replayed = False

        async def replay_receive():
            nonlocal replayed
            if replayed:
                return {"type": "http.disconnect"}
            replayed = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay_receive, send)

    async def _reject(self, send, max_bytes: int, no_store: bool = False):
        detail = f"Request body exceeds {max_bytes} bytes"
        body = ('{"detail":"' + detail + '"}').encode("utf-8")
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"cache-control", b"no-store"),
        ]
        await send({"type": "http.response.start", "status": 413, "headers": headers})
        await send({"type": "http.response.body", "body": body})
