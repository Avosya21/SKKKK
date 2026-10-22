from __future__ import annotations

import io

from app.app.config import get_settings
from app.app.models.artifact import Artifact
from app.app.models.entity import Chunk
from app.app.models.job import Job
from app.app.services.storage import artifact_dir, parsed_dir, project_root, raw_input_dir


def _project(client, manager_token, name="Purge art"):
    return client.post(
        "/projects",
        json={"name": name},
        headers={"Authorization": f"Bearer {manager_token}"},
    ).json()["id"]


def _upload(client, token, pid: int, filename: str, payload: bytes):
    r = client.post(
        f"/projects/{pid}/upload",
        headers={"Authorization": f"Bearer {token}"},
        data={"source_type": "note"},
        files=[("files", (filename, io.BytesIO(payload), "text/plain"))],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body) == 1
    return body[0]


def test_delete_artifact_requires_auth(anon_client):
    r = anon_client.delete("/projects/1/artifacts/art_missing")
    assert r.status_code == 401


def test_delete_artifact_unknown_404(client, manager_token, user_token):
    pid = _project(client, manager_token)
    r = client.delete(
        f"/projects/{pid}/artifacts/art_missing",
        headers={"Authorization": f"Bearer {manager_token}"},
    )
    assert r.status_code == 404


def test_delete_artifact_wrong_project_404(client, manager_token, user_token, tmp_data):
    pid_a = _project(client, manager_token, "A")
    pid_b = _project(client, manager_token, "B")
    art = _upload(client, manager_token, pid_a, "keep.txt", b"keep-a")
    r = client.delete(
        f"/projects/{pid_b}/artifacts/{art['artifact_id']}",
        headers={"Authorization": f"Bearer {manager_token}"},
    )
    assert r.status_code == 404
    listed = client.get(
        f"/projects/{pid_a}/artifacts",
        headers={"Authorization": f"Bearer {manager_token}"},
    ).json()
    assert [row["artifact_id"] for row in listed] == [art["artifact_id"]]


def test_user_can_purge_one_artifact_and_keep_the_other(client, manager_token, user_token, tmp_data):
    pid = _project(client, manager_token)
    gone = _upload(client, manager_token, pid, "gone.txt", b"remove-me")
    keep = _upload(client, manager_token, pid, "keep.txt", b"stay-here")
    settings = get_settings()
    gone_id = gone["artifact_id"]
    keep_id = keep["artifact_id"]

    from app.app.db import SessionLocal
    from sqlalchemy import select

    db = SessionLocal()
    try:
        gone_row = db.scalar(select(Artifact).where(Artifact.artifact_id == gone_id))
        keep_row = db.scalar(select(Artifact).where(Artifact.artifact_id == keep_id))
        assert gone_row is not None and keep_row is not None
        gone_pk = gone_row.id
        gone_stored = gone_row.stored_filename
        keep_stored = keep_row.stored_filename
        db.add(
            Chunk(
                artifact_id=gone_pk,
                project_id=pid,
                chunk_index=0,
                text="from gone",
                token_estimate=2,
            )
        )
        db.commit()
    finally:
        db.close()

    ws = artifact_dir(settings, pid, gone_id)
    (ws / "reports").mkdir(parents=True, exist_ok=True)
    (ws / "reports" / "transcript.html").write_text("html", encoding="utf-8")
    tmp_ws = project_root(settings, pid) / "tmp" / gone_id
    tmp_ws.mkdir(parents=True, exist_ok=True)
    (tmp_ws / "scratch.bin").write_bytes(b"x")
    parsed = parsed_dir(settings, pid, "note") / f"{gone_id}_final.txt"
    parsed.write_text("parsed", encoding="utf-8")
    keep_parsed = parsed_dir(settings, pid, "note") / f"{keep_id}_final.txt"
    keep_parsed.write_text("keep-parsed", encoding="utf-8")

    raw_gone = raw_input_dir(settings, pid, "note") / gone_stored
    raw_keep = raw_input_dir(settings, pid, "note") / keep_stored
    assert raw_gone.is_file()
    assert raw_keep.is_file()

    r = client.delete(
        f"/projects/{pid}/artifacts/{gone_id}",
        headers={"Authorization": f"Bearer {manager_token}"},
    )
    assert r.status_code == 204
    assert not raw_gone.exists()
    assert not ws.exists()
    assert not tmp_ws.exists()
    assert not parsed.exists()
    assert raw_keep.is_file()
    assert keep_parsed.is_file()
    assert project_root(settings, pid).is_dir()

    listed = client.get(
        f"/projects/{pid}/artifacts",
        headers={"Authorization": f"Bearer {manager_token}"},
    ).json()
    ids = [row["artifact_id"] for row in listed]
    assert gone_id not in ids
    assert keep_id in ids

    from app.app.db import SessionLocal
    from sqlalchemy import select

    db = SessionLocal()
    try:
        assert db.scalar(select(Artifact).where(Artifact.artifact_id == gone_id)) is None
        assert db.scalar(select(Artifact).where(Artifact.artifact_id == keep_id)) is not None
        assert db.scalars(select(Chunk).where(Chunk.artifact_id == gone_pk)).all() == []
        assert db.scalars(select(Job).where(Job.artifact_id == gone_pk)).all() == []
    finally:
        db.close()


def test_delete_artifact_with_speech_acts_and_events(client, manager_token, user_token, tmp_data):
    """Регресс: speech_acts/decision_events ссылаются на artifacts.id (FK) и раньше не чистились
    в purge_artifact — delete падал с IntegrityError, если материал уже разобран на реплики."""
    from app.app.db import SessionLocal
    from app.app.models.decision_memory import DecisionEvent, Subject
    from app.app.models.speech_act import SpeechAct
    from sqlalchemy import select

    pid = _project(client, manager_token)
    art = _upload(client, manager_token, pid, "acted.txt", b"some dialogue")

    db = SessionLocal()
    try:
        art_row = db.scalar(select(Artifact).where(Artifact.artifact_id == art["artifact_id"]))
        assert art_row is not None
        subject = Subject(project_id=pid, title="Предмет спора")
        db.add(subject)
        db.flush()
        act = SpeechAct(
            project_id=pid,
            artifact_id=art_row.id,
            unit_ref="M1",
            unit_key=1,
            act="proposed",
            quote="цитата реплики",
            prompt_version="v-test",
        )
        db.add(act)
        db.flush()
        rule_event = DecisionEvent(
            project_id=pid,
            subject_id=subject.id,
            kind="proposed",
            occurred_at="2026-10-01T10:00:00",
            artifact_id=art_row.id,
            speech_act_id=act.id,
            decided_by="rule",
        )
        human_event = DecisionEvent(
            project_id=pid,
            subject_id=subject.id,
            kind="agreed",
            occurred_at="2026-10-02T11:00:00",
            speech_act_id=act.id,
            decided_by="human",
        )
        db.add_all([rule_event, human_event])
        db.commit()
        subject_id = subject.id
        art_pk = art_row.id
        act_id = act.id
        rule_id = rule_event.id
        human_id = human_event.id
    finally:
        db.close()

    r = client.delete(
        f"/projects/{pid}/artifacts/{art['artifact_id']}",
        headers={"Authorization": f"Bearer {manager_token}"},
    )
    assert r.status_code == 204

    db = SessionLocal()
    try:
        assert db.scalars(select(SpeechAct).where(SpeechAct.artifact_id == art_pk)).all() == []
        assert db.get(SpeechAct, act_id) is None
        assert db.get(DecisionEvent, rule_id) is None  # событие материала ушло с ним
        human = db.get(DecisionEvent, human_id)
        assert human is not None and human.speech_act_id is None  # решение человека пережило материал
        assert db.get(Subject, subject_id) is not None
    finally:
        db.close()


def test_delete_artifact_clears_graph_links(client, manager_token, user_token, tmp_data):
    pid = _project(client, manager_token)
    gone = _upload(client, manager_token, pid, "gone.txt", b"remove-me")
    keep = _upload(client, manager_token, pid, "keep.txt", b"stay-here")

    from app.app.db import SessionLocal
    from app.app.models.entity import Entity, Link, Resolution
    from sqlalchemy import select

    db = SessionLocal()
    try:
        gone_row = db.scalar(select(Artifact).where(Artifact.artifact_id == gone["artifact_id"]))
        keep_row = db.scalar(select(Artifact).where(Artifact.artifact_id == keep["artifact_id"]))
        assert gone_row is not None and keep_row is not None
        e1 = Entity(
            artifact_id=gone_row.id,
            entity_type="fact",
            value_json='{"key": "a"}',
            confidence=0.6,
            is_active=True,
            needs_review=True,
        )
        e2 = Entity(
            artifact_id=keep_row.id,
            entity_type="fact",
            value_json='{"key": "b"}',
            confidence=0.6,
            is_active=True,
            needs_review=True,
        )
        db.add_all([e1, e2])
        db.flush()
        link = Link(
            project_id=pid,
            source_entity_id=e1.id,
            target_entity_id=e2.id,
            link_type="related",
            is_automatic=True,
        )
        db.add(link)
        db.flush()
        db.add(
            Resolution(
                link_id=link.id,
                winning_entity_id=e1.id,
                losing_entity_id=e2.id,
                reason="merge",
                status="resolved",
            )
        )
        db.commit()
        keep_entity_id = e2.id
    finally:
        db.close()

    r = client.delete(
        f"/projects/{pid}/artifacts/{gone['artifact_id']}",
        headers={"Authorization": f"Bearer {manager_token}"},
    )
    assert r.status_code == 204

    db = SessionLocal()
    try:
        assert db.scalar(select(Artifact).where(Artifact.artifact_id == gone["artifact_id"])) is None
        assert db.get(Entity, keep_entity_id) is not None
        assert db.scalars(select(Link).where(Link.project_id == pid)).all() == []
        assert db.scalars(select(Resolution)).all() == []
    finally:
        db.close()


def test_project_page_has_artifact_delete_column(client, manager_token, user_token):
    pid = _project(client, manager_token)
    r = client.get(f"/projects/{pid}", headers={"Authorization": f"Bearer {manager_token}"})
    assert r.status_code == 200
    html = r.text
    assert 'id="deleteArtifactModal"' in html
    assert "Подтверждаю полное удаление артефакта" in html
    assert "col-delete" in html
    assert "visually-hidden" in html
    assert "artifact-delete-icon" in html
