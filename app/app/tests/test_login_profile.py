"""Шаг 3 входа — карточка (ФИО, компания, должность, фото) и своя карточка после входа."""

from __future__ import annotations

import pytest

from app.app.tests.conftest import ADMIN_PASS
from app.app.tests.test_user_profile import jpeg

GOOD = "correct-horse-battery"


@pytest.fixture()
def req(client, monkeypatch):
    from app.app.config import get_settings

    monkeypatch.setenv("PERSONAL_PASSWORD", "required")
    get_settings.cache_clear()
    return client


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _step1(client, name="Admin"):
    r = client.post("/auth/login", json={"name": name, "telegram_id": "tg-" + name, "password": ADMIN_PASS})
    assert r.status_code == 200, r.text
    return r.json()


def _to_profile_step(client, name="Admin"):
    first = _step1(client, name)
    assert first["step"] == "setup"
    r = client.post("/auth/login/setup", json={"ticket": first["ticket"], "password": GOOD, "password_confirm": GOOD})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["step"] == "profile" and "access_token" not in data
    return data["ticket"]


def _options(client, ticket):
    r = client.post("/auth/login/profile/options", json={"ticket": ticket})
    assert r.status_code == 200, r.text
    return r.json()


def _submit(client, ticket, *, photo=True, **fields):
    data = {"ticket": ticket, "last_name": "Иванов", "first_name": "Иван", "job_title": "Аналитик"}
    data.update({k: v for k, v in fields.items()})
    files = {"avatar": ("me.jpg", jpeg(), "image/jpeg")} if photo else None
    return client.post("/auth/login/profile", data=data, files=files)


def _acme(client, ticket):
    return next(c["id"] for c in _options(client, ticket)["companies"] if c["name"] == "ACME")


def test_first_login_goes_through_profile(req):
    ticket = _to_profile_step(req)
    opts = _options(req, ticket)
    assert [c["name"] for c in opts["companies"]] == ["ACME", "Заказчик Тест"]
    assert "side" not in str(opts["companies"]) and "Исполнитель" not in str(opts)  # сторона не видна
    assert "голос" in opts["consent_text"] and opts["company_locked"] is False

    r = _submit(req, ticket, company_id=_acme(req, ticket))
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    assert req.get("/projects", headers=_bearer(token)).status_code == 200
    me = req.get("/me/profile", headers=_bearer(token)).json()
    assert me["full_name"] == "Иван Иванов" and me["company"] == "ACME" and me["complete"] is True
    assert me["voice_consent_at"].endswith("Z")  # время на экран — с поясом
    photo = req.get(me["avatar_url"], headers=_bearer(token))
    assert photo.status_code == 200 and photo.headers["content-type"] == "image/webp"
    assert "immutable" in photo.headers["cache-control"]


def test_second_login_skips_profile(req):
    ticket = _to_profile_step(req)
    _submit(req, ticket, company_id=_acme(req, ticket))
    step = _step1(req)
    assert step["step"] == "personal"
    r = req.post("/auth/login/personal", json={"ticket": step["ticket"], "password": GOOD})
    assert r.status_code == 200 and "access_token" in r.json()


def test_existing_password_but_no_profile_goes_to_profile(req):
    from app.app.services import personal_password

    personal_password.set_initial("Admin", GOOD, GOOD)
    step = _step1(req)
    r = req.post("/auth/login/personal", json={"ticket": step["ticket"], "password": GOOD})
    assert r.json()["step"] == "profile"


@pytest.mark.parametrize(
    ("fields", "photo", "field"),
    [
        ({}, False, "avatar"),
        ({"company_id": ""}, True, "company_id"),
        ({"company_id": 999}, True, "company_id"),
        ({"last_name": " "}, True, "last_name"),
        ({"first_name": "Ив4н"}, True, "first_name"),
        ({"job_title": ""}, True, "job_title"),
    ],
)
def test_profile_errors_point_to_field(req, fields, photo, field):
    ticket = _to_profile_step(req)
    fields = {"company_id": _acme(req, ticket), **fields}
    r = _submit(req, ticket, photo=photo, **fields)
    assert r.status_code == 400, r.text
    assert r.json()["field"] == field and r.json()["detail"]
    # Пропуск не сгорел — ошибку можно исправить.
    assert _submit(req, ticket, company_id=_acme(req, ticket)).status_code == 200


def test_not_an_image_is_rejected(req):
    ticket = _to_profile_step(req)
    r = req.post(
        "/auth/login/profile",
        data={"ticket": ticket, "last_name": "Иванов", "first_name": "Иван", "job_title": "Аналитик",
              "company_id": _acme(req, ticket)},
        files={"avatar": ("me.jpg", b"GIF89a-not-really", "image/jpeg")},
    )
    assert r.status_code == 400 and r.json()["field"] == "avatar"


def test_profile_ticket_opens_only_profile_step(req):
    ticket = _to_profile_step(req)
    assert req.post("/auth/login/personal", json={"ticket": ticket, "password": GOOD}).status_code == 401
    assert req.post("/auth/login/setup", json={"ticket": ticket, "password": GOOD, "password_confirm": GOOD}).status_code == 401
    assert req.get("/projects", headers=_bearer(ticket)).status_code == 401
    first = _step1(req)  # пропуск шага 2 не открывает форму карточки
    assert req.post("/auth/login/profile/options", json={"ticket": first["ticket"]}).status_code == 401


def test_session_without_profile_flag_is_rejected(req, db_engine):
    """Сессия, выданная до карточек (pp есть, pf нет), — 401: человек дозаполнит карточку при входе."""
    import app.app.db as db_mod
    from app.app.config import get_settings
    from app.app.dependencies import create_access_token
    from app.app.models.user import User

    _step1(req)
    with db_mod.SessionLocal() as db:
        user = db.query(User).filter_by(name="Admin").one()
        token = create_access_token(get_settings(), user, "admin", pv=0, pp=True)
    assert req.get("/projects", headers=_bearer(token)).status_code == 401


def test_admin_assigned_company_is_locked(req, db_engine):
    import app.app.db as db_mod
    from app.app.models.company import Company
    from app.app.models.user import User
    from app.app.services import user_profile
    from app.app.services.companies import sync_companies

    ticket = _to_profile_step(req)
    with db_mod.SessionLocal() as db:
        sync_companies(db)
        customer = db.query(Company).filter_by(name="Заказчик Тест").one()
        user = db.query(User).filter_by(name="Admin").one()
        user_profile.set_company(db, user.id, customer.id)
        customer_id = customer.id
    opts = _options(req, ticket)
    assert opts["company_locked"] is True and opts["company_id"] == customer_id
    r = _submit(req, ticket, company_id=_acme(req, ticket))
    token = r.json()["access_token"]
    assert req.get("/me/profile", headers=_bearer(token)).json()["company"] == "Заказчик Тест"


def _logged_in(req):
    ticket = _to_profile_step(req)
    return _submit(req, ticket, company_id=_acme(req, ticket)).json()["access_token"]


def test_edit_own_profile_but_not_company(req):
    token = _logged_in(req)
    r = req.put("/me/profile", json={"last_name": "Петров", "first_name": "Пётр", "job_title": "Руководитель"},
                headers=_bearer(token))
    assert r.status_code == 200 and r.json()["full_name"] == "Пётр Петров"
    bad = req.put("/me/profile", json={"last_name": "Петров", "first_name": "Пётр", "job_title": "Руководитель",
                                       "company_id": 2}, headers=_bearer(token))
    assert bad.status_code == 422
    bad = req.put("/me/profile", json={"last_name": "", "first_name": "Пётр", "job_title": "Руководитель"},
                  headers=_bearer(token))
    assert bad.status_code == 400


def test_replace_own_avatar(req, tmp_data):
    token = _logged_in(req)
    me = req.get("/me/profile", headers=_bearer(token)).json()
    before = me["avatar_url"]
    r = req.put("/me/avatar", files={"avatar": ("new.jpg", jpeg(color=(5, 200, 5)), "image/jpeg")},
                headers=_bearer(token))
    assert r.status_code == 200
    after = r.json()["avatar_url"]
    assert after != before  # новая версия в адресе — браузер не покажет старое из кеша
    assert req.get(after, headers=_bearer(token)).status_code == 200
    files = sorted(p.name for p in (tmp_data / "people" / str(me["user_id"])).iterdir())
    new_key = after.split("v=")[1]
    assert files == [f"avatar-{new_key}-512.webp", f"avatar-{new_key}-96.webp"]  # старые удалены


def test_avatar_needs_login(req, anon_client):
    token = _logged_in(req)
    url = req.get("/me/profile", headers=_bearer(token)).json()["avatar_url"]
    req.cookies.clear()
    assert req.get(url).status_code == 401
