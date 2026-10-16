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
    assert empty.status_code == 200 and empty.json()["platform_enabled"] is False
    assert [p["display_name"] for p in empty.json()["people"]] == ["Manager"]  # автор проекта записан человеком сразу

    a = client.post(f"/projects/{pid}/people", json={"display_name": "Ольга Пример", "side": "customer", "company_name": "ООО Пример"}, headers=h)
    assert a.status_code == 201
    b = client.post(f"/projects/{pid}/people", json={"display_name": "Olga P"}, headers=h).json()["id"]
    a_id = a.json()["id"]
    dup = client.post(f"/projects/{pid}/people", json={"display_name": "ольга  пример"}, headers=h)
    assert dup.status_code == 400 and "уже есть" in dup.json()["detail"]

    # Р1: у человека с компанией сторона — от компании в проекте; поменять её у человека нельзя
    refused = client.put(f"/projects/{pid}/people/{a_id}", json={"side": "executor"}, headers=h)
    assert refused.status_code == 400 and "Стороны компаний" in refused.json()["detail"]
    upd = client.put(f"/projects/{pid}/people/{a_id}", json={"authority": "approver", "job_title": "Директор"}, headers=h)
    row = next(p for p in upd.json()["people"] if p["id"] == a_id)
    assert row["side"] == "customer" and row["side_source"] == "project_party" and row["authority"] == "approver" and row["job_title"] == "Директор"
    moved = client.put(f"/projects/{pid}/parties", json={"company_name": "ООО Пример", "side": "executor"}, headers=h).json()
    assert next(p for p in moved["people"] if p["id"] == a_id)["side"] == "executor"  # сторона компании — всем её людям
    no_company = client.put(f"/projects/{pid}/people/{b}", json={"side": "customer"}, headers=h).json()
    assert next(p for p in no_company["people"] if p["id"] == b)["side"] == "customer"  # без компании — как раньше
    cleared = client.put(f"/projects/{pid}/people/{a_id}", json={"clear_authority": True}, headers=h).json()
    assert next(p for p in cleared["people"] if p["id"] == a_id)["authority"] is None
    assert client.put(f"/projects/{pid}/people/{a_id}", json={"side": "чужая"}, headers=h).status_code == 400

    merged = client.post(f"/projects/{pid}/people/{a_id}/merge", json={"drop_id": b}, headers=h).json()
    assert b not in [p["id"] for p in merged["people"]] and a_id in [p["id"] for p in merged["people"]]
    assert "Olga P" in next(p for p in merged["people"] if p["id"] == a_id)["aliases"]
    log = client.get(f"/projects/{pid}/people/log", headers=h).json()["items"]
    assert {"create", "set_side", "update", "merge", "side_from_party"} <= {r["action"] for r in log}


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
    assert r.status_code == 200 and r.json()["created"] == 0
    assert [p["display_name"] for p in r.json()["people"]] == ["Manager"]


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


def test_create_project_with_parties_and_customer_people(client, manager_token):
    h = _auth(manager_token)
    r = client.post("/projects", json={
        "name": "С людьми", "customer_company": "ООО Пример",
        "customer_people": [{"name": "Ольга Заказчикова", "job_title": "Директор", "can_approve": True}, {"name": "Пётр Сотрудников"}],
    }, headers=h)
    assert r.status_code == 201
    pid = r.json()["id"]
    got = client.get(f"/projects/{pid}/people", headers=h).json()
    by_name = {p["display_name"]: p for p in got["people"]}
    assert by_name["Ольга Заказчикова"]["side"] == "customer" and by_name["Ольга Заказчикова"]["authority"] == "approver"
    assert by_name["Ольга Заказчикова"]["company_name"] == "ООО Пример" and by_name["Пётр Сотрудников"]["authority"] is None
    assert {"company_name": "ООО Пример", "side": "customer"} in got["parties"]
    assert any(p["user"] for p in got["people"])  # автор проекта записан человеком с учёткой


def test_create_project_rejects_duplicate_customer_names(client, manager_token):
    r = client.post("/projects", json={"name": "Дубли", "customer_people": [{"name": "Иван Пример"}, {"name": "иван  пример"}]}, headers=_auth(manager_token))
    assert r.status_code == 400


def test_create_project_without_people_still_works(client, manager_token):
    r = client.post("/projects", json={"name": "Просто"}, headers=_auth(manager_token))
    assert r.status_code == 201


def test_form_requires_customer_unless_internal_and_adds_new_company(client, manager_token):
    """ЛГ1 (Р2, Р3): форма шлёт ``internal``; без заказчика — только внутренний проект; новая компания — в справочник."""
    h = _auth(manager_token)
    missing = client.post("/projects", json={"name": "Без заказчика", "internal": False}, headers=h)
    assert missing.status_code == 400 and "Внутренний проект" in missing.json()["detail"]
    inner = client.post("/projects", json={"name": "Планёрки", "internal": True, "executor_company": "ACME",
                                           "customer_company": "Лишняя", "customer_people": [{"name": "Лишний Человек"}]}, headers=h)
    assert inner.status_code == 201
    got = client.get(f"/projects/{inner.json()['id']}/people", headers=h).json()
    assert got["parties"] == [{"company_name": "ACME", "side": "executor"}]
    assert not any(p["display_name"] == "Лишний Человек" for p in got["people"])
    made = client.post("/projects", json={"name": "Новый клиент", "internal": False, "customer_company": "ООО «Клиент Новый»"}, headers=h)
    assert made.status_code == 201
    names = {c["name"]: c["side"] for c in client.get("/companies", headers=h).json()["items"]}
    assert names.get("ООО «Клиент Новый»") == "customer"
    people_view = client.get(f"/projects/{made.json()['id']}/people", headers=h).json()
    assert any(c["name"] == "ООО «Клиент Новый»" for c in people_view["companies"])  # выбор для сторон проекта


def test_internal_project_is_marked_in_settings(client, manager_token, db_engine):
    from app.app.db import SessionLocal
    from app.app.models.project import Project

    pid = client.post("/projects", json={"name": "Внутренний", "internal": True}, headers=_auth(manager_token)).json()["id"]
    db = SessionLocal()
    try:
        assert db.get(Project, pid).settings().get("internal_project") is True
    finally:
        db.close()


def test_company_names_are_normalized_to_shared_list(client, admin_token, manager_token, tmp_data):
    h_admin = _auth(admin_token)
    added = client.put("/companies", json={"name": "Компания Общая", "side": "customer"}, headers=h_admin)
    assert added.status_code == 200 and any(c["name"] == "Компания Общая" and c["side"] == "customer" for c in added.json()["items"])
    pid = _project(client, admin_token, "Нормализация")
    unknown_side = client.post(f"/projects/{pid}/people", json={"display_name": "Анна Пример", "company_name": "  компания   общая "}, headers=h_admin)
    assert unknown_side.status_code == 400 and "нет в сторонах проекта" in unknown_side.json()["detail"]  # Р1: сначала сторона
    p = client.post(f"/projects/{pid}/people", json={"display_name": "Анна Пример", "company_name": "  компания   общая ", "side": "customer"},
                    headers=h_admin).json()
    assert {"company_name": "Компания Общая", "side": "customer"} in p["parties"]
    assert next(x for x in p["people"] if x["display_name"] == "Анна Пример")["company_name"] == "Компания Общая"


def test_companies_api_permissions_and_file_roundtrip(client, admin_token, manager_token, user_token, tmp_data):
    from app.app.config import get_settings

    assert client.get("/companies", headers=_auth(user_token)).status_code == 403
    assert client.get("/companies", headers=_auth(manager_token)).status_code == 200
    assert client.put("/companies", json={"name": "X", "side": "customer"}, headers=_auth(manager_token)).status_code == 403
    h = _auth(admin_token)
    assert client.put("/companies", json={"name": "Плохая | компания", "side": "customer"}, headers=h).status_code == 400
    assert client.put("/companies", json={"name": "Новая Компания", "side": "customer"}, headers=h).status_code == 200
    text = get_settings().companies_path.read_text(encoding="utf-8")
    assert "Новая Компания | Заказчик" in text and text.startswith("ACME")
    changed = client.put("/companies", json={"name": "новая компания", "side": "executor"}, headers=h).json()["items"]
    assert [c["side"] for c in changed if c["name"] == "Новая Компания"] == ["executor"]  # то же название, другой регистр — не дубль
    hidden = client.put("/companies", json={"name": "Новая Компания", "side": "executor", "active": False}, headers=h).json()["items"]
    row = next(c for c in hidden if c["name"] == "Новая Компания")
    assert row["active"] is False
    assert "Новая Компания" not in get_settings().companies_path.read_text(encoding="utf-8")
    assert client.get("/admin/companies", headers=_auth(user_token)).status_code in (401, 403)


def test_new_project_form_has_parties_fields(client, manager_token):
    html = client.get("/", headers=_auth(manager_token)).text
    assert "customer_company" in html and "project-customer-people" in html and "executor_company" in html


def test_deleting_project_removes_registry(client, admin_token, db_engine):
    """Реестр людей уходит вместе с проектом: без этого удаление падало бы на внешних ключах."""
    from app.app.db import SessionLocal
    from app.app.models.person import PeopleLog, Person, PersonAlias, ProjectParty, UnknownActor
    from app.app.services import people

    h = _auth(admin_token)
    pid = client.post("/projects", json={"name": "Удаляемый", "customer_company": "ООО Пример",
                                         "customer_people": [{"name": "Ольга Заказчикова"}]}, headers=h).json()["id"]
    keep = client.post("/projects", json={"name": "Остаётся", "customer_people": [{"name": "Иван Оставшийся"}]}, headers=h).json()["id"]
    db = SessionLocal()
    try:
        people.note_unknown(db, pid, "chat_author", "Кто-то")
        a, b = people.create_person(db, pid, "Дубль А"), people.create_person(db, pid, "Дубль Б")
        people.merge(db, pid, a.id, b.id)  # объединённый человек ссылается на другого (merged_into)
        db.commit()
    finally:
        db.close()
    assert client.delete(f"/projects/{pid}", headers=h).status_code in (200, 204)
    db = SessionLocal()
    try:
        for model in (Person, PersonAlias, ProjectParty, UnknownActor, PeopleLog):
            assert db.query(model).filter(model.project_id == pid).count() == 0, model.__name__
        assert db.query(Person).filter(Person.project_id == keep).count() >= 1  # чужой проект не тронут
    finally:
        db.close()
