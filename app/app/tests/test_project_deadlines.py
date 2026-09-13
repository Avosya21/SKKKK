from __future__ import annotations

import json
from datetime import date, datetime

from sqlalchemy import select

from app.app.models.artifact import Artifact
from app.app.models.entity import Entity
from app.app.models.project import Project
from app.app.models.user import User
from app.app.services.project_deadlines import (
    artifact_reference_date,
    list_project_deadlines,
    parse_deadline_date,
    parse_deadline_range,
)


def test_parse_deadline_date_variants():
    assert parse_deadline_date("2026-03-15") == date(2026, 3, 15)
    assert parse_deadline_date("до 15.03.2026") == date(2026, 3, 15)
    assert parse_deadline_date("15 марта 2026") == date(2026, 3, 15)
    assert parse_deadline_date("срок не указан") is None
    assert parse_deadline_date(None) is None


def test_relative_deadline_from_call_date():
    # Созвон 19.08.2026 (ср) → «четверг-пятница» = 20–21.08
    ref = date(2026, 8, 19)
    start, end = parse_deadline_range("четверг-пятница", reference=ref)
    assert start == date(2026, 8, 20)
    assert end == date(2026, 8, 21)

    start, end = parse_deadline_range("до конца текущей недели", reference=ref)
    assert start == date(2026, 8, 23)  # воскресенье той же недели


def test_artifact_reference_date_from_filename():
    art = Artifact(
        artifact_id="x",
        project_id=1,
        user_id=1,
        original_filename="Rapporto_Запись_встречи_19.08.mp4",
        stored_filename="x.mp4",
        file_path="x",
        file_type="audio",
        source_type="call",
        status="done",
        sha256="abc",
    )
    assert artifact_reference_date(art) == date(datetime.now().year, 8, 19)


def test_list_project_deadlines_dedupes_and_sorts(db_engine, client, admin_token):
    from app.app.db import SessionLocal

    db = SessionLocal()
    user = db.scalar(select(User).limit(1))
    project = Project(name="Deadlines", description="", created_by=user.id, is_active=True)
    db.add(project)
    db.flush()

    art = Artifact(
        artifact_id="art_deadline_test",
        project_id=project.id,
        user_id=user.id,
        original_filename="call.mp3",
        stored_filename="call.mp3",
        file_path="x",
        file_type="audio",
        source_type="call",
        status="done",
        sha256="abc",
    )
    db.add(art)
    db.flush()

    payload = {
        "description": "Подготовить план",
        "assignee": "Аналитик",
        "deadline": "2026-04-01",
        "status": "open",
    }
    db.add(
        Entity(
            artifact_id=art.id,
            entity_type="task",
            value_json=json.dumps(payload, ensure_ascii=False),
            confidence=0.7,
            is_active=True,
            needs_review=False,
        )
    )
    db.add(
        Entity(
            artifact_id=art.id,
            entity_type="task",
            value_json=json.dumps(payload, ensure_ascii=False),
            confidence=0.7,
            is_active=True,
            needs_review=False,
        )
    )
    db.add(
        Entity(
            artifact_id=art.id,
            entity_type="task",
            value_json=json.dumps(
                {
                    "description": "Согласовать ТЗ",
                    "assignee": "РП",
                    "deadline": "до конца недели",
                    "status": "unknown",
                },
                ensure_ascii=False,
            ),
            confidence=0.7,
            is_active=True,
            needs_review=False,
        )
    )
    db.add(
        Entity(
            artifact_id=art.id,
            entity_type="task",
            value_json=json.dumps(
                {
                    "description": "Без срока — не должна попасть в дедлайны",
                    "assignee": "Кто-то",
                    "status": "open",
                },
                ensure_ascii=False,
            ),
            confidence=0.7,
            is_active=True,
            needs_review=False,
        )
    )
    db.commit()

    data = list_project_deadlines(db, project.id)
    assert data["total"] == 2
    assert data["items"][0]["description"] == "Подготовить план"
    assert data["items"][0]["deadline_date"] == "2026-04-01"

    r = client.get(
        f"/projects/{project.id}/deadlines",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2
    db.close()


def test_list_project_deadlines_skips_stage_duration_estimates(db_engine, client, admin_token):
    from app.app.db import SessionLocal

    db = SessionLocal()
    user = db.scalar(select(User).limit(1))
    project = Project(name="Estimates", description="", created_by=user.id, is_active=True)
    db.add(project)
    db.flush()
    art = Artifact(
        artifact_id="art_estimate_skip",
        project_id=project.id,
        user_id=user.id,
        original_filename="tz.docx",
        stored_filename="tz.docx",
        file_path="x",
        file_type="document",
        source_type="document",
        status="done",
        sha256="est",
    )
    db.add(art)
    db.flush()
    db.add(
        Entity(
            artifact_id=art.id,
            entity_type="task",
            value_json=json.dumps(
                {
                    "description": "Этап 1 MVP заказ → сделка",
                    "assignee": "Исполнитель",
                    "deadline": "4–6 нед.",
                    "status": "unknown",
                },
                ensure_ascii=False,
            ),
            confidence=0.7,
            is_active=True,
            needs_review=False,
        )
    )
    db.add(
        Entity(
            artifact_id=art.id,
            entity_type="task",
            value_json=json.dumps(
                {
                    "description": "Заполнить артикул перед этапом 2",
                    "assignee": "Заказчик",
                    "deadline": "перед этапом 2",
                    "status": "unknown",
                },
                ensure_ascii=False,
            ),
            confidence=0.7,
            is_active=True,
            needs_review=False,
        )
    )
    db.add(
        Entity(
            artifact_id=art.id,
            entity_type="task",
            value_json=json.dumps(
                {
                    "description": "Согласовать таблицу статусов",
                    "assignee": "Заказчик",
                    "deadline": "к пятнице",
                    "status": "open",
                },
                ensure_ascii=False,
            ),
            confidence=0.7,
            is_active=True,
            needs_review=False,
        )
    )
    db.commit()
    data = list_project_deadlines(db, project.id)
    assert data["total"] == 1
    assert data["items"][0]["deadline"] == "к пятнице"
    db.close()


def test_artifact_knowledge_text_prefers_lab_final(tmp_data):
    from app.app.config import get_settings
    from app.app.services.storage import artifact_knowledge_text, create_project_tree, parsed_dir

    get_settings.cache_clear()
    settings = get_settings()
    create_project_tree(settings, 1)
    path = parsed_dir(settings, 1, "call") / "art_lab_src_final.txt"
    path.write_text("LAB PROOF NAMED TURNS", encoding="utf-8")
    art = Artifact(
        artifact_id="art_lab_src",
        project_id=1,
        user_id=1,
        original_filename="call.webm",
        stored_filename="call.webm",
        file_path="x",
        file_type="audio",
        source_type="call",
        status="done",
        sha256="abc",
        transcription_text="OLD WHISPER MERGE",
    )
    assert artifact_knowledge_text(settings, art) == "LAB PROOF NAMED TURNS"
    get_settings.cache_clear()


def test_reextract_entities_dry_run_and_roles(client, manager_token, user_token, admin_token):
    from app.app.db import SessionLocal

    pid = client.post(
        "/projects",
        json={"name": "Reextract"},
        headers={"Authorization": f"Bearer {manager_token}"},
    ).json()["id"]

    db = SessionLocal()
    user = db.scalar(select(User).limit(1))
    art = Artifact(
        artifact_id="art_reextract_stub",
        project_id=pid,
        user_id=user.id,
        original_filename="call.webm",
        stored_filename="call.webm",
        file_path="x",
        file_type="audio",
        source_type="call",
        status="done",
        sha256="abc",
        transcription_text="[speaker_0] Сделаем план к пятнице.",
    )
    db.add(art)
    db.commit()
    db.close()

    forbidden = client.post(
        f"/projects/{pid}/reextract-entities",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert forbidden.status_code == 404

    ok = client.post(
        f"/projects/{pid}/reextract-entities",
        headers={"Authorization": f"Bearer {manager_token}"},
    )
    assert ok.status_code == 200
    body = ok.json()
    assert body["artifacts"] == 1
    assert body["succeeded"] == 1
    assert body["tasks_with_deadline"] == 0

    listed = client.get(
        f"/projects/{pid}/deadlines",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert listed.status_code == 200
    assert listed.json()["total"] == 0


def test_reextract_entities_returns_409_when_running(client, admin_token, manager_token):
    from app.app.db import SessionLocal
    from app.app.services.project_tasks import TASK_ENTITIES_REEXTRACT, begin_task

    pid = client.post(
        "/projects",
        json={"name": "ReextractLock"},
        headers={"Authorization": f"Bearer {manager_token}"},
    ).json()["id"]

    db = SessionLocal()
    begin_task(db, task_type=TASK_ENTITIES_REEXTRACT, project_id=pid)
    db.close()

    r = client.post(
        f"/projects/{pid}/reextract-entities",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 409
