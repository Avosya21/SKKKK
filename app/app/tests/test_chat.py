import json
from unittest.mock import patch

from sqlalchemy import select

from app.app.models.chat_message import ChatMessage
from app.app.services.chat import CHAT_PENDING_TEXT, answer_chat, chat_is_pending


def test_chat_without_data(client, db_engine, admin_token, tmp_data):
    from app.app.db import SessionLocal
    from app.app.models.project import Project
    from app.app.models.user import User

    db = SessionLocal()
    p = Project(name="ChatProj", description="", created_by=1, is_active=True)
    db.add(p)
    db.commit()
    db.refresh(p)
    pid = p.id
    user = db.scalar(select(User).limit(1))
    uid = user.id
    db.close()

    with patch("app.app.services.chat.embed_texts", return_value=[[0.1] * 1024]):
        with patch("app.app.services.chat.search_vectors", return_value=[]):
            db2 = SessionLocal()
            from app.app.config import get_settings

            out = answer_chat(db2, get_settings(), pid, uid, "Кто поставщик серверов?")
            db2.close()
    assert "нет" in out["answer"].lower()


def test_chat_history_saved_and_loaded(client, db_engine, admin_token, tmp_data):
    from app.app.db import SessionLocal
    from app.app.models.project import Project
    from app.app.models.user import User

    db = SessionLocal()
    p = Project(name="HistProj", description="", created_by=1, is_active=True)
    db.add(p)
    db.commit()
    db.refresh(p)
    user = db.scalar(select(User).limit(1))
    pid, uid = p.id, user.id
    db.close()

    with patch("app.app.services.chat.embed_texts", return_value=[[0.1] * 1024]):
        with patch("app.app.services.chat.search_vectors", return_value=[]):
            db2 = SessionLocal()
            from app.app.config import get_settings

            answer_chat(db2, get_settings(), pid, uid, "Первый вопрос")
            db2.close()

    r = client.get(
        f"/projects/{pid}/chat/history",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    messages = r.json()["messages"]
    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "Первый вопрос"
    assert messages[1]["role"] == "assistant"
    assert messages[1]["waiting"] is False


def test_chat_pending_placeholder_and_status(client, db_engine, admin_token, tmp_data):
    from app.app.db import SessionLocal
    from app.app.models.project import Project
    from app.app.models.user import User

    db = SessionLocal()
    p = Project(name="PendingChat", description="", created_by=1, is_active=True)
    db.add(p)
    db.commit()
    db.refresh(p)
    user = db.scalar(select(User).limit(1))
    pid, uid = p.id, user.id
    db.add(
        ChatMessage(
            project_id=pid,
            user_id=uid,
            role="user",
            content="Жду ответ",
            waiting=False,
        )
    )
    db.add(
        ChatMessage(
            project_id=pid,
            user_id=uid,
            role="assistant",
            content=CHAT_PENDING_TEXT,
            waiting=True,
        )
    )
    db.commit()
    db.close()

    headers = {"Authorization": f"Bearer {admin_token}"}
    hist = client.get(f"/projects/{pid}/chat/history", headers=headers).json()["messages"]
    assert hist[-1]["waiting"] is True

    status = client.get(f"/projects/{pid}/chat/status", headers=headers).json()
    assert status["pending"] is True

    r = client.post(
        "/chat",
        headers={**headers, "Content-Type": "application/json"},
        json={"project_id": pid, "question": "Ещё вопрос"},
    )
    assert r.status_code == 409


def test_chat_history_isolated_per_user(client, db_engine, admin_token, user_token, tmp_data):
    from app.app.db import SessionLocal
    from app.app.models.project import Project
    from app.app.models.user import User
    from sqlalchemy import select

    db = SessionLocal()
    p = Project(name="IsoProj", description="", created_by=1, is_active=True)
    db.add(p)
    db.commit()
    db.refresh(p)
    admin = db.scalar(select(User).where(User.telegram_id == "tg-admin"))
    regular = db.scalar(select(User).where(User.telegram_id == "tg-user"))
    pid = p.id
    db.close()

    with patch("app.app.services.chat.embed_texts", return_value=[[0.1] * 1024]):
        with patch("app.app.services.chat.search_vectors", return_value=[]):
            db2 = SessionLocal()
            from app.app.config import get_settings

            answer_chat(db2, get_settings(), pid, admin.id, "Вопрос админа")
            answer_chat(db2, get_settings(), pid, regular.id, "Вопрос пользователя")
            db2.close()

    admin_hist = client.get(
        f"/projects/{pid}/chat/history",
        headers={"Authorization": f"Bearer {admin_token}"},
    ).json()["messages"]
    user_hist = client.get(
        f"/projects/{pid}/chat/history",
        headers={"Authorization": f"Bearer {user_token}"},
    ).json()["messages"]

    assert len(admin_hist) == 2
    assert admin_hist[0]["content"] == "Вопрос админа"
    assert len(user_hist) == 2
    assert user_hist[0]["content"] == "Вопрос пользователя"


def test_chat_scoped_to_project(client, db_engine, admin_token, tmp_data):
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.project import Project
    from app.app.models.user import User
    from sqlalchemy import select

    db = SessionLocal()
    admin = db.scalar(select(User).limit(1))
    p1 = Project(name="P1", description="", created_by=admin.id, is_active=True)
    p2 = Project(name="P2", description="", created_by=admin.id, is_active=True)
    db.add_all([p1, p2])
    db.commit()
    db.refresh(p1)
    db.refresh(p2)
    art = Artifact(
        project_id=p1.id,
        user_id=admin.id,
        artifact_id="art_chat_scope",
        original_filename="a.txt",
        stored_filename="a.txt",
        file_path="/tmp/a.txt",
        sha256="abc",
        file_type="document",
        source_type="documents",
        status="done",
    )
    db.add(art)
    db.commit()
    db.refresh(art)
    hit = {
        "chunk_id": 1,
        "artifact_id": art.id,
        "chunk_index": 0,
        "text": "бюджет 4 млн",
        "speaker": None,
        "score": 0.9,
    }
    pid1, uid = p1.id, admin.id
    db.close()

    with patch("app.app.services.chat.embed_texts", return_value=[[0.1] * 1024]):
        with patch("app.app.services.chat.search_vectors") as search_mock:
            search_mock.return_value = [hit]
            db2 = SessionLocal()
            from app.app.config import get_settings

            answer_chat(db2, get_settings(), pid1, uid, "Какой бюджет?")
            db2.close()
            assert search_mock.call_args.kwargs["project_id"] == pid1


def test_chat_uses_entities_from_hit_artifacts(client, db_engine, admin_token, tmp_data):
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.entity import Entity
    from app.app.models.project import Project
    from app.app.models.user import User
    from sqlalchemy import select

    db = SessionLocal()
    admin = db.scalar(select(User).limit(1))
    p = Project(name="EntProj", description="", created_by=admin.id, is_active=True)
    db.add(p)
    db.commit()
    db.refresh(p)
    art = Artifact(
        project_id=p.id,
        user_id=admin.id,
        artifact_id="art_ent_chat",
        original_filename="call.wav",
        stored_filename="call.wav",
        file_path="/tmp/call.wav",
        sha256="def",
        file_type="audio",
        source_type="calls",
        status="done",
    )
    db.add(art)
    db.commit()
    db.refresh(art)
    ent = Entity(
        artifact_id=art.id,
        entity_type="fact",
        value_json=json.dumps({"text": "бюджет 4 млн"}, ensure_ascii=False),
        confidence=0.9,
        is_active=True,
        needs_review=False,
    )
    db.add(ent)
    db.commit()
    hit = {
        "chunk_id": 1,
        "artifact_id": art.id,
        "chunk_index": 0,
        "text": "обсуждаем бюджет",
        "speaker": "SPEAKER_00",
        "score": 0.88,
    }
    pid, uid = p.id, admin.id
    db.close()

    captured: dict = {}

    def fake_completion(settings, *, system, user, max_tokens, temperature=0.2):
        captured["user"] = user
        return "4 млн по материалам созвона"

    with patch("app.app.services.chat.embed_texts", return_value=[[0.1] * 1024]):
        with patch("app.app.services.chat.search_vectors", return_value=[hit]):
            with patch("app.app.services.chat.chat_completion", side_effect=fake_completion):
                db2 = SessionLocal()
                from app.app.config import get_settings

                settings = get_settings()
                settings.dry_run = False
                out = answer_chat(db2, settings, pid, uid, "Какой бюджет?")
                db2.close()
    assert "4 млн" in out["answer"]
    assert "Сущности по найденным материалам" in captured["user"]
    assert "бюджет 4 млн" in captured["user"]


def test_chat_includes_director_document_when_vector_search_misses(client, db_engine, admin_token, tmp_data):
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.project import Project
    from app.app.models.user import User
    from sqlalchemy import select

    db = SessionLocal()
    admin = db.scalar(select(User).limit(1))
    p = Project(name="DocChat", description="", created_by=admin.id, is_active=True)
    db.add(p)
    db.commit()
    db.refresh(p)
    doc = Artifact(
        project_id=p.id,
        user_id=admin.id,
        artifact_id="art_dir_q",
        original_filename="Вопрос 19 08 2026.txt",
        stored_filename="q.txt",
        file_path="/tmp/q.txt",
        sha256="qhash",
        file_type="txt",
        source_type="document",
        status="done",
        priority_weight=0.8,
    )
    call = Artifact(
        project_id=p.id,
        user_id=admin.id,
        artifact_id="art_call_old",
        original_filename="старый созвон.webm",
        stored_filename="c.webm",
        file_path="/tmp/c.webm",
        sha256="chash",
        file_type="audio",
        source_type="call",
        status="done",
    )
    db.add_all([doc, call])
    db.commit()
    db.refresh(doc)
    db.refresh(call)
    pid, uid = p.id, admin.id
    db.close()

    captured: dict = {}

    def fake_completion(settings, *, system, user, max_tokens, temperature=0.2):
        captured["user"] = user
        return "Ответ по документу директора"

    call_hit = {
        "chunk_id": 1,
        "artifact_id": call.id,
        "chunk_index": 0,
        "text": "Когда вы предлагаете по этому поводу встретиться?",
        "speaker": "SPEAKER_00",
        "score": 0.9,
    }

    with patch("app.app.services.chat._load_document_raw_excerpt") as raw_mock:
        raw_mock.return_value = (
            "Вопрос от директора\n\n"
            "Возможно ли создать такую номенклатуру, в которой длина не будет фиксированной характеристикой?"
        )
        with patch("app.app.services.chat.embed_texts", return_value=[[0.1] * 1024]):
            with patch("app.app.services.chat.search_vectors", return_value=[call_hit]):
                with patch("app.app.services.chat.chat_completion", side_effect=fake_completion):
                    db2 = SessionLocal()
                    from app.app.config import get_settings

                    settings = get_settings()
                    settings.dry_run = False
                    out = answer_chat(
                        db2,
                        settings,
                        pid,
                        uid,
                        "какой последний вопрос от директора был задан?",
                    )
                    db2.close()

    assert "номенклатур" in captured["user"].lower()
    assert "Вопрос 19 08 2026.txt" in captured["user"]
    assert "Загруженные документы проекта" in captured["user"]
    assert any(s["filename"] == "Вопрос 19 08 2026.txt" for s in out["sources"])


def test_chat_asks_clarification_on_source_split(client, db_engine, admin_token, tmp_data):
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.project import Project
    from app.app.models.user import User
    from sqlalchemy import select

    db = SessionLocal()
    admin = db.scalar(select(User).limit(1))
    p = Project(name="SplitChat", description="", created_by=admin.id, is_active=True)
    db.add(p)
    db.commit()
    db.refresh(p)
    doc = Artifact(
        project_id=p.id,
        user_id=admin.id,
        artifact_id="art_split_doc",
        original_filename="Протокол.txt",
        stored_filename="p.txt",
        file_path="/tmp/p.txt",
        sha256="ph1",
        file_type="txt",
        source_type="document",
        status="done",
        priority_weight=0.5,
    )
    call = Artifact(
        project_id=p.id,
        user_id=admin.id,
        artifact_id="art_split_call",
        original_filename="Созвон.webm",
        stored_filename="s.webm",
        file_path="/tmp/s.webm",
        sha256="sh1",
        file_type="audio",
        source_type="call",
        status="done",
        priority_weight=0.5,
    )
    db.add_all([doc, call])
    db.commit()
    db.refresh(doc)
    db.refresh(call)
    pid, uid = p.id, admin.id
    db.close()

    hits = [
        {
            "chunk_id": 1,
            "artifact_id": call.id,
            "chunk_index": 0,
            "text": "на созвоне решили иначе",
            "speaker": "SPEAKER_00",
            "score": 0.9,
        },
        {
            "chunk_id": 2,
            "artifact_id": doc.id,
            "chunk_index": 0,
            "text": "в протоколе другой приоритет",
            "speaker": None,
            "score": 0.88,
        },
    ]

    with patch("app.app.services.chat.embed_texts", return_value=[[0.1] * 1024]):
        with patch("app.app.services.chat.search_vectors", return_value=hits):
            with patch("app.app.services.chat._document_context_blocks", return_value=[]):
                db2 = SessionLocal()
                from app.app.config import get_settings

                settings = get_settings()
                settings.dry_run = False
                out = answer_chat(
                    db2,
                    settings,
                    pid,
                    uid,
                    "что считать актуальным приоритетом?",
                )
                db2.close()

    assert "уточнение" in out["answer"].lower()
    assert out.get("clarification") is True


def test_project_priority_question_does_not_ask_about_mention_conflicts(client, db_engine, admin_token, tmp_data):
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.entity import Entity, Link
    from app.app.models.project import Project
    from app.app.models.user import User
    from sqlalchemy import select

    db = SessionLocal()
    admin = db.scalar(select(User).limit(1))
    p = Project(name="PriorProj", description="", created_by=admin.id, is_active=True)
    db.add(p)
    db.commit()
    db.refresh(p)
    doc = Artifact(
        project_id=p.id,
        user_id=admin.id,
        artifact_id="art_tz_doc",
        original_filename="ТЗ.docx",
        stored_filename="t.docx",
        file_path="/tmp/t.docx",
        sha256="t1",
        file_type="docx",
        source_type="document",
        status="done",
        priority_weight=0.8,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    e1 = Entity(
        artifact_id=doc.id,
        entity_type="mention",
        value_json='{"name": "Рarus", "relation": "mentions"}',
        confidence=0.9,
        is_active=True,
        needs_review=False,
    )
    e2 = Entity(
        artifact_id=doc.id,
        entity_type="mention",
        value_json='{"name": "CRM Рarus", "relation": "mentions"}',
        confidence=0.9,
        is_active=True,
        needs_review=False,
    )
    db.add_all([e1, e2])
    db.commit()
    db.refresh(e1)
    db.refresh(e2)
    db.add(
        Link(
            project_id=p.id,
            source_entity_id=e1.id,
            target_entity_id=e2.id,
            link_type="POTENTIAL_CONFLICT",
            similarity_score=0.9,
            is_automatic=True,
        )
    )
    db.commit()
    pid, uid = p.id, admin.id
    doc_id = doc.id
    db.close()

    captured: dict = {}

    def fake_completion(settings, *, system, user, max_tokens, temperature=0.2):
        captured["user"] = user
        return "Приоритет — номенклатура и интegrация 1С–B24"

    hits = [
        {
            "chunk_id": 1,
            "artifact_id": doc.id,
            "chunk_index": 0,
            "text": "приоритет проекта",
            "speaker": None,
            "score": 0.9,
        }
    ]

    with patch("app.app.services.chat.embed_texts", return_value=[[0.1] * 1024]):
        with patch("app.app.services.chat.search_vectors", return_value=hits):
            with patch("app.app.services.chat.chat_completion", side_effect=fake_completion):
                db2 = SessionLocal()
                from app.app.config import get_settings

                settings = get_settings()
                settings.dry_run = False
                out = answer_chat(
                    db2,
                    settings,
                    pid,
                    uid,
                    "что у нас сейчас считается актуальным приоритетом в проекте?",
                )
                db2.close()

    assert out.get("clarification") is not True
    assert "уточнение" not in out["answer"].lower()
    assert "user" in captured


def test_chat_guidance_reply_reanswers_original_question(client, db_engine, admin_token, tmp_data):
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.chat_guidance import ChatGuidance
    from app.app.models.chat_message import ChatMessage
    from app.app.models.project import Project
    from app.app.models.user import User
    from sqlalchemy import func, select

    db = SessionLocal()
    admin = db.scalar(select(User).limit(1))
    p = Project(name="GuideChat", description="", created_by=admin.id, is_active=True)
    db.add(p)
    db.commit()
    db.refresh(p)
    pid, uid = p.id, admin.id
    art = Artifact(
        project_id=pid,
        user_id=uid,
        artifact_id="art_guide",
        original_filename="note.txt",
        stored_filename="n.txt",
        file_path="/tmp/n.txt",
        sha256="nh1",
        file_type="txt",
        source_type="document",
        status="done",
    )
    db.add(art)
    db.commit()
    db.refresh(art)
    art_id = art.id
    db.add(
        ChatMessage(
            project_id=pid,
            user_id=uid,
            role="user",
            content="что главнее?",
            waiting=False,
        )
    )
    db.add(
        ChatMessage(
            project_id=pid,
            user_id=uid,
            role="assistant",
            content="Нужно уточнение",
            waiting=False,
            meta_json='{"kind":"clarification","original_question":"что главнее?","topic":"source_split"}',
        )
    )
    db.commit()
    db.close()

    captured: dict = {}

    def fake_completion(settings, *, system, user, max_tokens, temperature=0.2):
        captured["user"] = user
        return "Ответ с учётом указания"

    hits = [
        {
            "chunk_id": 1,
            "artifact_id": art_id,
            "chunk_index": 0,
            "text": "материалы проекта",
            "speaker": None,
            "score": 0.9,
        }
    ]

    with patch("app.app.services.chat.embed_texts", return_value=[[0.1] * 1024]):
        with patch("app.app.services.chat.search_vectors", return_value=hits):
            with patch("app.app.services.chat.enrich_and_rerank_hits", side_effect=lambda db, h, limit=None: h):
                with patch("app.app.services.chat._document_context_blocks", return_value=[]):
                    with patch("app.app.services.chat.chat_completion", side_effect=fake_completion):
                        db2 = SessionLocal()
                        from app.app.config import get_settings

                        settings = get_settings()
                        settings.dry_run = False
                        out = answer_chat(db2, settings, pid, uid, "берём протокол от 19.08")
                        guidance_count = db2.scalar(
                            select(func.count()).select_from(ChatGuidance).where(ChatGuidance.project_id == pid)
                        )
                        db2.close()

    assert guidance_count == 1
    assert "Указания пользователя из чата" in captured["user"]
    assert "берём протокол" in captured["user"]
    assert "Ответ с учётом" in out["answer"]
