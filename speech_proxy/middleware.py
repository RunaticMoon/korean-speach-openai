import asyncio
import secrets
import uuid
from collections.abc import Callable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import Settings
from .errors import APIError


class RequestGuard:
    """Authenticate and bound request bodies before multipart parsing or spooling."""

    def __init__(self, app: ASGIApp, settings: Callable[[], Settings]) -> None:
        self.app = app
        self.settings = settings
        self.active = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid.uuid4().hex
        response_started = False

        async def send_with_id(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                message["headers"] = [
                    *message.get("headers", []),
                    (b"x-request-id", request_id.encode()),
                    (b"cache-control", b"no-store"),
                ]
            await send(message)

        settings = self.settings()
        path = scope.get("path", "")
        protected = path.startswith("/v1") or path in {"/usage", "/ready"}
        expensive = path.startswith("/v1/audio/")
        counted = False
        try:
            headers = dict(scope.get("headers", []))
            if protected:
                authorization = headers.get(b"authorization", b"")
                scheme, _, token = authorization.partition(b" ")
                expected = settings.proxy_api_key.get_secret_value().encode()
                if scheme.lower() != b"bearer" or not secrets.compare_digest(token, expected):
                    raise APIError(
                        401,
                        "Invalid or missing API key",
                        "invalid_api_key",
                        error_type="authentication_error",
                        headers={"WWW-Authenticate": "Bearer"},
                    )
            if expensive:
                if self.active >= settings.max_concurrent_requests:
                    raise APIError(
                        503,
                        "Server is busy; retry later",
                        "server_busy",
                        error_type="server_error",
                        headers={"Retry-After": "1"},
                    )
                self.active += 1
                counted = True
            limit = (
                settings.max_upload_bytes + 65_536
                if path == "/v1/audio/transcriptions"
                else settings.max_tts_input_chars * 12 + 8192
            )
            length = headers.get(b"content-length")
            if length is not None:
                try:
                    size = int(length)
                except ValueError as exc:
                    raise APIError(400, "Invalid Content-Length", "invalid_request") from exc
                if size < 0:
                    raise APIError(400, "Invalid Content-Length", "invalid_request")
                if size > limit:
                    raise APIError(413, "Request body is too large", "request_too_large")

            async with asyncio.timeout(settings.request_timeout_seconds):
                body = bytearray()
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    body.extend(message.get("body", b""))
                    if len(body) > limit:
                        raise APIError(413, "Request body is too large", "request_too_large")
                    if not message.get("more_body", False):
                        break
                consumed = False

                async def bounded_receive() -> Message:
                    nonlocal consumed
                    if consumed:
                        return await receive()
                    consumed = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}

                await self.app(scope, bounded_receive, send_with_id)
        except APIError as exc:
            if not response_started:
                await exc.response()(scope, receive, send_with_id)
        except TimeoutError:
            if not response_started:
                await APIError(
                    504, "Request timed out", "request_timeout", error_type="server_error"
                ).response()(scope, receive, send_with_id)
        finally:
            if counted:
                self.active -= 1
