from unittest.mock import patch

from sqlalchemy import select

from app.app.models.project import Project
from app.app.models.user import User
from app.app.models.user_activity_log import UserActivityLog
from app.app.services.chat import answer_chat
from app.app.services.user_activity import (
    ACTION_ANSWER,
    ACTION_QUESTION,
    BLOCK_CHAT,
    backfill_chat_activity,
    build_chat_user_memory,
    list_activity,
    log_activity,
)


def test_login_writes_auth_activity(client, db_engine, admin_token):
    from app.app.db import SessionLocal

    db = SessionLocal()
    rows = db.scalars(select(UserActivityLog).where(UserActivityLog.block == "auth")).all()
    db.close()
    assert any(r.action == "login" for r in rows)
    assert any(r.title.startswith("Вход") or "аккаунт" in r.title for r in rows)


def test_chat_writes_question_and_final_answer(client, db_engine, admin_token, tmp_data):
    from app.app.db import SessionLocal
    from app.app.config import get_settings

    db = SessionLocal()
    user = db.scalar(select(User).limit(1))
    p = Project(name="ActChat", description="", created_by=user.id, is_active=True)
    db.add(p)
    db.commit()
    db.refresh(p)
    pid, uid = p.id, user.id
    db.close()

    with patch("app.app.services.chat.embed_texts", return_value=[[0.1] * 1024]):
        with patch("app.app.services.chat.search_vectors", return_value=[]):
            db2 = SessionLocal()
            answer_chat(db2, get_settings(), pid, uid, "Кто заказчик?")
            db2.close()

    db3 = SessionLocal()
    rows = db3.scalars(
        select(UserActivityLog)
        .where(
            UserActivityLog.project_id == pid,
            UserActivityLog.user_id == uid,
            UserActivityLog.block == BLOCK_CHAT,
        )
        .order_by(UserActivityLog.id.asc())
    ).all()
    db3.close()
    assert [r.action for r in rows] == [ACTION_QUESTION, ACTION_ANSWER]
    assert rows[0].content == "Кто заказчик?"
    assert "нет" in (rows[1].content or "").lower()
    assert rows[1].content != "Думаю над ответом..."


def test_followup_chat_reads_activity_log(client, db_engine, admin_token, tmp_data):
    from app.app.db import SessionLocal
    from app.app.config import get_settings
    from app.app.models.artifact import Artifact

    db = SessionLocal()
    user = db.scalar(select(User).limit(1))
    p = Project(name="MemChat", description="", created_by=user.id, is_active=True)
    db.add(p)
    db.commit()
    db.refresh(p)
    art = Artifact(
        project_id=p.id,
        user_id=user.id,
        artifact_id="art_mem",
        original_filename="созвон.webm",
        stored_filename="c.webm",
        file_path="/tmp/c.webm",
        sha256="memhash",
        file_type="audio",
        source_type="call",
        status="done",
    )
    db.add(art)
    db.commit()
    db.refresh(art)
    pid, uid, art_id = p.id, user.id, art.id
    db.close()

    hit = {
        "chunk_id": 1,
        "artifact_id": art_id,
        "chunk_index": 0,
        "text": "бюджет 4 млн на 2026 год",
        "speaker": "SPEAKER_00",
        "score": 0.9,
    }
    captured: list[str] = []

    def fake_completion(settings, *, system, user, max_tokens, temperature=0.2):
        captured.append(user)
        return "4 млн на 2026"

    with patch("app.app.services.chat.embed_texts", return_value=[[0.1] * 1024]):
        with patch("app.app.services.chat.search_vectors", return_value=[hit]):
            with patch("app.app.services.chat.chat_completion_no_think", side_effect=fake_completion):
                db2 = SessionLocal()
                settings = get_settings()
                settings.dry_run = False
                answer_chat(db2, settings, pid, uid, "Какой бюджет?")
                db2.close()

    assert captured
    assert "Предыдущий диалог" not in captured[0]

    captured.clear()
    with patch("app.app.services.chat.embed_texts", return_value=[[0.1] * 1024]):
        with patch("app.app.services.chat.search_vectors", return_value=[]):
            with patch("app.app.services.chat.chat_completion_no_think", side_effect=fake_completion):
                db3 = SessionLocal()
                settings = get_settings()
                settings.dry_run = False
                out = answer_chat(db3, settings, pid, uid, "а это про какой год?")
                db3.close()

    assert "а это про какой год?" in captured[0]
    assert "Предыдущий диалог этого пользователя" in captured[0]
    assert "Какой бюджет?" in captured[0]
    assert "4 млн на 2026" in captured[0]
    assert "нет" not in out["answer"].lower() or "2026" in out["answer"]


def test_chat_memory_keeps_early_turns(client, db_engine, admin_token, tmp_data):
    from app.app.config import get_settings
    from app.app.db import SessionLocal

    db = SessionLocal()
    user = db.scalar(select(User).limit(1))
    p = Project(name="LongMem", description="", created_by=user.id, is_active=True)
    db.add(p)
    db.commit()
    db.refresh(p)
    pid, uid = p.id, user.id
    db.close()

    captured: list[str] = []

    def fake_completion(settings, *, system, user, max_tokens, temperature=0.2):
        captured.append(user)
        return "ок"

    with patch("app.app.services.chat.embed_texts", return_value=[[0.1] * 1024]):
        with patch("app.app.services.chat.search_vectors", return_value=[]):
            with patch("app.app.services.chat.chat_completion_no_think", side_effect=fake_completion):
                db2 = SessionLocal()
                settings = get_settings()
                settings.dry_run = False
                for i in range(1, 9):
                    answer_chat(db2, settings, pid, uid, f"Тема номер {i}: уникальный маркер MARK{i}")
                db2.close()

    last = captured[-1]
    assert "Пользователь: Тема номер 1: уникальный маркер MARK1" in last
    assert "Пользователь: Тема номер 7: уникальный маркер MARK7" in last
    assert "Пользователь: Тема номер 8:" not in last
    assert "Вопрос: Тема номер 8:" in last


def test_activity_memory_isolated_per_user(client, db_engine, admin_token, user_token, tmp_data):
    from app.app.db import SessionLocal
    from app.app.config import get_settings

    db = SessionLocal()
    admin = db.scalar(select(User).where(User.telegram_id == "tg-admin"))
    regular = db.scalar(select(User).where(User.telegram_id == "tg-user"))
    admin_id = admin.id
    regular_id = regular.id
    p = Project(name="IsoAct", description="", created_by=regular_id, is_active=True)
    db.add(p)
    db.commit()
    db.refresh(p)
    pid = p.id
    db.close()

    with patch("app.app.services.chat.embed_texts", return_value=[[0.1] * 1024]):
        with patch("app.app.services.chat.search_vectors", return_value=[]):
            db2 = SessionLocal()
            answer_chat(db2, get_settings(), pid, admin_id, "Секрет админа про бюджет")
            db2.close()

    db3 = SessionLocal()
    memory = build_chat_user_memory(db3, project_id=pid, user_id=regular_id)
    db3.close()
    assert "Секрет админа" not in memory

    mine = client.get(
        f"/projects/{pid}/activity",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert mine.status_code == 200
    texts = " ".join(e["title"] + (e.get("content") or "") for e in mine.json()["events"])
    assert "Секрет админа" not in texts

    admin_all = client.get(
        f"/projects/{pid}/activity?all_users=true",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert admin_all.status_code == 200
    assert admin_all.json()["all_users"] is True
    blob = " ".join(e.get("content") or "" for e in admin_all.json()["events"])
    assert "Секрет админа" in blob


def test_list_activity_hides_others_from_non_admin(client, db_engine, admin_token, user_token, tmp_data):
    from app.app.db import SessionLocal

    db = SessionLocal()
    admin = db.scalar(select(User).where(User.telegram_id == "tg-admin"))
    regular = db.scalar(select(User).where(User.telegram_id == "tg-user"))
    admin_id = admin.id
    regular_id = regular.id
    p = Project(name="HideAct", description="", created_by=regular_id, is_active=True)
    db.add(p)
    db.commit()
    db.refresh(p)
    log_activity(
        db,
        user_id=admin_id,
        project_id=p.id,
        block="upload",
        action="upload",
        title="Загрузил файл",
        content="secret.docx",
    )
    pid = p.id
    db.close()

    as_user = client.get(
        f"/projects/{pid}/activity?all_users=true",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert as_user.status_code == 200
    assert as_user.json()["all_users"] is False
    assert as_user.json()["events"] == []


def test_backfill_chat_activity_once(client, db_engine, admin_token, tmp_data):
    from app.app.db import SessionLocal
    from app.app.models.chat_message import ChatMessage

    db = SessionLocal()
    user = db.scalar(select(User).limit(1))
    p = Project(name="Backfill", description="", created_by=user.id, is_active=True)
    db.add(p)
    db.commit()
    db.refresh(p)
    db.add(
        ChatMessage(
            project_id=p.id,
            user_id=user.id,
            role="user",
            content="Старый вопрос",
            waiting=False,
        )
    )
    db.add(
        ChatMessage(
            project_id=p.id,
            user_id=user.id,
            role="assistant",
            content="Старый ответ",
            waiting=False,
        )
    )
    db.commit()
    n1 = backfill_chat_activity(db)
    n2 = backfill_chat_activity(db)
    events = list_activity(db, project_id=p.id, user_id=user.id)
    db.close()
    assert n1 == 2
    assert n2 == 0
    contents = [e["content"] for e in events]
    assert "Старый вопрос" in contents
    assert "Старый ответ" in contents
