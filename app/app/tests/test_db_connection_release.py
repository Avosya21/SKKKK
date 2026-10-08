"""Соединение БД не должно удерживаться на время ответа LLM (иначе пул исчерпывается и портал отвечает 500).

Причина исторической ошибки `QueuePool limit ... reached`: обработчик читал данные, не закрывал транзакцию и уходил на десятки секунд
в LLM — соединение из пула всё это время было занято. Здесь LLM-вызов подменён функцией, которая проверяет, держит ли текущий поток
соединение (`db.thread_held_connections`), на реальных путях с выключенным DRY_RUN.
"""

from __future__ import annotations

from unittest.mock import patch

from sqlalchemy import select

from app.app import db as db_mod
from app.app.models.project import Project
from app.app.models.user import User


def _project_and_user(SessionLocal):
    db = SessionLocal()
    p = Project(name="HoldProj", description="", created_by=1, is_active=True)
    db.add(p)
    db.commit()
    db.refresh(p)
    uid = db.scalar(select(User).limit(1)).id
    pid = p.id
    db.close()
    return pid, uid


def _real_settings():
    from app.app.config import get_settings

    return get_settings().model_copy(update={"dry_run": False})


def test_thread_held_connections_tracks_open_transaction(client, db_engine):
    from app.app.db import SessionLocal

    before = len(db_mod.thread_held_connections())  # соединения, оставшиеся от других тестов этого потока
    db = SessionLocal()
    db.execute(select(Project).limit(1)).all()  # чтение открывает транзакцию и берёт соединение
    assert len(db_mod.thread_held_connections()) == before + 1
    db.commit()
    assert len(db_mod.thread_held_connections()) == before
    db.close()


def test_chat_releases_connection_before_llm_call(client, db_engine, admin_token, tmp_data):
    from app.app.db import SessionLocal
    from app.app.services.chat import answer_chat

    pid, uid = _project_and_user(SessionLocal)
    held_at_call: list[list[str]] = []
    baseline = len(db_mod.thread_held_connections())  # чужие незакрытые соединения не в счёт

    def fake_llm(*_a, **_k):
        held_at_call.append(db_mod.thread_held_connections())
        return "Ответ модели"

    with patch("app.app.services.chat.embed_texts", return_value=[[0.1] * 1024]), \
            patch("app.app.services.chat.search_vectors", return_value=[]), \
            patch("app.app.services.chat._project_tz_excerpt", return_value="Краткая выжимка проекта"), \
            patch("app.app.services.chat.chat_completion_no_think", fake_llm):
        db2 = SessionLocal()
        out = answer_chat(db2, _real_settings(), pid, uid, "Кто поставщик серверов?")
        db2.close()
    assert out["answer"] == "Ответ модели"
    assert len(held_at_call) == 1 and len(held_at_call[0]) == baseline, (
        "чат держал соединение БД во время ответа LLM:\n" + "\n".join(held_at_call[0][baseline:][:1]))


def test_entity_extraction_does_not_reload_artifact_during_llm(client, db_engine, admin_token, tmp_data, monkeypatch):
    """Вызывающий делает db.commit() перед извлечением — artifact просрочен. Чтение его полей открывало транзакцию,
    и она висела все вызовы модели (worker.log 2026-10-08 14:29:21)."""
    import json

    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.services import entities as ents_mod

    pid, uid = _project_and_user(SessionLocal)
    held_at_call: list[list[str]] = []
    reply = json.dumps({"summary": "", "decisions": [], "tasks": [], "facts": {}, "mentions": [], "participants": []})

    def fake_llm(*_a, **_k):
        held_at_call.append(db_mod.thread_held_connections())
        return reply

    monkeypatch.setattr(ents_mod, "chat_completion_no_think", fake_llm)
    db = SessionLocal()
    art = Artifact(artifact_id=f"art_hold_{pid}", project_id=pid, user_id=uid, original_filename="doc.txt", stored_filename="doc.txt",
                   file_path="/x", file_type="document", source_type="document", sha256=f"hold{pid}", status="done")
    db.add(art)
    db.commit()  # как run_knowledge_pipeline: коммит перед извлечением просрочивает artifact
    baseline = len(db_mod.thread_held_connections())
    payload = ents_mod.extract_entities_llm(_real_settings(), art, "Первый абзац.\n\nВторой абзац.")
    db.close()
    assert payload.artifact_id == f"art_hold_{pid}"
    assert held_at_call and all(len(h) == baseline for h in held_at_call), (
        "извлечение держало соединение БД во время ответа LLM:\n" + "\n".join(held_at_call[0][baseline:][:1]))


LLM_CALLS = {"chat_completion_no_think", "chat_completion", "chat_from_env", "chat_from_env_no_think", "lmstudio_chat_raw",
             "lmstudio_completions_raw", "cursor_chat_raw", "_build_tz_body", "extract_entities_llm", "chat_completion_vision"}


def test_every_llm_call_with_a_session_in_scope_is_preceded_by_commit():
    """Статический сторож: функция, у которой есть сессия БД (`db`), обязана вызвать `db.commit()` до обращения к LLM.

    Функции без сессии (нормализация текста, сборка по готовому материалу) не проверяются: их вызывающие проверяются здесь же.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for folder in ("services", "routers", "workers"):
        for path in sorted((root / folder).glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
                if not any(a.arg == "db" for a in fn.args.args + fn.args.kwonlyargs):
                    continue
                first_llm = None
                commits: list[int] = []
                for node in ast.walk(fn):
                    if isinstance(node, ast.Call):
                        name = getattr(node.func, "id", getattr(node.func, "attr", ""))
                        if name in LLM_CALLS and (first_llm is None or node.lineno < first_llm):
                            first_llm = node.lineno
                        if name in {"commit", "rollback", "close"} and "db" in ast.unparse(node.func):
                            commits.append(node.lineno)
                if first_llm is not None and not any(c < first_llm for c in commits):
                    offenders.append(f"{path.name}:{fn.name} (LLM в строке {first_llm})")
    assert not offenders, "вызов LLM при открытой транзакции БД (нужен db.commit() перед ним): " + ", ".join(offenders)
