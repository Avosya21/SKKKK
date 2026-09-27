"""REQUIREMENTS-PLAN: контекст извлечения (этап 3), виды пунктов и формулировки (этап 4), перенос ручных решений (А10)."""
from __future__ import annotations

import json

from app.app import db as db_mod
from app.app.config import get_settings

FINAL = ("[00:10 - 00:20] Елена: Предлагаю приложение в Битрикс24 для ввода характеристик номенклатуры.\n"
         "[00:21 - 00:30] Павел: Да, согласен, делаем приложение.\n"
         "[00:31 - 00:40] Елена: Назначим созвон по номенклатуре на вторник.\n"
         "[00:41 - 00:50] Елена: Вариант А — только функция, вариант Б — предзаполнить 40 видов, ждём выбора.\n")


def _project_call(db, settings, name="План требований"):
    from app.app.models.artifact import Artifact
    from app.app.models.project import Project
    from app.app.models.user import User
    from app.app.services.storage import create_project_tree, parsed_dir

    user = db.query(User).first()
    project = Project(name=name, created_by=user.id, is_active=True)
    db.add(project)
    db.flush()
    create_project_tree(settings, project.id)
    folder = parsed_dir(settings, project.id, "call")
    folder.mkdir(parents=True, exist_ok=True)
    art = Artifact(artifact_id=f"art_rp_{project.id}", project_id=project.id, user_id=user.id, original_filename="созвон 26.08.2026.webm",
                   stored_filename="c.webm", file_path="/x", file_type="audio", source_type="call", sha256=f"rp{project.id}", status="done")
    db.add(art)
    db.flush()
    (folder / f"{art.artifact_id}_final.txt").write_text(FINAL, encoding="utf-8")
    db.commit()
    return project, art


REPLY = {
    "summary": "Обсудили ввод характеристик номенклатуры.",
    "decisions": [{"text": "В Битрикс24 сделать приложение для ввода характеристик номенклатуры (толщина, длина)", "kind": "project",
                   "object": "Битрикс24", "topic": "ввод характеристик номенклатуры", "turns": [0], "decided_by": [{"name": "Павел", "turn": 1}]}],
    "tasks": [{"description": "Назначить созвон по нормализации номенклатуры на вторник", "kind": "org", "turns": [2]},
              {"description": "Это доработать до конца недели", "kind": "work", "turns": [0]}],
    "open_questions": [{"text": "Вариант А — только функция или вариант Б — предзаполнить 40 видов: ждём выбора", "options": ["только функция", "предзаполнить 40 видов"],
                        "topic": "ввод характеристик номенклатуры", "turns": [3]}],
    "facts": {}, "mentions": [], "participants": [],
}


def test_extraction_gets_context_and_routes_kinds(client, admin_token, tmp_data, monkeypatch):
    from app.app.services import contract as ctr
    from app.app.services import entities as ents_mod
    from app.app.services.entities import extract_entities_llm, persist_entities

    settings = get_settings()
    seen: list[str] = []

    def fake(_settings, *, system, user, **_k):
        seen.append(user)
        return json.dumps(REPLY, ensure_ascii=False)

    monkeypatch.setattr(ents_mod, "chat_completion_no_think", fake)
    monkeypatch.setattr(ents_mod, "EXTRACT_PART_CHARS", 120)  # несколько частей — проверить хвост предыдущей
    db = db_mod.SessionLocal()
    try:
        project, art = _project_call(db, settings)
        payload = extract_entities_llm(settings, art, "", context="## Карточка проекта\nИнтеграция 1С и Битрикс24")
        assert len(seen) >= 2 and all("## Карточка проекта" in u for u in seen)
        assert "Конец предыдущей части" not in seen[0] and "Конец предыдущей части" in seen[1]
        assert payload.decisions[0].kind == "project" and payload.tasks[0].kind == "org" and payload.open_questions
        persist_entities(db, settings.model_copy(update={"dry_run": True}), art, payload)
        db.commit()
        from sqlalchemy import select

        from app.app.models.entity import Entity

        rows = {e.entity_type + ":" + (json.loads(e.value_json).get("text") or json.loads(e.value_json).get("description") or ""): json.loads(e.value_json)
                for e in db.scalars(select(Entity).where(Entity.artifact_id == art.id)).all()}
        question = next(v for k, v in rows.items() if k.startswith("open_question:"))
        assert question["options"] == ["только функция", "предзаполнить 40 видов"] and question["speaker"] == "Елена"
        vague = next(v for k, v in rows.items() if "Это доработать" in k)
        assert vague.get("unclear_wording") is True
        ctr._cache.clear()
        files = ctr.build_contract(db, settings, project.id).files
        statements = [r["statement"] for r in files["requirements"]["items"]]
        assert not any("Назначить созвон" in s for s in statements)  # организационное — не требование (А7)
        app_req = next(r for r in files["requirements"]["items"] if "приложение" in r["statement"])
        assert app_req["item_kind"] == "project" and app_req["evidence"][0]["agreed_by"][0]["name"] == "Павел"
        assert [q["question"] for q in files["open_questions"]["items"]] == ["Вариант А — только функция или вариант Б — предзаполнить 40 видов: ждём выбора"]
    finally:
        db.close()


def test_reextract_transfers_human_decisions(client, admin_token, tmp_data):
    """А10: подтверждение и отказ человека переносятся на новый пункт с той же мыслью; не нашлось — в отчёт."""
    from sqlalchemy import select

    from app.app.models.entity import Entity
    from app.app.services.entities import EntitiesPayload, persist_entities
    from app.app.services.storage import project_root

    settings = get_settings().model_copy(update={"dry_run": True})
    db = db_mod.SessionLocal()
    try:
        project, art = _project_call(db, settings, "Перенос")
        human = {"by": "Admin", "action": "confirm", "at": "2026-09-20T10:00:00+00:00"}
        db.add_all([
            Entity(artifact_id=art.id, entity_type="decision", is_active=True, needs_review=False,
                   value_json=json.dumps({"text": "Приложение в Битрикс24 для ввода характеристик номенклатуры", "human_review": human}, ensure_ascii=False)),
            Entity(artifact_id=art.id, entity_type="task", is_active=False, needs_review=False,
                   value_json=json.dumps({"description": "Посчитать часы на установку коннектора", "human_review": {**human, "action": "reject"}}, ensure_ascii=False)),
            Entity(artifact_id=art.id, entity_type="decision", is_active=True, needs_review=False,
                   value_json=json.dumps({"text": "Цвет RAL хранится в карточке номенклатуры", "human_review": human}, ensure_ascii=False)),
        ])
        db.commit()
        payload = EntitiesPayload(decisions=[{"text": "Сделать приложение в Битрикс24 для ввода характеристик номенклатуры (толщина, длина)", "kind": "project"}],
                                  tasks=[{"description": "Посчитать часы, затраченные на установку коннектора", "kind": "org"}])
        persist_entities(db, settings, art, payload)
        db.commit()
        rows = db.scalars(select(Entity).where(Entity.artifact_id == art.id, Entity.entity_type.in_(("decision", "task")))).all()
        by_text = {(json.loads(e.value_json).get("text") or json.loads(e.value_json).get("description")): e for e in rows}
        app = by_text["Сделать приложение в Битрикс24 для ввода характеристик номенклатуры (толщина, длина)"]
        hours = by_text["Посчитать часы, затраченные на установку коннектора"]
        assert json.loads(app.value_json)["human_review"]["action"] == "confirm" and not app.needs_review and app.is_active
        assert json.loads(hours.value_json)["human_review"]["action"] == "reject" and hours.is_active is False
        report = json.loads((project_root(settings, project.id) / "project_state" / "reextract_human_transfer.json").read_text(encoding="utf-8"))
        assert report[art.artifact_id]["transferred"] == 2
        assert [m["text"] for m in report[art.artifact_id]["not_found"]] == ["Цвет RAL хранится в карточке номенклатуры"]
    finally:
        db.close()


def test_project_card_draft_and_human_edit(client, admin_token, tmp_data, monkeypatch):
    from app.app.services import extraction_context as xc
    from app.app.services.tz import tz_paths

    settings = get_settings().model_copy(update={"dry_run": False})
    calls: list[str] = []

    def fake(_settings, *, system, user, **_k):
        calls.append(user)
        return "## Цель\nИнтеграция 1С и Битрикс24"

    monkeypatch.setattr("app.app.services.llm.chat_completion_no_think", fake)
    db = db_mod.SessionLocal()
    try:
        project, _art = _project_call(db, settings, "Карточка")
        tz_path, _ = tz_paths(settings, project.id)
        tz_path.write_text("# Выжимка\nВнедрение 1С УТ и Битрикс24 для Пилота", encoding="utf-8")
        assert xc.ensure_project_card(db, settings, project.id) == "## Цель\nИнтеграция 1С и Битрикс24"
        assert "Выжимка проекта" in calls[0]
        assert xc.ensure_project_card(db, settings, project.id) and len(calls) == 1  # есть карточка — модель не зовём
        xc.save_project_card(settings, project.id, "## Цель\nПравка человека", source="human", by="Admin")
        assert xc.ensure_project_card(db, settings, project.id, force=True) == "## Цель\nПравка человека" and len(calls) == 1
    finally:
        db.close()


def test_call_outline_is_cached(client, admin_token, tmp_data, monkeypatch):
    from app.app.services import extraction_context as xc

    settings = get_settings().model_copy(update={"dry_run": False})
    calls: list[str] = []

    def fake(_settings, *, system, user, **_k):
        calls.append(user)
        return json.dumps({"topics": [{"start": "00:10", "title": "Ввод характеристик номенклатуры", "outcome": "решили делать приложение"}]},
                          ensure_ascii=False)

    monkeypatch.setattr("app.app.services.llm.chat_completion_no_think", fake)
    db = db_mod.SessionLocal()
    try:
        _project, art = _project_call(db, settings, "Оглавление")
        text = xc.call_outline(settings, art)
        assert text == "[00:10] Ввод характеристик номенклатуры — решили делать приложение"
        assert xc.call_outline(settings, art) == text and len(calls) == 1
    finally:
        db.close()


# --- этапы 5–8: темы, актуальность, очередь ---


def _call_art(db, settings, project, user, aid, name, final, **extra):
    from app.app.models.artifact import Artifact
    from app.app.services.storage import create_project_tree, parsed_dir

    create_project_tree(settings, project.id)
    folder = parsed_dir(settings, project.id, "call")
    folder.mkdir(parents=True, exist_ok=True)
    art = Artifact(artifact_id=aid, project_id=project.id, user_id=user.id, original_filename=name, stored_filename=f"{aid}.webm",
                   file_path="/x", file_type="audio", source_type="call", sha256=aid, status="done", **extra)
    db.add(art)
    db.flush()
    (folder / f"{aid}_final.txt").write_text(final, encoding="utf-8")
    return art


def _ent(db, art, etype, value, topic_id=None, **extra):
    from app.app.models.entity import Entity

    ent = Entity(artifact_id=art.id, entity_type=etype, value_json=json.dumps(value, ensure_ascii=False), is_active=True,
                 needs_review=extra.pop("needs_review", True), topic_id=topic_id, **extra)
    db.add(ent)
    db.flush()
    return ent


OLD_FINAL = "[00:10 - 00:20] Елена: Сделаем приложение в Битрикс24 для ввода характеристик номенклатуры.\n[00:21 - 00:25] Павел: Да.\n"
NEW_FINAL = ("[00:05 - 00:15] Елена: Характеристики ведём в 1С: нормализуем справочник номенклатуры профнастила.\n"
             "[00:16 - 00:20] Павел: Согласен, так и делаем.\n")


def _relevance_setup(db, settings, *, old_name="созвон 26.08.2026.webm", new_name="созвон 02.09.2026.webm", human_at=None, new_uploaded=None):
    from datetime import datetime

    from app.app.models.entity import Topic
    from app.app.models.project import Project
    from app.app.models.user import User

    user = db.query(User).first()
    project = Project(name="Актуальность", created_by=user.id, is_active=True)
    db.add(project)
    db.flush()
    topic = Topic(project_id=project.id, label="ввод характеристик номенклатуры")
    db.add(topic)
    db.flush()
    old = _call_art(db, settings, project, user, f"art_rel_old_{project.id}", old_name, OLD_FINAL, upload_date=datetime(2026, 8, 27, 9, 0))
    new = _call_art(db, settings, project, user, f"art_rel_new_{project.id}", new_name, NEW_FINAL,
                    upload_date=new_uploaded or datetime(2026, 9, 3, 9, 0))
    app_value = {"text": "Сделать приложение в Битрикс24 для ввода характеристик номенклатуры", "kind": "project", "speaker": "Елена",
                 "speaker_source": "voice", "start_sec": 10.0}
    if human_at:
        app_value["human_review"] = {"by": "Admin", "action": "confirm", "at": human_at}
    app_ent = _ent(db, old, "decision", app_value, topic.id, needs_review=not human_at)
    norm = _ent(db, new, "decision", {"text": "Характеристики ведём в 1С: нормализуем справочник номенклатуры профнастила", "kind": "project",
                                      "speaker": "Елена", "speaker_source": "voice", "start_sec": 5.0,
                                      "decided_by": [{"name": "Павел", "start_sec": 16.0}]}, topic.id)
    db.commit()
    return project, topic, app_ent, norm, new


def _llm(monkeypatch, replies):
    calls = []

    def fake(_settings, *, system, user, **_k):
        calls.append((system, user))
        for key, reply in replies.items():
            if key in system:
                return json.dumps(reply(user) if callable(reply) else reply, ensure_ascii=False)
        raise AssertionError("неожиданный промпт")

    monkeypatch.setattr("app.app.services.llm.chat_completion_no_think", fake)
    monkeypatch.setattr("app.app.services.gpu_phase.prepare_for_llm", lambda *_a, **_k: None)
    return calls


def test_topics_assigned_by_hint_and_model(client, admin_token, tmp_data, monkeypatch):
    from app.app.models.entity import Entity, Topic
    from app.app.models.project import Project
    from app.app.models.user import User
    from app.app.services.topics import assign_topics

    settings = get_settings().model_copy(update={"dry_run": False})

    def reply(user):
        ids = [line.split()[0] for line in user.splitlines() if line.startswith("E")]
        return {"items": [{"id": ids[0], "topic": "new: ввод характеристик номенклатуры"},
                          {"id": ids[1], "topic": "new: Ввод характеристик номенклатуры."},
                          {"id": ids[2], "topic": "new: нормализация контактов"}]}

    calls = _llm(monkeypatch, {"раскладываешь": reply})
    db = db_mod.SessionLocal()
    try:
        user = db.query(User).first()
        project = Project(name="Темы", created_by=user.id, is_active=True)
        db.add(project)
        db.flush()
        art = _call_art(db, settings, project, user, f"art_top_{project.id}", "созвон.webm", OLD_FINAL)
        a = _ent(db, art, "decision", {"text": "Приложение в Битрикс24 для характеристик"})
        b = _ent(db, art, "task", {"description": "Нормализовать справочник номенклатуры в 1С"})
        c = _ent(db, art, "decision", {"text": "Нормализация ФИО контактов"})
        db.commit()
        stats = assign_topics(db, settings, project.id)
        assert stats["by_model"] == 3 and stats["new_topics"] == 2 and len(calls) == 1
        db.expire_all()
        assert db.get(Entity, a.id).topic_id == db.get(Entity, b.id).topic_id != db.get(Entity, c.id).topic_id
        d = _ent(db, art, "decision", {"text": "Контакты: ФИО в реквизиты", "topic": "нормализация контактов"})
        db.commit()
        stats = assign_topics(db, settings, project.id)
        assert stats["by_hint"] == 1 and len(calls) == 1  # подсказка совпала с темой — модель не звали
        assert db.get(Entity, d.id).topic_id == db.get(Entity, c.id).topic_id
        assert all(t.reviewed_at is None for t in db.query(Topic).filter(Topic.project_id == project.id))
    finally:
        db.close()


def _superseded_reply(app_id, norm_id, state="superseded"):
    return {"items": [{"id": f"E{app_id}", "state": state, "by": f"E{norm_id}", "quote": "нормализуем справочник номенклатуры",
                       "reason": "перешли на нормализацию в 1С", "confidence": 0.9},
                      {"id": f"E{norm_id}", "state": "current", "confidence": 0.9}]}


def test_relevance_live_supersedes_and_contract_shows_it(client, admin_token, tmp_data, monkeypatch):
    from app.app.models.entity import Entity, Resolution
    from app.app.services import contract as ctr
    from app.app.services.requirement_review import review_list
    from app.app.services.review_relevance import review_topics

    settings = get_settings().model_copy(update={"dry_run": False})
    db = db_mod.SessionLocal()
    try:
        project, topic, app_ent, norm, _new = _relevance_setup(db, settings)
        _llm(monkeypatch, {"актуальность": _superseded_reply(app_ent.id, norm.id)})
        stats = review_topics(db, settings, project.id, "live")
        assert stats["superseded"] == 1 and stats["current"] == 1
        db.expire_all()
        assert db.get(Entity, app_ent.id).is_active is False
        res = db.query(Resolution).filter(Resolution.winning_entity_id == norm.id).one()
        assert res.status == "model_resolved" and "ввод характеристик" in res.reason
        ctr._cache.clear()
        reqs = {r["source_entity_id"]: r for r in ctr.build_contract(db, settings, project.id).files["requirements"]["items"]}
        assert reqs[app_ent.id]["status"] == "superseded" and reqs[app_ent.id]["superseded_by"] == reqs[norm.id]["id"]
        ctr._cache.clear()
        waiting = review_list(db, settings, project.id, verification="needs_human")
        assert [r["source_entity_id"] for r in waiting["items"]] == [norm.id] and waiting["counts"]["superseded"] == 1
        stale = review_list(db, settings, project.id, verification="superseded")["items"]
        assert stale[0]["replaced_by"]["statement"].startswith("Характеристики ведём в 1С") and stale[0]["topic"] == topic.label
        assert stale[0]["replaced_by"]["agreed_by"][0]["name"] == "Павел"
    finally:
        db.close()


def test_relevance_shadow_hides_and_confirm_needs_force(client, admin_token, tmp_data, monkeypatch):
    from app.app.models.entity import Entity
    from app.app.services import contract as ctr
    from app.app.services.review_relevance import review_topics

    settings = get_settings().model_copy(update={"dry_run": False})
    db = db_mod.SessionLocal()
    try:
        project, _topic, app_ent, norm, _new = _relevance_setup(db, settings)
        _llm(monkeypatch, {"актуальность": _superseded_reply(app_ent.id, norm.id)})
        review_topics(db, settings, project.id, "shadow")
        db.expire_all()
        assert db.get(Entity, app_ent.id).is_active is True  # тень: не выключаем
        pid, eid = project.id, app_ent.id
    finally:
        db.close()
    ctr._cache.clear()
    h = {"Authorization": f"Bearer {admin_token}"}
    waiting = client.get(f"/projects/{pid}/requirements/review?verification=needs_human", headers=h).json()
    assert eid not in [r["source_entity_id"] for r in waiting["items"]]
    stale = client.get(f"/projects/{pid}/requirements/review?verification=superseded", headers=h).json()["items"]
    rid = next(r["id"] for r in stale if r["source_entity_id"] == eid)
    r = client.post(f"/projects/{pid}/requirements/{rid}/confirm", json={}, headers=h)
    assert r.status_code == 409 and "устаревшим" in r.json()["detail"]
    r = client.post(f"/projects/{pid}/requirements/entity/{eid}/mark-current", headers=h)
    assert r.status_code == 200
    ctr._cache.clear()
    waiting = client.get(f"/projects/{pid}/requirements/review?verification=needs_human", headers=h).json()
    assert eid in [r["source_entity_id"] for r in waiting["items"]]
    assert client.post(f"/projects/{pid}/requirements/{rid}/confirm", json={}, headers=h).status_code == 200


def test_relevance_without_dates_is_unclear(client, admin_token, tmp_data, monkeypatch):
    """А4: дата записи неизвестна — «что позже» по дате загрузки не решаем."""
    from app.app.models.entity import Entity
    from app.app.services.review_relevance import review_topics

    settings = get_settings().model_copy(update={"dry_run": False})
    db = db_mod.SessionLocal()
    try:
        project, _topic, app_ent, norm, _new = _relevance_setup(db, settings, old_name="демо.webm", new_name="номенклатура.webm")
        _llm(monkeypatch, {"актуальность": _superseded_reply(app_ent.id, norm.id)})
        stats = review_topics(db, settings, project.id, "live")
        assert stats["superseded"] == 0 and stats["unclear"] == 1
        db.expire_all()
        ent = db.get(Entity, app_ent.id)
        assert ent.is_active and "нет даты записи" in json.loads(ent.value_json)["relevance"]["note"]
    finally:
        db.close()


def test_relevance_human_confirmed_before_and_after(client, admin_token, tmp_data, monkeypatch):
    """А6: подтверждено до появления заменяющего материала — заменяется и во «Важное»; после — ждёт человека."""
    from app.app.models.entity import Entity, Link
    from app.app.services.review_conflicts import link_review
    from app.app.services.review_relevance import review_topics

    settings = get_settings().model_copy(update={"dry_run": False})
    db = db_mod.SessionLocal()
    try:
        project, _t, app_ent, norm, _new = _relevance_setup(db, settings, human_at="2026-08-28T10:00:00+00:00")
        _llm(monkeypatch, {"актуальность": _superseded_reply(app_ent.id, norm.id)})
        assert review_topics(db, settings, project.id, "live")["superseded"] == 1
        db.expire_all()
        assert db.get(Entity, app_ent.id).is_active is False
        link = db.query(Link).filter(Link.project_id == project.id).one()
        assert link_review(link)["important"] is True
    finally:
        db.close()
    db = db_mod.SessionLocal()
    try:
        project, _t, app_ent, norm, _new = _relevance_setup(db, settings, human_at="2026-09-10T10:00:00+00:00")
        _llm(monkeypatch, {"актуальность": _superseded_reply(app_ent.id, norm.id)})
        assert review_topics(db, settings, project.id, "live")["unclear"] == 1
        db.expire_all()
        assert db.get(Entity, app_ent.id).is_active is True
    finally:
        db.close()


def test_duplicate_live_merges_sources(client, admin_token, tmp_data, monkeypatch):
    from app.app.models.entity import Entity
    from app.app.services import contract as ctr
    from app.app.services.review_relevance import review_topics

    settings = get_settings().model_copy(update={"dry_run": False})
    db = db_mod.SessionLocal()
    try:
        project, topic, app_ent, _norm, new = _relevance_setup(db, settings)
        dup = _ent(db, new, "decision", {"text": "Приложение в Битрикс24 для ввода характеристик номенклатуры", "kind": "project",
                                         "speaker": "Елена", "speaker_source": "voice", "start_sec": 5.0}, topic.id)
        db.commit()
        reply = {"items": [{"id": f"E{dup.id}", "state": "duplicate", "by": f"E{app_ent.id}", "reason": "то же", "confidence": 0.9}]}
        _llm(monkeypatch, {"актуальность": reply})
        assert review_topics(db, settings, project.id, "live")["duplicate"] == 1
        db.expire_all()
        assert db.get(Entity, dup.id).is_active is False
        ctr._cache.clear()
        reqs = {r["source_entity_id"]: r for r in ctr.build_contract(db, settings, project.id).files["requirements"]["items"]}
        assert reqs[app_ent.id]["merged_from"] == [dup.id] and len(reqs[app_ent.id]["evidence"]) >= 2 and dup.id not in reqs
    finally:
        db.close()


def test_relevance_waits_while_project_is_busy(client, admin_token, tmp_data):
    from app.app.models.job import Job
    from app.app.services.review_relevance import review_artifact_relevance

    settings = get_settings()
    db = db_mod.SessionLocal()
    try:
        project, _t, _a, _n, new = _relevance_setup(db, settings)
        db.add(Job(artifact_id=new.id, kind="process_artifact", status="queued", phase="knowledge"))
        db.commit()
        assert review_artifact_relevance(db, settings, new, "live")["deferred"] == 1
    finally:
        db.close()