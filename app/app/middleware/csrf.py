"""CSRF: для мутаций с cookie-сессией Origin/Referer должен быть из allowlist."""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.app.config import get_settings
from app.app.dependencies import SESSION_COOKIE_NAME
from app.app.middleware.hosts import csrf_allowed_hosts, host_from_url, host_variants

_MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Префиксы без CSRF (нет cookie-сессии портала или вход ещё не завершён).
_EXEMPT_PREFIXES = (
    "/auth/login",
    "/api/meetings/public/",
    "/mcp/",
    "/.well-known/",
    "/static/",
    "/client-log",
)


def _exempt(path: str) -> bool:
    if path in ("/client-log", "/health", "/health/ready"):
        return True
    return any(path == p or path.startswith(p) for p in _EXEMPT_PREFIXES)


def _source_host(request: Request) -> str | None:
    origin = (request.headers.get("origin") or "").strip()
    if origin and origin.lower() != "null":
        return host_from_url(origin)
    referer = (request.headers.get("referer") or "").strip()
    if referer:
        return host_from_url(referer)
    return None


class CsrfOriginMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method not in _MUTATING or _exempt(request.url.path):
            return await call_next(request)
        if SESSION_COOKIE_NAME not in request.cookies:
            return await call_next(request)

        settings = get_settings()
        source = _source_host(request)
        if not source:
            return JSONResponse({"detail": "CSRF: Origin or Referer required"}, status_code=403)

        allowed = csrf_allowed_hosts(settings, request)
        if not (host_variants(source) & allowed):
            return JSONResponse({"detail": "CSRF: Origin not allowed"}, status_code=403)

        return await call_next(request)
