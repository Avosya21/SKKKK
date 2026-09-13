from __future__ import annotations

import re

import httpx
from fastapi import HTTPException, Response, status

from app.app.config import Settings

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
CHALLENGE_TIMEOUT_SEC = 5


def acme_challenge_url(settings: Settings, token: str) -> str:
    port = settings.skkkk_acme_http_port
    return f"http://127.0.0.1:{port}/.well-known/acme-challenge/{token}"


def proxy_acme_challenge(settings: Settings, token: str) -> Response:
    if not TOKEN_RE.fullmatch(token):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    headers = {}
    if settings.skkkk_public_host:
        headers["Host"] = settings.skkkk_public_host
    try:
        upstream = httpx.get(
            acme_challenge_url(settings, token),
            headers=headers,
            timeout=CHALLENGE_TIMEOUT_SEC,
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="acme upstream") from exc
    return Response(content=upstream.content, status_code=upstream.status_code)
