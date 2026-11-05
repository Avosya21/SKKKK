"""Проверка заголовка Host (защита от DNS rebinding)."""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.app.config import get_settings
from app.app.middleware.hosts import allowed_host_names, host_variants, request_host


class TrustedHostAllowlistMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        settings = get_settings()
        host = request_host(request)
        if not host:
            return JSONResponse({"detail": "Invalid Host header"}, status_code=400)

        allowed = allowed_host_names(settings)
        if not (host_variants(host) & allowed):
            return JSONResponse({"detail": "Invalid Host header"}, status_code=400)

        return await call_next(request)
