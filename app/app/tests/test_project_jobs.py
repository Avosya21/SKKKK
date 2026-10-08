"""Фоновая работа по проекту: ход, перепроверка, переизвлечение, повтор, остановка (PLATFORM-FOUNDATION-PLAN, ПФ9)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from app.app.services import project_jobs as pj


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def db(db_engine):
    import app.app.db as db_mod

    session = db_mod.SessionLocal()
    yield session
    session.close()


@pytest.fixture()
def setup(db):
    """Проект с тремя готовыми материалами и одним файлом обмена (материалом не считается)."""
    from app.app.models.artifact import Artifact
    from app.app.models.project import Project
    from app.app.models.user import User

    user = User(name="jobs-user", telegram_id="tg-jobs", role="admin", password_hash="x")
    db.add(user)
    db.commit()
    project = Project(name="Фоновая работа", created_by=user.id, is_active=True)
    db.add(project)
    db.commit()
    arts = []
    for i, name in enumerate(["созвон.webm", "протокол.docx", "переписка.html", "analysis_reply_7.json"]):
        is_call = name.endswith("webm")
        art = Artifact(artifact_id=f"art_jobs_{i}", project_id=project.id, user_id=user.id, original_filename=name, stored_filename=f"{i}",
                       file_path="/x", file_type="audio" if is_call else "document", source_type="call" if is_call else "document",
                       sha256=f"jobs{i}", status="done")
        db.add(art)
        arts.append(art)
    db.commit()
    return project.id, [a.id for a in arts]


def _job(db, art_id, scope, status, *, started=None, finished=None, error=None, created=None):
    from app.app.models.job import Job

    job = Job(artifact_id=art_id, kind="auto_review", status=status, phase="review", payload_json=json.dumps({"scope": scope, "mode": "shadow"}, sort_keys=True),
              started_at=started, finished_at=finished, error_message=error)
    if created is not None:
        job.created_at = created
    db.add(job)
    db.commit()
    return job


def _settings():
    from app.app.config import get_settings

    return get_settings().model_copy(update={"auto_review_mode": "shadow"})


def test_progress_idle_then_active_with_stages_errors_and_estimate(db, setup):
    pid, ids = setup
    idle = pj.progress(db, pid)
    assert idle["active"] is False and idle["stages"] == [] and idle["percent"] is None and idle["current"] is None
    now = datetime.utcnow()
    _job(db, ids[0], "reextract", "done", started=now - timedelta(seconds=200), finished=now - timedelta(seconds=100))
    _job(db, ids[1], "reextract", "running", started=now - timedelta(seconds=30))
    _job(db, ids[2], "reextract", "queued")
    _job(db, ids[0], "artifact", "error", started=now - timedelta(seconds=60), finished=now - timedelta(seconds=50), error="модель недоступна")
    _job(db, ids[0], "summary", "queued")
    got = pj.progress(db, pid)
    assert got["active"] and got["pending"] == 3 and got["finished_24h"] == 2 and got["percent"] == 40
    stages = {s["scope"]: s for s in got["stages"]}
    assert (stages["reextract"]["done"], stages["reextract"]["running"], stages["reextract"]["queued"]) == (1, 1, 1)
    assert stages["artifact"]["error"] == 1 and stages["summary"]["queued"] == 1
    assert [s["scope"] for s in got["stages"]] == ["reextract", "artifact", "summary"]  # порядок конвейера, пустые этапы не показываются
    assert got["current"]["stage"] == "Извлечение требований" and got["current"]["material"] == "протокол.docx"
    assert got["errors_total"] == 1 and got["errors"][0]["message"] == "модель недоступна" and got["errors"][0]["stage"] == "Проверка требований моделью"
    assert got["estimate_minutes"] >= 1  # по средней длительности завершённых задач и ожидающим


def test_progress_shows_every_queue_kind_not_only_auto_review(db, setup):
    """Расшифровка, обновление индекса и кнопки-пересборки тоже видны (раньше карточка их не показывала, 2026-10-08)."""
    from app.app.models.job import Job

    pid, ids = setup
    now = datetime.utcnow()
    db.add_all([
        Job(artifact_id=ids[0], kind="process_artifact", status="running", phase="asr", started_at=now),
        Job(artifact_id=ids[1], kind="refresh_knowledge", status="queued", phase="refresh"),
        Job(artifact_id=ids[2], kind="project_task", status="error", phase="project", error_message="сбой",
            payload_json=json.dumps({"task": "entities_reextract", "project_id": pid}), finished_at=now),
        Job(artifact_id=ids[2], kind="project_task", status="queued", phase="project", payload_json=json.dumps({"task": "new_button", "project_id": pid})),
    ])
    db.commit()
    got = pj.progress(db, pid)
    labels = [s["label"] for s in got["stages"]]
    assert labels == ["Расшифровка и разбор материала", "Обновление индекса и выжимки созвона", "Переизвлечение задач и сроков", "task:new_button"]
    assert got["current"]["stage"] == "Расшифровка и разбор материала" and got["current"]["material"] == "созвон.webm"
    assert got["errors"][0]["material"] == "весь проект" and got["errors"][0]["retry"] is True
    assert got["pending"] == 3 and got["cancellable"] == 2  # расшифровку из очереди не убирают


def test_transcription_is_neither_cancelled_nor_retried(db, setup):
    from app.app.models.job import Job

    pid, ids = setup
    waiting = Job(artifact_id=ids[0], kind="process_artifact", status="queued", phase="asr")
    failed = Job(artifact_id=ids[1], kind="process_artifact", status="error", phase="asr", error_message="сбой", finished_at=datetime.utcnow())
    db.add_all([waiting, failed])
    db.commit()
    assert pj.progress(db, pid)["errors"][0]["retry"] is False and pj.progress(db, pid)["retryable"] == 0
    assert pj.cancel_pending(db, pid) == 0 and pj.retry_errors(db, pid) == 0
    db.expire_all()
    assert db.get(Job, waiting.id).status == "queued" and db.get(Job, failed.id).status == "error"


def test_old_finished_jobs_are_not_counted(db, setup):
    pid, ids = setup
    old = datetime.utcnow() - timedelta(days=3)
    _job(db, ids[0], "reextract", "done", started=old, finished=old, created=old)
    assert pj.progress(db, pid)["stages"] == []


def test_retry_errors_requeues_only_failed(db, setup):
    from app.app.models.job import Job

    pid, ids = setup
    failed = _job(db, ids[0], "artifact", "error", error="сбой")
    failed.attempts = 3
    db.commit()
    done = _job(db, ids[1], "artifact", "done")
    assert pj.retry_errors(db, pid) == 1
    db.expire_all()
    again = db.get(Job, failed.id)
    assert (again.status, again.attempts, again.error_message) == ("queued", 0, None) and db.get(Job, done.id).status == "done"
    assert pj.retry_errors(db, pid) == 0


def test_cancel_removes_only_waiting_jobs(db, setup):
    from app.app.models.job import Job

    pid, ids = setup
    waiting_id = _job(db, ids[0], "artifact", "queued").id
    running_id = _job(db, ids[1], "artifact", "running", started=datetime.utcnow()).id
    assert pj.cancel_pending(db, pid) == 1
    db.expire_all()
    assert db.get(Job, waiting_id) is None and db.get(Job, running_id) is not None


def test_queue_review_enqueues_every_check_once_and_one_summary(db, setup):
    pid, ids = setup
    first = pj.queue_review(db, _settings(), pid)
    assert first["materials"] == 3 and first["jobs_queued"] == 3 * 4 + 1  # файл обмена не считается; одна выжимка в конце
    assert pj.queue_review(db, _settings(), pid)["jobs_queued"] == 0  # повторный запуск не плодит дубли
    stages = {s["scope"]: s["queued"] for s in pj.progress(db, pid)["stages"]}
    assert stages == {"artifact": 3, "topics": 3, "relevance": 3, "conflicts": 3, "summary": 1}


def test_queue_review_selected_only_and_unknown_selection(db, setup):
    pid, ids = setup
    only = pj.queue_review(db, _settings(), pid, artifact_ids=[ids[1]])
    assert only["materials"] == 1 and only["jobs_queued"] == 5
    with pytest.raises(pj.JobsError):
        pj.queue_review(db, _settings(), pid, artifact_ids=[999999])


def test_queue_reextract_selected_keeps_project_topics(db, setup):
    from app.app.models.entity import Entity, Topic

    pid, ids = setup
    topic = Topic(project_id=pid, label="Общая тема")
    db.add(topic)
    db.commit()
    ent = Entity(artifact_id=ids[0], entity_type="decision", value_json='{"text": "x"}', confidence=0.6, is_active=True, needs_review=True, topic_id=topic.id)
    other = Entity(artifact_id=ids[1], entity_type="decision", value_json='{"text": "y"}', confidence=0.6, is_active=True, needs_review=True, topic_id=topic.id)
    db.add_all([ent, other])
    db.commit()
    got = pj.queue_reextract(db, _settings(), pid, artifact_ids=[ids[0]])
    assert got["jobs_queued"] == 1
    db.expire_all()
    assert db.get(Entity, ent.id).topic_id is None and db.get(Entity, other.id).topic_id == topic.id  # у выбранного сброшена тема, у остальных нет
    assert db.get(Topic, topic.id) is not None  # общие темы проекта не стёрты
    assert [s["scope"] for s in pj.progress(db, pid)["stages"]] == ["reextract"]


def test_review_refused_when_auto_review_is_off(db, setup):
    pid, _ = setup
    off = _settings().model_copy(update={"auto_review_mode": "off"})
    with pytest.raises(pj.JobsError, match="выключена"):
        pj.queue_review(db, off, pid)


# ---------------------------------------------------------------- API

def _user_id(telegram_id):
    from app.app.db import SessionLocal
    from app.app.models.user import User

    db = SessionLocal()
    try:
        return db.query(User).filter(User.telegram_id == telegram_id).one().id
    finally:
        db.close()


@pytest.fixture()
def api_project(client, admin_token, manager_token, user_token, monkeypatch):
    from app.app.routers import project_background

    monkeypatch.setattr(project_background, "running_worker_kinds", lambda _d: {"auto_review"})
    pid = client.post("/projects", json={"name": "API фона"}, headers=_auth(manager_token)).json()["id"]
    client.post(f"/projects/{pid}/members", json={"user_id": _user_id("tg-user"), "role": "user"}, headers=_auth(manager_token))
    return pid


def _add_material(pid):
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.user import User

    db = SessionLocal()
    try:
        user = db.query(User).first()
        art = Artifact(artifact_id="art_bg_api", project_id=pid, user_id=user.id, original_filename="заметка.docx", stored_filename="n",
                       file_path="/x", file_type="document", source_type="document", sha256="bgapi", status="done")
        db.add(art)
        db.commit()
        return art.id
    finally:
        db.close()


def test_api_view_for_staff_run_for_admin_only(client, admin_token, manager_token, user_token, api_project):
    pid = api_project
    art_id = _add_material(pid)
    seen = client.get(f"/projects/{pid}/background", headers=_auth(manager_token))
    assert seen.status_code == 200
    body = seen.json()
    assert body["active"] is False and body["can_run"] is False and body["worker_ready"] is True
    assert body["materials"] == [{"id": art_id, "name": "заметка.docx", "kind": "документ"}]
    assert client.get(f"/projects/{pid}/background", headers=_auth(admin_token)).json()["can_run"] is True
    assert client.get(f"/projects/{pid}/background", headers=_auth(user_token)).status_code in (403, 404)
    for path in ("review", "reextract", "retry", "cancel"):
        assert client.post(f"/projects/{pid}/background/{path}", json={"artifact_ids": []}, headers=_auth(manager_token)).status_code == 403
        assert client.post(f"/projects/{pid}/background/{path}", json={"artifact_ids": []}, headers=_auth(user_token)).status_code in (403, 404)


def test_api_admin_runs_review_and_sees_progress(client, admin_token, api_project):
    pid = api_project
    art_id = _add_material(pid)
    h = _auth(admin_token)
    started = client.post(f"/projects/{pid}/background/review", json={"artifact_ids": [art_id]}, headers=h)
    assert started.status_code == 200 and started.json()["jobs_queued"] == 5
    prog = client.get(f"/projects/{pid}/background", headers=h).json()
    assert prog["active"] and prog["pending"] == 5 and {s["scope"] for s in prog["stages"]} == {"artifact", "topics", "relevance", "conflicts", "summary"}
    assert client.post(f"/projects/{pid}/background/cancel", headers=h).json()["cancelled"] == 5
    assert client.get(f"/projects/{pid}/background", headers=h).json()["active"] is False
    assert client.post(f"/projects/{pid}/background/review", json={"artifact_ids": [987654]}, headers=h).status_code == 400


def test_api_refuses_to_start_without_worker(client, admin_token, api_project, monkeypatch):
    from app.app.routers import project_background

    monkeypatch.setattr(project_background, "running_worker_kinds", lambda _d: set())
    pid = api_project
    _add_material(pid)
    r = client.post(f"/projects/{pid}/background/review", json={}, headers=_auth(admin_token))
    assert r.status_code == 400 and "Воркер" in r.json()["detail"]
    assert client.get(f"/projects/{pid}/background", headers=_auth(admin_token)).json()["worker_ready"] is False


def test_project_page_has_background_card_for_staff_only(client, manager_token, user_token, api_project):
    pid = api_project
    assert "Фоновая работа" in client.get(f"/projects/{pid}", headers=_auth(manager_token)).text
    page = client.get(f"/projects/{pid}", headers=_auth(user_token)).text
    assert "Фоновая работа" not in page and "/static/background.js" not in page
