"""Ограничение перебора паролей (пункт 4.7)."""
from __future__ import annotations

import time

from app.app.services.rate_limit import FailureLimiter
from app.app.tests.conftest import MANAGER_PASS


def test_limiter_blocks_after_limit_and_expires():
    lim = FailureLimiter(limit=3, window_sec=0.3)
    for _ in range(3):
        assert lim.retry_after("k") == 0
        lim.fail("k")
    assert lim.retry_after("k") >= 1
    assert lim.retry_after("other") == 0
    time.sleep(0.4)
    assert lim.retry_after("k") == 0


def _bad(client, name="Manager"):
    return client.post("/auth/login", json={"name": name, "telegram_id": "t", "password": "wrong-password"})


def test_login_brute_force_is_throttled_then_correct_password_still_blocked(client):
    codes = [_bad(client).status_code for _ in range(8)]
    assert codes == [401] * 8
    blocked = _bad(client)
    assert blocked.status_code == 429 and int(blocked.headers["Retry-After"]) >= 1
    # даже верный пароль не проходит, пока действует блокировка (иначе перебор бессмыслен)
    ok = client.post("/auth/login", json={"name": "Manager", "telegram_id": "t", "password": MANAGER_PASS})
    assert ok.status_code == 429


def test_successful_logins_do_not_count(client):
    for _ in range(12):
        r = client.post("/auth/login", json={"name": "Manager", "telegram_id": "t", "password": MANAGER_PASS})
        assert r.status_code == 200


def test_name_not_on_allowlist_counts_for_ip(client):
    for _ in range(8):
        assert _bad(client, name="not-in-list-xyz").status_code == 401
    assert _bad(client, name="not-in-list-xyz").status_code == 429


def test_security_headers_on_pages_and_api(client):
    for path in ("/login", "/health"):
        h = client.get(path).headers
        assert h["x-content-type-options"] == "nosniff"
        assert h["x-frame-options"] == "SAMEORIGIN"
        assert h["referrer-policy"] == "same-origin"
        assert "microphone=(self)" in h["permissions-policy"]
        assert "strict-transport-security" not in h  # осознанно: см. main.SECURITY_HEADERS


def test_client_log_accepts_js_errors_and_limits_flood(client, caplog):
    import logging

    caplog.set_level(logging.INFO)
    r = client.post("/client-log", json={"page": "/m/…", "message": "boom", "source": "app.js:1"})
    assert r.status_code == 204
    assert any("client.js_error" in rec.getMessage() for rec in caplog.records)
    assert client.post("/client-log", content=b"not json").status_code == 204  # мусор не роняет
    for _ in range(40):
        assert client.post("/client-log", json={"message": "x"}).status_code == 204  # всплеск тихо режется
