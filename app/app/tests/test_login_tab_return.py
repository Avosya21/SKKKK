"""Вкладка помнит свою страницу: после входа в другой вкладке обновление /login возвращает туда."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app.app.dependencies import safe_return_path

ROOT = Path(__file__).resolve().parents[3]


def _next(location: str) -> str:
    query = parse_qs(urlparse(location).query)
    assert "next" in query
    return query["next"][0]


def test_safe_return_path_rejects_open_redirects():
    assert safe_return_path("/projects/4") == "/projects/4"
    assert safe_return_path("/projects/4?tab=tasks#card") == "/projects/4?tab=tasks#card"
    assert safe_return_path("/") == "/"
    assert safe_return_path(None) is None
    assert safe_return_path("") is None
    assert safe_return_path("/login") is None
    assert safe_return_path("/login?next=/projects/1") is None
    assert safe_return_path("https://evil.example/phish") is None
    assert safe_return_path("//evil.example") is None
    assert safe_return_path("/\\evil.example") is None
    assert safe_return_path("/projects/1\r\nSet-Cookie: x") is None


def test_anonymous_html_keeps_page_in_login_next(anon_client):
    r = anon_client.get("/projects/15", follow_redirects=False)
    assert r.status_code == 302
    assert _next(r.headers.get("location") or "") == "/projects/15"


def test_login_with_session_returns_to_this_tabs_page(cookie_auth_client):
    r = cookie_auth_client.get("/login", params={"next": "/projects/15"}, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers.get("location") == "/projects/15"


def test_login_with_session_ignores_foreign_next(cookie_auth_client):
    r = cookie_auth_client.get(
        "/login",
        params={"next": "https://evil.example/phish"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert 'id="login-form"' in r.text


def test_login_without_next_stays_on_form_for_explicit_sign_in(cookie_auth_client):
    """Вкладка, где человек сам вводит пароль, остаётся формой: уход на список проектов делает finish()."""
    r = cookie_auth_client.get("/login", follow_redirects=False)
    assert r.status_code == 200
    assert 'id="login-form"' in r.text


def test_client_saves_tab_page_before_login_redirect():
    text = (ROOT / "app" / "app" / "static" / "app.js").read_text(encoding="utf-8")
    assert "primary_analitics_return" in text
    assert "goLogin()" in text
    assert "addEventListener('storage'" in text
    assert "rememberReferrer" in text
    assert "location.href = '/login'" not in text
    login = (ROOT / "app" / "app" / "templates" / "login.html").read_text(encoding="utf-8")
    assert "resumeLoginTab" in login
    assert "location.href = '/'" in login
