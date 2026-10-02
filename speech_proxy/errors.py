from typing import Any

from starlette.responses import JSONResponse


class APIError(Exception):
    """A deliberately sanitized error safe to expose to a client."""

    def __init__(
        self,
        status: int,
        message: str,
        code: str = "invalid_request",
        param: str | None = None,
        error_type: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.param = param
        self.error_type = error_type or (
            "rate_limit_error"
            if status == 429
            else "authentication_error"
            if status == 401
            else "server_error"
            if status >= 500
            else "invalid_request_error"
        )
        self.headers = headers or {}

    def payload(self) -> dict[str, Any]:
        return {
            "error": {
                "message": self.message,
                "type": self.error_type,
                "param": self.param,
                "code": self.code,
            }
        }

    def response(self) -> JSONResponse:
        return JSONResponse(self.payload(), status_code=self.status, headers=self.headers)
