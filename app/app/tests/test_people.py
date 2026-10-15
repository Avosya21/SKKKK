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


def test_party_always_points_to_directory_company(db, project):
    """ЛГ1: сторона проекта хранит компанию справочника; новая компания попадает туда со стороной по роли (Р2)."""
    from app.app.models.company import Company
    from app.app.services.companies import canonical_company, load_companies_file, loose_key

    known = people.set_party(db, project.id, "acme", "executor")  # в справочнике «ACME»
    acme = canonical_company(db, "ACME")
    assert (known.company_id, known.company_name) == (acme.id, "ACME")
    fresh = people.set_party(db, project.id, "ООО «Новый Пример»", "customer")
    added = db.get(Company, fresh.company_id)
    assert added.name == "ООО «Новый Пример»" and added.side == "customer" and added.active
    assert any(line.name == "ООО «Новый Пример»" for line in load_companies_file())  # и в файле-источнике
    again = people.set_party(db, project.id, "Новый Пример", "executor")  # то же название без формы и кавычек
    assert again.id == fresh.id and again.side == "executor" and again.company_id == added.id
    assert db.get(Company, added.id).side == "customer"  # сторона в справочнике — только по умолчанию (Д1)
    assert people.party_side(db, project.id, "новый пример ооо") == "executor"
    assert loose_key("ООО «Пример-Альфа»") == loose_key("ПРИМЕР АЛЬФА ООО") != loose_key("Пример Бета")


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


# ---------------------------------------------------------------- первичное наполнение (people_seed)

def _m(name, source="model", side=None, where="participant"):
    from app.app.services.people_seed import Mention

    return Mention(name, source, side, where=where)


def test_seed_clusters_reversed_order_first_name_and_parentheses():
    from app.app.services.people_seed import cluster_mentions

    clusters, ambiguous = cluster_mentions([
        _m("Ольга Пример"), _m("Пример Ольга"), _m("Ольга"),
        _m("Пётр Образцов (Peter Sample)"), _m("Peter Sample"), _m("Пётр Образцов"),
        _m("Олег Первый"), _m("Олег Второй"), _m("Олег"),
    ])
    by_name = {c.display_name: c for c in clusters}
    assert set(by_name["Ольга Пример"].names) == {"Ольга Пример", "Пример Ольга", "Ольга"}
    assert {"Peter Sample", "Пётр Образцов (Peter Sample)"} <= set(by_name["Пётр Образцов"].names)
    assert {m.name for m in ambiguous} == {"Олег"}  # два Олега: одиночное имя не склеиваем
    assert "Олег" not in {n for c in clusters for n in c.names}


def test_seed_side_strongest_source_and_majority():
    from app.app.services.people_seed import Cluster, cluster_side

    strong = Cluster(names=["А Б"], mentions=[_m("А Б", "card", "executor"), _m("А Б", "model", "customer"), _m("А Б", "model", "customer")])
    assert cluster_side(strong) == ("executor", "card")  # карточка главнее двух догадок
    voted = Cluster(names=["В Г"], mentions=[_m("В Г", side="customer"), _m("В Г", side="customer"), _m("В Г", side="executor")])
    assert cluster_side(voted) == ("customer", "model")
    tie = Cluster(names=["Д Е"], mentions=[_m("Д Е", side="customer"), _m("Д Е", side="executor")])
    assert cluster_side(tie) == ("unknown", "model")
    none = Cluster(names=["Ж З"], mentions=[_m("Ж З")])
    assert cluster_side(none) == ("unknown", "model")


def test_seed_apply_is_idempotent_and_keeps_human_side(db, project, monkeypatch):
    from app.app.config import get_settings
    from app.app.services import people_seed
    from app.app.services.people_seed import Mention

    monkeypatch.setattr(people_seed, "collect_mentions", lambda *_a, **_k: [
        Mention("Анна Пример", "project_party", "customer", where="settings"), Mention("Анна", "model", None, where="chat"),
        Mention("Борис Пример", "model", "executor", where="participant")])
    settings = get_settings()
    first = people_seed.seed_project(db, settings, project.id, apply=True)
    assert first["created"] == 2
    anna = people.resolve(db, project.id, "Анна Пример").person
    people.set_side(db, project.id, anna.id, "executor", source="human")
    second = people_seed.seed_project(db, settings, project.id, apply=True)
    db.expire_all()
    assert second["created"] == 0 and second["skipped_existing"] == 2
    assert people.resolve(db, project.id, "Анна Пример").person.side == "executor"  # решение человека не тронуто
    assert len(people.list_people(db, project.id)) == 2


def test_seed_links_portal_account_by_exact_name_even_if_not_project_member(db, project, monkeypatch):
    """Админ видит все проекты и не числится участником: его карточка всё равно сопоставляется по точному имени."""
    from app.app.config import get_settings
    from app.app.services import people_seed
    from app.app.tests.test_voice_link import _person

    account = _person(db, "anna-login", "Анна", "Пример")
    twin_a, twin_b = _person(db, "olga-1", "Ольга", "Тёзкина"), _person(db, "olga-2", "Ольга", "Тёзкина")
    monkeypatch.setattr(people_seed, "_mentioned_names", lambda *_a, **_k: {"Пример Анна", "Ольга Тёзкина"})
    mentions = people_seed.collect_mentions(db, get_settings(), project.id)
    linked = {m.name: m.user_id for m in mentions if m.source == "card" and m.user_id}
    assert linked.get("Анна Пример") == account.id and linked.get("Пример Анна") == account.id
    assert twin_a.id not in linked.values() and twin_b.id not in linked.values()  # тёзки — не угадываем
    assert next(m for m in mentions if m.name == "Анна Пример").company_name  # компания из карточки


def test_seed_takes_people_only_from_chats_and_fully_processed_calls(db, project, admin, monkeypatch):
    """Документы и недоразобранные созвоны людей не дают: ни спикеров, ни участников из извлечения."""
    from types import SimpleNamespace

    from app.app.config import get_settings
    from app.app.models.artifact import Artifact
    from app.app.models.entity import Entity
    from app.app.services import chat_export, people_seed, speaker_profiles, transcript_turns

    def art(key, file_type, source_type, status="done"):
        row = Artifact(artifact_id=key, project_id=project.id, user_id=admin.id, original_filename=key, stored_filename=key,
                       file_path=key, file_type=file_type, source_type=source_type, status=status, sha256=key)
        db.add(row)
        db.flush()
        db.add(Entity(artifact_id=row.id, entity_type="participant", value_json=json.dumps({"name": "Участник " + key})))
        return row

    chat, doc = art("chat", "txt", "document"), art("doc", "docx", "document")
    call_ok, call_pending = art("call-ok", "audio", "call"), art("call-pending", "audio", "call")
    call_running = art("call-running", "audio", "call", status="asr_running")
    db.commit()
    monkeypatch.setattr(chat_export, "artifact_messages_path", lambda _s, a: a.artifact_id)
    monkeypatch.setattr(chat_export, "load_export", lambda key: {"messages": [{"author": "Автор Переписки"}]} if key == "chat" else None)
    monkeypatch.setattr(speaker_profiles, "speakers_confirm_status",
                        lambda _db, project_id, artifact_id: {"reason": "speakers_unconfirmed" if artifact_id == "call-pending" else None})
    monkeypatch.setattr(transcript_turns, "load_turns",
                        lambda _s, a: [SimpleNamespace(known_speaker="Спикер " + a.artifact_id)])

    names = people_seed._mentioned_names(db, get_settings(), project.id)
    assert names == {"Автор Переписки", "Участник chat", "Спикер call-ok", "Участник call-ok"}
    assert {m.where for m in people_seed.collect_mentions(db, get_settings(), project.id)} >= {"chat", "speaker", "participant"}
    assert not any(a.artifact_id in " ".join(names) for a in (doc, call_pending, call_running))
    assert chat.id and call_ok.id


def test_merging_accounts_moves_registry_people(db, project, admin):
    from app.app.models.project import Project
    from app.app.models.user import User
    from app.app.services.user_identity import merge_users_into

    second = Project(name="Второй", created_by=admin.id, is_active=True)
    db.add(second)
    db.commit()
    winner, loser = _user(db, "keep-example"), _user(db, "drop-example")
    moved = people.create_person(db, second.id, "Анна Пример", user_id=loser.id)  # у победителя в этом проекте никого: переходит
    kept = people.create_person(db, project.id, "Борис Пример", user_id=winner.id)
    dropped = people.create_person(db, project.id, "Boris P", user_id=loser.id)  # у победителя уже есть человек: объединяются
    db.commit()
    merge_users_into(db, winner, [db.get(User, loser.id)])
    db.commit()
    db.expire_all()
    assert db.get(people.Person, moved.id).user_id == winner.id
    assert dropped.merged_into == kept.id and people.resolve(db, project.id, "Boris P").person.id == kept.id
    assert kept.user_id == winner.id


def test_clean_name_drops_placeholders_roles_and_descriptions():
    from app.app.services.people_seed import clean_name

    assert clean_name("speaker_2") is None and clean_name("SPEAKER_00") is None and clean_name("Спикер 3") is None
    assert clean_name("фоновый голос") is None and clean_name("Директор (Пример)") is None and clean_name("  ") is None
    assert clean_name("speaker_4 (Анна)") == "Анна"  # имя в скобках
    assert clean_name("Анна (Пример, фин.директор)") == "Анна"  # компания и должность — описание
    assert clean_name("Виталий Пример (ИВ)") == "Виталий Пример"  # аббревиатура
    assert clean_name("Илья Пример (Ilya Sample)") == "Илья Пример (Ilya Sample)"  # второе имя человека остаётся
    assert clean_name("Ирина Пример") == "Ирина Пример"


def test_seed_clusters_initial_and_ignores_placeholders():
    from app.app.services.people_seed import cluster_mentions

    clusters, _ = cluster_mentions([_m("Ирина Образцова"), _m("Ирина О."), _m("Ирина О. (Пример, торговля)"), _m("speaker_0"), _m("фоновый голос")])
    assert len(clusters) == 1 and clusters[0].display_name == "Ирина Образцова"
    assert {"Ирина О."} <= set(clusters[0].names)


def test_prune_removes_untouched_placeholders_only(db, project, admin):
    from app.app.services import people_seed

    junk = people.create_person(db, project.id, "speaker_1")  # занесён прежней версией наполнения, никто его не трогал
    kept = people.create_person(db, project.id, "speaker_2", side="customer", side_source="human")  # сторону задал человек
    real = people.create_person(db, project.id, "Анна Пример")
    people.add_alias(db, project.id, real.id, "speaker_4")  # метка конкретной встречи у настоящего человека
    db.commit()
    result = people_seed.prune_placeholders(db, project.id, by_user_id=admin.id)
    db.commit()
    assert result == {"removed": 1, "aliases": 1, "merged": 0, "renamed": 0}
    names = [p.display_name for p in people.list_people(db, project.id)]
    assert "speaker_1" not in names and "speaker_2" in names and "Анна Пример" in names
    assert people.resolve(db, project.id, "speaker_4", allow_first_name=False) is None
    assert db.get(people.Person, junk.id) is None and db.get(people.Person, kept.id) is not None


def test_prune_merges_described_duplicates_and_renames_single(db, project, admin):
    from app.app.services import people_seed

    full = people.create_person(db, project.id, "Ирина Образцова")
    dup = people.create_person(db, project.id, "Ирина О. (Пример, торговля)")  # описание в скобках и сокращённая фамилия
    solo = people.create_person(db, project.id, "Марина (Пример, бухгалтерия)")  # такого человека ещё нет
    db.commit()
    result = people_seed.prune_placeholders(db, project.id, by_user_id=admin.id)
    db.commit()
    db.expire_all()
    assert result["merged"] == 1 and result["renamed"] == 1
    assert dup.merged_into == full.id and people.resolve(db, project.id, "Ирина О. (Пример, торговля)").person.id == full.id
    assert solo.display_name == "Марина" and people.resolve(db, project.id, "Марина").person.id == solo.id
