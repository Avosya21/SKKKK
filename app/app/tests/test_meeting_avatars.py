"""Созвон: имя сотрудника из карточки, фото в плитке (портал и гостевая страница), кеш фото."""

from __future__ import annotations

from app.app.tests.test_user_profile import jpeg


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _give_card(name: str) -> int:
    import app.app.db as db_mod
    from app.app.models.user import User
    from app.app.services import user_profile
    from app.app.services.companies import sync_companies

    with db_mod.SessionLocal() as db:
        company = sync_companies(db)[0]
        user = db.query(User).filter_by(name=name).one()
        user_profile.complete_onboarding(
            db, user, last_name="Петров", first_name="Пётр", job_title="Менеджер",
            company_id=company.id, avatar_bytes=jpeg(),
        )
        return user.id


def _live_meeting(client, token):
    pid = client.post("/projects", json={"name": "Meet"}, headers=_auth(token)).json()["id"]
    created = client.post(f"/projects/{pid}/meetings", headers=_auth(token)).json()
    client.post(f"/projects/{pid}/meetings/{created['id']}/start", headers=_auth(token))
    return pid, created["id"], created["public_token"]


def test_member_joins_with_card_name(client, manager_token):
    user_id = _give_card("Manager")
    pid, mid, _ = _live_meeting(client, manager_token)
    joined = client.post(f"/projects/{pid}/meetings/{mid}/join", headers=_auth(manager_token)).json()
    assert joined["identity"] == f"member:{user_id}"
    assert joined["display_name"] == "Пётр Петров"


def test_member_without_card_joins_with_login(client, manager_token):
    pid, mid, _ = _live_meeting(client, manager_token)
    joined = client.post(f"/projects/{pid}/meetings/{mid}/join", headers=_auth(manager_token)).json()
    assert joined["display_name"] == "Manager"


def test_guest_sees_photo_only_of_this_meeting_members(client, manager_token, admin_token):
    manager_id = _give_card("Manager")
    admin_id = _give_card("Admin")
    pid, mid, public = _live_meeting(client, manager_token)
    client.post(f"/projects/{pid}/meetings/{mid}/join", headers=_auth(manager_token))
    client.cookies.clear()
    ok = client.get(f"/api/meetings/public/{public}/avatars/{manager_id}?size=512")
    assert ok.status_code == 200 and ok.headers["content-type"] == "image/webp"
    assert "max-age=300" in ok.headers["cache-control"]  # адрес без версии — кеш короткий
    # Admin в этой встрече не был — его фото гостю не отдаём.
    assert client.get(f"/api/meetings/public/{public}/avatars/{admin_id}").status_code == 404
    assert client.get(f"/api/meetings/public/wrong-token/avatars/{manager_id}").status_code == 404
    # Без токена встречи и без входа — нельзя.
    assert client.get(f"/people/{manager_id}/avatar").status_code == 401


def test_portal_avatar_cache_depends_on_version(client, manager_token):
    user_id = _give_card("Manager")
    me = client.get("/me/profile", headers=_auth(manager_token)).json()
    versioned = client.get(me["avatar_url"], headers=_auth(manager_token))
    assert "immutable" in versioned.headers["cache-control"]
    plain = client.get(f"/people/{user_id}/avatar?size=512", headers=_auth(manager_token))
    assert plain.status_code == 200 and "max-age=300" in plain.headers["cache-control"]
    stale = client.get(f"/people/{user_id}/avatar?size=96&v=0000", headers=_auth(manager_token))
    assert "immutable" not in stale.headers["cache-control"]
