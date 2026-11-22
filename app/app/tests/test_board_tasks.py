"""Задачи на личной доске: кому приходят, когда пропадают и что попадает в последние 15."""

from datetime import datetime, timedelta, timezone

from app.app.models.artifact import Artifact
from app.app.models.job import Job
from app.app.models.meeting import Meeting, MeetingParticipant
from app.app.models.project import Project
from app.app.models.project_member import ProjectMember
from app.app.models.user import User


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _db():
    from app.app.db import SessionLocal

    return SessionLocal()


def _user_id(name: str) -> int:
    db = _db()
    try:
        return db.query(User.id).filter(User.name == name).one()[0]
    finally:
        db.close()


def _flags(named: dict):
    def speaker_action_flags(db, *, project_id, artifact_id, status, file_type):
        if artifact_id not in named:
            return {
                "speakers_match_available": False,
                "speakers_match_ready": False,
                "speakers_confirm_ready": False,
            }
        ready = named[artifact_id]
        return {
            "speakers_match_available": True,
            "speakers_match_ready": ready,
            "speakers_confirm_ready": ready,
        }

    return speaker_action_flags


def _states(by_meeting: dict):
    def analyze_state(settings, meeting, db=None):
        name = by_meeting.get(meeting.id, "none")
        return {"analyze_status": name, "can_analyze": name == "ready"}

    return analyze_state


def _art(db, **kw) -> None:
    db.add(
        Artifact(
            artifact_id=kw["artifact_id"],
            project_id=kw["project_id"],
            user_id=kw["user_id"],
            original_filename=kw.get("filename", "файл.pdf"),
            stored_filename="stored.bin",
            file_path="x",
            file_type=kw.get("file_type", "document"),
            source_type=kw.get("source_type", "document"),
            sha256="a" * 64,
            status=kw.get("status", "done"),
            upload_date=kw.get("when"),
            recorded_at_source=kw.get("source"),
        )
    )
    db.commit()


def _refs(body: dict, slot: str) -> list[str]:
    return [item["ref"] for item in body[slot]]


def test_board_requires_login(client):
    assert client.get("/me/board").status_code == 401
    assert client.post("/me/board/dismiss", json={"kind": "speakers", "ref": "x"}).status_code == 401


def test_speaker_task_follows_the_uploader_and_leaves_when_named(
    client, manager_token, user_token, admin_token, monkeypatch
):
    named: dict[str, bool] = {}
    monkeypatch.setattr("app.app.services.board_tasks.speaker_action_flags", _flags(named))
    monkeypatch.setattr("app.app.services.board_tasks.analyze_state", _states({}))
    manager = _auth(manager_token)
    pid = client.post("/projects", json={"name": "Голоса"}, headers=manager).json()["id"]
    manager_id = _user_id("Manager")
    user_id = _user_id("User")
    db = _db()
    try:
        db.add(ProjectMember(project_id=pid, user_id=user_id, role="user", granted_by=manager_id))
        _art(
            db,
            artifact_id="voices",
            project_id=pid,
            user_id=manager_id,
            filename="планёрка.wav",
            file_type="audio",
            source_type="call",
            status="speakers_pending",
        )
        hidden = client.post("/projects", json={"name": "Скрытый"}, headers=manager).json()["id"]
        _art(
            db,
            artifact_id="hidden-voices",
            project_id=hidden,
            user_id=manager_id,
            filename="архив.wav",
            file_type="audio",
            source_type="call",
            status="speakers_pending",
        )
        db.get(Project, hidden).is_active = False
        db.commit()
    finally:
        db.close()

    named["voices"] = False
    named["hidden-voices"] = False
    body = client.get("/me/board", headers=manager).json()
    assert _refs(body, "open") == ["voices"]
    assert body["open"][0]["title"] == "Определить спикеров"
    assert body["open"][0]["href"] == "/artifacts/voices/report-speaker-match"
    assert body["open"][0]["project_name"] == "Голоса"
    assert "voices" not in _refs(client.get("/me/board", headers=_auth(user_token)).json(), "open")
    assert client.get("/me/board", headers=_auth(admin_token)).json() == {"open": [], "done": []}

    named["voices"] = True
    after = client.get("/me/board", headers=manager).json()
    assert after["open"] == []
    assert after["done"] == []

    db = _db()
    try:
        db.query(Artifact).filter(Artifact.artifact_id == "voices").one().status = "done"
        db.commit()
    finally:
        db.close()
    done = client.get("/me/board", headers=manager).json()["done"]
    assert [item["ref"] for item in done] == ["voices"]
    assert done[0]["title"] == "Созвон"
    assert done[0]["href"] == "/artifacts/voices/report"


def test_meeting_task_belongs_to_the_room_creator_until_someone_sends_it(
    client, manager_token, user_token, admin_token, monkeypatch
):
    states: dict[int, str] = {}
    monkeypatch.setattr("app.app.services.board_tasks.speaker_action_flags", _flags({}))
    monkeypatch.setattr("app.app.services.board_tasks.analyze_state", _states(states))
    manager = _auth(manager_token)
    member = _auth(user_token)
    pid = client.post("/projects", json={"name": "Созвоны"}, headers=manager).json()["id"]
    manager_id = _user_id("Manager")
    user_id = _user_id("User")
    db = _db()
    try:
        db.add(ProjectMember(project_id=pid, user_id=user_id, role="user", granted_by=manager_id))
        meeting = Meeting(
            project_id=pid,
            created_by=manager_id,
            room_name=f"room-board-{pid}",
            status="ended",
            public_token_hash=f"{pid:064d}",
            ended_at=datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc),
        )
        db.add(meeting)
        db.commit()
        mid = meeting.id
        db.add(
            MeetingParticipant(
                meeting_id=mid,
                user_id=user_id,
                identity=f"member:{user_id}",
                display_name="User",
                participant_type="member",
            )
        )
        db.commit()
    finally:
        db.close()

    states[mid] = "ready"
    creator = client.get("/me/board", headers=manager).json()
    assert _refs(creator, "open") == [str(mid)]
    assert creator["open"][0]["title"] == "Анализ внутреннего созвона"
    assert creator["open"][0]["href"] == f"/projects/{pid}#meetings"
    guest = client.get("/me/board", headers=member).json()
    assert guest["open"] == []
    assert guest["done"] == []
    assert client.get("/me/board", headers=_auth(admin_token)).json()["open"] == []

    states[mid] = "queued"
    db = _db()
    try:
        _art(
            db,
            artifact_id=f"meet-{mid}",
            project_id=pid,
            user_id=manager_id,
            filename=f"встреча-{mid}.wav",
            file_type="audio",
            source_type="call",
            status="uploaded",
        )
    finally:
        db.close()
    sent = client.get("/me/board", headers=manager).json()
    assert sent["open"] == []
    assert sent["done"] == []
    assert client.get("/me/board", headers=member).json()["done"] == []

    finished = datetime(2026, 11, 20, 11, 15, tzinfo=timezone.utc)
    db = _db()
    try:
        art = db.query(Artifact).filter(Artifact.artifact_id == f"meet-{mid}").one()
        art.status = "done"
        db.add(Job(artifact_id=art.id, status="done", finished_at=finished))
        db.commit()
    finally:
        db.close()
    done = client.get("/me/board", headers=manager).json()["done"]
    assert [item["ref"] for item in done] == [str(mid)]
    assert done[0]["hint"] == ""
    assert done[0]["at"].startswith("2026-11-20T11:15:00")
    assert _refs(client.get("/me/board", headers=member).json(), "done") == [str(mid)]
    assert client.get("/me/board", headers=_auth(admin_token)).json()["done"] == []


def test_dismiss_hides_only_this_users_open_task(client, manager_token, user_token, monkeypatch):
    named = {"voices": False}
    monkeypatch.setattr("app.app.services.board_tasks.speaker_action_flags", _flags(named))
    monkeypatch.setattr("app.app.services.board_tasks.analyze_state", _states({}))
    manager = _auth(manager_token)
    pid = client.post("/projects", json={"name": "Снять"}, headers=manager).json()["id"]
    db = _db()
    try:
        _art(
            db,
            artifact_id="voices",
            project_id=pid,
            user_id=_user_id("Manager"),
            filename="созвон.wav",
            file_type="audio",
            source_type="call",
            status="transcript_ready",
        )
    finally:
        db.close()

    assert _refs(client.get("/me/board", headers=manager).json(), "open") == ["voices"]
    assert client.post(
        "/me/board/dismiss",
        json={"kind": "speakers", "ref": "voices"},
        headers=_auth(user_token),
    ).status_code == 404
    assert client.post(
        "/me/board/dismiss",
        json={"kind": "speakers", "ref": "voices"},
        headers=manager,
    ).status_code == 204
    assert client.get("/me/board", headers=manager).json()["open"] == []
    assert client.post(
        "/me/board/dismiss",
        json={"kind": "speakers", "ref": "voices"},
        headers=manager,
    ).status_code == 404
    assert client.post("/me/board/dismiss", json={"kind": "other", "ref": "voices"}, headers=manager).status_code == 422


def test_done_list_is_this_users_last_fifteen(client, manager_token, user_token, monkeypatch):
    monkeypatch.setattr("app.app.services.board_tasks.speaker_action_flags", _flags({}))
    monkeypatch.setattr("app.app.services.board_tasks.analyze_state", _states({}))
    manager = _auth(manager_token)
    pid = client.post("/projects", json={"name": "Архив"}, headers=manager).json()["id"]
    manager_id = _user_id("Manager")
    user_id = _user_id("User")
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    db = _db()
    try:
        db.add(ProjectMember(project_id=pid, user_id=user_id, role="user", granted_by=manager_id))
        for i in range(16):
            _art(
                db,
                artifact_id=f"doc-{i:02d}",
                project_id=pid,
                user_id=manager_id,
                filename=f"файл-{i}.pdf",
                when=start + timedelta(days=i),
            )
        _art(
            db,
            artifact_id="jira-doc",
            project_id=pid,
            user_id=manager_id,
            filename="jira.md",
            when=start + timedelta(days=40),
            source="jira",
        )
        _art(
            db,
            artifact_id="theirs",
            project_id=pid,
            user_id=user_id,
            filename="чужой.pdf",
            when=start + timedelta(days=41),
        )
    finally:
        db.close()

    refs = _refs(client.get("/me/board", headers=manager).json(), "done")
    assert refs == [f"doc-{i:02d}" for i in range(15, 0, -1)]
    assert "doc-00" not in refs
    assert "jira-doc" not in refs
    assert "theirs" not in refs
    assert _refs(client.get("/me/board", headers=_auth(user_token)).json(), "done") == ["theirs"]
