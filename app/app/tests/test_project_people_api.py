"""API «Люди и стороны» проекта (PLATFORM-FOUNDATION-PLAN, ПФ3). Все имена вымышленные."""
from __future__ import annotations


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _project(client, token, name="Люди"):
    return client.post("/projects", json={"name": name}, headers=_auth(token)).json()["id"]


def test_staff_creates_edits_and_merges_people(client, manager_token):
    pid = _project(client, manager_token)
    h = _auth(manager_token)
    empty = client.get(f"/projects/{pid}/people", headers=h)
    assert empty.status_code == 200 and empty.json()["people"] == [] and empty.json()["platform_enabled"] is False

    a = client.post(f"/projects/{pid}/people", json={"display_name": "Ольга Пример", "side": "customer", "company_name": "ООО Пример"}, headers=h)
    assert a.status_code == 201
    b = client.post(f"/projects/{pid}/people", json={"display_name": "Olga P"}, headers=h).json()["id"]
    a_id = a.json()["id"]
    dup = client.post(f"/projects/{pid}/people", json={"display_name": "ольга  пример"}, headers=h)
    assert dup.status_code == 400 and "уже есть" in dup.json()["detail"]

    upd = client.put(f"/projects/{pid}/people/{a_id}", json={"side": "executor", "authority": "approver", "job_title": "Директор"}, headers=h)
    row = next(p for p in upd.json()["people"] if p["id"] == a_id)
    assert row["side"] == "executor" and row["side_source"] == "human" and row["authority"] == "approver" and row["job_title"] == "Директор"
    cleared = client.put(f"/projects/{pid}/people/{a_id}", json={"clear_authority": True}, headers=h).json()
    assert next(p for p in cleared["people"] if p["id"] == a_id)["authority"] is None
    assert client.put(f"/projects/{pid}/people/{a_id}", json={"side": "чужая"}, headers=h).status_code == 400

    merged = client.post(f"/projects/{pid}/people/{a_id}/merge", json={"drop_id": b}, headers=h).json()
    assert [p["id"] for p in merged["people"]] == [a_id]
    assert "Olga P" in merged["people"][0]["aliases"]
    log = client.get(f"/projects/{pid}/people/log", headers=h).json()["items"]
    assert {"create", "set_side", "update", "merge"} <= {r["action"] for r in log}


def test_unknown_queue_resolves_to_person_new_person_or_ignore(client, manager_token, db_engine):
    from app.app.db import SessionLocal
    from app.app.services import people

    pid = _project(client, manager_token, "Кто это")
    h = _auth(manager_token)
    known = client.post(f"/projects/{pid}/people", json={"display_name": "Сергей Пример"}, headers=h).json()["id"]
    db = SessionLocal()
    try:
        ids = [people.note_unknown(db, pid, "chat_author", label, example="привет").id for label in ("Serge P", "Гость Один", "Бот")]
        db.commit()
    finally:
        db.close()
    assert client.get(f"/projects/{pid}/people/unknown", headers=h).json()["items"][0]["examples"] == ["привет"]
    assert client.get(f"/projects/{pid}/people", headers=h).json()["unknown_open"] == 3

    client.post(f"/projects/{pid}/people/unknown/{ids[0]}/resolve", json={"person_id": known}, headers=h)
    made = client.post(f"/projects/{pid}/people/unknown/{ids[1]}/resolve", json={"new_name": "Гость Один", "side": "customer"}, headers=h).json()
    assert any(p["display_name"] == "Гость Один" and p["side"] == "customer" for p in made["people"])
    done = client.post(f"/projects/{pid}/people/unknown/{ids[2]}/resolve", json={"ignore": True}, headers=h).json()
    assert done["unknown_open"] == 0
    assert "Serge P" in next(p for p in done["people"] if p["id"] == known)["aliases"]


def test_parties_and_from_user(client, manager_token, user_token, db_engine):
    from app.app.db import SessionLocal
    from app.app.models.user import User

    pid = _project(client, manager_token, "Стороны")
    h = _auth(manager_token)
    out = client.put(f"/projects/{pid}/parties", json={"company_name": "ООО Пример", "side": "customer"}, headers=h).json()
    assert out["parties"] == [{"company_name": "ООО Пример", "side": "customer"}]
    assert client.put(f"/projects/{pid}/parties", json={"company_name": "X", "side": "unknown"}, headers=h).status_code == 400

    db = SessionLocal()
    try:
        uid = db.query(User).filter(User.telegram_id == "tg-user").one().id
    finally:
        db.close()
    assert client.post(f"/projects/{pid}/people/from-user", json={"user_id": uid}, headers=h).status_code == 400  # не участник проекта
    client.post(f"/projects/{pid}/members", json={"user_id": uid, "role": "user"}, headers=h)
    listed = client.get(f"/projects/{pid}/people", headers=h).json()
    assert any(m["user_id"] == uid for m in listed["members_without_person"])
    created = client.post(f"/projects/{pid}/people/from-user", json={"user_id": uid}, headers=h)
    assert created.status_code == 201
    person = next(p for p in created.json()["people"] if p["user"] and p["user"]["id"] == uid)
    assert person["side_source"] in ("card", "human") and created.json()["members_without_person"] == []


def test_ordinary_user_has_no_access(client, manager_token, user_token, admin_token):
    pid = _project(client, manager_token, "Закрыто")
    for method, path in (("get", f"/projects/{pid}/people"), ("post", f"/projects/{pid}/people/seed")):
        r = getattr(client, method)(path, headers=_auth(user_token))
        assert r.status_code in (403, 404)
    assert client.get(f"/projects/{pid}/people", headers=_auth(admin_token)).status_code == 200


def test_seed_endpoint_runs_without_model(client, manager_token):
    pid = _project(client, manager_token, "Наполнение")
    r = client.post(f"/projects/{pid}/people/seed", headers=_auth(manager_token))
    assert r.status_code == 200 and r.json()["created"] == 0 and r.json()["people"] == []


def test_platform_switch_admin_only_and_changes_flag(client, admin_token, manager_token):
    pid = _project(client, admin_token, "Флаг")
    assert client.put(f"/projects/{pid}/people-platform", json={"enabled": True}, headers=_auth(manager_token)).status_code in (403, 404)
    on = client.put(f"/projects/{pid}/people-platform", json={"enabled": True}, headers=_auth(admin_token))
    assert on.status_code == 200 and on.json()["platform_enabled"] is True
    off = client.put(f"/projects/{pid}/people-platform", json={"enabled": False}, headers=_auth(admin_token)).json()
    assert off["platform_enabled"] is False
    log = client.get(f"/projects/{pid}/people/log", headers=_auth(admin_token)).json()["items"]
    assert {"platform_on", "platform_off"} <= {r["action"] for r in log}


def test_project_page_shows_people_card_only_to_staff(client, manager_token, user_token):
    pid = _project(client, manager_token, "Страница")
    uid = None
    from app.app.db import SessionLocal
    from app.app.models.user import User

    db = SessionLocal()
    try:
        uid = db.query(User).filter(User.telegram_id == "tg-user").one().id
    finally:
        db.close()
    client.post(f"/projects/{pid}/members", json={"user_id": uid, "role": "user"}, headers=_auth(manager_token))
    staff = client.get(f"/projects/{pid}", headers=_auth(manager_token)).text
    assert "Люди и стороны" in staff and "/static/people.js" in staff
    ordinary = client.get(f"/projects/{pid}", headers=_auth(user_token)).text
    assert "Люди и стороны" not in ordinary and "/static/people.js" not in ordinary
    assert client.get("/static/people.js").status_code == 200
