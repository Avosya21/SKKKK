"""SOURCE-ANCHORS-PLAN: выгрузка Telegram (этап 1), пункты переписки по сообщениям (этап 2), время в поиске устаревшего
(этап 3), ссылки на место источника (этап 4)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app.app import db as db_mod
from app.app.config import get_settings

MSK = timezone(timedelta(hours=3))

TELEGRAM = """<!DOCTYPE html><html><head><meta charset="utf-8"/><title>Exported Data</title></head>
<body onload="CheckLocation();"><div class="page_wrap"><div class="page_header"><div class="content"><div class="text bold">
📍ПИЛОТ | АКМЕ
</div></div></div><div class="page_body chat_page"><div class="history">
<div class="message service" id="message-1"><div class="body details">25 August 2026</div></div>
<div class="message default clearfix" id="message106"><div class="pull_left userpic_wrap"><div class="userpic userpic8"><div class="initials">IA</div></div></div>
<div class="body"><div class="pull_right date details" title="25.08.2026 15:58:22 UTC+03:00">15:58</div>
<div class="from_name">Ilya Abramov</div>
<div class="reply_to details">In reply to <a href="#go_to_message105" onclick="return GoToMessage(105)">this message</a></div>
<div class="text">Здравствуйте. Приложение в Битрикс24 для ввода характеристик номенклатуры делаем?</div></div></div>
<div class="message default clearfix" id="message107"><div class="body"><div class="pull_right date details" title="25.08.2026 16:02:00 UTC+03:00">16:02</div>
<div class="from_name">Елена</div><div class="text">Нет, характеристики ведём в 1С:<br>нормализуем справочник номенклатуры профнастила</div></div></div>
<div class="message default clearfix joined" id="message108"><div class="body"><div class="pull_right date details" title="25.08.2026 16:03:10 UTC+03:00">16:03</div>
<div class="media_wrap clearfix"><a class="photo_wrap clearfix pull_left" href="photos/p.jpg"><img class="photo" src="photos/p_thumb.jpg"/></a></div>
<div class="text">Вот схема</div></div></div>
<div class="message default clearfix" id="message109"><div class="body"><div class="pull_right date details" title="25.08.2026 16:10:00 UTC+03:00">16:10</div>
<div class="from_name">Ilya Abramov</div>
<div class="reply_to details">In reply to <a href="#go_to_message107" onclick="return GoToMessage(107)">this message</a></div>
<div class="text">Да, согласен, так и делаем.</div></div></div>
<div class="message default clearfix" id="message110"><div class="body"><div class="pull_right date details" title="26.08.2026 16:37:09 UTC+03:00">16:37</div>
<div class="from_name">Елена</div><div class="media_wrap clearfix"><div class="media clearfix pull_left media_file"><div class="body">
<div class="title bold">Нормализация_НСИ.docx</div><div class="description">Not included</div></div></div></div></div></div>
</div></div></div></body></html>"""


def test_parse_telegram_export():
    from app.app.services import contract_text as ct
    from app.app.services.chat_export import looks_like_telegram_export, parse_telegram_html, render_chat_text

    assert looks_like_telegram_export(TELEGRAM)
    export = parse_telegram_html(TELEGRAM)
    assert export["title"] == "📍ПИЛОТ | АКМЕ"
    msgs = {m["id"]: m for m in export["messages"]}
    assert sorted(msgs) == [106, 107, 108, 109, 110]  # служебное «25 August 2026» пропущено
    assert msgs[106]["at"] == "2026-08-25T15:58:22+03:00" and msgs[106]["reply_to"] == 105
    assert msgs[107]["text"] == "Нет, характеристики ведём в 1С:\nнормализуем справочник номенклатуры профнастила"
    assert msgs[108]["author"] == "Елена" and msgs[108]["text"] == "[фото]\nВот схема"  # joined — автор предыдущего
    assert msgs[110]["text"] == "[файл: Нормализация_НСИ.docx]"
    parsed = ct.parse_chat(render_chat_text(export))
    assert [(m.speaker, m.at()) for m in parsed][:2] == [("Ilya Abramov", datetime(2026, 8, 25, 15, 58)), ("Елена", datetime(2026, 8, 25, 16, 2))]


def _chat_artifact(db, settings, project, user, aid, html, uploaded):
    from app.app.models.artifact import Artifact
    from app.app.services.pipeline_documents import process_document_artifact
    from app.app.services.storage import create_project_tree, raw_input_dir

    create_project_tree(settings, project.id)
    folder = raw_input_dir(settings, project.id, "document")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{aid}.html"
    path.write_text(html, encoding="utf-8")
    art = Artifact(artifact_id=aid, project_id=project.id, user_id=user.id, original_filename="messages.html", stored_filename=path.name,
                   file_path=str(path), file_type="html", source_type="document", sha256=aid, status="uploaded", upload_date=uploaded)
    db.add(art)
    db.flush()
    text = process_document_artifact(db, settings, art)
    db.commit()
    return art, text


def _project(db, name):
    from app.app.models.project import Project
    from app.app.models.user import User

    user = db.query(User).first()
    project = Project(name=name, created_by=user.id, is_active=True)
    db.add(project)
    db.flush()
    return project, user


def test_document_pipeline_ingests_export_and_overlap(client, admin_token, tmp_data, monkeypatch):
    """Этап 1 + Я1: выгрузка не нормализуется моделью; дата материала — по сообщениям; пересечение — у ранней выгрузки."""
    from app.app.services import entities as ents
    from app.app.services.chat_export import artifact_messages_path, load_export, owned_elsewhere
    from app.app.services.recorded_at import material_date

    monkeypatch.setattr("app.app.services.pipeline_documents.normalize_document", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("LLM")))
    settings = get_settings().model_copy(update={"dry_run": False})
    db = db_mod.SessionLocal()
    try:
        project, user = _project(db, "Переписка")
        first, text = _chat_artifact(db, settings, project, user, f"art_tg_a_{project.id}", TELEGRAM, datetime(2026, 8, 27, 10, 0))
        assert "**15:58 Ilya Abramov:** Здравствуйте." in text and first.recorded_at_source == "messages"
        assert material_date(first)[0].isoformat() == "2026-08-25"
        later_html = TELEGRAM.replace('id="message110"', 'id="message111"')
        second, _ = _chat_artifact(db, settings, project, user, f"art_tg_b_{project.id}", later_html, datetime(2026, 9, 17, 10, 0))
        path = artifact_messages_path(settings, second)
        assert owned_elsewhere(path.parent, load_export(path)) == {106, 107, 108, 109, 110}
        parts = ents._numbered_message_parts(settings, second)
        assert len(parts) == 1 and parts[0][1].startswith("M111 ") and "M109 [25.08.2026 16:10] Ilya Abramov (в ответ на M107)" in parts[0][0]
        fully = TELEGRAM  # полностью совпадающая выгрузка — пунктов не даёт, модель не зовём
        third, _ = _chat_artifact(db, settings, project, user, f"art_tg_c_{project.id}", fully, datetime(2026, 9, 20, 10, 0))
        monkeypatch.setattr(ents, "chat_completion_no_think", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("LLM")))
        assert ents.extract_entities_llm(settings, third, "").decisions == []
    finally:
        db.close()


def test_bind_chat_source_author_time_and_agreement():
    from app.app.services.chat_export import parse_telegram_html
    from app.app.services.entities import bind_chat_source

    msgs = parse_telegram_html(TELEGRAM)["messages"]
    value = {"text": "Характеристики номенклатуры ведём в 1С: нормализуем справочник профнастила", "messages": [107],
             "decided_by": [{"name": "Ilya Abramov", "message": 109}, {"name": "Елена", "message": 108}]}
    bound = bind_chat_source(value, msgs, set())
    assert bound["message_id"] == 107 and bound["speaker"] == "Елена" and bound["speaker_source"] == "chat"
    assert bound["message_at"] == "2026-08-25T16:02:00+03:00"
    assert bound["decided_by"] == [{"name": "Ilya Abramov", "message_id": 109, "at": "2026-08-25T16:10:00+03:00"}]  # автор сам себя не утверждает
    far = dict(value, decided_by=[{"name": "Елена", "message": 110}])
    assert "decided_by" not in bind_chat_source({**far, "text": "Приложение в Битрикс24 для ввода характеристик номенклатуры", "messages": [106]}, msgs, set())
    assert bind_chat_source(value, msgs, {107}) is None  # сообщение принадлежит более ранней выгрузке (Я1)
    lost = bind_chat_source({"text": "Интеграция с банком по API", "messages": [106]}, msgs, set())
    assert lost["source_status"] == "unbound" and "message_id" not in lost


def test_chat_items_in_contract_and_relevance(client, admin_token, tmp_data, monkeypatch):
    """Этапы 2–4: у пункта переписки автор, время, ссылка на сообщение; замена внутри переписки — по времени сообщений."""
    from app.app.models.entity import Entity, Topic
    from app.app.services import contract as ctr
    from app.app.services.entities import EntitiesPayload, persist_entities
    from app.app.services.review_relevance import review_topics

    settings = get_settings().model_copy(update={"dry_run": False})
    db = db_mod.SessionLocal()
    try:
        project, user = _project(db, "Переписка контракт")
        art, _ = _chat_artifact(db, settings, project, user, f"art_tg_k_{project.id}", TELEGRAM, datetime(2026, 8, 27, 10, 0))
        payload = EntitiesPayload(decisions=[
            {"text": "Сделать приложение в Битрикс24 для ввода характеристик номенклатуры", "kind": "project", "messages": [106]},
            {"text": "Характеристики номенклатуры ведём в 1С: нормализуем справочник профнастила", "kind": "project", "messages": [107],
             "decided_by": [{"name": "Ilya Abramov", "message": 109}]}])
        persist_entities(db, settings.model_copy(update={"dry_run": True}), art, payload)
        topic = Topic(project_id=project.id, label="ввод характеристик номенклатуры")
        db.add(topic)
        db.flush()
        rows = db.query(Entity).filter(Entity.artifact_id == art.id, Entity.entity_type == "decision").order_by(Entity.id).all()
        app_ent, norm = rows
        for ent in rows:
            ent.topic_id = topic.id
        db.commit()
        ctr._cache.clear()
        reqs = {r["source_entity_id"]: r for r in ctr.build_contract(db, settings, project.id).files["requirements"]["items"]}
        ev = reqs[norm.id]["evidence"][0]
        assert ev["speaker"] == "Елена" and ev["speaker_basis"] == "chat" and ev["date_basis"] == "message"
        assert ev["said_at"].startswith("2026-08-25T16:02") and ev["message_id"] == 107
        assert ev["url"].startswith(f"/artifacts/{art.artifact_id}/report?m=107") and ev["agreed_by"][0]["name"] == "Ilya Abramov"

        reply = {"items": [{"id": f"E{app_ent.id}", "state": "superseded", "by": f"E{norm.id}", "quote": "ведём в 1С",
                            "reason": "перешли на 1С", "confidence": 0.9}]}
        monkeypatch.setattr("app.app.services.llm.chat_completion_no_think", lambda *_a, **_k: json.dumps(reply, ensure_ascii=False))
        monkeypatch.setattr("app.app.services.gpu_phase.prepare_for_llm", lambda *_a, **_k: None)
        assert review_topics(db, settings, project.id, "live")["superseded"] == 1  # два сообщения одного дня — по времени
        db.expire_all()
        assert db.get(Entity, app_ent.id).is_active is False
    finally:
        db.close()


def test_same_day_call_needs_start_time(client, admin_token, tmp_data):
    """Я3/Я5: созвон и сообщение одного дня сравниваются, только если известно время начала созвона."""
    from types import SimpleNamespace

    from app.app.services.recorded_at import SOURCE_MANUAL, SOURCE_MANUAL_TIME, item_moment, manual_recorded_at, manual_recorded_at_time
    from app.app.services.review_relevance import _later

    chat_art = SimpleNamespace(id=2, recorded_at_source="messages", recorded_at=datetime(2026, 8, 25, 12, 58), original_filename="messages.html",
                               upload_date=datetime(2026, 9, 1), file_type="html")
    call_art = SimpleNamespace(id=1, recorded_at_source=SOURCE_MANUAL, recorded_at=manual_recorded_at(datetime(2026, 8, 25).date()),
                               original_filename="созвон.webm", upload_date=datetime(2026, 9, 1), file_type="audio")

    def item(art, value):
        day, moment = item_moment(art, value)
        return {"art": art, "value": value, "day": day, "moment": moment}

    msg = item(chat_art, {"message_at": "2026-08-25T16:02:00+03:00"})
    call = item(call_art, {"start_sec": 600.0})
    assert _later(call, msg) is None  # у созвона нет времени начала — «неясно»
    call_art.recorded_at, call_art.recorded_at_source = manual_recorded_at_time(datetime(2026, 8, 25, 14, 0)), SOURCE_MANUAL_TIME
    call = item(call_art, {"start_sec": 600.0})
    assert call["moment"] == datetime(2026, 8, 25, 14, 10, tzinfo=MSK) and _later(call, msg) is True
    assert item(chat_art, {"text": "без привязки"})["day"] is None  # непривязанный пункт переписки — день неизвестен


def test_source_url_forms():
    from app.app.services.source_links import source_url

    assert source_url("art_1", start_sec=754.25) == "/artifacts/art_1/report?t=754.2"
    assert source_url("art_1", message_id=106, quote="Приложение  в Битрикс24") == "/artifacts/art_1/report?m=106&q=%D0%9F%D1%80%D0%B8%D0%BB%D0%BE%D0%B6%D0%B5%D0%BD%D0%B8%D0%B5+%D0%B2+%D0%91%D0%B8%D1%82%D1%80%D0%B8%D0%BA%D1%8124"
    assert source_url("art_1", quote="") == "/artifacts/art_1/report" and source_url(None, start_sec=1) is None


def test_report_pages_get_jump_script_and_chat_anchors(client, admin_token, tmp_data):
    settings = get_settings().model_copy(update={"dry_run": False})
    db = db_mod.SessionLocal()
    try:
        project, user = _project(db, "Отчёт переписки")
        art, _ = _chat_artifact(db, settings, project, user, f"art_tg_r_{project.id}", TELEGRAM, datetime(2026, 8, 27, 10, 0))
        art.status = "done"
        db.commit()
        aid = art.artifact_id
    finally:
        db.close()
    client.cookies.set("skkkk_token", admin_token)
    r = client.get(f"/artifacts/{aid}/report?m=107", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200, r.text[:300]
    assert "/static/report-jump.js" in r.text and 'id="m107"' in r.text and 'class="chat-msg"' in r.text


def test_recorded_at_with_time_api(client, admin_token, tmp_data):
    from app.app.models.artifact import Artifact

    db = db_mod.SessionLocal()
    try:
        project, user = _project(db, "Время начала")
        db.add(Artifact(artifact_id=f"art_time_{project.id}", project_id=project.id, user_id=user.id, original_filename="созвон.webm",
                        stored_filename="x.webm", file_path="/x", file_type="audio", source_type="call", sha256=f"time{project.id}", status="done",
                        upload_date=datetime(2026, 8, 27, 10, 0)))
        db.commit()
        pid, aid = project.id, f"art_time_{project.id}"
    finally:
        db.close()
    h = {"Authorization": f"Bearer {admin_token}"}
    r = client.patch(f"/projects/{pid}/artifacts/{aid}/recorded-at", json={"datetime_local": "2026-07-22T14:30"}, headers=h)
    assert r.status_code == 200 and r.json()["material_date"] == "2026-07-22" and r.json()["material_date_source"] == "manual_time"
    db = db_mod.SessionLocal()
    try:
        art = db.query(Artifact).filter(Artifact.artifact_id == aid).one()
        assert art.recorded_at == datetime(2026, 7, 22, 11, 30)  # 14:30 МСК → UTC
    finally:
        db.close()


def test_rebind_chat_entities_and_chat_source(client, admin_token, tmp_data):
    """Пункты переписки, извлечённые до разбора выгрузки, привязываются к сообщениям без модели; проверка видит источник."""
    from app.app.models.entity import Entity
    from app.app.services.chat_export import parse_telegram_html
    from app.app.services.entities import rebind_chat_entities
    from app.app.services.review_requirements import _chat_source

    settings = get_settings().model_copy(update={"dry_run": False})
    db = db_mod.SessionLocal()
    try:
        project, user = _project(db, "Привязка переписки")
        art, _ = _chat_artifact(db, settings, project, user, f"art_tg_rb_{project.id}", TELEGRAM, datetime(2026, 8, 27, 10, 0))
        ent = Entity(artifact_id=art.id, entity_type="decision", value_json=json.dumps(
            {"text": "Характеристики номенклатуры ведём в 1С: нормализуем справочник профнастила"}, ensure_ascii=False))
        db.add(ent)
        db.commit()
        assert rebind_chat_entities(db, settings, art)["bound"] == 1
        db.expire_all()
        value = json.loads(db.get(Entity, ent.id).value_json)
        assert value["message_id"] == 107 and value["speaker"] == "Елена" and value["speaker_source"] == "chat"
        lines = _chat_source(value, value["text"], parse_telegram_html(TELEGRAM)["messages"])
        assert any(line.startswith("M107 [25.08.2026 16:02] Елена") for line in lines)
    finally:
        db.close()