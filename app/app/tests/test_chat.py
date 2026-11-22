import json
from unittest.mock import patch

from sqlalchemy import select

from app.app.models.chat_message import ChatMessage
from app.app.services.chat import (
    CHAT_PENDING_TEXT,
    CHAT_TRIM_NOTE,
    _fit_chat_user,
    answer_chat,
    chat_entity_line,
    chat_is_pending,
    chat_summary_for_focus,
    pick_focus_hits,
    screens_beside_speech,
)


def test_fit_chat_user_trims_oversized_prompt(monkeypatch):
    """Большой контекст ужимается под окно модели, вопрос в начале сохраняется."""
    from app.app.config import Settings
    from app.app.services.llm_lanes import estimate_tokens

    class _Profile:
        context_length = 32768

    monkeypatch.setattr("app.app.services.llm_keeper.load_profile", lambda: _Profile())
    settings = Settings(lmstudio_chat_token_budget=8000)
    system = "system"
    user = "Вопрос: что решили?\n\n" + ("блок контекста проекта. " * 8000)
    out = _fit_chat_user(settings, system, user, max_tokens=2048)
    assert out.startswith("Вопрос: что решили?")
    assert CHAT_TRIM_NOTE in out
    assert estimate_tokens(system, out) <= 8000
    assert len(out) < len(user)


def test_pick_focus_hits_keeps_speech_when_screens_rank_higher():
    """Кадр формы с заголовком документа не занимает все места: в фокус входят реплики."""
    screen = "[12:22 - 12:29] Экран (показывал Иван): форма «Авансовые отчеты», таблица сумм"
    speech = "[44:19 - 44:39] Иван: у авансового отчёта нету статуса, что он подписан"
    hits = [
        {"chunk_id": 1, "text": screen, "start_sec": 742},
        {"chunk_id": 2, "text": screen + " ещё", "start_sec": 800},
        {"chunk_id": 3, "text": speech, "start_sec": 2659},
        {"chunk_id": 4, "text": "[45:58 - 46:48] Иван: подписанная PDF засунется в присоединённые файлы", "start_sec": 2758},
    ]
    picked = pick_focus_hits(hits, "Сформулируй вопрос по авансовому отчету последнего созвона", limit=2)
    assert [h["chunk_id"] for h in picked] == [3, 4]


def test_screen_beside_speech_keeps_the_frame_of_that_minute():
    """Кадр той же минуты остаётся, кадр другой минуты с тем же заголовком — нет."""
    speech = [{"chunk_id": 3, "text": "реплика", "start_sec": 2659, "end_sec": 2679}]
    early = {"chunk_id": 1, "text": "форма", "start_sec": 742, "end_sec": 749}
    same_minute = {"chunk_id": 9, "text": "присоединённые файлы", "start_sec": 2660, "end_sec": 2680}
    picked = screens_beside_speech(speech, [early, same_minute])
    assert [h["chunk_id"] for h in picked] == [9]


def test_pick_focus_hits_keeps_screens_when_question_is_about_the_screen():
    screen = "[08:36 - 08:45] Экран (показывал Иван): форма «Авансовые отчеты»"
    speech = "[09:00 - 09:10] Иван: смотри на форму"
    hits = [
        {"chunk_id": 1, "text": screen, "start_sec": 516},
        {"chunk_id": 2, "text": speech, "start_sec": 540},
    ]
    picked = pick_focus_hits(hits, "что было на экране в начале созвона", limit=1)
    assert [h["chunk_id"] for h in picked] == [1]


def test_chat_summary_for_focus_drops_ready_made_sections():
    """«Не закрыто» и «Итог» не входят в оглавление чата: оттуда письмо копирует выдуманный пункт."""
    summary = """## О встрече
Созвон про авансовый отчёт.

## По темам
- Три подписи: директор, подотчётное лицо, бухгалтер.

## Не закрыто
- Если факт подписания хранится в Битрикс24.

## Итог встречи и договорённости
- Согласовано создание дубля авансового отчёта в ЗУП.
"""
    kept = chat_summary_for_focus(summary)
    assert "Три подписи" in kept
    assert "Битрикс24" not in kept
    assert "Согласовано создание дубля" not in kept
    assert "Не закрыто" not in kept
    assert "Итог встречи" not in kept


def test_chat_entity_line_marks_paraphrase_without_quote():
    line = chat_entity_line(
        "decision",
        json.dumps({"text": "Факт подписания хранится в Битрикс24", "quote": ""}, ensure_ascii=False),
        None,
    )
    assert "дословной цитаты нет" in line
    long = chat_entity_line(
        "decision",
        json.dumps({"text": "а" * 800, "quote": ""}, ensure_ascii=False),
        None,
        limit=600,
    )
    assert long.endswith("дословной цитаты нет — не приводи этот текст как речь")
    assert len(long) <= 600
    quoted = chat_entity_line(
        "decision",
        json.dumps({"text": "решили", "quote": "да, так и делаем"}, ensure_ascii=False),
        None,
    )
    assert "дословной цитаты нет" not in quoted


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
    admin = db.scalar(select(User).where(User.telegram_id == "tg-admin"))
    regular = db.scalar(select(User).where(User.telegram_id == "tg-user"))
    admin_id = admin.id
    regular_id = regular.id
    p = Project(name="IsoProj", description="", created_by=regular_id, is_active=True)
    db.add(p)
    db.commit()
    db.refresh(p)
    pid = p.id
    db.close()

    with patch("app.app.services.chat.embed_texts", return_value=[[0.1] * 1024]):
        with patch("app.app.services.chat.search_vectors", return_value=[]):
            db2 = SessionLocal()
            from app.app.config import get_settings

            answer_chat(db2, get_settings(), pid, admin_id, "Вопрос админа")
            answer_chat(db2, get_settings(), pid, regular_id, "Вопрос пользователя")
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
            with patch("app.app.services.chat.chat_completion_no_think", side_effect=fake_completion):
                db2 = SessionLocal()
                from app.app.config import get_settings

                settings = get_settings()
                settings.dry_run = False
                out = answer_chat(db2, settings, pid, uid, "Какой бюджет?")
                db2.close()
    assert "4 млн" in out["answer"]
    assert "Сущности по найденным материалам" in captured["user"]
    assert "бюджет 4 млн" in captured["user"]

    # Запрос не поместился в окно модели — чат сокращает контекст и повторяет, а не отвечает «не удалось».
    prompts: list[str] = []

    def overflow_then_ok(settings, *, system, user, max_tokens, temperature=0.2):
        prompts.append(user)
        if len(prompts) == 1:
            raise RuntimeError('LM Studio HTTP 400 for http://x/v1/completions: {"error":"Context size has been exceeded."}')
        return "4 млн, ответ со второй попытки"

    with patch("app.app.services.chat.embed_texts", return_value=[[0.1] * 1024]):
        with patch("app.app.services.chat.search_vectors", return_value=[hit]):
            with patch("app.app.services.chat.chat_completion_no_think", side_effect=overflow_then_ok):
                db3 = SessionLocal()
                out = answer_chat(db3, settings, pid, uid, "Какой бюджет?")
                db3.close()
    assert "со второй попытки" in out["answer"] and len(prompts) == 2
    assert len(prompts[1]) <= len(prompts[0]) and "обсуждаем бюджет" in prompts[1] and "Какой бюджет?" in prompts[1]


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
        # модель дописывает свой список источников — в ответе его быть не должно (он есть под ответом со ссылками)
        return "Ответ по документу директора\n\n**Источники:**\n- art_dir_q (Вопрос 19 08 2026.txt)"

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
                with patch("app.app.services.chat.chat_completion_no_think", side_effect=fake_completion):
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
    assert out["answer"] == "Ответ по документу директора"
    doc_source = next(s for s in out["sources"] if s["filename"] == "Вопрос 19 08 2026.txt")
    assert doc_source["url"] == "/artifacts/art_dir_q/report"  # у документа тоже ссылка


def test_strip_sources_tail_only_trailing_list():
    from app.app.services.chat import strip_sources_tail

    body = "Сроки сдвинули на 20.10 (созвон 09.10.2026, 17:21)."
    assert strip_sources_tail(f"{body}\n\nИсточники:\n- art_1 (встреча-191.wav)\n- Актуальная выжимка") == body
    assert strip_sources_tail(f"{body}\n\n**Источники:**\n\n1. art_1\n2. art_2\n") == body
    assert strip_sources_tail(f"{body}\nИсточник: art_1 (встреча-191.wav)") == body
    # заголовок, после которого обычный текст, а не список, — не трогаем
    kept = f"{body}\nИсточники: в ТЗ и в переписке.\nДальше ответ продолжается."
    assert strip_sources_tail(kept) == kept
    assert strip_sources_tail("Источники: только это") == "Источники: только это"  # весь ответ не стирается


def test_chat_history_hides_old_sources_tail_and_adds_links(client, db_engine, admin_token, tmp_data):
    from app.app.db import SessionLocal
    from app.app.models.project import Project
    from app.app.models.user import User

    db = SessionLocal()
    admin = db.scalar(select(User).where(User.role == "admin").limit(1))
    p = Project(name="OldAnswers", description="", created_by=admin.id, is_active=True)
    db.add(p)
    db.commit()
    db.refresh(p)
    db.add_all([
        ChatMessage(project_id=p.id, user_id=admin.id, role="user", content="Что со сроками?", waiting=False),
        ChatMessage(project_id=p.id, user_id=admin.id, role="assistant", waiting=False,
                    content="Сроки сдвинули.\n\nИсточники:\n- art_old (встреча-106.wav)",
                    sources_json=json.dumps([{"artifact_id": "art_old", "filename": "встреча-106.wav", "kind": "entity"}])),
    ])
    db.commit()
    pid = p.id
    db.close()

    messages = client.get(f"/projects/{pid}/chat/history", headers={"Authorization": f"Bearer {admin_token}"}).json()["messages"]
    answer = messages[-1]
    assert answer["content"] == "Сроки сдвинули."
    assert answer["sources"][0]["url"] == "/artifacts/art_old/report"


def test_chat_prompt_asks_no_sources_list():
    from app.app.services import chat

    prompt = chat._load_prompt()
    assert "Отдельный список источников в конце не пиши" in prompt
    assert "перечисли источники" not in prompt


def test_chat_prompt_includes_document_summary_file(client, db_engine, admin_token, tmp_data):
    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.project import Project
    from app.app.models.user import User
    from app.app.services.storage import parsed_dir
    from sqlalchemy import select

    db = SessionLocal()
    admin = db.scalar(select(User).limit(1))
    p = Project(name="DocSumChat", description="", created_by=admin.id, is_active=True)
    db.add(p)
    db.commit()
    db.refresh(p)
    doc = Artifact(
        project_id=p.id,
        user_id=admin.id,
        artifact_id="art_doc_sum_chat",
        original_filename="Вопрос директора.txt",
        stored_filename="q.txt",
        file_path="/tmp/q.txt",
        sha256="docsumhash",
        file_type="txt",
        source_type="document",
        status="done",
        priority_weight=0.8,
        transcription_text="сырой текст без протокола",
    )
    db.add(doc)
    db.commit()
    pid, uid = p.id, admin.id
    db.close()

    settings = get_settings()
    parsed = parsed_dir(settings, pid, "document")
    parsed.mkdir(parents=True, exist_ok=True)
    (parsed / "art_doc_sum_chat_clean.txt").write_text(
        "Вопрос от директора: нужна ли плавающая длина номенклатуры?",
        encoding="utf-8",
    )
    (parsed / "art_doc_sum_chat_doc_summary.md").write_text(
        "## Суть документа\n\nМАРКЕР-ВЫЖИМКИ-ДОКУМЕНТА: плавающая длина.\n",
        encoding="utf-8",
    )

    captured: dict = {}

    def fake_completion(settings, *, system, user, max_tokens, temperature=0.2):
        captured["user"] = user
        return "по выжимке документа"

    with patch("app.app.services.chat.embed_texts", return_value=[[0.1] * 1024]):
        with patch("app.app.services.chat.search_vectors", return_value=[]):
            with patch("app.app.services.chat.chat_completion_no_think", side_effect=fake_completion):
                db2 = SessionLocal()
                settings.dry_run = False
                out = answer_chat(
                    db2,
                    settings,
                    pid,
                    uid,
                    "какой последний вопрос от директора?",
                )
                db2.close()

    prompt = captured["user"]
    assert "МАРКЕР-ВЫЖИМКИ-ДОКУМЕНТА" in prompt
    assert "плавающая длина номенклатуры" in prompt
    assert "Выжимка документа:" in prompt
    assert any(s["filename"] == "Вопрос директора.txt" for s in out["sources"])


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
            with patch("app.app.services.chat.chat_completion_no_think", side_effect=fake_completion):
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
                    with patch("app.app.services.chat.chat_completion_no_think", side_effect=fake_completion):
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


def _chat_unit(axis: int) -> list[float]:
    vec = [0.0] * 1024
    vec[axis] = 1.0
    return vec


def test_chat_uses_entity_hit_when_chunks_miss(client, db_engine, admin_token, tmp_data):
    from sqlalchemy import event

    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.entity import Entity
    from app.app.models.project import Project
    from app.app.models.user import User
    from app.app.services.ollama_client import EMBED_DIM
    from app.app.services.vector_index import (
        init_vector_store,
        load_sqlite_vec,
        upsert_entity_vectors,
    )

    @event.listens_for(db_engine, "connect")
    def _on_connect(dbapi_conn, _record):
        load_sqlite_vec(dbapi_conn)

    init_vector_store(db_engine)

    db = SessionLocal()
    admin = db.scalar(select(User).limit(1))
    p = Project(name="EntityChat", description="", created_by=admin.id, is_active=True)
    db.add(p)
    db.flush()
    other = Artifact(
        project_id=p.id,
        user_id=admin.id,
        artifact_id="art_budget_other",
        original_filename="budget.docx",
        stored_filename="budget.docx",
        file_path="budget.docx",
        sha256="entbudget",
        file_type="docx",
        source_type="document",
        status="done",
        priority_weight=0.8,
    )
    db.add(other)
    db.flush()
    fact = Entity(
        artifact_id=other.id,
        entity_type="fact",
        value_json='{"text": "бюджет 500 тысяч на лицензии 1С"}',
        confidence=0.9,
        is_active=True,
        needs_review=True,
    )
    db.add(fact)
    db.flush()
    upsert_entity_vectors(db, [(fact.id, _chat_unit(0))])
    db.commit()
    pid, uid = p.id, admin.id
    db.close()

    captured: dict = {}

    def fake_completion(settings, *, system, user, max_tokens, temperature=0.2):
        captured["user"] = user
        return "бюджет из сущности"

    with patch("app.app.services.chat.embed_texts", return_value=[_chat_unit(0)]):
        with patch("app.app.services.chat.search_vectors", return_value=[]):
            with patch("app.app.services.chat._document_context_blocks", return_value=[]):
                with patch("app.app.services.chat.chat_completion_no_think", side_effect=fake_completion):
                    db2 = SessionLocal()
                    from app.app.config import get_settings

                    settings = get_settings()
                    settings.dry_run = False
                    out = answer_chat(db2, settings, pid, uid, "какой бюджет на лицензии?")
                    db2.close()

    prompt = captured["user"]
    assert "Сущности проекта по вопросу" in prompt
    assert "бюджет 500 тысяч" in prompt
    assert any(s.get("kind") == "entity" and s["filename"] == "budget.docx" for s in out["sources"])
    assert EMBED_DIM == 1024


def test_chat_always_injects_tz_excerpt_when_file_exists(client, db_engine, admin_token, tmp_data):
    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.project import Project
    from app.app.models.user import User
    from app.app.services.storage import project_root

    db = SessionLocal()
    admin = db.scalar(select(User).limit(1))
    p = Project(name="TzChat", description="", created_by=admin.id, is_active=True)
    db.add(p)
    db.commit()
    db.refresh(p)
    pid, uid = p.id, admin.id
    db.close()

    settings = get_settings()
    tz_dir = project_root(settings, pid) / "project_state"
    tz_dir.mkdir(parents=True, exist_ok=True)
    (tz_dir / "current_tz.md").write_text(
        "МАРКЕР-ВЫЖИМКИ-ПРОЕКТА: приоритет — интеграция с Bitrix24.",
        encoding="utf-8",
    )

    captured: dict = {}

    def fake_completion(settings, *, system, user, max_tokens, temperature=0.2):
        captured["user"] = user
        return "по выжимке проекта"

    with patch("app.app.services.chat.embed_texts", return_value=[[0.1] * 1024]):
        with patch("app.app.services.chat.search_vectors", return_value=[]):
            with patch("app.app.services.chat._document_context_blocks", return_value=[]):
                with patch("app.app.services.chat.chat_completion_no_think", side_effect=fake_completion):
                    db2 = SessionLocal()
                    settings.dry_run = False
                    out = answer_chat(db2, settings, pid, uid, "что сейчас главное в проекте?")
                    db2.close()

    prompt = captured["user"]
    assert "Актуальная выжимка проекта (current_tz.md)" in prompt
    assert "МАРКЕР-ВЫЖИМКИ-ПРОЕКТА" in prompt
    assert "нет" not in out["answer"].lower()


def _meeting(pid: int, uid: int, name: str, day: int):
    from datetime import datetime

    from app.app.models.artifact import Artifact
    from app.app.services.recorded_at import SOURCE_MEETING

    return Artifact(
        project_id=pid,
        user_id=uid,
        artifact_id=f"art_meet_{name}",
        original_filename=f"{name}.wav",
        stored_filename=f"{name}.wav",
        file_path=f"{name}.wav",
        sha256=f"meet{name}",
        file_type="audio",
        source_type="call",
        status="done",
        priority_weight=1.0,
        recorded_at=datetime(2026, 10, day, 8, 0),
        recorded_at_source=SOURCE_MEETING,
        duration_sec=1200,
    )


def test_meetings_in_question_by_time_and_name():
    from datetime import date

    from app.app.services.chat import _meetings_in_question

    old, mid, new = _meeting(1, 1, "встреча-176", 5), _meeting(1, 1, "встреча-184", 6), _meeting(1, 1, "встреча-192", 9)
    meetings = [new, mid, old]  # новые сверху, как _project_meetings
    today = date(2026, 10, 10)
    assert _meetings_in_question("задачи на основании последнего созвона?", meetings, today=today) == [new]
    assert _meetings_in_question("что было на предпоследней встрече", meetings, today=today) == [new, mid]
    assert _meetings_in_question("что решили вчера", meetings, today=today) == [new]
    assert _meetings_in_question("итоги встречи 05.10", meetings, today=today) == [old]
    assert _meetings_in_question("что на встреча-184?", meetings, today=today) == [mid]
    assert _meetings_in_question("последний документ от заказчика", meetings, today=today) == []
    assert _meetings_in_question("как дела с прогнозом", meetings, today=today) == []


def test_chat_last_meeting_in_context_when_search_misses(client, db_engine, admin_token, tmp_data):
    """Урок 09.10: поиск по смыслу не нашёл свежий созвон — чат назвал последним старый и ответил «данных нет»."""
    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.project import Project
    from app.app.models.user import User
    from app.app.services.storage import parsed_dir

    db = SessionLocal()
    admin = db.scalar(select(User).limit(1))
    p = Project(name="LastMeetingChat", description="", created_by=admin.id, is_active=True)
    db.add(p)
    db.commit()
    db.refresh(p)
    pid, uid = p.id, admin.id
    db.add_all([_meeting(pid, uid, "встреча-176", 5), _meeting(pid, uid, "встреча-192", 9)])
    db.commit()
    db.close()

    settings = get_settings()
    parsed = parsed_dir(settings, pid, "call")
    parsed.mkdir(parents=True, exist_ok=True)
    (parsed / "art_meet_встреча-192_call_summary.md").write_text("МАРКЕР-НОВОГО-СОЗВОНА: план развития", encoding="utf-8")
    (parsed / "art_meet_встреча-176_call_summary.md").write_text("МАРКЕР-СТАРОГО-СОЗВОНА", encoding="utf-8")

    captured: dict = {}

    def fake_completion(settings, *, system, user, max_tokens, temperature=0.2):
        captured["user"] = user
        return "по последнему созвону"

    with patch("app.app.services.chat.embed_texts", return_value=[[0.1] * 1024]):
        with patch("app.app.services.chat.search_vectors", return_value=[]):
            with patch("app.app.services.chat._document_context_blocks", return_value=[]):
                with patch("app.app.services.chat.chat_completion_no_think", side_effect=fake_completion):
                    db2 = SessionLocal()
                    settings.dry_run = False
                    out = answer_chat(db2, settings, pid, uid, "какие задачи на развитие по последнему созвону?")
                    db2.close()

    prompt = captured["user"]
    assert "Созвоны проекта по датам" in prompt
    assert prompt.index("встреча-192.wav — созвон 09.10.2026") < prompt.index("встреча-176.wav — созвон 05.10.2026")
    assert "Созвон, о котором вопрос: встреча-192.wav" in prompt
    assert "МАРКЕР-НОВОГО-СОЗВОНА" in prompt
    assert "МАРКЕР-СТАРОГО-СОЗВОНА" not in prompt
    assert any(s["filename"] == "встреча-192.wav" for s in out["sources"])


def test_chat_window_estimate_and_overflow_detection():
    from app.app.services import chat, llm_keeper

    window = llm_keeper.load_profile().context_length
    assert chat._fits_model_window("система", "короткий вопрос")
    assert not chat._fits_model_window("", "я" * int(window * chat.CHAT_CHARS_PER_TOKEN))
    assert chat._is_context_overflow(RuntimeError('{"error":"Context size has been exceeded."}'))
    assert chat._is_context_overflow(RuntimeError("tokens to keep ... greater than the context length (n_keep: 34724>= n_ctx: 32768)"))
    assert not chat._is_context_overflow(RuntimeError("connection refused"))


def test_chat_parts_fit_window_keeping_found_fragments():
    """Запрос укладывается в окно заранее: второстепенное сокращается до нижних пределов, найденные фрагменты остаются."""
    from app.app.services import chat

    lines = [f"фрагмент {i} " + "т" * 1500 for i in range(28)]
    dialogue = "Предыдущий диалог:\n" + "\n".join(f"Пользователь: вопрос {i} " + "д" * 400 for i in range(60))
    parts = dict(memory=dialogue, supplement="с" * 11000, tz_excerpt="в" * 2500, decisions="р" * 1800, graph_part="г" * 4800)
    need = sum(len(x) for x in lines) + sum(len(v) for v in parts.values())

    roomy = chat.fit_chat_parts(need + 5000, lines, **parts)
    assert roomy["lines"] == lines and roomy["trimmed"] == [] and roomy["memory"] == dialogue  # места хватает — ничего не трогаем
    assert chat.fit_chat_parts(None, lines, **parts)["trimmed"] == []  # окно неизвестно — не ограничиваем

    tight = chat.fit_chat_parts(sum(len(x) for x in lines) + 12000, lines, **parts)
    assert tight["lines"] == lines, "фрагменты сокращаются последними"
    assert tight["graph_part"] == "" and "граф" in tight["trimmed"] and "прошлый диалог" in tight["trimmed"]
    assert "вопрос 59" in tight["memory"] and "вопрос 0 " not in tight["memory"] and "(более ранние реплики опущены)" in tight["memory"]
    assert len(tight["supplement"]) >= 3000 and len(tight["decisions"]) >= 1500

    tiny = chat.fit_chat_parts(20000, lines, **parts)
    assert chat.CHAT_MIN_FRAGMENTS <= len(tiny["lines"]) < len(lines) and tiny["lines"] == lines[: len(tiny["lines"])]
    assert any(t.startswith("фрагментов отброшено") for t in tiny["trimmed"])
