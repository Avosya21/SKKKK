"""Jira в портале: токен, выбор проектов, загрузка задач целиком и вложений как материалов, обновление, сверка, доступ. Jira — подставная."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.app.services import jira_import as ji
from app.app.services import jira_render as jr
from app.app.tests.jira_fake import FakeJira, issue


@pytest.fixture()
def jira(monkeypatch):
    fake = FakeJira()
    monkeypatch.setenv("JIRA_BASE_URL", fake.base)
    monkeypatch.setenv("JIRA_IMPORT_MAX_ISSUES", "0")
    monkeypatch.setenv("JIRA_IMPORT_MEDIA", "true")
    from app.app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr(ji, "make_client", fake.factory)
    for name in ("connect", "visible_projects", "import_projects", "client_for", "scheduled_pass"):
        func = getattr(ji, name)
        defaults = list(func.__defaults__ or ())
        defaults[-1] = fake.factory
        monkeypatch.setattr(func, "__defaults__", tuple(defaults))
    # загрузка в тестах идёт сразу, а не в фоновом потоке
    monkeypatch.setattr(ji, "start_sync", lambda settings, link_ids, user_id, factory=fake.factory: [ji._sync_one(settings, i, user_id, factory) for i in link_ids])
    yield fake
    get_settings.cache_clear()


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _artifacts(client, token, project_id):
    r = client.get(f"/projects/{project_id}/artifacts", headers=_h(token))
    assert r.status_code == 200, r.text
    return r.json()


def _doc(name: str) -> str:
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact

    with SessionLocal() as db:
        art = db.scalar(select(Artifact).where(Artifact.original_filename == name))
        return open(art.file_path, encoding="utf-8", newline="").read()


def _import(client, token, key="ABC", jira_token="tok-manager"):
    assert client.post("/jira/connect", json={"token": jira_token}, headers=_h(token)).status_code == 200
    r = client.post("/jira/import", json={"projects": [{"key": key}]}, headers=_h(token))
    assert r.status_code == 200, r.text
    return r.json()["started"][0]["project_id"]


def test_token_is_encrypted_and_never_returned(client, jira, manager_token):
    r = client.post("/jira/connect", json={"token": "tok-manager"}, headers=_h(manager_token))
    assert r.status_code == 200 and r.json() == {"connected": True, "name": "Учётка manager", "projects": 2}
    state = client.get("/jira/status", headers=_h(manager_token)).json()
    assert state["available"] and state["connected"] and state["can_import"] and not state["can_link_existing"]
    assert "tok-manager" not in str(state)
    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.jira import JiraAccount

    with SessionLocal() as db:
        account = db.scalar(select(JiraAccount))
        assert "tok-manager" not in account.token_enc and ji.decrypt_token(get_settings(), account.token_enc) == "tok-manager"
    r = client.post("/jira/connect", json={"token": "wrong"}, headers=_h(manager_token))
    assert r.status_code == 400 and "401" in r.json()["detail"]
    r = client.post("/jira/connect", json={"token": "токен-кириллицей"}, headers=_h(manager_token))  # раньше падало в httpx
    assert r.status_code == 400 and "недопустимые символы" in r.json()["detail"]
    assert client.get("/jira/projects/ABC/count", headers=_h(manager_token)).json() == {"key": "ABC", "issues": 2}
    assert client.delete("/jira/connect", headers=_h(manager_token)).status_code == 204
    assert not client.get("/jira/status", headers=_h(manager_token)).json()["connected"]


def test_import_brings_everything_without_cuts(client, jira, manager_token):
    project_id = _import(client, manager_token)
    projects = {p["id"]: p for p in client.get("/projects", headers=_h(manager_token)).json()}
    assert projects[project_id]["name"] == "Проект Альфа" and projects[project_id]["jira_key"] == "ABC"
    arts = _artifacts(client, manager_token, project_id)
    assert sorted(a["original_filename"] for a in arts) == [
        "Jira ABC — сводка проекта.md", "Jira ABC-1 — Настроить обмен.md", "Jira ABC-1 — схема.txt", "Jira ABC-2 — Сверить остатки.md"]
    assert all(a["status"] == "uploaded" and a["source_type"] == "document" for a in arts)  # дальше — общий конвейер
    state = client.get(f"/projects/{project_id}/jira", headers=_h(manager_token)).json()["links"][0]
    assert state["status"] == "idle" and state["issues"] == 2 and state["files"] == 1 and state["issues_total"] == 2
    assert state["status_text"] == ("в Jira задач 2, загружено 2: новых 2, обновлено 0, без изменений 0; файлов: новых 1, уже были 0, не загружено 1")
    assert state["not_loaded"] == [{"issue": "ABC-1", "reason": "дамп.zip: вид файла .zip портал не разбирает"}]

    text = _doc("Jira ABC-1 — Настроить обмен.md")
    assert "\r" not in text
    for expected in (
        "# [ABC-1] Настроить обмен", "Задача Jira ABC-1 «Настроить обмен» проекта ABC (Проект Альфа): статус «В работе» (в работе), исполнитель — Тестовый Исполнитель",
        f"Ссылка: {jira.base}/browse/ABC-1", "- Резолюция: Готово", "- Решена: 2026-11-01 12:00", "- Срок: 2026-11-15", "- Метки: обмен, срочно",
        "- Компоненты: Обмен с сайтом", "- Версии исправления: Релиз 2", "- Затронутые версии: Релиз 1", "- Родительская задача: ABC-100 — Эпик обмена",
        "- Оценка времени: 2d", "- Осталось времени: 4h", "- Затрачено времени: 1d 4h", "- Наблюдателей: 3", "- Наблюдатели: Тестовый Автор, Тестовый Исполнитель", "- Окружение: Тестовый контур",
        "## Дополнительные поля", "- Заказчик: Заказчик А / Отдел закупок", "- Оценка в баллах: 5", "- Спринт: Спринт 7 (active)",
        "Описание задачи ABC-1.\nВторая строка (вложение: схема.txt).", "## Подзадачи (1)", "- ABC-11 — Подзадача раз [Готово]",
        "## Связанные задачи (1)", "- блокирует ABC-12 — Зависимая [Новая]", "## Внешние ссылки (1)", "- Инструкция: https://docs.example/instr",
        "## Комментарии (6)", "Комментарий 5: договорились", "### Тестовый Исполнитель, 2026-10-20 10:00 (изменён 2026-10-21 11:00)", "КОНЕЦ-КОММЕНТАРИЯ",
        "## Трудозатраты (4, всего 4ч)", "Итого по людям: Тестовый Исполнитель — 4ч (записей: 4).", "- 2026-10-04 09:00, Тестовый Исполнитель: 1h — работа 4",
        "## Вложения (2)", "- дамп.zip (0 КБ, загрузил(а) Тестовый Исполнитель, 2026-11-01 09:00; содержимое в портал не загружено: вид файла .zip портал не разбирает)",
        "; содержимое загружено в портал отдельным материалом)", "## История изменений (2)",
        "- 2026-10-05 10:00, Тестовый Автор: Статус (status): «Новая» → «В работе»; Исполнитель (assignee): «пусто» → «Тестовый Исполнитель»",
        "- 2026-10-06 10:00, Тестовый Исполнитель: Описание (description): текст изменён"):
        assert expected in text, expected
    assert "Rank" not in text and "0|i0001z" not in text and text.count("Слово ") == 6000  # служебное поле не попало, длинный комментарий цел

    summary = _doc("Jira ABC — сводка проекта.md")
    for expected in ("# Jira: проект ABC — Проект Альфа (сводка)", "- ID в Jira: 10000",
                     "- Руководитель проекта: Тестовый Автор (учётная запись author)",
                     f"- Аватар проекта: 48x48 {jira.base}/secure/projectavatar?pid=10000",
                     f"- Аватар руководителя: 48x48 {jira.base}/secure/useravatar?ownerId=author",
                     "- Компоненты: Обмен с сайтом (id 10010)", "- Версии: Релиз 1 (id 10020, выпущена 2026-09-01)",
                     "## Роли", "- Administrators (id 10002): Тестовый Автор (пользователь, author)",
                     "## Описание проекта", "Описание проекта.",
                     "- Всего задач: 2", "- в работе: 2", "- Тестовый Исполнитель: всего 2, не завершено 2",
                     "| 2 | ABC-2 | Задача | В работе | в работе | Тестовый Исполнитель | Тестовый Автор | 2026-10-01 10:00 | — | 2026-11-02 10:00 | Сверить остатки |"):
        assert expected in summary, expected
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.job import Job

    with SessionLocal() as db:
        doc = db.scalar(select(Artifact).where(Artifact.original_filename == "Jira ABC-1 — Настроить обмен.md"))
        assert db.scalar(select(Job).where(Job.artifact_id == doc.id)).kind == "process_artifact"
        assert doc.recorded_at is not None and (doc.recorded_at.year, doc.recorded_at.month) == (2026, 11)
        attachment = db.scalar(select(Artifact).where(Artifact.original_filename == "Jira ABC-1 — схема.txt"))
        assert open(attachment.file_path, "rb").read() == jira.files["501"]  # байты вложения — как в Jira


def test_update_replaces_changed_issue_and_skips_unchanged(client, jira, manager_token):
    project_id = _import(client, manager_token)
    before = {a["original_filename"]: a["artifact_id"] for a in _artifacts(client, manager_token, project_id)}
    jira.detail_calls.clear()
    changed = issue("ABC-2", "Сверить остатки", "2026-11-05T12:00:00.000+0300")
    changed["fields"]["description"] = "Описание изменили."
    jira.put(changed)
    jira.put(issue("ABC-3", "Новая задача", "2026-11-05T13:00:00.000+0300", category="new", status="Новая"))
    r = client.post(f"/projects/{project_id}/jira/sync", headers=_h(manager_token))
    assert r.status_code == 200, r.text
    assert sorted(jira.detail_calls) == ["ABC-2", "ABC-3"]  # неизменённую ABC-1 не перечитываем
    after = {a["original_filename"]: a["artifact_id"] for a in _artifacts(client, manager_token, project_id)}
    assert len(after) == 5 and "Jira ABC-3 — Новая задача.md" in after
    assert after["Jira ABC-1 — Настроить обмен.md"] == before["Jira ABC-1 — Настроить обмен.md"]
    assert after["Jira ABC-2 — Сверить остатки.md"] != before["Jira ABC-2 — Сверить остатки.md"]  # прежний документ заменён
    assert after["Jira ABC — сводка проекта.md"] != before["Jira ABC — сводка проекта.md"]  # сводка пересобрана
    assert "| ABC-3 | Задача | Новая | не начата |" in _doc("Jira ABC — сводка проекта.md") and "Описание изменили." in _doc("Jira ABC-2 — Сверить остатки.md")
    text = client.get(f"/projects/{project_id}/jira", headers=_h(manager_token)).json()["links"][0]["status_text"]
    assert "в Jira задач 3, загружено 3: новых 1, обновлено 1, без изменений 1" in text and "не загружено 1" in text
    jira.detail_calls.clear()
    client.post(f"/projects/{project_id}/jira/sync", headers=_h(manager_token))
    assert jira.detail_calls == [] and len(_artifacts(client, manager_token, project_id)) == 5  # повтор ничего не плодит


def test_format_change_reloads_documents(client, jira, manager_token, monkeypatch):
    project_id = _import(client, manager_token)
    jira.detail_calls.clear()
    monkeypatch.setattr(jr, "FORMAT_VERSION", jr.FORMAT_VERSION + 1)
    client.post(f"/projects/{project_id}/jira/sync", headers=_h(manager_token))
    assert sorted(jira.detail_calls) == ["ABC-1", "ABC-2"]  # вид документа сменился — задачи перечитаны
    assert len(_artifacts(client, manager_token, project_id)) == 4  # содержимое то же — дубликатов нет


def test_failed_parse_is_reloaded_on_next_sync(client, jira, manager_token):
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact

    project_id = _import(client, manager_token)
    before = {a["original_filename"]: a["artifact_id"] for a in _artifacts(client, manager_token, project_id)}
    with SessionLocal() as db:  # разбор двух материалов упал (например, модель не загрузилась)
        for row in db.scalars(select(Artifact).where(Artifact.project_id == project_id)).all():
            if row.original_filename.startswith("Jira ABC-1") or "сводка" in row.original_filename:
                row.status = "error"
        db.commit()
    jira.detail_calls.clear()
    client.post(f"/projects/{project_id}/jira/sync", headers=_h(manager_token))
    after = {a["original_filename"]: (a["artifact_id"], a["status"]) for a in _artifacts(client, manager_token, project_id)}
    assert len(after) == len(before) and "ABC-1" in jira.detail_calls and "ABC-2" not in jira.detail_calls
    for name, (artifact_id, status) in after.items():
        failed = name.startswith("Jira ABC-1 —") or "сводка" in name
        assert status != "error" and (artifact_id != before[name]) == failed, name


def test_material_date_comes_from_jira_not_from_upload(client, jira, manager_token):
    project_id = _import(client, manager_token)
    rows = _artifacts(client, manager_token, project_id)
    assert rows and {a["material_date_source"] for a in rows} == {"jira"}, [(a["original_filename"], a["material_date_source"]) for a in rows]
    today = __import__("datetime").date.today().isoformat()
    assert all(a["material_date"] and a["material_date"] != today for a in rows)  # даты из Jira, не день загрузки
    client.post(f"/projects/{project_id}/jira/sync", headers=_h(manager_token))  # повторная проверка дат не сбивает
    again = {a["original_filename"]: a["material_date"] for a in _artifacts(client, manager_token, project_id)}
    assert again == {a["original_filename"]: a["material_date"] for a in rows}


def test_issue_course_is_listed_apart_from_full_history(client, jira, manager_token):
    _import(client, manager_token)
    text = _doc("Jira ABC-1 — Настроить обмен.md")
    assert "## Ход задачи: кто и когда менял статус, исполнителя, срок (2)" in text
    assert "- 2026-10-05 10:00, Тестовый Автор: статус — было «Новая», стало «В работе»" in text
    assert "- 2026-10-05 10:00, Тестовый Автор: исполнитель — было «пусто», стало «Тестовый Исполнитель»" in text
    assert text.index("## Ход задачи") < text.index("## История изменений")  # полная история остаётся ниже целиком


def test_document_under_processing_is_not_replaced(client, jira, manager_token, monkeypatch):
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.job import Job

    project_id = _import(client, manager_token)
    before = {a["original_filename"]: a["artifact_id"] for a in _artifacts(client, manager_token, project_id)}
    with SessionLocal() as db:  # воркер прямо сейчас разбирает документ задачи ABC-1
        pk = db.scalar(select(Artifact.id).where(Artifact.original_filename.like("Jira ABC-1 —%")))
        job = db.scalar(select(Job).where(Job.artifact_id == pk))
        job.status = "running"
        job_id = job.id
        db.commit()
    monkeypatch.setattr(jr, "FORMAT_VERSION", jr.FORMAT_VERSION + 1)
    jira.issues["ABC"]["ABC-1"]["fields"]["description"] = "Описание поменяли, пока шёл разбор."
    client.post(f"/projects/{project_id}/jira/sync", headers=_h(manager_token))
    after = {a["original_filename"]: a["artifact_id"] for a in _artifacts(client, manager_token, project_id)}
    assert after["Jira ABC-1 — Настроить обмен.md"] == before["Jira ABC-1 — Настроить обмен.md"]  # не тронут
    assert "отложено 1" in client.get(f"/projects/{project_id}/jira", headers=_h(manager_token)).json()["links"][0]["status_text"]
    with SessionLocal() as db:
        db.get(Job, job_id).status = "done"
        db.commit()
    client.post(f"/projects/{project_id}/jira/sync", headers=_h(manager_token))
    assert "Описание поменяли" in _doc("Jira ABC-1 — Настроить обмен.md")  # следующий проход обновил


def test_chat_gets_named_issue_document_itself(client, jira, manager_token):
    """Вопрос называет задачу по ключу — в контекст чата идёт её документ, а не похожие разделы других задач."""
    from app.app.db import SessionLocal
    from app.app.services.chat import jira_focus_block

    project_id = _import(client, manager_token)
    with SessionLocal() as db:
        block = jira_focus_block(db, project_id, "Кто комментировал abc-1 и сколько трудозатрат? А что с XYZ-9 и ГОСТ-2020?")
        assert block.startswith("Задачи Jira, названные в вопросе") and "[Jira ABC-1 — Настроить обмен.md]" in block
        assert "## Комментарии (" in block and "## Трудозатраты (" in block and "ABC-2" not in block.split("]")[0]
        assert jira_focus_block(db, project_id, "Что решили на встрече?") == ""
        assert jira_focus_block(db, project_id + 1000, "Что с ABC-1?") == ""  # чужой проект задачу не видит
        assert len(jira_focus_block(db, project_id, "ABC-1", limit_chars=300)) < 1500 and "(раздел сокращён" in jira_focus_block(db, project_id, "ABC-1", limit_chars=300)


def test_chat_always_gets_jira_project_overview(client, jira, manager_token):
    """Общий вопрос без ключа задачи («сколько тикетов создано в Jira?») — сводка проекта идёт помощнику всегда, без поиска."""
    from app.app.db import SessionLocal
    from app.app.services.chat import jira_overview_block

    project_id = _import(client, manager_token)
    with SessionLocal() as db:
        block = jira_overview_block(db, project_id)
        assert block.startswith("Сводка проекта в Jira") and "- Всего задач: 2" in block
        assert "| ABC-1 |" in block and "| ABC-2 |" in block and "| Автор | Создана |" in block
        assert jira_overview_block(db, project_id + 1000) == ""  # проект без Jira: блока нет
        short = jira_overview_block(db, project_id, limit_chars=100, floor=300)
        assert "(перечень сокращён" in short and len(short) < len(block)


def test_jira_documents_are_not_rewritten_by_model(client, jira, manager_token):
    from app.app.services import pipeline_documents

    _import(client, manager_token)
    for name in ("Jira ABC-1 — Настроить обмен.md", "Jira ABC — сводка проекта.md"):
        assert pipeline_documents._is_jira_document(_doc(name)), name  # конвейер берёт такой текст дословно, без очистки моделью
    assert not pipeline_documents._is_jira_document("Протокол встречи\nРешили перенести срок.")


def test_check_reports_completeness_and_detects_drift(client, jira, manager_token, user_token):
    project_id = _import(client, manager_token)
    report = client.get(f"/projects/{project_id}/jira/check", headers=_h(manager_token)).json()
    link = report["links"][0]
    assert report["ok"] is True and link["problems"] == [], link["problems"]
    assert link["who_when_checked"] >= 5 * 2 + 7 + 4 + 2 + 2  # автор, исполнитель, время у задач, комментариев, трудозатрат, вложений, истории
    assert (link["jira_issues"], link["portal_issues"], link["comments"], link["worklogs"], link["history"]) == (2, 2, 7, 4, 2)
    assert link["extra_field_names"] == ["Заказчик", "Оценка в баллах", "Спринт"] and link["omitted_field_names"] == ["Rank", "workratio"]
    assert link["extra_fields"] == 3 and link["jira_attachments"] == 2 and link["portal_files"] == 1 and len(link["not_loaded"]) == 1
    assert link["pipeline"] == {"uploaded": 4}
    assert client.get(f"/projects/{project_id}/jira/check", headers=_h(user_token)).status_code == 404  # чужой проект
    # задача изменилась в Jira, появилась новая, вложение пропало с диска
    changed = issue("ABC-2", "Сверить остатки", "2026-11-06T12:00:00.000+0300")
    changed["fields"]["description"] = "Новый текст."
    jira.put(changed)
    jira.put(issue("ABC-4", "Ещё задача", "2026-11-06T13:00:00.000+0300"))
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact

    with SessionLocal() as db:
        path = db.scalar(select(Artifact).where(Artifact.original_filename == "Jira ABC-1 — схема.txt")).file_path
    import os

    os.remove(path)
    problems = client.get(f"/projects/{project_id}/jira/check", headers=_h(manager_token)).json()["links"][0]["problems"]
    assert any(p.startswith("ABC-4: задачи нет в портале") for p in problems)
    assert any(p.startswith("ABC-2: документ в портале отличается от Jira") for p in problems)
    assert any("схема.txt» пропал с диска" in p for p in problems)


def test_missing_attachment_file_is_downloaded_again(client, jira, manager_token):
    """Карточка вложения есть, файла на диске нет: следующая проверка скачивает байты снова и не плодит вторую карточку."""
    project_id = _import(client, manager_token)
    jira.detail_calls.clear()
    from pathlib import Path

    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact

    with SessionLocal() as db:
        attachment = db.scalar(select(Artifact).where(Artifact.original_filename == "Jira ABC-1 — схема.txt"))
        issue_doc = db.scalar(select(Artifact).where(Artifact.original_filename == "Jira ABC-1 — Настроить обмен.md"))
        path, issue_public_id = attachment.file_path, issue_doc.artifact_id
    Path(path).unlink()
    assert client.post(f"/projects/{project_id}/jira/sync", headers=_h(manager_token)).status_code == 200
    assert jira.detail_calls == ["ABC-1"]  # задачу с пропавшим файлом перечитываем, остальные — нет
    names = [a["original_filename"] for a in _artifacts(client, manager_token, project_id)]
    assert names.count("Jira ABC-1 — схема.txt") == 1
    with SessionLocal() as db:
        attachment = db.scalar(select(Artifact).where(Artifact.original_filename == "Jira ABC-1 — схема.txt"))
        issue_doc = db.scalar(select(Artifact).where(Artifact.original_filename == "Jira ABC-1 — Настроить обмен.md"))
        assert Path(attachment.file_path).is_file()
        assert Path(attachment.file_path).read_bytes() == jira.files["501"]
        assert issue_doc.artifact_id == issue_public_id  # документ задачи не заменяли
    jira.detail_calls.clear()
    client.post(f"/projects/{project_id}/jira/sync", headers=_h(manager_token))
    assert jira.detail_calls == []


def test_limit_is_reported_not_silent(client, jira, manager_token, monkeypatch):
    monkeypatch.setenv("JIRA_IMPORT_MAX_ISSUES", "1")
    from app.app.config import get_settings

    get_settings.cache_clear()
    project_id = _import(client, manager_token)
    state = client.get(f"/projects/{project_id}/jira", headers=_h(manager_token)).json()["links"][0]
    assert state["issues"] == 1 and state["issues_total"] == 2 and "загружено 1 (ограничение JIRA_IMPORT_MAX_ISSUES=1)" in state["status_text"]
    assert "- Всего задач: 2 (в портал загружено 1)" in _doc("Jira ABC — сводка проекта.md")  # сводка знает обо всех задачах
    report = client.get(f"/projects/{project_id}/jira/check", headers=_h(manager_token)).json()
    assert report["ok"] is False and "загружены не все задачи: 1 из 2" in report["links"][0]["problems"][0]


def test_failed_download_is_retried_and_media_follows_setting(client, jira, manager_token, monkeypatch):
    jira.issues["ABC"]["ABC-1"]["fields"]["attachment"].append(jira.attachment("503", "запись.mp3"))
    jira.fail_download.add("501")
    monkeypatch.setenv("JIRA_IMPORT_MEDIA", "false")
    from app.app.config import get_settings

    get_settings.cache_clear()
    project_id = _import(client, manager_token)
    reasons = sorted(i["reason"] for i in client.get(f"/projects/{project_id}/jira", headers=_h(manager_token)).json()["links"][0]["not_loaded"])
    assert reasons[0].startswith("дамп.zip: вид файла") and reasons[1].startswith("запись.mp3: вид файла .mp3") and reasons[2].startswith("схема.txt: не скачалось")
    jira.fail_download.clear()
    jira.put({**jira.issues["ABC"]["ABC-1"], "fields": {**jira.issues["ABC"]["ABC-1"]["fields"], "updated": "2026-11-07T10:00:00.000+0300"}})
    client.post(f"/projects/{project_id}/jira/sync", headers=_h(manager_token))
    assert "Jira ABC-1 — схема.txt" in [a["original_filename"] for a in _artifacts(client, manager_token, project_id)]  # сбой сети — повтор


def test_access_follows_jira_visibility(client, jira, manager_token, user_token):
    client.post("/jira/connect", json={"token": "tok-manager"}, headers=_h(manager_token))
    started = client.post("/jira/import", json={"projects": [{"key": "ABC"}, {"key": "XYZ"}]}, headers=_h(manager_token)).json()["started"]
    abc, xyz = started[0]["project_id"], started[1]["project_id"]
    assert client.get("/projects", headers=_h(user_token)).json() == []  # без токена чужих проектов не видно
    assert client.get(f"/projects/{abc}", headers=_h(user_token)).status_code == 404
    client.post("/jira/connect", json={"token": "tok-user"}, headers=_h(user_token))
    assert [p["id"] for p in client.get("/projects", headers=_h(user_token)).json()] == [abc]  # ABC видит в Jira, XYZ — нет
    assert client.get(f"/projects/{xyz}/jira", headers=_h(user_token)).status_code == 404
    assert client.post("/jira/import", json={"projects": [{"key": "ABC"}]}, headers=_h(user_token)).status_code == 403
    assert client.post(f"/projects/{abc}/jira/sync", headers=_h(user_token)).status_code == 403
    jira.visible["tok-user"] = []  # в Jira доступ отозвали
    from app.app.config import get_settings

    ji.scheduled_pass(get_settings(), jira.factory)
    assert client.get("/projects", headers=_h(user_token)).json() == []
    jira.visible["tok-user"] = ["ABC"]
    client.get("/jira/projects", headers=_h(user_token))
    assert len(client.get("/projects", headers=_h(user_token)).json()) == 1
    client.delete("/jira/connect", headers=_h(user_token))
    assert client.get("/projects", headers=_h(user_token)).json() == []


def test_merge_into_existing_is_admin_only_and_key_is_unique(client, jira, admin_token, manager_token):
    existing = client.post("/projects", json={"name": "Уже есть", "internal": True}, headers=_h(admin_token)).json()["id"]
    client.post("/jira/connect", json={"token": "tok-manager"}, headers=_h(manager_token))
    r = client.post("/jira/import", json={"projects": [{"key": "XYZ", "project_id": existing}]}, headers=_h(manager_token))
    assert r.status_code == 400 and "администратор" in r.json()["detail"]
    r = client.post("/jira/import", json={"projects": [{"key": "OLD"}]}, headers=_h(manager_token))
    assert r.status_code == 400 and "не видит" in r.json()["detail"]
    client.post("/jira/connect", json={"token": "tok-admin"}, headers=_h(admin_token))
    r = client.post("/jira/import", json={"projects": [{"key": "XYZ", "project_id": existing}]}, headers=_h(admin_token))
    assert r.status_code == 200 and r.json()["started"][0]["project_id"] == existing
    assert sorted(a["original_filename"] for a in _artifacts(client, admin_token, existing)) == ["Jira XYZ — сводка проекта.md", "Jira XYZ-1 — Первая задача.md"]
    other = client.post("/projects", json={"name": "Другой", "internal": True}, headers=_h(admin_token)).json()["id"]
    assert client.put(f"/projects/{other}/jira/link", json={"jira_key": "XYZ"}, headers=_h(admin_token)).status_code == 409
    assert client.put(f"/projects/{other}/jira/link", json={"jira_key": "old"}, headers=_h(manager_token)).status_code == 403
    assert client.put(f"/projects/{other}/jira/link", json={"jira_key": "old"}, headers=_h(admin_token)).json()["jira_key"] == "OLD"
    assert existing in [p["id"] for p in client.get("/projects", headers=_h(manager_token)).json()]  # токен менеджера видит XYZ
    assert client.delete(f"/projects/{existing}/jira/link/XYZ", headers=_h(admin_token)).status_code == 204
    assert existing not in [p["id"] for p in client.get("/projects", headers=_h(manager_token)).json()]
    assert len(_artifacts(client, admin_token, existing)) == 2  # материалы остаются
    assert client.delete(f"/projects/{other}", headers=_h(admin_token)).status_code in (200, 204)  # удаление проекта с привязкой


def test_jira_hidden_without_server_address(client, manager_token, monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "")
    from app.app.config import get_settings

    get_settings.cache_clear()
    try:
        assert client.get("/jira/status", headers=_h(manager_token)).json()["available"] is False
        assert client.post("/jira/connect", json={"token": "x"}, headers=_h(manager_token)).status_code == 400
    finally:
        get_settings.cache_clear()


def test_render_helpers():
    assert jr.clean_text("a\r\nb [^f.xls] !i.png|thumbnail! [т|https://x] [~u]\r\n{code:sql}select 1;{code}\r\nh2. Итог\r\n# раз\r\n||a||b||") == (
        "a\nb (вложение: f.xls) (изображение: i.png) т (https://x) @u\n\n```\nselect 1;\n```\n\n**Итог**\n1. раз\n|a|b|")
    assert jr.flat({"value": "A", "child": {"value": "B"}}) == "A / B" and jr.flat([{"name": "x"}, "y", None]) == "x, y" and jr.flat(2.5) == "2.5"
    assert jr.duration(9000) == "2ч 30м" and jr.duration(0) == "" and jr.duration(1800) == "30м"
    assert jr._hours(120600, "4d 1h 30m") == "33ч 30м (в Jira записано «4d 1h 30m»: d — рабочий день, w — рабочая неделя, не календарные)"
    assert jr._hours(5400, "1h 30m") == "1ч 30м" and jr._hours(None, "2d") == "2d" and jr._hours(None, None) == ""
    assert ji.safe_filename('a/b:c*?"<>|d.') == "a b c d" and ji.jira_time("2026-11-02T10:00:00.000+0300").utcoffset().total_seconds() == 10800
    empty = jr.project_document({"key": "ABC", "name": "Пустой"}, [], "https://jira.example")
    assert jr.is_rendered(empty) and not jr.is_rendered("# [черновик] заметки\nтекст") and not jr.is_rendered("")
    assert "В проекте нет задач." in empty and "- Всего задач: 0" in empty
    assert "## Описание проекта" in empty and "В Jira описание проекта не задано." in empty
    filled = jr.project_document({"key": "ABC", "name": "Полный", "id": "7", "description": "",
                                  "roles_resolved": [{"name": "Users", "id": 3, "missing": True, "actors": []}]}, [], "https://jira.example")
    assert "- ID в Jira: 7" in filled and "- Users (id 3): состав Jira не отдала" in filled
    raw = issue("ABC-9", "Заголовок", "2026-11-02T10:00:00.000+0300")
    raw["changelog_complete"] = False
    raw["changelog"] = {"total": 5, "histories": [{"created": "2026-10-05T10:00:00.000+0300", "author": {"name": "a"}, "items": []}]}
    assert "(Jira отдала 1 записей из 5" in jr.issue_document(raw, "https://jira.example")
