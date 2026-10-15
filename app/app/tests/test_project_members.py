from __future__ import annotations


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _user_id(db_engine, telegram_id: str) -> int:
    from app.app.db import SessionLocal
    from app.app.models.user import User

    db = SessionLocal()
    try:
        return db.query(User).filter(User.telegram_id == telegram_id).one().id
    finally:
        db.close()


def test_grant_member_sees_project_and_cannot_assign_admin(client, manager_token, user_token, db_engine):
    pid = client.post("/projects", json={"name": "Share"}, headers=_auth(manager_token)).json()["id"]
    uid = _user_id(db_engine, "tg-user")
    listed = client.get(f"/projects/{pid}/members", headers=_auth(manager_token))
    assert listed.status_code == 200
    assert any(c["user_id"] == uid for c in listed.json()["candidates"])

    bad = client.post(
        f"/projects/{pid}/members",
        json={"user_id": uid, "role": "admin"},
        headers=_auth(manager_token),
    )
    assert bad.status_code == 400

    ok = client.post(
        f"/projects/{pid}/members",
        json={"user_id": uid, "role": "user"},
        headers=_auth(manager_token),
    )
    assert ok.status_code == 200
    assert any(m["user_id"] == uid and m["role"] == "user" for m in ok.json()["members"])

    ids = [p["id"] for p in client.get("/projects", headers=_auth(user_token)).json()]
    assert pid in ids
    assert client.get(f"/projects/{pid}", headers=_auth(user_token)).status_code == 200
    html = client.get(f"/projects/{pid}", headers=_auth(user_token)).text
    assert "Выдать доступ" not in html


def test_granted_manager_can_use_staff_api(client, admin_token, user_token, db_engine):
    pid = client.post("/projects", json={"name": "StaffShare"}, headers=_auth(admin_token)).json()["id"]
    uid = _user_id(db_engine, "tg-user")
    r = client.post(
        f"/projects/{pid}/members",
        json={"user_id": uid, "role": "manager"},
        headers=_auth(admin_token),
    )
    assert r.status_code == 200
    assert client.post(f"/projects/{pid}/reextract-entities", headers=_auth(user_token)).status_code == 403
    as_manager = client.post(
        "/auth/login",
        json={"name": "User", "telegram_id": "tg-user", "password": "manager-pass"},
    )
    assert as_manager.status_code == 200
    assert as_manager.json()["role"] == "manager"
    reextract = client.post(
        f"/projects/{pid}/reextract-entities",
        headers=_auth(as_manager.json()["access_token"]),
    )
    assert reextract.status_code == 200


def test_user_cannot_grant_access(client, manager_token, user_token, db_engine):
    pid = client.post("/projects", json={"name": "NoGrant"}, headers=_auth(manager_token)).json()["id"]
    assert client.get(f"/projects/{pid}/members", headers=_auth(user_token)).status_code == 404


def test_user_member_sees_project_but_cannot_grant_or_rebuild(client, manager_token, user_token, db_engine):
    pid = client.post("/projects", json={"name": "UserMember"}, headers=_auth(manager_token)).json()["id"]
    uid = _user_id(db_engine, "tg-user")
    assert client.post(
        f"/projects/{pid}/members",
        json={"user_id": uid, "role": "user"},
        headers=_auth(manager_token),
    ).status_code == 200
    assert client.get(f"/projects/{pid}", headers=_auth(user_token)).status_code == 200
    assert client.get(f"/projects/{pid}/members", headers=_auth(user_token)).status_code == 403
    assert client.post(f"/projects/{pid}/reextract-entities", headers=_auth(user_token)).status_code == 403
    html = client.get(f"/projects/{pid}", headers=_auth(user_token)).text
    assert "Выдать доступ" not in html


def test_remove_member_hides_project(client, manager_token, user_token, db_engine):
    pid = client.post("/projects", json={"name": "Revoke"}, headers=_auth(manager_token)).json()["id"]
    uid = _user_id(db_engine, "tg-user")
    client.post(
        f"/projects/{pid}/members",
        json={"user_id": uid, "role": "user"},
        headers=_auth(manager_token),
    )
    assert client.delete(f"/projects/{pid}/members/{uid}", headers=_auth(manager_token)).status_code == 204
    ids = [p["id"] for p in client.get("/projects", headers=_auth(user_token)).json()]
    assert pid not in ids
    assert client.get(f"/projects/{pid}", headers=_auth(user_token)).status_code == 404


def test_candidates_come_from_allowlist_without_account(client, manager_token):
    pid = client.post("/projects", json={"name": "Allowlist"}, headers=_auth(manager_token)).json()["id"]
    listed = client.get(f"/projects/{pid}/members", headers=_auth(manager_token))
    assert listed.status_code == 200
    logins = {c["login"] for c in listed.json()["candidates"]}
    assert "User" in logins
    assert "Admin" in logins
    assert "Bootstrap admin" in logins
    assert "Manager" not in logins
    r = client.post(
        f"/projects/{pid}/members",
        json={"login": "Bootstrap admin", "role": "user"},
        headers=_auth(manager_token),
    )
    assert r.status_code == 200
    names = [m["name"] for m in r.json()["members"]]
    assert "Bootstrap admin" in names
    leftover = {c["login"] for c in r.json()["candidates"]}
    assert "Bootstrap admin" not in leftover


def test_user_password_hides_staff_blocks_on_own_project(client, manager_token):
    pid = client.post("/projects", json={"name": "OwnAsUser"}, headers=_auth(manager_token)).json()["id"]
    r = client.post(
        "/auth/login",
        json={"name": "Manager", "telegram_id": "tg-manager", "password": "user-pass"},
    )
    assert r.status_code == 200
    assert r.json()["role"] == "user"
    html = client.get(f"/projects/{pid}", headers=_auth(r.json()["access_token"])).text
    assert "Выдать доступ" not in html
    assert "Удалённый доступ (MCP)" not in html
    assert 'id="mcp-access-pane"' not in html
    assert "currentRole = \"user\"" in html


def test_project_page_has_grant_block_for_manager(client, manager_token):
    pid = client.post("/projects", json={"name": "GrantUI"}, headers=_auth(manager_token)).json()["id"]
    html = client.get(f"/projects/{pid}", headers=_auth(manager_token)).text
    assert "Выдать доступ" in html
    assert 'id="grant-access-panel"' in html
    assert "Удалённый доступ (MCP)" in html
