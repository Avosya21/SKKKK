"""Сквозные пользовательские сценарии портала (API TestClient, DRY_RUN=1)."""

from __future__ import annotations

import io
from unittest.mock import patch


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_project(client, token: str, name: str = "Сценарий") -> int:
    r = client.post("/projects", json={"name": name}, headers=_auth(token))
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_s01_anonymous_home_redirects_to_login(anon_client):
    r = anon_client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in (r.headers.get("location") or "")


def test_s02_login_page_is_public(anon_client):
    r = anon_client.get("/login")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")


def test_s03_wrong_password_rejected(anon_client):
    r = anon_client.post(
        "/auth/login",
        json={"name": "Admin", "telegram_id": "tg-bad", "password": "wrong"},
    )
    assert r.status_code == 401


def test_s04_login_returns_token_and_cookie(anon_client):
    r = anon_client.post(
        "/auth/login",
        json={"name": "Admin", "telegram_id": "tg-ok", "password": "admin-pass"},
    )
    assert r.status_code == 200
    assert r.json()["access_token"]
    assert r.json()["role"] == "admin"
    assert r.cookies.get("skkkk_session")


def test_s05_logout_then_projects_401(cookie_auth_client):
    assert cookie_auth_client.get("/projects").status_code == 200
    cookie_auth_client.post("/auth/logout")
    r = cookie_auth_client.get("/projects")
    assert r.status_code == 401


def test_s06_user_cannot_create_project(client, user_token):
    r = client.post("/projects", json={"name": "Нет"}, headers=_auth(user_token))
    assert r.status_code == 403


def test_s07_user_does_not_see_manager_project(client, manager_token, user_token):
    pid = _create_project(client, manager_token, "Общий")
    r = client.get("/projects", headers=_auth(user_token))
    assert r.status_code == 200
    ids = [p["id"] for p in r.json()]
    assert pid not in ids


def test_s08_user_cannot_open_foreign_project_html(client, manager_token, user_token):
    pid = _create_project(client, manager_token, "Страница")
    r = client.get(f"/projects/{pid}", headers=_auth(user_token), follow_redirects=False)
    assert r.status_code == 404


def test_s09_anonymous_project_html_redirects(anon_client, client, manager_token):
    pid = _create_project(client, manager_token, "Секрет")
    client.cookies.clear()
    r = anon_client.get(f"/projects/{pid}", follow_redirects=False)
    assert r.status_code in {302, 401}


def test_s10_user_cannot_upload_to_foreign_project(client, manager_token, user_token):
    pid = _create_project(client, manager_token, "Заметки")
    r = client.post(
        f"/projects/{pid}/upload",
        headers=_auth(user_token),
        data={"source_type": "note"},
        files=[("files", ("hello.txt", io.BytesIO(b"hello world"), "text/plain"))],
    )
    assert r.status_code == 404
    listed = client.get(f"/projects/{pid}/artifacts", headers=_auth(user_token))
    assert listed.status_code == 404


def test_s10b_manager_upload_note_and_list_artifacts(client, manager_token):
    pid = _create_project(client, manager_token, "Заметки свои")
    r = client.post(
        f"/projects/{pid}/upload",
        headers=_auth(manager_token),
        data={"source_type": "note"},
        files=[("files", ("hello.txt", io.BytesIO(b"hello world"), "text/plain"))],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body[0]["status"] == "uploaded"
    listed = client.get(f"/projects/{pid}/artifacts", headers=_auth(manager_token))
    assert listed.status_code == 200
    assert listed.json()[0]["artifact_id"] == body[0]["artifact_id"]


def test_s11_dry_run_job_completes(client, manager_token):
    from app.app.workers.gpu_worker import run_once

    pid = _create_project(client, manager_token, "Очередь")
    client.post(
        f"/projects/{pid}/upload",
        headers=_auth(manager_token),
        data={"source_type": "note"},
        files=[("files", ("q.txt", io.BytesIO(b"queue"), "text/plain"))],
    )
    assert run_once()
    arts = client.get(f"/projects/{pid}/artifacts", headers=_auth(manager_token)).json()
    assert arts[0]["status"] == "done"


def test_s12_anonymous_audio_401(anon_client):
    assert anon_client.get("/artifacts/art_missing/audio").status_code == 401


def test_s13_anonymous_report_redirects(anon_client):
    r = anon_client.get("/artifacts/art_missing/report", follow_redirects=False)
    assert r.status_code == 302


def test_s14_anonymous_download_401(anon_client):
    assert anon_client.get("/artifacts/art_x/download").status_code == 401


def test_s15_anonymous_enroll_401(anon_client):
    r = anon_client.post(
        "/api/artifacts/art_missing/speaker-enroll",
        json={"diar_label": "speaker_0", "display_name": "Иван"},
    )
    assert r.status_code == 401


def test_s16_search_limit_over_max_422(client, manager_token, user_token):
    pid = _create_project(client, manager_token, "Поиск")
    r = client.get(
        f"/projects/{pid}/search",
        params={"q": "бюджет", "limit": 51},
        headers=_auth(manager_token),
    )
    assert r.status_code == 422


def test_s17_search_ok_empty_index(client, manager_token, user_token):
    pid = _create_project(client, manager_token, "Поиск2")
    with patch("app.app.routers.knowledge.embed_texts", return_value=[[0.1] * 1024]):
        r = client.get(
            f"/projects/{pid}/search",
            params={"q": "бюджет", "limit": 5},
            headers=_auth(manager_token),
        )
    assert r.status_code == 200, r.text
    assert r.json()["results"] == []


def test_s18_create_note(client, manager_token, user_token):
    pid = _create_project(client, manager_token, "Заметка")
    r = client.post(
        f"/projects/{pid}/notes",
        json={"text": "Нужно уточнить поставщика", "refs": []},
        headers=_auth(manager_token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["id"]


def test_s19_create_link_missing_entity_404(client, manager_token, user_token):
    pid = _create_project(client, manager_token, "Связи")
    r = client.post(
        "/links",
        json={
            "project_id": pid,
            "source_entity_id": 999999,
            "target_entity_id": 999998,
            "link_type": "RELATED",
            "description": "нет таких сущностей",
        },
        headers=_auth(manager_token),
    )
    assert r.status_code == 404


def test_s20_conflicts_admin_ok_user_403(client, manager_token, admin_token, user_token):
    pid = _create_project(client, manager_token, "Конфликты")
    ok = client.get(f"/projects/{pid}/conflicts", headers=_auth(admin_token))
    assert ok.status_code == 200
    denied = client.get(f"/projects/{pid}/conflicts", headers=_auth(user_token))
    assert denied.status_code == 403


def test_s21_mcp_info_manager_ok_user_403(client, manager_token, user_token):
    pid = _create_project(client, manager_token, "MCP")
    mgr = client.get(f"/projects/{pid}/mcp/info", headers=_auth(manager_token))
    assert mgr.status_code == 200, mgr.text
    assert "mcp_url" in mgr.json()
    usr = client.get(f"/projects/{pid}/mcp/info", headers=_auth(user_token))
    assert usr.status_code == 403


def test_s22_graph_data_empty(client, manager_token, user_token):
    pid = _create_project(client, manager_token, "Граф")
    r = client.get(f"/projects/{pid}/graph/data", headers=_auth(manager_token))
    assert r.status_code == 200
    data = r.json()
    assert "nodes" in data and "edges" in data


def test_s23_tz_content_empty_ok(client, manager_token, user_token):
    pid = _create_project(client, manager_token, "ТЗ")
    r = client.get(f"/projects/{pid}/tz/content", headers=_auth(manager_token))
    assert r.status_code in {200, 404}


def test_s24_deadlines_empty(client, manager_token, user_token):
    pid = _create_project(client, manager_token, "Сроки")
    r = client.get(f"/projects/{pid}/deadlines", headers=_auth(manager_token))
    assert r.status_code == 200


def test_s25_chat_without_index(client, manager_token, user_token):
    pid = _create_project(client, manager_token, "Чат")
    with patch("app.app.services.chat.embed_texts", return_value=[[0.1] * 1024]):
        with patch("app.app.services.chat.search_vectors", return_value=[]):
            r = client.post(
                "/chat",
                json={"project_id": pid, "question": "Кто поставщик серверов?"},
                headers=_auth(manager_token),
            )
    assert r.status_code == 200, r.text
    assert r.json().get("answer")


def test_s26_chat_answers_while_processing_runs(client, manager_token, user_token, monkeypatch):
    """Р2 (DECISIONS 2026-10-05): чат не блокируется расшифровкой — модель работает в два потока (LLM-LANES-PLAN)."""
    from app.app.db import SessionLocal
    from app.app.models.job import Job

    monkeypatch.setattr("app.app.llm_settings.uses_local_gemma", lambda *_a, **_k: True)
    pid = _create_project(client, manager_token, "GPU")
    up = client.post(
        f"/projects/{pid}/upload",
        headers=_auth(manager_token),
        data={"source_type": "note"},
        files=[("files", ("busy.txt", io.BytesIO(b"x"), "text/plain"))],
    )
    assert up.status_code == 200
    db = SessionLocal()
    try:
        job = db.query(Job).first()
        assert job is not None
        job.status = "running"
        db.commit()
    finally:
        db.close()
    r = client.post(
        "/chat",
        json={"project_id": pid, "question": "Что в стенограмме?"},
        headers=_auth(manager_token),
    )
    assert r.status_code == 200, r.text
    assert "GPU" not in (r.json().get("answer") or "")


def test_s27_health_public_no_secrets(anon_client):
    r = anon_client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data == {"status": "ok"} or data.get("status") == "ok"
    assert "queue" not in data
    assert "vram_mib" not in data


def test_s28_health_ready_needs_session(anon_client):
    assert anon_client.get("/health/ready").status_code == 401


def test_s29_health_ready_with_login(client, admin_token):
    r = client.get("/health/ready", headers=_auth(admin_token))
    assert r.status_code == 200
    assert "queue" in r.json()


def test_s31_user_can_delete_one_artifact(client, manager_token, user_token):
    pid = _create_project(client, manager_token, "Два файла")
    first = client.post(
        f"/projects/{pid}/upload",
        headers=_auth(manager_token),
        data={"source_type": "note"},
        files=[("files", ("a.txt", io.BytesIO(b"aaa"), "text/plain"))],
    ).json()[0]
    second = client.post(
        f"/projects/{pid}/upload",
        headers=_auth(manager_token),
        data={"source_type": "note"},
        files=[("files", ("b.txt", io.BytesIO(b"bbb"), "text/plain"))],
    ).json()[0]
    r = client.delete(
        f"/projects/{pid}/artifacts/{first['artifact_id']}",
        headers=_auth(manager_token),
    )
    assert r.status_code == 204
    listed = client.get(f"/projects/{pid}/artifacts", headers=_auth(manager_token)).json()
    ids = [row["artifact_id"] for row in listed]
    assert first["artifact_id"] not in ids
    assert second["artifact_id"] in ids

