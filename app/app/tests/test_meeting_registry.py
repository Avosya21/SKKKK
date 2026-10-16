"""Участники комнаты -> люди проекта и «Кто это?» (PLATFORM-FOUNDATION-PLAN, ПФ7). Все имена вымышленные."""
from __future__ import annotations

import pytest

from app.app.services import people


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def room(client, manager_token, monkeypatch):
    """Проект с идущей встречей и функциями: войти гостем / завершить."""
    from app.app.services import meetings

    monkeypatch.setattr(meetings, "close_livekit_room", lambda _room: True)
    pid = client.post("/projects", json={"name": "Комната"}, headers=_auth(manager_token)).json()["id"]
    created = client.post(f"/projects/{pid}/meetings", headers=_auth(manager_token)).json()
    mid, token = created["id"], created["public_token"]
    client.post(f"/projects/{pid}/meetings/{mid}/start", headers=_auth(manager_token))

    def guest(name, guest_id, company=None):
        body = {"display_name": name, "guest_id": guest_id} | ({"company_name": company} if company else {})
        return client.post(f"/api/meetings/public/{token}/join", json=body)

    def end():
        return client.post(f"/projects/{pid}/meetings/{mid}/end", headers=_auth(manager_token))

    return pid, mid, guest, end


def _enable(pid):
    from app.app.db import SessionLocal
    from app.app.models.project import Project

    db = SessionLocal()
    try:
        project = db.get(Project, pid)
        project.set_settings({"memory_platform": True})
        db.commit()
    finally:
        db.close()


def _participants(mid):
    from app.app.db import SessionLocal
    from app.app.models.meeting import MeetingParticipant

    db = SessionLocal()
    try:
        return {r.display_name: r.person_id for r in db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == mid).all()}
    finally:
        db.close()


def test_platform_off_leaves_participants_unlinked(room):
    pid, mid, guest, end = room
    guest("Гость Один", "g-1")
    assert end().status_code == 200
    assert set(_participants(mid).values()) == {None}


def test_new_guest_becomes_person_known_is_linked_ambiguous_is_question(room):
    """Р7 (PEOPLE-FLOW-PLAN, ЛГ4): гость согласился на запись при входе — после встречи он человек проекта (компания и
    сторона — из указанной при входе); известный — привязка; одиночное имя нескольких людей — «Кто это?»."""
    from app.app.db import SessionLocal

    pid, mid, guest, end = room
    _enable(pid)
    db = SessionLocal()
    try:
        people.set_party(db, pid, "Заказчик Тест", "customer")
        known_id = people.create_person(db, pid, "Елена Образцова", side="customer", side_source="human").id
        people.create_person(db, pid, "Анна Первая")
        people.create_person(db, pid, "Анна Вторая")
        db.commit()
    finally:
        db.close()
    guest("Елена Образцова", "g-known")
    guest("Незнакомый Гость", "g-unknown", company="заказчик тест")
    guest("Без Компании", "g-nocompany")
    guest("Анна", "g-ambiguous")
    assert end().status_code == 200
    got = _participants(mid)
    assert got["Елена Образцова"] == known_id and got["Анна"] is None
    db = SessionLocal()
    try:
        fresh = people.get(db, pid, got["Незнакомый Гость"])
        assert (fresh.company_name, fresh.side) == ("Заказчик Тест", "customer") and fresh.voice_consent_at is not None
        bare = people.get(db, pid, got["Без Компании"])
        assert bare.company_name is None and bare.side == "unknown" and bare.voice_consent_at is not None
        queue = people.open_unknown(db, pid)
        assert [(q.kind, q.label) for q in queue] == [("guest", "Анна")]
        assert "встречи" in queue[0].examples_json
    finally:
        db.close()


def test_employee_becomes_person_with_card_side(room, manager_token):
    from app.app.db import SessionLocal
    from app.app.models.user import User

    pid, mid, guest, end = room
    _enable(pid)
    # сотрудник входит в комнату по учётке: участник типа member с user_id
    db = SessionLocal()
    try:
        from app.app.models.meeting import MeetingParticipant

        manager = db.query(User).filter(User.telegram_id == "tg-manager").one()
        db.add(MeetingParticipant(meeting_id=mid, user_id=manager.id, identity=f"member:{manager.id}", display_name="Manager", participant_type="member"))
        db.commit()
        manager_id = manager.id
    finally:
        db.close()
    assert end().status_code == 200
    db = SessionLocal()
    try:
        person = people.list_people(db, pid)
        mine = [p for p in person if p.user_id == manager_id]
        assert len(mine) == 1  # у автора проекта человек уже был: второй не создан
    finally:
        db.close()
    assert _participants(mid)["Manager"] == mine[0].id


def test_resolving_guest_links_all_meetings_and_future_ones(room, client, manager_token):
    from app.app.db import SessionLocal

    pid, mid, guest, end = room
    _enable(pid)
    db = SessionLocal()
    try:  # одиночное имя, под которое подходят двое, — вопрос «Кто это?»
        people.create_person(db, pid, "Гость Первый")
        people.create_person(db, pid, "Гость Второй")
        db.commit()
    finally:
        db.close()
    guest("Гость", "g-a")
    end()
    h = _auth(manager_token)
    queue = client.get(f"/projects/{pid}/people/unknown", headers=h).json()["items"]
    assert [q["label"] for q in queue] == ["Гость"]
    made = client.post(f"/projects/{pid}/people/unknown/{queue[0]['id']}/resolve", json={"new_name": "Иван Иванов", "side": "customer"}, headers=h)
    assert made.status_code == 200
    person_id = next(p["id"] for p in made.json()["people"] if p["display_name"] == "Иван Иванов")
    assert _participants(mid)["Гость"] == person_id  # запись прошлой встречи получила человека

    created = client.post(f"/projects/{pid}/meetings", headers=h).json()  # вторая встреча: тот же гость узнаётся без вопроса
    client.post(f"/projects/{pid}/meetings/{created['id']}/start", headers=h)
    client.post(f"/api/meetings/public/{created['public_token']}/join", json={"display_name": "Гость", "guest_id": "g-b"})
    client.post(f"/projects/{pid}/meetings/{created['id']}/end", headers=h)
    assert _participants(created["id"])["Гость"] == person_id
    db = SessionLocal()
    try:
        assert people.open_unknown(db, pid) == []
    finally:
        db.close()


def test_link_is_idempotent_and_does_not_duplicate_questions(room):
    from app.app.db import SessionLocal
    from app.app.models.meeting import Meeting

    pid, mid, guest, end = room
    _enable(pid)
    db = SessionLocal()
    try:
        people.create_person(db, pid, "Повторный Первый")
        people.create_person(db, pid, "Повторный Второй")
        db.commit()
    finally:
        db.close()
    guest("Повторный", "g-r")
    guest("Новый Гость", "g-new")
    end()
    db = SessionLocal()
    try:
        meeting = db.get(Meeting, mid)
        before = len(people.list_people(db, pid))
        again = people.link_meeting_participants(db, meeting)
        db.commit()
        assert again["unknown"] == 1 and len(people.open_unknown(db, pid)) == 1  # вопрос один, а не два
        assert again["created"] == 0 and len(people.list_people(db, pid)) == before  # гость не задвоился
    finally:
        db.close()


def test_registry_side_beats_company_default_in_analyze_card(room):
    from app.app.db import SessionLocal
    from app.app.models.meeting import MeetingParticipant
    from app.app.services import meeting_analyze

    pid, mid, guest, end = room
    _enable(pid)
    guest("Ольга Заказчикова", "g-s")
    db = SessionLocal()
    try:
        people.create_person(db, pid, "Ольга Заказчикова", side="customer", side_source="human", company_name="ООО Пример")
        db.commit()
        identity = db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == mid).one().identity
    finally:
        db.close()
    end()
    db = SessionLocal()
    try:
        card = meeting_analyze._participant_card(db, identity, pid)
        assert card["side"] == "customer" and card["company"] == "ООО Пример" and card["person_id"]
        assert meeting_analyze._participant_card(db, identity, None) == {}  # без проекта (или без платформы) — прежнее поведение
    finally:
        db.close()
