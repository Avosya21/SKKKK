"""Новые материалы получают дату сами: имя файла со временем, свойства документа, шапка документа; карточка проекта
обновляется вслед за выжимкой; кэш контракта видит темы и даты (SOURCE-ANCHORS-PLAN / REQUIREMENTS-PLAN, доводка)."""
from __future__ import annotations

import json
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

from app.app import db as db_mod
from app.app.config import get_settings
from app.app.services import recorded_at as ra

CORE = """<?xml version="1.0" encoding="UTF-8"?><cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
 xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<dcterms:created xsi:type="dcterms:W3CDTF">2026-08-10T09:00:00Z</dcterms:created><dcterms:modified xsi:type="dcterms:W3CDTF">{modified}</dcterms:modified></cp:coreProperties>"""


def _docx(path, modified="2026-08-17T12:42:11Z"):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("docProps/core.xml", CORE.format(modified=modified))
        zf.writestr("word/document.xml", "<w:document/>")
    return path


@pytest.mark.parametrize("name,expected", [
    ("Запись 2026-08-26 14-30.webm", datetime(2026, 8, 26, 11, 30)),
    ("meeting_2026-08-26_14.30.12.mp4", datetime(2026, 8, 26, 11, 30)),
    ("созвон 26.08.2026 14:30.m4a", datetime(2026, 8, 26, 11, 30)),
    ("созвон 26.08.2026.webm", None),
    ("отчёт 2026-08-26 v2.docx", None),
])
def test_filename_datetime(name, expected):
    assert ra.filename_datetime(name) == expected


def test_document_meta_dates(tmp_path):
    import pymupdf

    assert ra.document_meta_date(_docx(tmp_path / "a.docx"), "docx") == datetime(2026, 8, 17, 12, 42, 11)
    future = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert ra.document_meta_date(_docx(tmp_path / "b.docx", modified=future), "docx") == datetime(2026, 8, 10, 9, 0)  # будущая — не она
    pdf = tmp_path / "c.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.set_metadata({"modDate": "D:20260819150000+03'00'"})
    doc.save(pdf)
    doc.close()
    assert ra.document_meta_date(pdf, "pdf") == datetime(2026, 8, 19, 12, 0)
    (tmp_path / "broken.docx").write_bytes(b"not a zip")
    assert ra.document_meta_date(tmp_path / "broken.docx", "docx") is None


@pytest.mark.parametrize("text,expected", [
    ("Техническое задание\nЗаказчик: Пилот\nДата: 14.08.2026\n# 1. Общие сведения", "2026-08-14"),
    ("Протокол совещания от 19 августа 2026 г.\nПрисутствовали: …", "2026-08-19"),
    ("Коммерческое предложение от «5» августа 2026", "2026-08-05"),
    ("Срок сдачи до 15.10.2026. Оплата до 01.11.2026", None),  # сроки — не дата документа
    ("Письмо от 01.12.2026", None),  # позже загрузки — не дата документа
])
def test_text_header_date(text, expected):
    found = ra.text_header_date(text, datetime(2026, 9, 1))
    assert (found.isoformat() if found else None) == expected


def test_resolve_order_and_refine(tmp_path):
    docx = _docx(tmp_path / "ТЗ.docx")
    at, source = ra.resolve_recorded_at(original_filename="ТЗ.docx", file_path=docx, file_type="docx", upload_date=datetime(2026, 9, 1))
    assert source == ra.SOURCE_DOC_META and at == datetime(2026, 8, 17, 12, 42, 11)
    at, source = ra.resolve_recorded_at(original_filename="ТЗ 20.08.2026.docx", file_path=docx, file_type="docx", upload_date=datetime(2026, 9, 1))
    assert source == ra.SOURCE_FILENAME  # имя файла главнее свойств
    at, source = ra.resolve_recorded_at(original_filename="Запись 2026-08-26 14-30.webm", file_path=None, file_type="audio")
    assert source == ra.SOURCE_FILENAME_TIME and ra.recorded_moment(type("A", (), {"recorded_at": at, "recorded_at_source": source})()) is not None

    class Art:
        file_type = "docx"
        upload_date = datetime(2026, 9, 1)
        recorded_at, recorded_at_source = datetime(2026, 8, 17, 12, 42), ra.SOURCE_DOC_META

    art = Art()
    assert ra.refine_from_text(art, "Дата: 14.08.2026") and art.recorded_at_source == ra.SOURCE_DOC_TEXT
    art.recorded_at_source = ra.SOURCE_MANUAL
    assert not ra.refine_from_text(art, "Дата: 01.08.2026")  # ручную дату автоматика не трогает


def test_new_document_gets_date_from_header(client, admin_token, tmp_data):
    """Новый документ: свойства файла при загрузке, затем шапка документа при разборе — без участия человека."""
    from app.app.models.project import Project
    from app.app.models.user import User
    from app.app.services.pipeline_documents import process_document_artifact
    from app.app.services.storage import create_project_tree, raw_input_dir
    from app.app.services.upload import create_artifact_record

    settings = get_settings().model_copy(update={"dry_run": True})
    db = db_mod.SessionLocal()
    try:
        user = db.query(User).first()
        project = Project(name="Автодаты", created_by=user.id, is_active=True)
        db.add(project)
        db.flush()
        create_project_tree(settings, project.id)
        folder = raw_input_dir(settings, project.id, "document")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "protocol.txt"
        path.write_text("Протокол встречи от 19.08.2026\nРешили: мастер-система номенклатуры — 1С.\n", encoding="utf-8")
        art, _job = create_artifact_record(db, project_id=project.id, user_id=user.id, original_filename="Протокол.txt",
                                           stored_filename="art_autodate.txt", file_path=path, file_type="txt", source_type="document",
                                           sha256=f"autodate{project.id}", priority_weight=0.8, duration_sec=None, queue_job=False)
        assert art.recorded_at_source == ra.SOURCE_UPLOAD
        process_document_artifact(db, settings, art)
        db.commit()
        assert art.recorded_at_source == ra.SOURCE_DOC_TEXT and ra.material_date(art)[0].isoformat() == "2026-08-19"
    finally:
        db.close()


def test_project_card_follows_newer_summary(client, admin_token, tmp_data, monkeypatch):
    import os
    import time

    from app.app.models.project import Project
    from app.app.models.user import User
    from app.app.services import extraction_context as xc
    from app.app.services.tz import tz_paths

    settings = get_settings().model_copy(update={"dry_run": False})
    calls = []
    monkeypatch.setattr("app.app.services.llm.chat_completion_no_think", lambda *_a, **_k: calls.append(1) or f"## Цель\nверсия {len(calls)}")
    db = db_mod.SessionLocal()
    try:
        user = db.query(User).first()
        project = Project(name="Карточка свежая", created_by=user.id, is_active=True)
        db.add(project)
        db.commit()
        tz_path, _ = tz_paths(settings, project.id)
        tz_path.write_text("# Выжимка v1", encoding="utf-8")
        assert xc.ensure_project_card(db, settings, project.id) == "## Цель\nверсия 1"
        assert xc.ensure_project_card(db, settings, project.id) == "## Цель\nверсия 1" and len(calls) == 1
        later = time.time() + 5
        tz_path.write_text("# Выжимка v2", encoding="utf-8")
        os.utime(tz_path, (later, later))
        assert xc.ensure_project_card(db, settings, project.id) == "## Цель\nверсия 2"  # выжимка новее — черновик обновился
        xc.save_project_card(settings, project.id, "## Цель\nправка человека", source="human", by="Admin")
        os.utime(tz_path, (later + 10, later + 10))
        assert xc.ensure_project_card(db, settings, project.id) == "## Цель\nправка человека" and len(calls) == 2
    finally:
        db.close()


def test_contract_cache_sees_topics_and_dates(client, admin_token, tmp_data):
    from app.app.models.artifact import Artifact
    from app.app.models.entity import Entity, Topic
    from app.app.models.project import Project
    from app.app.models.user import User
    from app.app.services import contract as ctr

    settings = get_settings()
    db = db_mod.SessionLocal()
    try:
        user = db.query(User).first()
        project = Project(name="Кэш контракта", created_by=user.id, is_active=True)
        db.add(project)
        db.flush()
        art = Artifact(artifact_id=f"art_fp_{project.id}", project_id=project.id, user_id=user.id, original_filename="a.txt", stored_filename="a.txt",
                       file_path="/x", file_type="txt", source_type="document", sha256=f"fp{project.id}", status="done")
        db.add(art)
        db.flush()
        ent = Entity(artifact_id=art.id, entity_type="decision", value_json=json.dumps({"text": "Мастер-система — 1С"}, ensure_ascii=False))
        db.add(ent)
        db.commit()
        before = ctr._fingerprint(db, settings, project.id)
        topic = Topic(project_id=project.id, label="мастер-система")
        db.add(topic)
        db.flush()
        ent.topic_id = topic.id
        db.commit()
        after_topic = ctr._fingerprint(db, settings, project.id)
        assert after_topic != before
        art.recorded_at, art.recorded_at_source = datetime(2026, 8, 19, 12, 0), ra.SOURCE_DOC_TEXT
        db.commit()
        assert ctr._fingerprint(db, settings, project.id) != after_topic
    finally:
        db.close()
