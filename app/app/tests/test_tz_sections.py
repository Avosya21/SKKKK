"""Выжимка по разделам (PORTAL-ACCURACY-MANAGER-PLAN, Д2): номера предметов, проверка полноты, задачи и вопросы без модели."""
from __future__ import annotations

import json
from types import SimpleNamespace

from app.app.services import tz_sections as ts


def _subj(i: int, title: str, state: str, summary: str = "", since: str = "2026-09-01T10:00:00+03:00") -> SimpleNamespace:
    return SimpleNamespace(id=i, title=title, state=state, summary=summary, state_since=since)


def test_item_line_and_trim():
    line = ts.item_line(_subj(7, "Обмен заказами", "agreed", summary="Решили обмен раз в час. " * 20))
    assert line.startswith("#7 [согласовано, 2026-09-01] Обмен заказами — Решили обмен раз в час.")
    assert line.endswith("…") and len(line) < ts.ITEM_SUMMARY_CHARS + 60
    assert ts.item_line(_subj(8, "Печатная форма", "documented", since="")) == "#8 [в документе] Печатная форма"


def test_ids_cited_stripped_and_missing():
    raw = ("## Ключевые решения\n\n**Обмен**\n- Обмен заказами раз в час [#1, #2].\n- Печатная форма в м2 [#4]\n"
           "Организационное: [#9]\n\n## Что сделано\n\n- Коннектор установлен [#5].")
    assert ts.cited_ids(raw) == {1, 2, 4, 9, 5}
    clean = ts.strip_ids(raw)
    assert "[#" not in clean and "Организационное" not in clean
    assert "- Обмен заказами раз в час." in clean and "- Коннектор установлен." in clean
    subjects = [_subj(1, "a", "agreed"), _subj(3, "Перенос остатков", "agreed"), _subj(6, "Документ", "documented"),
                _subj(9, "Время созвона", "agreed"), _subj(5, "Коннектор", "implemented")]
    assert [s.id for s in ts.missing_items(raw, subjects)] == [3]  # «в документе» без номера не обязателен, «Организационное» засчитано


def _project(client, manager_token, name):
    h = {"Authorization": f"Bearer {manager_token}"}
    return client.post("/projects", json={"name": name}, headers=h).json()["id"]


def test_build_sections_live_only_with_tasks_and_questions(client, manager_token, monkeypatch):
    import uuid

    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.decision_memory import Subject
    from app.app.models.entity import Entity
    from app.app.models.project import Project
    from app.app.services.extraction_context import save_project_card
    from app.app.services.memory_mode import set_mode

    pid = _project(client, manager_token, "Выжимка по разделам")
    settings = get_settings()
    db = SessionLocal()
    project = db.get(Project, pid)
    tag = uuid.uuid4().hex[:8]
    art = Artifact(artifact_id=f"art_2026-10-01_100000_{tag}", project_id=pid, user_id=1, original_filename="тз.docx", stored_filename="t.docx",
                   file_path="t", file_type="docx", source_type="document", sha256=tag.ljust(64, "0"), status="done")
    db.add(art)
    db.flush()
    db.add_all([
        Entity(artifact_id=art.id, entity_type="task", value_json=json.dumps({"description": "Прислать выгрузку остатков", "assignee": "Анна Петрова",
                                                                               "deadline": "01.10.2026", "status": "open"}, ensure_ascii=False)),
        Entity(artifact_id=art.id, entity_type="task", value_json=json.dumps({"description": "Старая сделанная", "status": "done"}, ensure_ascii=False)),
        Entity(artifact_id=art.id, entity_type="open_question", value_json=json.dumps({"text": "Нужна ли ставка НДС 22 %?", "options": []}, ensure_ascii=False)),
        Entity(artifact_id=art.id, entity_type="open_question",
               value_json=json.dumps({"text": "Устаревший вопрос", "relevance": {"state": "superseded"}}, ensure_ascii=False)),
    ])
    s1 = Subject(project_id=pid, title="Обмен заказами раз в час", state="agreed", state_since="2026-09-01", summary="Обмен раз в час.")
    s2 = Subject(project_id=pid, title="Перенос остатков из старой базы", state="agreed", state_since="2026-09-02")
    s3 = Subject(project_id=pid, title="Открытый вопрос", state="open")
    db.add_all([s1, s2, s3])
    db.commit()
    save_project_card(settings, pid, "Цель: обмен 1С и CRM. Заказчик: Альфа. Исполнитель: Бета.", source="human")

    calls = []

    def fake_llm(_settings, *, system, user, max_tokens, temperature):
        calls.append(user)
        return f"## Суть проекта\n\nОбмен 1С и CRM для Альфы.\n\n## Ключевые решения\n\n- Обмен заказами раз в час [#{s1.id}]."

    monkeypatch.setattr("app.app.services.llm.chat_completion_no_think", fake_llm)
    monkeypatch.setattr("app.app.services.ollama_client.embed_texts", lambda _s, texts: [[0.1] * 1024 for _ in texts])
    monkeypatch.setattr("app.app.services.vector_index.search_vectors", lambda *a, **k: [{"text": "Фрагмент про цель проекта."}])

    set_mode(db, project, "shadow")
    assert ts.build_sections_body(db, settings, pid) == (None, {}) and calls == []

    set_mode(db, project, "live")
    body, stats = ts.build_sections_body(db, settings, pid)
    assert f"#{s1.id} [согласовано, 2026-09-01] Обмен заказами раз в час" in calls[0] and "Открытый вопрос" not in calls[0]
    assert "Заказчик: Альфа" in calls[0] and "Фрагмент про цель проекта." in calls[0]
    assert "[#" not in body and "- Обмен заказами раз в час." in body
    assert "Также согласовано и сделано: Перенос остатков из старой базы (согласовано)." in body  # не упомянутое не теряется
    assert "| Анна Петрова | Прислать выгрузку остатков | 01.10.2026 (просрочено)" in body and "Старая сделанная" not in body
    assert "- Нужна ли ставка НДС 22 %?" in body and "Устаревший вопрос" not in body
    assert stats["missing"] == 1 and stats["cited"] == 1 and stats["mode"] == "sections"
    db.close()


def test_tasks_section_limit_and_overdue_first(client, manager_token, monkeypatch):
    from app.app.db import SessionLocal
    from app.app.services import task_board

    rows = [{"description": f"Задача {i}", "assignee": None, "deadline": f"{10 + i}.10.2026", "deadline_date": f"2026-10-{10 + i}",
             "status": "open", "overdue": i == 5} for i in range(20)]
    monkeypatch.setattr(task_board, "all_tasks", lambda db, pid, today=None: rows)
    db = SessionLocal()
    out = ts.tasks_section(db, 1)
    db.close()
    lines = [line for line in out.splitlines() if line.startswith("| — ")]
    assert lines[0].startswith("| — | Задача 5 |") and "(просрочено)" in lines[0]
    assert len(lines) == ts.TASKS_IN_SUMMARY and out.rstrip().endswith("в блоке «Задачи» на странице проекта.")
