"""Общий allowlist имён Host для CSRF и TrustedHost."""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import Request

from app.app.config import Settings


def normalize_host(value: str) -> str:
    return (value or "").strip().lower()


def host_variants(host: str) -> set[str]:
    """Имя с портом и без — оба варианта для сравнения."""
    h = normalize_host(host)
    if not h:
        return set()
    out = {h}
    if h.startswith("[") and "]" in h:
        # IPv6 [addr]:port
        close = h.find("]")
        bare = h[1:close]
        out.add(bare)
        out.add(h[: close + 1])
    elif h.count(":") == 1:
        out.add(h.rsplit(":", 1)[0])
    return {x for x in out if x}


def host_from_url(url: str) -> str | None:
    raw = (url or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    try:
        parsed = urlparse(raw)
    except ValueError:
        return None
    return normalize_host(parsed.netloc) or None


def request_host(request: Request) -> str:
    return normalize_host(request.headers.get("host", ""))


def allowed_host_names(settings: Settings) -> set[str]:
    """Разрешённые Host: loopback, WEB_*, PRIMARY_*, ALLOWED_HOSTS.

    Request Host сюда не добавляется — иначе TrustedHost бессмысленен.
    """
    names: set[str] = set()
    for base in ("localhost", "127.0.0.1", "::1", "testserver"):
        names |= host_variants(base)
        names |= host_variants(f"{base}:{settings.web_port}")

    wh = normalize_host(settings.web_host)
    if wh and wh not in ("0.0.0.0", "::"):
        names |= host_variants(wh)
        names |= host_variants(f"{wh}:{settings.web_port}")

    public = host_from_url(settings.web_public_url)
    if public:
        names |= host_variants(public)

    pub_host = normalize_host(settings.primary_analitics_public_host)
    if pub_host:
        names |= host_variants(pub_host)
        names |= host_variants(f"{pub_host}:{settings.primary_analitics_https_port}")

    for part in (settings.allowed_hosts or "").split(","):
        item = part.strip()
        if not item:
            continue
        if "://" in item:
            netloc = host_from_url(item)
            if netloc:
                names |= host_variants(netloc)
        else:
            names |= host_variants(item)

    return names


def csrf_allowed_hosts(settings: Settings, request: Request) -> set[str]:
    """CSRF: настроенный allowlist + фактический Host (после TrustedHost он уже доверенный)."""
    names = allowed_host_names(settings)
    rh = request_host(request)
    if rh:
        names |= host_variants(rh)
    return names
