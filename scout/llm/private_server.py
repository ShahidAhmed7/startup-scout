"""Protect every inference/admin route, including vLLM's non-OpenAI endpoints."""

import hmac
import os


class BearerGate:
    def __init__(self, app):
        self.app = app
        self.expected = ("Bearer " + os.environ["QWEN_API_KEY"]).encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 4401})
        path = scope.get("path", "")
        if path == "/health" and scope.get("method") == "GET":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        authenticated = hmac.compare_digest(
            headers.get(b"authorization", b""), self.expected
        )
        allowed = (path == "/v1/models" and scope.get("method") == "GET") or (
            path == "/v1/chat/completions" and scope.get("method") == "POST"
        )
        if not authenticated or not allowed:
            await send(
                {
                    "type": "http.response.start",
                    "status": 401 if not authenticated else 404,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send(
                {"type": "http.response.body", "body": b'{"error":"unavailable"}'}
            )
            return
        return await self.app(scope, receive, send)
