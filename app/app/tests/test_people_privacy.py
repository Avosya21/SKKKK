"""Права и приватность реестра людей (PLATFORM-FOUNDATION-PLAN, ПФ8). Все имена вымышленные."""
from __future__ import annotations

import json

import pytest

from app.app.services import people


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _user_id(telegram_id: str) -> int:
    from app.app.db import SessionLocal
    from app.app.models.user import User

    db = SessionLocal()
    try:
        return db.query(User).filter(User.telegram_id == telegram_id).one().id
    finally:
        db.close()


@pytest.fixture()
def world(client, admin_token, manager_token, user_token, member_card):
    """Проект менеджера (в нём участник-«user») и проект админа, куда у менеджера и «user» доступа нет."""
    mine = client.post("/projects", json={"name": "Проект менеджера", "customer_company": "ООО Пример",
                                          "customer_people": [{"name": "Ольга Заказчикова"}]}, headers=_auth(manager_token)).json()["id"]
    client.post(f"/projects/{mine}/members", json={"user_id": _user_id("tg-user"), "role": "user", **member_card}, headers=_auth(manager_token))
    foreign = client.post("/projects", json={"name": "Чужой проект"}, headers=_auth(admin_token)).json()["id"]
    return mine, foreign


STAFF_READ = ["/projects/{p}/people", "/projects/{p}/people/unknown", "/projects/{p}/people/log"]


def test_staff_endpoints_by_role(client, admin_token, manager_token, user_token, world):
    mine, foreign = world
    for path in STAFF_READ:
        assert client.get(path.format(p=mine), headers=_auth(admin_token)).status_code == 200
        assert client.get(path.format(p=mine), headers=_auth(manager_token)).status_code == 200
        assert client.get(path.format(p=mine), headers=_auth(user_token)).status_code in (403, 404)  # участник-«user» — не персонал
        assert client.get(path.format(p=foreign), headers=_auth(manager_token)).status_code == 404  # чужой проект не виден
        assert client.get(path.format(p=foreign), headers=_auth(user_token)).status_code == 404
    client.cookies.clear()
    assert client.get(STAFF_READ[0].format(p=mine)).status_code in (401, 403)


def test_staff_writes_forbidden_for_participant_and_outsider(client, manager_token, user_token, world):
    mine, foreign = world
    person = client.get(f"/projects/{mine}/people", headers=_auth(manager_token)).json()["people"][0]["id"]
    calls = [
        ("post", f"/projects/{{p}}/people", {"display_name": "Новый Человек"}),
        ("put", f"/projects/{{p}}/people/{person}", {"side": "customer"}),
        ("post", f"/projects/{{p}}/people/seed", None),
        ("put", f"/projects/{{p}}/parties", {"company_name": "Х", "side": "customer"}),
        ("put", f"/projects/{{p}}/people/{person}/voice-consent", {"given": True}),
        ("delete", f"/projects/{{p}}/people/{person}", None),
    ]
    for method, path, body in calls:
        kwargs = {"json": body} if body is not None else {}
        for token, pid in ((user_token, mine), (manager_token, foreign), (user_token, foreign)):
            r = getattr(client, method)(path.format(p=pid), headers=_auth(token), **kwargs)
            assert r.status_code in (403, 404), (method, path, r.status_code)


def test_platform_switch_and_forget_are_admin_only(client, admin_token, manager_token, world):
    mine, _ = world
    person = client.get(f"/projects/{mine}/people", headers=_auth(manager_token)).json()["people"][0]["id"]
    assert client.put(f"/projects/{mine}/people-platform", json={"enabled": True}, headers=_auth(manager_token)).status_code == 403
    assert client.delete(f"/projects/{mine}/people/{person}", headers=_auth(manager_token)).status_code == 403
    assert client.put(f"/projects/{mine}/people-platform", json={"enabled": True}, headers=_auth(admin_token)).status_code == 200


def test_directory_for_any_participant_has_no_private_fields(client, manager_token, user_token, world):
    mine, foreign = world
    ok = client.get(f"/projects/{mine}/people/directory", headers=_auth(user_token))
    assert ok.status_code == 200
    items = ok.json()["people"]
    assert {p["display_name"] for p in items} >= {"Ольга Заказчикова"}
    for item in items:
        assert set(item) == {"id", "display_name", "company_name", "job_title", "side"}  # ни написаний, ни голосов, ни согласий, ни учёток
    assert client.get(f"/projects/{foreign}/people/directory", headers=_auth(user_token)).status_code == 404
    text = json.dumps(ok.json(), ensure_ascii=False)
    for secret in ("aliases", "voice", "consent", "user", "side_source", "authority"):
        assert secret not in text


def test_registry_never_reaches_contract_or_agent_payloads(client, manager_token, world):
    """Реестр людей (написания, голоса, согласия, вопросы «Кто это?») не попадает в контракт и данные для внешнего агента."""
    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.services import contract as ctr

    mine, _ = world
    db = SessionLocal()
    try:
        person = people.resolve(db, mine, "Ольга Заказчикова").person
        people.add_alias(db, mine, person.id, "Секретное Написание")
        people.give_voice_consent(db, mine, person.id, basis="договор")
        people.note_unknown(db, mine, "chat_author", "Тайный Автор", example="цитата из переписки")
        db.commit()
        files = ctr.get_contract(db, get_settings(), mine).files
    finally:
        db.close()
    dump = json.dumps(files, ensure_ascii=False, default=str)
    for secret in ("Секретное Написание", "Тайный Автор", "voice_consent", "цитата из переписки", "unknown_actors", "people_log"):
        assert secret not in dump, secret


def test_forget_person_erases_names_voices_and_log(client, admin_token, manager_token, world):
    from app.app.db import SessionLocal
    from app.app.models.person import PeopleLog, Person, PersonAlias
    from app.app.models.speaker_profile import SpeakerProfile

    mine, _ = world
    db = SessionLocal()
    try:
        keep = people.create_person(db, mine, "Борис Остающийся")
        victim = people.create_person(db, mine, "Виктор Забываемый", side="customer", side_source="human")
        twin = people.create_person(db, mine, "Витя З.")  # объединён с ним: тот же человек
        people.merge(db, mine, victim.id, twin.id)
        people.give_voice_consent(db, mine, victim.id, basis="договор")
        db.add(SpeakerProfile(scope="project", project_id=mine, display_name="Виктор Забываемый", embedding=b"\0" * 8, embedding_dim=2, person_id=victim.id))
        db.commit()
        victim_id, keep_id = victim.id, keep.id
    finally:
        db.close()
    r = client.delete(f"/projects/{mine}/people/{victim_id}", headers=_auth(admin_token))
    assert r.status_code == 200
    assert {p["id"] for p in r.json()["people"]} >= {keep_id} and victim_id not in {p["id"] for p in r.json()["people"]}
    db = SessionLocal()
    try:
        assert db.query(Person).filter(Person.project_id == mine, Person.display_name.in_(["Виктор Забываемый", "Витя З."])).count() == 0
        assert db.query(PersonAlias).filter(PersonAlias.alias.in_(["Виктор Забываемый", "Витя З."])).count() == 0
        assert db.query(SpeakerProfile).filter(SpeakerProfile.person_id == victim_id).count() == 0
        blob = " ".join((e.before_json or "") + (e.after_json or "") for e in db.query(PeopleLog).filter(PeopleLog.project_id == mine).all())
        assert "Виктор" not in blob and "Витя" not in blob  # имя стёрто и из журнала
        assert db.query(PeopleLog).filter(PeopleLog.action == "forget").count() == 1
        assert db.query(Person).filter(Person.id == keep_id).one().display_name == "Борис Остающийся"  # другие не тронуты
    finally:
        db.close()
    assert client.delete(f"/projects/{mine}/people/{victim_id}", headers=_auth(admin_token)).status_code == 400  # второй раз — нет такого


def test_forget_keeps_account_and_global_voice(client, admin_token, world):
    from app.app.db import SessionLocal
    from app.app.models.speaker_profile import SpeakerProfile
    from app.app.models.user import User

    mine, _ = world
    db = SessionLocal()
    try:
        account = db.query(User).filter(User.telegram_id == "tg-user").one()
        person = next(p for p in people.list_people(db, mine) if p.user_id == account.id)  # участник — человек проекта (ЛГ3)
        shared = SpeakerProfile(scope="global", display_name="Сотрудник Пример", embedding=b"\0" * 8, embedding_dim=2, user_id=account.id, person_id=person.id)
        db.add(shared)
        db.commit()
        shared_id, account_id, person_id = shared.id, account.id, person.id
    finally:
        db.close()
    assert client.delete(f"/projects/{mine}/people/{person_id}", headers=_auth(admin_token)).status_code == 200
    db = SessionLocal()
    try:
        assert db.get(User, account_id) is not None
        kept = db.get(SpeakerProfile, shared_id)
        assert kept is not None and kept.user_id == account_id and kept.person_id is None  # общий голос сотрудника остаётся, связь снята
    finally:
        db.close()


def test_people_card_ui_hides_delete_from_manager(client, admin_token, manager_token, world):
    mine, _ = world
    js = client.get("/static/people.js").text
    assert "data-act=\"forget\"" in js and "window.PX.role === 'admin'" in js  # кнопка только для админа; сервер всё равно проверяет


def test_scan_script_finds_registry_names_in_files_and_ignores_common_words(db_engine, tmp_path, monkeypatch, capsys):
    import importlib.util
    from pathlib import Path

    from app.app.db import SessionLocal
    from app.app.models.project import Project
    from app.app.models.user import User

    db = SessionLocal()
    try:
        admin = User(name="scan-admin", telegram_id="tg-scan", role="admin", password_hash="x")
        db.add(admin)
        db.commit()
        project = Project(name="Скан", created_by=admin.id, is_active=True)
        db.add(project)
        db.commit()
        people.create_person(db, project.id, "Леонид Редкофамильный")
        people.create_person(db, project.id, "Анна")  # одиночное обычное имя искать не нужно
        db.commit()
    finally:
        db.close()
    path = Path(__file__).resolve().parents[3] / "scripts" / "scan_personal_data.py"
    spec = importlib.util.spec_from_file_location("scan_personal_data_script", path)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    dirty = tmp_path / "dirty.md"
    dirty.write_text("Тут упомянут леонид редкофамильный, а ещё Анна и менеджер.\n", encoding="utf-8")
    clean = tmp_path / "clean.md"
    clean.write_text("Анна пишет менеджеру. Иван Пример — вымышленный пример.\n", encoding="utf-8")
    monkeypatch.setattr(script, "_files", lambda _staged: [dirty, clean])
    monkeypatch.setattr(script, "ROOT", tmp_path)
    assert script.main([]) == 1
    out = capsys.readouterr().out
    assert "dirty.md:1" in out and "clean.md" not in out and "Совпадений: 1" in out
    monkeypatch.setattr(script, "_files", lambda _staged: [clean])
    assert script.main([]) == 0
