"""ASGI middleware shared across the API.

Currently exposes only :class:`RequestIdMiddleware`, which assigns or
forwards an ``X-Request-ID`` header and binds it to the per-request
``contextvars`` slot used by the logger.
"""

from __future__ import annotations

import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.core.request_context import reset_request_id, set_request_id

REQUEST_ID_HEADER = "X-Request-ID"


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Tag every request with an id and echo it back to the client."""

    def __init__(self, app: ASGIApp, *, header_name: str = REQUEST_ID_HEADER) -> None:
        super().__init__(app)
        self._header_name = header_name

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[override]
        incoming = request.headers.get(self._header_name)
        request_id = incoming or uuid.uuid4().hex[:16]
        token = set_request_id(request_id)
        try:
            response = await call_next(request)
        finally:
            reset_request_id(token)
        response.headers[self._header_name] = request_id
        return response
