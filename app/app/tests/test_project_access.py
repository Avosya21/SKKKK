from __future__ import annotations

from fastapi import HTTPException

from app.app.models.project import Project
from app.app.models.user import User
from app.app.services.project_access import can_access_project, can_grant_access, require_project, staff_ui_role


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_can_access_project_admin_any():
    admin = User(id=1, name="A", telegram_id="t", role="admin", password_hash="x")
    other = Project(id=9, name="X", created_by=2, is_active=True)
    assert can_access_project(admin, other) is True


def test_staff_ui_role_user_password_hides_owner_staff():
    owner = User(id=2, name="M", telegram_id="t", role="user", password_hash="x")
    own = Project(id=1, name="Mine", created_by=2, is_active=True)
    assert staff_ui_role(None, owner, own) == "user"
    assert can_grant_access(None, owner, own) is False


def test_can_access_project_owner_only():
    manager = User(id=2, name="M", telegram_id="t", role="manager", password_hash="x")
    own = Project(id=1, name="Mine", created_by=2, is_active=True)
    foreign = Project(id=2, name="Theirs", created_by=3, is_active=True)
    assert can_access_project(manager, own) is True
    assert can_access_project(manager, foreign) is False


def test_manager_lists_only_own_admin_sees_all(client, admin_token, manager_token):
    a = client.post("/projects", json={"name": "AdminProj"}, headers=_auth(admin_token))
    m = client.post("/projects", json={"name": "MgrProj"}, headers=_auth(manager_token))
    assert a.status_code == 201 and m.status_code == 201
    admin_ids = {p["id"] for p in client.get("/projects", headers=_auth(admin_token)).json()}
    mgr_ids = {p["id"] for p in client.get("/projects", headers=_auth(manager_token)).json()}
    assert a.json()["id"] in admin_ids
    assert m.json()["id"] in admin_ids
    assert m.json()["id"] in mgr_ids
    assert a.json()["id"] not in mgr_ids


def test_role_change_keeps_created_project(client, manager_token):
    pid = client.post("/projects", json={"name": "KeepMine"}, headers=_auth(manager_token)).json()["id"]
    r = client.post(
        "/auth/login",
        json={"name": "Manager", "telegram_id": "tg-manager", "password": "user-pass"},
    )
    assert r.status_code == 200
    assert r.json()["role"] == "user"
    ids = [p["id"] for p in client.get("/projects", headers=_auth(r.json()["access_token"])).json()]
    assert pid in ids
    assert client.get(f"/projects/{pid}", headers=_auth(r.json()["access_token"])).status_code == 200


def test_contributor_sees_project_they_uploaded_to(client, admin_token, manager_token, db_engine):
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.user import User as UserModel

    pid = client.post("/projects", json={"name": "Shared"}, headers=_auth(admin_token)).json()["id"]
    db = SessionLocal()
    try:
        mgr = db.query(UserModel).filter(UserModel.role == "manager").one()
        db.add(
            Artifact(
                artifact_id="art_contrib",
                project_id=pid,
                user_id=mgr.id,
                original_filename="n.txt",
                stored_filename="n.txt",
                file_path="n.txt",
                file_type="txt",
                source_type="note",
                sha256="4" * 64,
                status="done",
            )
        )
        db.commit()
    finally:
        db.close()
    ids = [p["id"] for p in client.get("/projects", headers=_auth(manager_token)).json()]
    assert pid in ids
    assert client.get(f"/projects/{pid}", headers=_auth(manager_token)).status_code == 200


def test_manager_get_foreign_project_404(client, admin_token, manager_token):
    pid = client.post("/projects", json={"name": "Hidden"}, headers=_auth(admin_token)).json()["id"]
    r = client.get(f"/projects/{pid}", headers=_auth(manager_token))
    assert r.status_code == 404


def test_manager_html_foreign_project_404(client, admin_token, manager_token):
    pid = client.post("/projects", json={"name": "HiddenHtml"}, headers=_auth(admin_token)).json()["id"]
    r = client.get(f"/projects/{pid}", headers=_auth(manager_token), follow_redirects=False)
    assert r.status_code == 404


def test_manager_cannot_open_lab_on_own_audio(client, manager_token, tmp_data, db_engine):
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.user import User as UserModel

    pid = client.post("/projects", json={"name": "LabOwn"}, headers=_auth(manager_token)).json()["id"]
    db = SessionLocal()
    try:
        owner = db.query(UserModel).filter(UserModel.role == "manager").one()
        db.add(
            Artifact(
                artifact_id="art_mgr_lab",
                project_id=pid,
                user_id=owner.id,
                original_filename="c.wav",
                stored_filename="c.wav",
                file_path="c.wav",
                file_type="audio",
                source_type="call",
                sha256="1" * 64,
                status="done",
            )
        )
        db.commit()
    finally:
        db.close()
    r = client.get("/artifacts/art_mgr_lab/report-lab", headers=_auth(manager_token), follow_redirects=False)
    assert r.status_code == 403


def test_user_forbidden_rebuild_summary(client, manager_token, user_token, db_engine):
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.user import User as UserModel

    pid = client.post("/projects", json={"name": "SumOwn"}, headers=_auth(manager_token)).json()["id"]
    db = SessionLocal()
    try:
        owner = db.query(UserModel).filter(UserModel.role == "manager").one()
        db.add(
            Artifact(
                artifact_id="art_mgr_sum",
                project_id=pid,
                user_id=owner.id,
                original_filename="c.txt",
                stored_filename="c.txt",
                file_path="c.txt",
                file_type="txt",
                source_type="document",
                sha256="2" * 64,
                status="done",
            )
        )
        db.commit()
    finally:
        db.close()
    r = client.post("/api/artifacts/art_mgr_sum/rebuild-summary", headers=_auth(user_token))
    assert r.status_code in {403, 404}


def test_user_forbidden_match_page(client, manager_token, user_token, db_engine):
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.user import User as UserModel

    pid = client.post("/projects", json={"name": "MatchOwn"}, headers=_auth(manager_token)).json()["id"]
    db = SessionLocal()
    try:
        owner = db.query(UserModel).filter(UserModel.role == "manager").one()
        db.add(
            Artifact(
                artifact_id="art_mgr_match",
                project_id=pid,
                user_id=owner.id,
                original_filename="c.wav",
                stored_filename="c.wav",
                file_path="c.wav",
                file_type="audio",
                source_type="call",
                sha256="3" * 64,
                status="done",
            )
        )
        db.commit()
    finally:
        db.close()
    r = client.get(
        "/artifacts/art_mgr_match/report-speaker-match",
        headers=_auth(user_token),
        follow_redirects=False,
    )
    assert r.status_code in {403, 404}


def test_require_project_inactive(db_engine):
    from app.app.db import SessionLocal

    db = SessionLocal()
    try:
        user = User(name="Own", telegram_id="tg-own", role="manager", password_hash="x")
        db.add(user)
        db.flush()
        project = Project(name="Dead", created_by=user.id, is_active=False)
        db.add(project)
        db.commit()
        db.refresh(user)
        db.refresh(project)
        try:
            require_project(db, user, project.id)
            raise AssertionError("expected 404")
        except HTTPException as exc:
            assert exc.status_code == 404
    finally:
        db.close()
