"""Jira от начала до конца по настоящей сети: подставная Jira на HTTP-сокете, настоящий httpx и фоновый поток портала.

Проверяет то, что не видно через подставной транспорт: постраничное дочитывание комментариев и трудозатрат, скачивание
вложений потоком (байты совпадают), фоновую загрузку с опросом состояния, сверку и проход расписания.
"""

from __future__ import annotations

import time

import pytest
from sqlalchemy import select

from app.app.services import jira_import as ji
from app.app.tests.jira_fake import FakeJira, issue


@pytest.fixture()
def jira_e2e(monkeypatch):
    fake = FakeJira()
    server = fake.serve()
    monkeypatch.setenv("JIRA_BASE_URL", fake.base)
    monkeypatch.setenv("JIRA_EMAIL", "")
    monkeypatch.setenv("JIRA_IMPORT_MAX_ISSUES", "0")
    monkeypatch.setenv("JIRA_VERIFY_TLS", "true")
    from app.app.config import get_settings

    get_settings.cache_clear()
    yield fake
    server.shutdown()
    server.server_close()
    get_settings.cache_clear()


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _wait_link(client, token, project_id, timeout=40.0):
    """Ждём фоновую загрузку до idle/error: импорт и проверка обновлений идут в отдельном потоке."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        links = client.get(f"/projects/{project_id}/jira", headers=_h(token)).json()["links"]
        if links and links[0]["status"] in ("idle", "error"):
            return links[0]
        time.sleep(0.1)
    raise AssertionError("загрузка из Jira не завершилась за отведённое время")


def test_e2e_import_check_update_over_real_http(client, jira_e2e, manager_token):
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact

    r = client.post("/jira/connect", json={"token": "wrong-token"}, headers=_h(manager_token))
    assert r.status_code == 400 and "401" in r.json()["detail"]
    r = client.post("/jira/connect", json={"token": "tok-manager"}, headers=_h(manager_token))
    assert r.status_code == 200 and r.json()["projects"] == 2
    assert client.get("/jira/projects/ABC/count", headers=_h(manager_token)).json()["issues"] == 2
    r = client.post("/jira/import", json={"projects": [{"key": "ABC"}]}, headers=_h(manager_token))
    assert r.status_code == 200, r.text
    project_id = r.json()["started"][0]["project_id"]
    link = _wait_link(client, manager_token, project_id)
    assert link["status"] == "idle", link
    assert link["status_text"] == "в Jira задач 2, загружено 2: новых 2, обновлено 0, без изменений 0; файлов: новых 1, уже были 0, не загружено 1"

    with SessionLocal() as db:
        doc = db.scalar(select(Artifact).where(Artifact.original_filename == "Jira ABC-1 — Настроить обмен.md"))
        text = open(doc.file_path, encoding="utf-8").read()
        attachment = db.scalar(select(Artifact).where(Artifact.original_filename == "Jira ABC-1 — схема.txt"))
        assert open(attachment.file_path, "rb").read() == jira_e2e.files["501"]
    # Jira вложила в задачу только 2 комментария и 2 записи трудозатрат — остальное портал дочитал постранично
    assert "## Комментарии (6)" in text and "Комментарий 5:" in text and "КОНЕЦ-КОММЕНТАРИЯ" in text
    assert "## Трудозатраты (4, всего 4ч)" in text and "работа 4" in text
    assert f"Ссылка: {jira_e2e.base}/browse/ABC-1" in text

    report = client.get(f"/projects/{project_id}/jira/check", headers=_h(manager_token)).json()
    assert report["ok"] is True, report["links"][0]["problems"]
    assert report["links"][0]["comments"] == 7 and report["links"][0]["worklogs"] == 4

    changed = issue("ABC-2", "Сверить остатки", "2026-11-08T12:00:00.000+0300")
    changed["fields"]["description"] = "Текст после правки."
    jira_e2e.put(changed)
    assert client.get(f"/projects/{project_id}/jira/check", headers=_h(manager_token)).json()["ok"] is False  # портал отстал
    assert client.post(f"/projects/{project_id}/jira/sync", headers=_h(manager_token)).status_code == 200
    link = _wait_link(client, manager_token, project_id)
    assert link["status"] == "idle" and "обновлено 1" in link["status_text"], link
    assert client.get(f"/projects/{project_id}/jira/check", headers=_h(manager_token)).json()["ok"] is True  # снова сходится


def test_e2e_scheduled_pass_over_real_http(client, jira_e2e, manager_token):
    from app.app.config import get_settings

    client.post("/jira/connect", json={"token": "tok-manager"}, headers=_h(manager_token))
    project_id = client.post("/jira/import", json={"projects": [{"key": "XYZ"}]}, headers=_h(manager_token)).json()["started"][0]["project_id"]
    assert _wait_link(client, manager_token, project_id)["status"] == "idle"
    jira_e2e.put(issue("XYZ-2", "Появилась позже", "2026-11-09T09:00:00.000+0300"))
    done = ji.scheduled_pass(get_settings())  # настоящий транспорт, токен из базы
    assert done == {"accounts": 1, "links": 1}
    names = [a["original_filename"] for a in client.get(f"/projects/{project_id}/artifacts", headers=_h(manager_token)).json()]
    assert "Jira XYZ-2 — Появилась позже.md" in names
