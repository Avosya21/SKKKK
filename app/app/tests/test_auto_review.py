"""Этап 1 автопроверки (docs/plans/AUTO-REVIEW-PLAN.md): очередь, воркер, запись вердикта модели, отмена, режимы."""
from __future__ import annotations

import json
from types import SimpleNamespace

from app.app import db as db_mod
from app.app.config import get_settings
from app.app.services.queue import KIND_AUTO_REVIEW, claim_next_job, enqueue_auto_review


def _project_with_artifact(db, *, status="done"):
    from app.app.models.artifact import Artifact
    from app.app.models.project import Project
    from app.app.models.user import User

    user = User(name="rev", telegram_id="tg-rev", role="admin", password_hash="x")
    db.add(user)
    db.flush()
    project = Project(name="Rev", created_by=user.id, is_active=True)
    db.add(project)
    db.flush()
    art = Artifact(
        artifact_id="art_rev",
        project_id=project.id,
        user_id=user.id,
        original_filename="call.webm",
        stored_filename="call.webm",
        file_path="/x",
        file_type="audio",
        source_type="call",
        sha256="abc",
        status=status,
    )
    db.add(art)
    db.flush()
    return project, art


def test_transcription_is_claimed_before_review(db_engine):
    from app.app.models.job import Job

    db = db_mod.SessionLocal()
    try:
        _project, art = _project_with_artifact(db)
        review = enqueue_auto_review(db, art.id, scope="artifact", mode="shadow")
        db.add(Job(artifact_id=art.id, status="queued", phase="asr"))  # поставлен позже, но важнее
        db.commit()
        first = claim_next_job(db_mod.SessionLocal(), 3)
        assert first.kind == "process_artifact"
        second = claim_next_job(db_mod.SessionLocal(), 3)
        assert second.id == review.id and second.kind == KIND_AUTO_REVIEW
    finally:
        db.close()


def test_enqueue_review_skips_duplicate(db_engine):
    db = db_mod.SessionLocal()
    try:
        _project, art = _project_with_artifact(db)
        assert enqueue_auto_review(db, art.id, scope="artifact", mode="shadow") is not None
        assert enqueue_auto_review(db, art.id, scope="artifact", mode="shadow") is None
        assert enqueue_auto_review(db, art.id, scope="conflicts", mode="shadow") is not None
    finally:
        db.close()


def test_worker_runs_review_without_touching_artifact(db_engine, monkeypatch):
    from app.app.models.artifact import Artifact
    from app.app.models.job import Job
    from app.app.workers import review as review_mod
    from app.app.workers.gpu_worker import run_once

    calls = []
    monkeypatch.setattr(get_settings(), "dry_run", False)
    monkeypatch.setattr(review_mod, "process_review", lambda _db, _s, job, art: calls.append((job.kind, art.artifact_id)))
    db = db_mod.SessionLocal()
    try:
        _project, art = _project_with_artifact(db)
        job = enqueue_auto_review(db, art.id, scope="artifact", mode="shadow")
        art_pk, job_pk = art.id, job.id
    finally:
        db.close()
    assert run_once()
    db = db_mod.SessionLocal()
    try:
        assert calls == [(KIND_AUTO_REVIEW, "art_rev")]
        assert db.get(Job, job_pk).status == "done"
        assert db.get(Artifact, art_pk).status == "done"
    finally:
        db.close()


def test_review_failure_does_not_break_artifact(db_engine, monkeypatch):
    from app.app.models.artifact import Artifact
    from app.app.models.job import Job
    from app.app.workers import review as review_mod
    from app.app.workers.gpu_worker import run_once

    def boom(*_a, **_k):
        raise RuntimeError("model down")

    monkeypatch.setattr(get_settings(), "dry_run", False)
    monkeypatch.setattr(review_mod, "process_review", boom)
    db = db_mod.SessionLocal()
    try:
        _project, art = _project_with_artifact(db)
        job = enqueue_auto_review(db, art.id, scope="artifact", mode="shadow")
        art_pk, job_pk = art.id, job.id
    finally:
        db.close()
    assert run_once()
    db = db_mod.SessionLocal()
    try:
        failed = db.get(Job, job_pk)
        assert failed.status == "error" and "model down" in failed.error_message
        art = db.get(Artifact, art_pk)
        assert art.status == "done" and not art.error_message
    finally:
        db.close()


def test_artifact_deleted_during_job_does_not_crash_worker(db_engine, monkeypatch):
    """Встречу удалили, пока воркер её обрабатывал (пр.40, задача 453): run_once не падает,
    а номер задачи, который SQLite уже отдал новой задаче другой встречи, воркер не трогает."""
    from sqlalchemy import delete

    from app.app.models.artifact import Artifact
    from app.app.models.job import Job
    from app.app.workers import review as review_mod
    from app.app.workers.gpu_worker import run_once

    def delete_artifact(db, _settings, job, artifact):
        # Как удаление из портала: строки убирает другой процесс, объекты в сессии воркера просто устаревают.
        job_pk = job.id
        db.execute(delete(Job).where(Job.artifact_id == artifact.id).execution_options(synchronize_session=False))
        db.execute(delete(Artifact).where(Artifact.id == artifact.id).execution_options(synchronize_session=False))
        db.add(Job(id=job_pk, artifact_id=other_pk, kind="refresh_knowledge", status="queued"))
        db.commit()

    monkeypatch.setattr(get_settings(), "dry_run", False)
    monkeypatch.setattr(review_mod, "process_review", delete_artifact)
    db = db_mod.SessionLocal()
    try:
        project, art = _project_with_artifact(db)
        other = Artifact(
            artifact_id="art_other", project_id=project.id, user_id=art.user_id, original_filename="b.webm",
            stored_filename="b.webm", file_path="/y", file_type="audio", source_type="call", sha256="def", status="done",
        )
        db.add(other)
        db.flush()
        other_pk = other.id
        job_pk = enqueue_auto_review(db, art.id, scope="artifact", mode="shadow").id
    finally:
        db.close()
    assert run_once()
    db = db_mod.SessionLocal()
    try:
        reused = db.get(Job, job_pk)
        assert reused.artifact_id == other_pk and reused.status == "queued" and not reused.error_message
    finally:
        db.close()


def test_unknown_job_kind_never_runs_transcription(db_engine, monkeypatch):
    from app.app.models.job import Job
    from app.app.workers import pipeline as pipeline_mod
    from app.app.workers.gpu_worker import run_once

    monkeypatch.setattr(get_settings(), "dry_run", False)
    monkeypatch.setattr(pipeline_mod, "process_artifact", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("не должно")))
    db = db_mod.SessionLocal()
    try:
        _project, art = _project_with_artifact(db)
        job = Job(artifact_id=art.id, kind="something_new", status="queued")
        db.add(job)
        db.commit()
        job_pk = job.id
    finally:
        db.close()
    assert run_once()
    db = db_mod.SessionLocal()
    try:
        assert db.get(Job, job_pk).status == "error"
    finally:
        db.close()


def test_process_review_without_reviewers_is_noop(db_engine):
    from app.app.workers.review import REVIEWERS, process_review

    db = db_mod.SessionLocal()
    try:
        _project, art = _project_with_artifact(db)
        job = enqueue_auto_review(db, art.id, scope="conflicts", mode="shadow")
        saved = list(REVIEWERS["conflicts"])
        REVIEWERS["conflicts"].clear()
        try:
            assert process_review(db, get_settings(), job, art)["skipped"] == "no reviewers"
        finally:
            REVIEWERS["conflicts"][:] = saved
    finally:
        db.close()


def _entity(value, *, ent_id=7, active=True):
    from app.app.models.entity import Entity

    return Entity(id=ent_id, artifact_id=1, entity_type="decision", value_json=json.dumps(value, ensure_ascii=False), is_active=active, needs_review=True)


def test_shadow_verdict_has_no_effect():
    from app.app.services.auto_review import record_model_review

    ent = _entity({"text": "Решили X", "speaker": "Анна"})
    review = record_model_review(ent, get_settings(), verdict="reject", reason="не решение", confidence=0.9, mode="shadow", model="qwen", prompt_version="req-v1")
    value = json.loads(ent.value_json)
    assert ent.is_active is True and ent.needs_review is True
    assert value["model_review"]["verdict"] == "reject" and value["speaker"] == "Анна"
    assert review["mode"] == "shadow" and "deactivated" not in review


def test_live_reject_and_undo():
    from app.app.services.auto_review import record_model_review, undo_model_review

    ent = _entity({"text": "шум"})
    record_model_review(ent, get_settings(), verdict="reject", reason="шум", confidence=0.95, mode="live", model="qwen", prompt_version="req-v1")
    assert ent.is_active is False
    undo_model_review(ent, user_name="Admin", user_id=1)
    value = json.loads(ent.value_json)
    assert ent.is_active is True
    assert value["model_review"]["undone"]["by"] == "Admin"
    assert value["human_review"]["action"] == "undo_model"


def test_human_decision_wins_over_model():
    from app.app.services.auto_review import record_model_review

    ent = _entity({"text": "Решили X", "human_review": {"by": "Admin", "action": "confirm"}})
    assert record_model_review(ent, get_settings(), verdict="reject", reason="?", confidence=1.0, mode="live", model="qwen", prompt_version="req-v1") is None
    assert ent.is_active is True and "model_review" not in json.loads(ent.value_json)


def test_audit_sample_and_modes(monkeypatch):
    from app.app.services.auto_review import is_audit, review_mode

    settings = get_settings()
    monkeypatch.setattr(settings, "auto_review_audit_every", 20)
    assert is_audit(40, settings) and not is_audit(41, settings)
    monkeypatch.setattr(settings, "auto_review_mode", "shadow")
    assert review_mode(settings) == "shadow"
    project = SimpleNamespace(settings=lambda: {"auto_review_mode": "live"})
    assert review_mode(settings, project) == "live"
    assert review_mode(settings, SimpleNamespace(settings=lambda: {"auto_review_mode": "bogus"})) == "off"


def test_model_review_is_provenance():
    from app.app.routers.knowledge import _keep_provenance
    from app.app.services.graph import entity_text

    value = {"text": "Решили X", "model_review": {"verdict": "confirm", "reason": "есть в реплике"}}
    assert entity_text(json.dumps(value, ensure_ascii=False)) == "Решили X"
    merged = json.loads(_keep_provenance(json.dumps(value, ensure_ascii=False), json.dumps({"text": "Решили Y"})))
    assert merged["model_review"]["verdict"] == "confirm"


def _worker_marker(tmp_path, *, with_kinds: bool):
    (tmp_path / "locks").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "locks" / "gpu_worker.pid").write_text("4242", encoding="utf-8")
    kinds = " kinds=process_artifact,auto_review,refresh_knowledge" if with_kinds else ""
    (tmp_path / "logs" / "worker.log").write_text(
        "10:00:00 INFO    primaryanalitics.worker     worker.started           pid=1111 dry_run=False kinds=process_artifact,auto_review\n"
        f"11:00:00 INFO    primaryanalitics.worker     worker.started           pid=4242 dry_run=False{kinds}\n",
        encoding="utf-8",
    )


def test_running_worker_kinds_detects_old_worker(tmp_path):
    from app.app.services.queue import running_worker_kinds

    _worker_marker(tmp_path, with_kinds=False)
    assert running_worker_kinds(tmp_path) == {"process_artifact"}  # старый воркер: только расшифровка
    _worker_marker(tmp_path, with_kinds=True)
    assert "auto_review" in running_worker_kinds(tmp_path)
    assert "auto_review" in running_worker_kinds(tmp_path / "nothing")  # воркер не запущен — новый поймёт


def test_backlog_refuses_old_worker_and_queues_with_new(db_engine, tmp_path, monkeypatch):
    from app.app.models.job import Job
    from app.app.services.auto_review import BacklogError, queue_backlog

    settings = get_settings()
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "auto_review_mode", "shadow")
    db = db_mod.SessionLocal()
    try:
        project, art = _project_with_artifact(db)
        db.commit()
        _worker_marker(tmp_path, with_kinds=False)
        import pytest

        with pytest.raises(BacklogError):
            queue_backlog(db, settings, project.id)
        assert db.query(Job).count() == 0
        _worker_marker(tmp_path, with_kinds=True)
        result = queue_backlog(db, settings, project.id)
        assert result["jobs_queued"] == 2 and result["mode"] == "shadow"
        assert queue_backlog(db, settings, project.id)["jobs_queued"] == 0  # без дублей
    finally:
        db.close()


def test_live_promotes_shadow_without_llm(client, admin_token, tmp_data, monkeypatch):
    from app.app.models.entity import Entity
    from app.app.services.review_requirements import PROMPT_VERSION, review_artifact_requirements

    def no_llm(*_a, **_k):
        raise AssertionError("модель звать не нужно: теневой вердикт уже есть")

    monkeypatch.setattr("app.app.services.llm.chat_completion_no_think", no_llm)
    monkeypatch.setattr("app.app.services.gpu_phase.prepare_for_llm", lambda *_a, **_k: None)
    db = db_mod.SessionLocal()
    try:
        _project, art = _project_with_artifact(db)
        shadow = {"verdict": "reject", "reason": "шум", "confidence": 0.95, "mode": "shadow", "model": "qwen",
                  "prompt_version": PROMPT_VERSION, "at": "2026-09-24T00:00:00+00:00", "reject_kind": "noise"}
        ent = Entity(artifact_id=art.id, entity_type="decision", value_json=json.dumps({"text": "шум", "model_review": shadow}, ensure_ascii=False),
                     is_active=True, needs_review=True)
        db.add(ent)
        db.commit()
        stats = review_artifact_requirements(db, get_settings(), art, "live")
        db.commit()
        db.refresh(ent)
        assert stats["promoted"] == 1 and stats["llm_calls"] == 0
        review = json.loads(ent.value_json)["model_review"]
        assert ent.is_active is False and review["mode"] == "live" and review["promoted_from_shadow"]
    finally:
        db.close()


def test_chat_not_blocked_by_background_review(client, admin_token, monkeypatch):
    from app.app.models.job import Job

    monkeypatch.setattr("app.app.llm_settings.uses_local_gemma", lambda _p: True)
    pid = client.post("/projects", json={"name": "ChatRev"}, headers={"Authorization": f"Bearer {admin_token}"}).json()["id"]
    db = db_mod.SessionLocal()
    try:
        from app.app.models.artifact import Artifact
        from app.app.models.user import User

        user = db.query(User).first()
        art = Artifact(artifact_id="art_chat_rev", project_id=pid, user_id=user.id, original_filename="a.webm", stored_filename="a.webm",
                       file_path="/x", file_type="audio", source_type="call", sha256="z", status="done")
        db.add(art)
        db.flush()
        db.add(Job(artifact_id=art.id, kind=KIND_AUTO_REVIEW, status="running", phase="review"))
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr("app.app.routers.knowledge.answer_chat", lambda *_a, **_k: {"answer": "ok", "sources": []})
    r = client.post("/chat", json={"project_id": pid, "question": "кто решил?"}, headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200, r.text
