"""Урок 2026-09-28 (проект «Клиент А»): чат ответил по раннему ТЗ и не назвал более позднее уточнение заказчика.

Цепочка была такой: копия чата Битрикс24 не разбиралась на сообщения → пункт «список опоздавших не отсылается»
привязывался к вопросу исполнителя → проверка конфликта увидела вопрос без ответа и выключила верный пункт; ответ
куратора заказчика считался «высказыванием» и по Р1 не мог заменить пункт ТЗ; блок «что в силе» отрезался лимитом
допконтекста; смысловой лидер поиска без совпадения слов выпадал из выдачи. Здесь — каждая защита отдельно и сквозной
сценарий «переписка заказчика заменяет пункт внутреннего ТЗ».
"""
from __future__ import annotations

import json
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import patch

from app.app import db as db_mod
from app.app.config import get_settings

BITRIX = """[Елена Соколова]
Здравствуйте
в Битрикс24 в итоге нужно отправлять два потока данных: сводку по опозданиям и данные по всем проходам?

[Артур Григорян]
не совсем, договаривались что будет раз в промежуток отправляться пакет данных по проходам

[Владислав Лебедев]
Отправляем в каждой итерации все проходы за текущий день?

[Елена Соколова]
Проходы/уходы нужно слать в Битрикс. И отдельно - список опоздавших? Или список опоздавших в Битрикс не отсылается, только общий "проходы/уходы"?

[Артур Григорян]
общего списка приходов/уходов достаточно для битрикса, я их буду вносить в график и там уже видно будет кто опоздал а кто нет

{Цитата}
Владислав Лебедев
Отправляем в каждой итерации все проходы за текущий день?
{Конец цитаты}

Да, только вместо фамилий табельные номера

[Владислав Лебедев]
{Цитата}
Артур Григорян
Да, только вместо фамилий табельные номера
{Конец цитаты}
Хорошо, передам
"""

TZ_TEXT = ("Что отправляется в Битрикс24\n"
           "● Регулярно в Битрикс24 отправляется сводка по опозданиям за предыдущий рабочий день.\n"
           "● Руководитель получает данные только по сотрудникам своего подразделения.\n")
TZ_ITEM = "В Битрикс24 регулярно отправляется сводка по опозданиям за предыдущий рабочий день"
CHAT_ITEM = ("Отдельный список опоздавших сотрудников в Битрикс24 не отправляется; анализ опозданий выполняется на стороне "
             "заказчика по данным о проходах")


# ---------------------------------------------------------------- разбор копии чата Битрикс24

def test_bitrix_copy_parsed_into_messages():
    from app.app.services.chat_export import looks_like_bitrix_copy, parse_bitrix_copy, render_bitrix_text

    assert looks_like_bitrix_copy(BITRIX)
    export = parse_bitrix_copy(BITRIX, date(2026, 9, 25))
    msgs = {m["id"]: m for m in export["messages"]}
    assert [m["author"] for m in export["messages"]] == ["Елена Соколова", "Артур Григорян", "Владислав Лебедев", "Елена Соколова",
                                                        "Артур Григорян", "Артур Григорян", "Владислав Лебедев"]
    assert msgs[5]["text"].startswith("общего списка приходов/уходов достаточно") and msgs[5]["reply_to"] is None
    assert msgs[6]["reply_to"] == 3 and msgs[6]["text"] == "Да, только вместо фамилий табельные номера"  # цитата не в тексте
    assert msgs[7]["reply_to"] == 6 and msgs[7]["text"] == "Хорошо, передам"
    assert msgs[1]["at"] == "2026-09-25T00:00:00+03:00" and msgs[1]["at_precision"] == "day"  # время не выдумано
    text = render_bitrix_text(export)
    assert "{Цитата}" not in text and "[Артур Григорян] (в ответ на Владислав Лебедев: «Отправляем в каждой итерации" in text


def test_plain_note_with_brackets_is_not_chat():
    from app.app.services.chat_export import looks_like_bitrix_copy

    assert not looks_like_bitrix_copy("[Важно]\nсверить смету\n\n[Важно]\nпозвонить\n\n[Важно]\nотправить")  # один «автор»
    assert not looks_like_bitrix_copy("Протокол\n[Елена]\nтекст\n[Иван]\nтекст\n[Елена]\nтекст")  # начинается не с автора


def _project(db, name):
    from app.app.models.project import Project
    from app.app.models.user import User

    user = db.query(User).first()
    project = Project(name=name, created_by=user.id, is_active=True)
    db.add(project)
    db.flush()
    return project, user


def _chat_note(db, settings, project, user):
    """Копия чата, загруженная заметкой (.txt) — как «Выгрузка чата 25.09.26.txt» в проекте Клиент А."""
    from app.app.models.artifact import Artifact
    from app.app.services.pipeline_documents import process_document_artifact
    from app.app.services.storage import create_project_tree, raw_input_dir

    create_project_tree(settings, project.id)
    folder = raw_input_dir(settings, project.id, "note")
    folder.mkdir(parents=True, exist_ok=True)
    aid = f"art_bx_{project.id}"
    path = folder / f"{aid}.txt"
    path.write_text(BITRIX, encoding="utf-8")
    art = Artifact(artifact_id=aid, project_id=project.id, user_id=user.id, original_filename="Выгрузка чата 25.09.26.txt",
                   stored_filename=path.name, file_path=str(path), file_type="txt", source_type="note", priority_weight=0.6,
                   sha256=aid, status="uploaded", upload_date=datetime(2026, 9, 26, 8, 0))
    db.add(art)
    db.flush()
    text = process_document_artifact(db, settings, art)
    art.status = "done"
    db.commit()
    return art, text


def test_pipeline_ingests_bitrix_copy_without_llm(client, admin_token, tmp_data, monkeypatch):
    from app.app.services import entities as ents
    from app.app.services.chat_export import load_artifact_export
    from app.app.services.recorded_at import material_date

    monkeypatch.setattr("app.app.services.pipeline_documents.normalize_document",
                        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("переписку не нормализует модель")))
    settings = get_settings().model_copy(update={"dry_run": False})
    db = db_mod.SessionLocal()
    try:
        project, user = _project(db, "Копия чата Б24")
        art, text = _chat_note(db, settings, project, user)
        export = load_artifact_export(settings, art)
        assert export["format"] == "bitrix_copy" and len(export["messages"]) == 7
        assert text.startswith("[Елена Соколова]\nЗдравствуйте") and "{Конец цитаты}" not in text
        assert material_date(art) == (date(2026, 9, 25), "filename")  # дата из имени файла, не «из текста сообщений»
        parts = ents._numbered_message_parts(settings, art)
        assert "M5 [25.09.2026] Артур Григорян: общего списка" in parts[0][1]
        assert "M6 [25.09.2026] Артур Григорян (в ответ на M3): Да, только" in parts[0][1]
    finally:
        db.close()


# ---------------------------------------------------------------- пункт из ответа на вопрос

def _messages():
    from app.app.services.chat_export import parse_bitrix_copy

    return parse_bitrix_copy(BITRIX, date(2026, 9, 25))["messages"]


def test_answer_becomes_source_only_when_model_points_at_it():
    from app.app.services.entities import bind_chat_source

    msgs = _messages()
    # Слова пункта ближе к вопросу Елены (M4), но модель назвала и ответ Артура (M5): источник — ответ
    bound = bind_chat_source({"text": CHAT_ITEM, "messages": [4, 5]}, msgs, set())
    assert bound["message_id"] == 5 and bound["speaker"] == "Артур Григорян"
    assert bound["answer_to"] == 4 and bound["asked_by"] == "Елена Соколова" and bound["message_at_precision"] == "day"
    # Модель назвала только вопрос: это может быть отклонённое предложение — пункт остаётся на вопросе
    alone = bind_chat_source({"text": CHAT_ITEM, "messages": [4]}, msgs, set())
    assert alone["message_id"] == 4 and alone["speaker"] == "Елена Соколова" and "answer_to" not in alone
    # Открытый вопрос — источник сам вопрос, даже если модель назвала ответ
    question = bind_chat_source({"text": CHAT_ITEM, "messages": [4, 5]}, msgs, set(), answer_follows=False)
    assert question["message_id"] == 4
    # Прямая привязка к ответу с цитатой: вопрос — процитированное сообщение другого участника
    direct = bind_chat_source({"text": "Вместо фамилий передавать табельные номера", "messages": [6]}, msgs, set())
    assert direct["message_id"] == 6 and direct["answer_to"] == 3 and direct["asked_by"] == "Владислав Лебедев"


def test_day_only_messages_have_no_moment():
    from app.app.services import contract_text as ct
    from app.app.services.entities import _message_line
    from app.app.services.recorded_at import item_moment
    from app.app.services.review_relevance import _later

    msgs = _messages()
    assert _message_line(msgs[4]).startswith("M5 [25.09.2026] Артур Григорян: общего списка")
    parsed = ct.messages_from_export({"messages": msgs}, None)
    assert parsed[4].day == date(2026, 9, 25) and parsed[4].day_only and parsed[4].at() is None
    art = SimpleNamespace(recorded_at=None, recorded_at_source="filename", file_type="txt", original_filename="x.txt", upload_date=None)
    assert item_moment(art, {"message_at": msgs[4]["at"], "message_at_precision": "day"}) == (date(2026, 9, 25), None)
    same = SimpleNamespace(id=1)
    a = {"moment": None, "day": date(2026, 9, 25), "art": same, "value": {"message_id": 4}}
    b = {"moment": None, "day": date(2026, 9, 25), "art": same, "value": {"message_id": 5}}
    assert _later(a, b) is True and _later(b, a) is False  # одна переписка: порядок сообщений известен и без времени


def test_document_source_keeps_answer_after_question():
    from app.app.services.review_requirements import _document_source

    doc = ("[Елена Соколова]\nуточните: в Битрикс24 отправлять сводку по опозданиям?\n[Артур Григорян]\n"
           "не нужно, достаточно общего списка проходов\n[Иван]\nдругая тема\n[Иван]\nещё тема")
    source = _document_source("В Битрикс24 отправлять сводку по опозданиям", doc)[0]
    assert "не нужно, достаточно общего списка проходов" in source


# ---------------------------------------------------------------- Р1: ответ заказчика в переписке

def test_customer_answer_weight_and_cancel_rules():
    from app.app.services.conflict_rules import (WEIGHT_AGREED, WEIGHT_STATEMENT, cancel_allowed, customer_answer,
                                                 source_weight)

    sides = {"артур клиент": "customer", "артур": "customer", "елена соколова": "vendor", "елена": "vendor"}
    note = SimpleNamespace(file_type="txt", source_type="note", priority_weight=0.6)
    answer = {"speaker": "Артур Григорян", "speaker_source": "chat", "message_id": 5, "answer_to": 4, "asked_by": "Елена Соколова"}
    assert customer_answer(answer, sides)
    assert source_weight("decision", answer, note, CHAT_ITEM, sides) == WEIGHT_AGREED
    assert source_weight("decision", answer, note, CHAT_ITEM) == WEIGHT_STATEMENT  # сторона неизвестна — высказывание
    vendor = dict(answer, speaker="Елена Соколова", asked_by="Артур Григорян")  # ответ исполнителя — не решение заказчика
    assert source_weight("decision", vendor, note, CHAT_ITEM, sides) == WEIGHT_STATEMENT
    chat = {"weight": WEIGHT_AGREED, "customer_answer": True}
    assert cancel_allowed(chat, {"weight": 2})  # внутренний документ (ТЗ исполнителя) — заменяет
    assert not cancel_allowed(chat, {"weight": 3})  # документ заказчика — нет, ждёт человека
    assert cancel_allowed({"weight": WEIGHT_AGREED}, {"weight": 3})  # утверждённое на созвоне — как раньше (Р1)


# ---------------------------------------------------------------- поиск: смысловые лидеры не выпадают

def test_semantic_leaders_survive_fusion(monkeypatch):
    from app.app.services.retrieval import fuse_chunk_hits

    vec = [{"chunk_id": i, "artifact_id": 1, "text": f"t{i}", "score": 0.8 - i * 0.01} for i in range(1, 25)]
    fts = [{"chunk_id": i, "artifact_id": 1, "text": f"t{i}"} for i in [1] + list(range(3, 14))]  # у №2 нет слов вопроса
    monkeypatch.setattr("app.app.services.chunk_fts.search_chunk_fts", lambda *_a, **_k: fts)
    monkeypatch.setattr("app.app.services.vector_index.chunk_scores", lambda *_a, **_k: {})

    def rerank(_db, rows):
        return [dict(r, priority_weight=0.5, weighted_score=r["score"]) for r in rows]

    ids = [h["chunk_id"] for h in fuse_chunk_hits(None, "вопрос", [0.1], vec, project_id=1, limit=12, min_score=0.35, rerank=rerank)]
    assert len(ids) == 12 and 2 in ids and 1 in ids and 3 in ids


# ---------------------------------------------------------------- сквозной сценарий

def _setup(db, settings, *, tz_priority=0.8):
    """ТЗ исполнителя (24.09) со сводкой по опозданиям и копия чата (25.09), где куратор заказчика её отменяет."""
    from app.app.models.artifact import Artifact
    from app.app.models.entity import Entity, Topic
    from app.app.services.entities import EntitiesPayload, persist_entities
    from app.app.services.storage import create_project_tree, parsed_dir

    project, user = _project(db, "Опоздания")
    create_project_tree(settings, project.id)
    folder = parsed_dir(settings, project.id, "document")
    folder.mkdir(parents=True, exist_ok=True)
    tz = Artifact(artifact_id=f"art_tz_{project.id}", project_id=project.id, user_id=user.id, original_filename="Дополнение к ТЗ.pdf",
                  stored_filename="tz.pdf", file_path="/x", file_type="pdf", source_type="document", priority_weight=tz_priority,
                  sha256=f"tz{project.id}", status="done", upload_date=datetime(2026, 9, 24, 7, 56))
    db.add(tz)
    db.flush()
    (folder / f"{tz.artifact_id}_clean.txt").write_text(TZ_TEXT, encoding="utf-8")
    for name, role in (("Артур клиент", "Инженер службы безопасности, куратор проекта (Заказчик)"),
                       ("Елена Соколова", "Проектный менеджер (Исполнитель)")):
        db.add(Entity(artifact_id=tz.id, entity_type="participant", value_json=json.dumps({"name": name, "role": role}, ensure_ascii=False),
                      is_active=True, needs_review=False))
    topic = Topic(project_id=project.id, label="передача опозданий в Битрикс24")
    db.add(topic)
    db.flush()
    tz_ent = Entity(artifact_id=tz.id, entity_type="decision", value_json=json.dumps({"text": TZ_ITEM, "kind": "requirement"}, ensure_ascii=False),
                    is_active=True, needs_review=True, topic_id=topic.id)
    db.add(tz_ent)
    db.commit()
    chat, _ = _chat_note(db, settings, project, user)
    persist_entities(db, settings.model_copy(update={"dry_run": True}), chat,
                     EntitiesPayload(decisions=[{"text": CHAT_ITEM, "kind": "requirement", "messages": [4, 5]}]))
    chat_ent = db.query(Entity).filter(Entity.artifact_id == chat.id, Entity.entity_type == "decision").one()
    chat_ent.topic_id = topic.id
    db.commit()
    return project, tz, chat, tz_ent, chat_ent


def _relevance_llm(monkeypatch, tz_id, chat_id):
    reply = {"items": [{"id": f"E{tz_id}", "state": "superseded", "by": f"E{chat_id}", "confidence": 0.95,
                        "quote": "общего списка приходов/уходов достаточно для битрикса", "reason": "заказчик отказался от сводки"},
                       {"id": f"E{chat_id}", "state": "current", "confidence": 0.9}]}
    monkeypatch.setattr("app.app.services.llm.chat_completion_no_think", lambda *_a, **_k: json.dumps(reply, ensure_ascii=False))
    monkeypatch.setattr("app.app.services.gpu_phase.prepare_for_llm", lambda *_a, **_k: None)


def test_customer_chat_answer_supersedes_internal_tz_item(client, admin_token, tmp_data, monkeypatch):
    from app.app.models.entity import Entity
    from app.app.services.conflict_context import conflict_lines
    from app.app.services.review_relevance import review_topics

    monkeypatch.setattr("app.app.services.pipeline_documents.normalize_document", lambda *_a, **_k: "")
    settings = get_settings().model_copy(update={"dry_run": False})
    db = db_mod.SessionLocal()
    try:
        project, tz, chat, tz_ent, chat_ent = _setup(db, settings)
        value = json.loads(chat_ent.value_json)
        assert value["speaker"] == "Артур Григорян" and value["answer_to"] == 4  # решение — у ответившего заказчика
        _relevance_llm(monkeypatch, tz_ent.id, chat_ent.id)
        assert review_topics(db, settings, project.id, "live")["superseded"] == 1  # Р1 больше не блокирует
        db.expire_all()
        assert db.get(Entity, tz_ent.id).is_active is False and db.get(Entity, chat_ent.id).is_active is True
        resolved, _open = conflict_lines(db, project.id, artifact_ids={tz.id, chat.id})
        assert resolved and resolved[0].startswith("- В силе: [decision] Отдельный список опоздавших")
        assert "сказал: Артур Григорян" in resolved[0] and "Раньше считалось (заменено): [decision] В Битрикс24 регулярно" in resolved[0]
    finally:
        db.close()


def test_customer_chat_answer_does_not_cancel_customer_document(client, admin_token, tmp_data, monkeypatch):
    from app.app.models.entity import Entity
    from app.app.services.review_relevance import review_topics

    monkeypatch.setattr("app.app.services.pipeline_documents.normalize_document", lambda *_a, **_k: "")
    settings = get_settings().model_copy(update={"dry_run": False})
    db = db_mod.SessionLocal()
    try:
        project, _tz, _chat, tz_ent, chat_ent = _setup(db, settings, tz_priority=1.0)  # документ заказчика
        _relevance_llm(monkeypatch, tz_ent.id, chat_ent.id)
        assert review_topics(db, settings, project.id, "live")["superseded"] == 0
        db.expire_all()
        tz_row = db.get(Entity, tz_ent.id)
        assert tz_row.is_active is True
        assert "не отменяет документ заказчика" in json.loads(tz_row.value_json)["relevance"]["note"]
    finally:
        db.close()


def test_newer_item_switched_off_goes_to_important(client, admin_token, tmp_data, monkeypatch):
    from app.app.models.entity import Entity, Link
    from app.app.services.review_conflicts import _pair, apply_conflict_verdict, conflict_rows

    monkeypatch.setattr("app.app.services.pipeline_documents.normalize_document", lambda *_a, **_k: "")
    settings = get_settings().model_copy(update={"dry_run": False})
    db = db_mod.SessionLocal()
    try:
        project, _tz, _chat, tz_ent, chat_ent = _setup(db, settings)
        link = Link(project_id=project.id, source_entity_id=chat_ent.id, target_entity_id=tz_ent.id, link_type="POTENTIAL_CONFLICT",
                    is_automatic=True)
        db.add(link)
        db.flush()
        pair = _pair(db, settings, link, {})
        assert pair["older"]["id"] == tz_ent.id and pair["newer"]["customer_answer"] is True
        verdict = apply_conflict_verdict(db, settings, pair, verdict="older_wins", reason="источник B — вопрос", confidence=0.95,
                                         mode="live", model="m", rule="rule:not_a_decision", relation="not_decision",
                                         reject_kind="not_decision")
        db.commit()
        assert verdict == "older_wins"
        assert json.loads(db.get(Link, link.id).model_review)["important"] is True  # не деньги и не сроки — но выключено новое
        assert db.get(Entity, chat_ent.id).is_active is False
        assert [row["link_id"] for row in conflict_rows(db, project.id, view="important")] == [link.id]
    finally:
        db.close()


def test_chat_prompt_keeps_what_is_in_force_and_dates(client, admin_token, tmp_data, monkeypatch):
    from app.app.models.entity import Entity
    from app.app.services.chat import answer_chat
    from app.app.services.review_relevance import review_topics

    monkeypatch.setattr("app.app.services.pipeline_documents.normalize_document", lambda *_a, **_k: "")
    settings = get_settings().model_copy(update={"dry_run": False})
    db = db_mod.SessionLocal()
    try:
        project, tz, chat, tz_ent, chat_ent = _setup(db, settings)
        _relevance_llm(monkeypatch, tz_ent.id, chat_ent.id)
        review_topics(db, settings, project.id, "live")
        for i in range(20):  # длинные пункты материала: допконтекст сверх лимита — блок «что в силе» не должен отрезаться
            db.add(Entity(artifact_id=chat.id, entity_type="task", is_active=True, needs_review=True,
                          value_json=json.dumps({"description": f"Задача {i}: " + "подготовить выгрузку локаций турникетов " * 20},
                                                ensure_ascii=False)))
        db.commit()
        pid, uid = project.id, project.created_by
        hits = [{"chunk_id": 1, "artifact_id": tz.id, "chunk_index": 0, "text": TZ_TEXT, "speaker": None, "score": 0.9},
                {"chunk_id": 2, "artifact_id": chat.id, "chunk_index": 0, "text": "[Артур Григорян]\nобщего списка приходов/уходов достаточно",
                 "speaker": None, "score": 0.8}]
    finally:
        db.close()
    captured: dict = {}

    def fake(_settings, *, system, user, **_k):
        captured["system"], captured["user"] = system, user
        return "ответ"

    with patch("app.app.services.chat.embed_texts", return_value=[[0.1] * 1024]), \
            patch("app.app.services.chat.search_vectors", return_value=hits), \
            patch("app.app.services.chat.chat_completion_no_think", side_effect=fake):
        db2 = db_mod.SessionLocal()
        try:
            answer_chat(db2, settings, pid, uid, "Нужно ли определять опоздания и передавать их в Битркис24?")
        finally:
            db2.close()
    prompt = captured["user"]
    extra = prompt.split("Дополнительно из БД проекта:")[1]
    assert extra.lstrip().startswith("Изменения решений (что в силе и что заменено):")  # первым и целиком
    assert "В силе: [decision] Отдельный список опоздавших" in extra
    assert "Документ «Дополнение к ТЗ.pdf» · дата загрузки 24.09.2026 (дата документа неизвестна)" in prompt
    assert prompt.count("отправляется сводка по опозданиям за предыдущий рабочий день.") == 1  # фрагмент ТЗ не повторён
    assert "общего списка приходов/уходов достаточно" in prompt
    assert "более позднее уточнение заказчика" in captured["system"].lower()
