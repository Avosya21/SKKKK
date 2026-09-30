"""Реестр людей проекта (docs/plans/PLATFORM-FOUNDATION-PLAN.md, ПФ2). Все имена вымышленные."""
from __future__ import annotations

import json

import pytest

from app.app.models.person import PeopleLog
from app.app.services import people


@pytest.fixture()
def db(db_engine):
    import app.app.db as db_mod

    session = db_mod.SessionLocal()
    yield session
    session.close()


def _user(db, login):
    from app.app.models.user import User

    row = User(name=login, telegram_id="tg-" + login, role="manager", password_hash="x")
    db.add(row)
    db.commit()
    return row


@pytest.fixture()
def admin(db):
    return _user(db, "admin-example")


@pytest.fixture()
def project(db, admin):
    from app.app.models.project import Project

    row = Project(name="Пример", created_by=admin.id, is_active=True)
    db.add(row)
    db.commit()
    return row


def test_alias_key_ignores_case_yo_and_punctuation():
    assert people.alias_key("  Ёлка-Палка,  Иван ") == people.alias_key("елка палка иван")
    assert people.alias_key("") == "" and people.alias_key(None) == ""


def test_create_person_makes_alias_and_log(db, project, admin):
    person = people.create_person(db, project.id, "Пётр  Пример", side="customer", side_source="human", by_user_id=admin.id)
    db.commit()
    assert [a.alias for a in people.aliases_of(db, person.id)] == ["Пётр Пример"]
    log = db.query(PeopleLog).filter_by(person_id=person.id).one()
    assert log.action == "create" and json.loads(log.after_json)["side"] == "customer"
    with pytest.raises(people.PeopleError):
        people.create_person(db, project.id, "петр пример")  # то же написание в проекте


def test_one_account_one_person_per_project(db, project):
    u = _user(db, "anna-example")
    people.create_person(db, project.id, "Анна Пример", user_id=u.id)
    with pytest.raises(people.PeopleError):
        people.create_person(db, project.id, "Анна Другая", user_id=u.id)


def test_resolve_exact_first_name_and_ambiguous(db, project):
    a = people.create_person(db, project.id, "Мария Образец")
    people.create_person(db, project.id, "Олег Образец")
    people.create_person(db, project.id, "Олег Другой")
    db.commit()
    assert people.resolve(db, project.id, "мария образец").how == "exact"
    hit = people.resolve(db, project.id, "Мария")
    assert hit.person.id == a.id and hit.how == "first_name"
    assert people.resolve(db, project.id, "Олег") is None  # два Олега — не угадываем
    assert people.resolve(db, project.id, "Мария", allow_first_name=False) is None
    assert people.resolve(db, project.id, "Неизвестный") is None


def test_weak_source_does_not_override_human_side(db, project):
    p = people.create_person(db, project.id, "Иван Пример")
    people.set_side(db, project.id, p.id, "customer", source="human")
    people.set_side(db, project.id, p.id, "executor", source="model")
    assert p.side == "customer" and p.side_source == "human"
    people.set_side(db, project.id, p.id, "executor", source="model", force=True)
    assert p.side == "executor"


def test_merge_moves_aliases_voices_and_user(db, project, admin):
    from app.app.models.speaker_profile import SpeakerProfile

    u = _user(db, "ira-example")
    keep = people.create_person(db, project.id, "Ирина Пример")
    drop = people.create_person(db, project.id, "Ira P", user_id=u.id, side="executor", side_source="card")
    voice = SpeakerProfile(scope="project", project_id=project.id, display_name="Ira P", embedding=b"\0" * 8, embedding_dim=2, person_id=drop.id)
    db.add(voice)
    db.commit()
    merged = people.merge(db, project.id, keep.id, drop.id, by_user_id=admin.id)
    db.commit()
    assert merged.id == keep.id and keep.user_id == u.id and keep.side == "executor"
    assert drop.merged_into == keep.id and drop.is_active is False and drop.user_id is None
    assert db.get(SpeakerProfile, voice.id).person_id == keep.id
    assert people.resolve(db, project.id, "Ira P").person.id == keep.id  # алиас перешёл
    assert [p.id for p in people.list_people(db, project.id)] == [keep.id]
    with pytest.raises(people.PeopleError):
        people.merge(db, project.id, keep.id, keep.id)


def test_unknown_queue_dedupes_and_resolution_creates_alias(db, project, admin):
    p = people.create_person(db, project.id, "Сергей Пример")
    first = people.note_unknown(db, project.id, "chat_author", "Serge P", example="привет")
    again = people.note_unknown(db, project.id, "chat_author", "serge p", example="как дела")
    assert first.id == again.id and json.loads(again.examples_json) == ["привет", "как дела"]
    assert len(people.open_unknown(db, project.id)) == 1
    people.resolve_unknown(db, project.id, first.id, person_id=p.id, by_user_id=admin.id)
    db.commit()
    assert people.open_unknown(db, project.id) == []
    assert people.resolve(db, project.id, "Serge P").person.id == p.id  # больше не спрашиваем


def test_speaker_label_resolution_does_not_make_alias(db, project):
    p = people.create_person(db, project.id, "Сергей Пример")
    row = people.note_unknown(db, project.id, "speaker_label", "speaker_2", artifact_id=None)
    people.resolve_unknown(db, project.id, row.id, person_id=p.id)
    assert people.resolve(db, project.id, "speaker_2") is None  # speaker_N — метка одного материала


def test_sides_map_uses_contract_vocabulary_and_safe_first_names(db, project):
    people.create_person(db, project.id, "Ольга Заказчикова", side="customer")
    people.create_person(db, project.id, "Павел Исполнителев", side="executor")
    people.create_person(db, project.id, "Павел Другой", side="customer")  # два Павла с разными сторонами
    people.create_person(db, project.id, "Неизвестный Человек")
    m = people.sides_map(db, project.id)
    assert m["ольга заказчикова"] == "customer" and m["ольга"] == "customer"
    assert m["павел исполнителев"] == "vendor" and "павел" not in m
    assert "неизвестный человек" not in m


def test_project_party_side(db, project):
    people.set_party(db, project.id, "ООО Пример", "customer")
    assert people.party_side(db, project.id, "ооо  пример") == "customer"
    people.set_party(db, project.id, "ООО Пример", "executor")
    assert people.party_side(db, project.id, "ООО ПРИМЕР") == "executor"
    assert people.party_side(db, project.id, "Другая") is None


def test_contract_uses_registry_only_when_platform_enabled(db, project, monkeypatch):
    from app.app.services import contract

    people.create_person(db, project.id, "Ольга Заказчикова", side="customer")
    db.commit()
    base = {"ольга заказчикова": "vendor"}  # прежняя догадка по роли ошиблась
    assert contract._registry_sides(db, project.id, dict(base)) == base  # флаг выключен

    project.set_settings({"memory_platform": True})
    db.commit()
    got = contract._registry_sides(db, project.id, dict(base))
    assert got["ольга заказчикова"] == "customer"  # реестр главнее роли
