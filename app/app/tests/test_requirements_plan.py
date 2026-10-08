"""REQUIREMENTS-PLAN: контекст извлечения (этап 3), виды пунктов и формулировки (этап 4), перенос ручных решений (А10)."""
from __future__ import annotations

import json

import pytest

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


@pytest.mark.parametrize("last_fails", [False, True])
def test_reextract_summary_after_all_checks(client, admin_token, tmp_data, monkeypatch, last_fails):
    """Итоговая выжимка после переизвлечения собирается один раз — отдельной задачей очереди за конфликтами последнего
    материала, когда актуальность и конфликты уже выключили устаревшее (2026-09-28, проект 34: собранная сразу после
    извлечения, она сохранила «сводку по опозданиям», отменённую перепиской). Упал последний материал — выжимка всё
    равно собирается после проверок остальных."""
    from app.app.models.artifact import Artifact
    from app.app.models.project import Project
    from app.app.models.user import User
    from app.app.services.requirements_reextract import queue_project_reextract
    from app.app.services.storage import create_project_tree
    from app.app.workers import review as review_mod
    from app.app.workers.gpu_worker import run_once

    settings = get_settings()
    monkeypatch.setattr(settings, "dry_run", False)
    steps: list[str] = []

    def fake_reextract(_db, _settings, art):
        steps.append(f"reextract {art.original_filename}")
        if last_fails and art.original_filename == "протокол 2.docx":
            raise RuntimeError("модель не ответила")
        return {"ok": True, "tasks": 0}

    def check(scope):
        def run(_db, _settings, art, _mode):
            steps.append(f"{scope} {art.original_filename}")
            return {}
        return run

    monkeypatch.setattr("app.app.services.entities.reextract_artifact_entities", fake_reextract)
    monkeypatch.setattr("app.app.services.graph.seed_graph", lambda *_a, **_k: None)
    monkeypatch.setattr("app.app.services.gpu_phase.prepare_for_llm", lambda *_a, **_k: None)
    monkeypatch.setattr("app.app.services.tz.rebuild_tz_fast", lambda *_a, **_k: steps.append("выжимка"))
    for scope in ("artifact", "topics", "relevance", "conflicts"):
        monkeypatch.setitem(review_mod.REVIEWERS, scope, [check(scope)])
    db = db_mod.SessionLocal()
    try:
        user = db.query(User).first()
        project = Project(name="Выжимка после проверок", created_by=user.id, is_active=True)
        db.add(project)
        db.flush()
        create_project_tree(settings, project.id)
        db.add_all([Artifact(artifact_id=f"art_rx_{project.id}_{i}", project_id=project.id, user_id=user.id, original_filename=f"протокол {i}.docx",
                             stored_filename=f"p{i}.docx", file_path="/x", file_type="docx", source_type="document", sha256=f"rx{project.id}{i}",
                             status="done", transcription_text=f"Протокол {i}") for i in (1, 2)])
        db.commit()
        assert queue_project_reextract(db, settings, project.id)["jobs_queued"] == 2
    finally:
        db.close()
    while run_once():  # воркер один, задачи — по порядку постановки
        pass
    checks = [f"{scope} протокол {i}.docx" for i in ((1,) if last_fails else (1, 2))
              for scope in ("artifact", "topics", "relevance", "conflicts")]
    assert steps == ["reextract протокол 1.docx", "reextract протокол 2.docx", *checks, "выжимка"]


@pytest.mark.parametrize("mode, last_fails", [("live", False), ("live", True), ("off", False)])
def test_upload_summary_after_checks(client, admin_token, tmp_data, monkeypatch, mode, last_fails):
    """Новые материалы: итоговая выжимка собирается один раз на партию — задачей очереди за проверками последнего
    материала, когда актуальность и конфликты уже выключили заменённое (решение владельца 2026-09-29). Раньше она
    собиралась сразу после знаний, до проверок: документ, заменивший требование, оставлял старое в выжимке и в ответах
    чата. Созвоны — так же, вместо каждого 3-го. Упал последний файл партии — выжимка после проверок остальных.
    Без автопроверки — сразу, как раньше."""
    from pathlib import Path

    from app.app.models.artifact import Artifact
    from app.app.models.job import Job
    from app.app.models.project import Project
    from app.app.models.user import User
    from app.app.services import pipeline_knowledge as pk
    from app.app.services.entities import stub_entities
    from app.app.services.storage import create_project_tree
    from app.app.workers import review as review_mod
    from app.app.workers.gpu_worker import run_once

    settings = get_settings()
    monkeypatch.setattr(settings, "dry_run", False)
    monkeypatch.setattr(settings, "auto_review_mode", mode)
    steps: list[str] = []

    def fake_process(db, _settings, art):  # разбор файла → знания; модель, индекс и граф подменены ниже
        steps.append(f"знания {art.original_filename}")
        if last_fails and art.original_filename == "протокол 3.docx":
            raise RuntimeError("документ не разобран")
        pk.run_knowledge_pipeline(db, _settings, art, f"Текст {art.original_filename}")

    def check(scope):
        def run(_db, _settings, art, _mode):
            steps.append(f"{scope} {art.original_filename}")
            return {}
        return run

    monkeypatch.setattr("app.app.workers.pipeline.process_artifact", fake_process)
    monkeypatch.setattr(pk, "_extraction_context", lambda *_a: "")
    monkeypatch.setattr(pk, "extract_entities_llm", lambda _s, art, text, **_k: stub_entities(art, text))
    for name in ("persist_entities", "write_document_report"):
        monkeypatch.setattr(pk, name, lambda *_a, **_k: Path("stub"))
    for name in ("prepare_for_llm", "index_artifact", "run_call_summary", "_suggest_speaker_names", "run_document_summary",
                 "seed_graph", "notify_artifact_user"):
        monkeypatch.setattr(pk, name, lambda *_a, **_k: None)
    monkeypatch.setattr("app.app.services.gpu_phase.prepare_for_llm", lambda *_a, **_k: None)
    monkeypatch.setattr("app.app.services.tz.rebuild_tz_fast", lambda *_a, **_k: steps.append("выжимка"))
    for scope in ("artifact", "topics", "relevance", "conflicts"):
        monkeypatch.setitem(review_mod.REVIEWERS, scope, [check(scope)])
    files = [("созвон 1.webm", "audio", "call"), ("протокол 2.docx", "docx", "document"), ("протокол 3.docx", "docx", "document")]
    db = db_mod.SessionLocal()
    try:
        user = db.query(User).first()
        project = Project(name="Выжимка после загрузки", created_by=user.id, is_active=True)
        db.add(project)
        db.flush()
        create_project_tree(settings, project.id)
        arts = [Artifact(artifact_id=f"art_up_{project.id}_{i}", project_id=project.id, user_id=user.id, original_filename=name,
                         stored_filename=f"u{i}", file_path="/x", file_type=file_type, source_type=source, sha256=f"up{project.id}{i}",
                         status="queued") for i, (name, file_type, source) in enumerate(files, 1)]
        db.add_all(arts)
        db.flush()
        db.add_all([Job(artifact_id=a.id, kind="process_artifact", status="queued") for a in arts])  # партия загрузки
        db.commit()
    finally:
        db.close()
    while run_once():  # воркер один: сначала разбор файлов, затем проверки — по порядку постановки
        pass
    names = [name for name, *_ in files]
    knowledge = [f"знания {name}" for name in names]
    if mode == "off":  # проверок нет — выжимка сразу, в конце партии
        assert steps == [*knowledge, "выжимка"]
        return
    checks = [f"{scope} {name}" for name in (names[:2] if last_fails else names)
              for scope in ("artifact", "topics", "relevance", "conflicts")]
    assert steps == [*knowledge, *checks, "выжимка"]


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


def test_relevance_without_dates_uses_upload_day(client, admin_token, tmp_data, monkeypatch):
    """Решение владельца 2026-09-28: нет даты записи — «что позже» по дате загрузки; загружены в один день — неясно (Я3)."""
    from datetime import datetime

    from app.app.models.entity import Entity
    from app.app.services.review_relevance import review_topics

    settings = get_settings().model_copy(update={"dry_run": False})
    db = db_mod.SessionLocal()
    try:
        project, _topic, app_ent, norm, _new = _relevance_setup(db, settings, old_name="демо.webm", new_name="номенклатура.webm",
                                                                new_uploaded=datetime(2026, 8, 27, 15, 0))
        _llm(monkeypatch, {"актуальность": _superseded_reply(app_ent.id, norm.id)})
        stats = review_topics(db, settings, project.id, "live")
        assert stats["superseded"] == 0 and stats["unclear"] == 1
        db.expire_all()
        ent = db.get(Entity, app_ent.id)
        assert ent.is_active and "одного дня" in json.loads(ent.value_json)["relevance"]["note"]

        project, _topic, app_ent, norm, _new = _relevance_setup(db, settings, old_name="демо.webm", new_name="номенклатура.webm")
        _llm(monkeypatch, {"актуальность": _superseded_reply(app_ent.id, norm.id)})
        stats = review_topics(db, settings, project.id, "live")  # загружены 27.08 и 03.09
        assert stats["superseded"] == 1
        db.expire_all()
        assert db.get(Entity, app_ent.id).is_active is False
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

def test_merge_topics_joins_ways_of_one_need(client, admin_token, tmp_data, monkeypatch):
    """Склейка тем: «ввод характеристик» (приложение) и «нормализация в 1С» — одна потребность; тема проверяется заново."""
    from app.app.models.entity import Entity, Topic
    from app.app.models.project import Project
    from app.app.models.user import User
    from app.app.services.topics import merge_topics

    settings = get_settings().model_copy(update={"dry_run": False})
    db = db_mod.SessionLocal()
    try:
        user = db.query(User).first()
        project = Project(name="Склейка тем", created_by=user.id, is_active=True)
        db.add(project)
        db.flush()
        art = _call_art(db, settings, project, user, f"art_merge_{project.id}", "созвон.webm", OLD_FINAL)
        a, b, c = (Topic(project_id=project.id, label=l, reviewed_at="2026-09-28T00:00:00") for l in
                   ("ввод характеристик номенклатуры", "нормализация номенклатуры в 1С", "нормализация контактов"))
        db.add_all([a, b, c])
        db.flush()
        e1 = _ent(db, art, "decision", {"text": "Приложение в Битрикс24 для ввода характеристик"}, a.id)
        e2 = _ent(db, art, "decision", {"text": "Нормализовать справочник номенклатуры в 1С"}, b.id)
        _ent(db, art, "decision", {"text": "ФИО контактов разнести по полям"}, c.id)
        db.commit()
        _llm(monkeypatch, {"Найди группы тем": {"groups": [{"topics": [f"t{a.id}", f"t{b.id}"], "label": "характеристики номенклатуры"}]}})
        stats = merge_topics(db, settings, project.id)
        assert stats["groups"] == 1 and stats["merged"] == 1
        db.expire_all()
        assert db.get(Entity, e2.id).topic_id == a.id == db.get(Entity, e1.id).topic_id
        keep = db.get(Topic, a.id)
        assert keep.label == "характеристики номенклатуры" and keep.reviewed_at is None and db.get(Topic, b.id) is None
        # Повторный прогон без новых тем модель не зовёт; старые темы между собой не склеиваются (темы не сливаются раз за разом)
        calls = _llm(monkeypatch, {"Найди группы тем": {"groups": [{"topics": [f"t{a.id}", f"t{c.id}"], "label": "всё"}]}})
        assert merge_topics(db, settings, project.id)["llm_calls"] == 0 and not calls
        d = Topic(project_id=project.id, label="справочник контрагентов")
        db.add(d)
        db.flush()
        _ent(db, art, "decision", {"text": "Контрагенты ведутся в 1С"}, d.id)
        db.commit()
        _llm(monkeypatch, {"Найди группы тем": {"groups": [{"topics": [f"t{a.id}", f"t{c.id}"], "label": "всё"},
                                                           {"topics": [f"t{c.id}", f"t{d.id}"], "label": "контрагенты и контакты"}]}})
        stats = merge_topics(db, settings, project.id)
        assert stats["groups"] == 1 and db.get(Topic, a.id) is not None and db.get(Topic, d.id) is None
    finally:
        db.close()


def test_live_promotes_shadow_duplicate_without_llm(client, admin_token, tmp_data, monkeypatch):
    from app.app.models.entity import Entity
    from app.app.services.review_relevance import review_topics

    settings = get_settings().model_copy(update={"dry_run": False})
    db = db_mod.SessionLocal()
    try:
        project, topic, app_ent, _norm, new = _relevance_setup(db, settings)
        dup = _ent(db, new, "decision", {"text": "Приложение в Битрикс24 для ввода характеристик номенклатуры", "speaker": "Елена",
                                         "speaker_source": "voice", "start_sec": 5.0}, topic.id)
        db.commit()
        _llm(monkeypatch, {"актуальность": {"items": [{"id": f"E{dup.id}", "state": "duplicate", "by": f"E{app_ent.id}", "confidence": 0.9}]}})
        review_topics(db, settings, project.id, "shadow")
        db.expire_all()
        assert db.get(Entity, dup.id).is_active is True  # тень
        calls = _llm(monkeypatch, {})  # в live модель не зовём — теневой вердикт применяется как есть
        stats = review_topics(db, settings, project.id, "live")
        assert stats["promoted"]["duplicate"] == 1 and calls == []
        db.expire_all()
        ent = db.get(Entity, dup.id)
        assert ent.is_active is False and json.loads(ent.value_json)["model_review"]["merged_into"] == app_ent.id
    finally:
        db.close()

def test_superseded_by_item_from_neighbor_topic(client, admin_token, tmp_data, db_engine, monkeypatch):
    """Способы одной потребности разведены по разным темам: пункт темы «ввод характеристик» заменён пунктом из темы
    «нормализация в 1С» — сосед по смыслу виден модели, хотя тема другая (замер «ПИЛОТА» 2026-09-28)."""
    from app.app.models.entity import Entity, Topic
    from app.app.services.review_relevance import review_topics
    from app.app.services.vector_index import upsert_entity_vectors
    from app.app.tests.test_entity_vectors import _prepare_vec

    _prepare_vec(db_engine)
    settings = get_settings().model_copy(update={"dry_run": False})
    db = db_mod.SessionLocal()
    try:
        project, topic, app_ent, norm, _new = _relevance_setup(db, settings)
        other = Topic(project_id=project.id, label="нормализация номенклатуры в 1С")
        db.add(other)
        db.flush()
        norm.topic_id = other.id
        db.commit()
        vec = [0.1] * 1024
        upsert_entity_vectors(db, [(app_ent.id, vec), (norm.id, vec)])
        db.commit()
        seen = []

        def reply(user):
            seen.append(user)
            return _superseded_reply(app_ent.id, norm.id)

        _llm(monkeypatch, {"актуальность": reply})
        stats = review_topics(db, settings, project.id, "live", topic_ids={topic.id})
        assert stats["superseded"] == 1 and stats["neighbors"] == 1
        assert "Похожие пункты из других тем" in seen[0] and f"E{norm.id}" in seen[0]
        db.expire_all()
        assert db.get(Entity, app_ent.id).is_active is False
        assert json.loads(db.get(Entity, norm.id).value_json).get("relevance") is None  # сосед в чужой теме не оценивается
    finally:
        db.close()


def test_big_topic_reviewed_in_batches_oldest_included(client, admin_token, tmp_data, db_engine, monkeypatch):
    """Тема больше MAX_ITEMS: раньше модель видела только последние 40 пунктов, и самый старый (его-то и заменяют) не
    проверялся — «ПИЛОТ», тема из 104 пунктов. Теперь порции по времени, заменивший из другой порции виден соседом."""
    from app.app.models.entity import Entity
    from app.app.services.review_relevance import MAX_ITEMS, review_topics
    from app.app.services.vector_index import upsert_entity_vectors
    from app.app.tests.test_entity_vectors import _prepare_vec

    _prepare_vec(db_engine)
    settings = get_settings().model_copy(update={"dry_run": False})
    db = db_mod.SessionLocal()
    try:
        project, topic, app_ent, norm, new = _relevance_setup(db, settings)
        norm.value_json = json.dumps({**json.loads(norm.value_json), "start_sec": 9999.0}, ensure_ascii=False)  # последний в теме
        fillers = [_ent(db, new, "task", {"description": f"Проверить поле номер {i}", "start_sec": 100.0 + i}, topic.id)
                   for i in range(MAX_ITEMS)]
        db.commit()
        upsert_entity_vectors(db, [(app_ent.id, [0.1] * 1024), (norm.id, [0.1] * 1024)])
        db.commit()
        seen = []

        def reply(user):
            seen.append(user)
            return _superseded_reply(app_ent.id, norm.id)

        _llm(monkeypatch, {"актуальность": reply})
        stats = review_topics(db, settings, project.id, "live", topic_ids={topic.id})
        assert stats["llm_calls"] == 2 and stats["items"] == MAX_ITEMS + 2 and stats["superseded"] == 1
        assessed = [line.split()[0] for user in seen for line in user.split("## Похожие")[0].splitlines() if line.startswith("E")]
        assert sorted(assessed) == sorted(f"E{e.id}" for e in [app_ent, norm, *fillers])  # каждый пункт — ровно один раз
        assert f"E{app_ent.id} |" in seen[0] and f"E{norm.id} |" in seen[0].split("## Похожие")[1]
        db.expire_all()
        assert db.get(Entity, app_ent.id).is_active is False
    finally:
        db.close()

def test_truncated_entities_reply_splits_chunk(monkeypatch):
    """Ответ упёрся в max_tokens (оборванный JSON) — часть делится пополам, а не повторяется целиком."""
    from types import SimpleNamespace

    from app.app.config import get_settings
    from app.app.services import entities as ents

    calls: list[str] = []

    def fake(_settings, *, system, user, max_tokens):
        calls.append(user)
        if "P1" in user and "P4" in user:
            return '{"summary": "обрыв'
        return json.dumps({"summary": "ok", "decisions": [], "tasks": [], "facts": {}, "mentions": [], "participants": []})

    monkeypatch.setattr(ents, "chat_completion_no_think", fake)
    art = SimpleNamespace(artifact_id="art_x", source_type="document", file_type="docx", recorded_at=None, created_at=None)
    monkeypatch.setattr(ents, "_numbered_message_parts", lambda *_a, **_k: None)
    monkeypatch.setattr(ents, "_numbered_turn_parts", lambda *_a, **_k: [])
    monkeypatch.setattr(ents, "_numbered_unit_parts", lambda *_a, **_k: ["P1 а\nP2 б\nP3 в\nP4 г"])
    monkeypatch.setattr(ents, "_material_timestamp", lambda _a: "")
    ents.extract_entities_llm(get_settings(), art, "x")
    assert len(calls) == 3
    assert "P1" in calls[1] and "P3" not in calls[1] and "P3" in calls[2]
