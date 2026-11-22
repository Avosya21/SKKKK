from __future__ import annotations

from pathlib import Path

from app.app.config import get_settings
from app.app.services.storage import project_root


def test_manager_can_create_project(client, manager_token):
    r = client.post(
        "/projects",
        json={"name": "Demo", "description": "test"},
        headers={"Authorization": f"Bearer {manager_token}"},
    )
    assert r.status_code == 201
    assert r.json()["name"] == "Demo"


def test_user_cannot_create_project_403(client, user_token):
    r = client.post(
        "/projects",
        json={"name": "Nope"},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert r.status_code == 403


def test_user_cannot_delete_project(client, manager_token, user_token):
    pid = client.post(
        "/projects",
        json={"name": "Keep"},
        headers={"Authorization": f"Bearer {manager_token}"},
    ).json()["id"]
    assert client.delete(f"/projects/{pid}", headers={"Authorization": f"Bearer {user_token}"}).status_code == 403
    assert client.get(f"/projects/{pid}", headers={"Authorization": f"Bearer {manager_token}"}).status_code == 200


def test_manager_purge_removes_files_and_db(client, manager_token, user_token, tmp_data):
    import io

    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.project import Project

    headers = {"Authorization": f"Bearer {manager_token}"}
    keep = client.post("/projects", json={"name": "Keep me"}, headers=headers).json()
    gone = client.post(
        "/projects",
        json={"name": "Wipe me", "description": "к удалению"},
        headers=headers,
    ).json()
    pid = gone["id"]
    settings = get_settings()
    root = project_root(settings, pid)
    marker = root / "raw_inputs" / "notes" / "secret.txt"
    marker.write_text("wipe", encoding="utf-8")
    client.post(
        f"/projects/{pid}/upload",
        headers={"Authorization": f"Bearer {manager_token}"},
        data={"source_type": "note"},
        files=[("files", ("hello.txt", io.BytesIO(b"hello"), "text/plain"))],
    )
    keep_root = project_root(settings, keep["id"])
    keep_marker = keep_root / "raw_inputs" / "notes" / "stay.txt"
    keep_marker.write_text("stay", encoding="utf-8")

    r = client.delete(f"/projects/{pid}", headers=headers)
    assert r.status_code == 204
    assert not root.exists()
    assert keep_marker.is_file()
    assert client.get(f"/projects/{pid}", headers=headers).status_code == 404
    ids = [p["id"] for p in client.get("/projects", headers=headers).json()]
    assert pid not in ids
    assert keep["id"] in ids

    db = SessionLocal()
    try:
        from sqlalchemy import select

        assert db.get(Project, pid) is None
        leftover = db.scalars(select(Artifact).where(Artifact.project_id == pid)).all()
        assert leftover == []
    finally:
        db.close()


def test_admin_can_delete_project(client, admin_token, manager_token):
    pid = client.post(
        "/projects",
        json={"name": "Admin wipe"},
        headers={"Authorization": f"Bearer {manager_token}"},
    ).json()["id"]
    assert client.delete(f"/projects/{pid}", headers={"Authorization": f"Bearer {admin_token}"}).status_code == 204


def test_delete_project_with_agent_protocol_rows(client, manager_token):
    """Регресс: agent_exchange_log/agent_questions/contract_items ссылаются на project_id
    (FK, foreign_keys=ON) и раньше не чистились в purge_project — delete падал с
    IntegrityError, если через проект хоть раз прошёл MCP-агент."""
    from app.app.db import SessionLocal
    from app.app.models.agent_exchange import AgentExchangeLog
    from app.app.models.agent_question import AgentQuestion
    from app.app.models.contract import ContractItem

    pid = client.post(
        "/projects",
        json={"name": "Agent touched"},
        headers={"Authorization": f"Bearer {manager_token}"},
    ).json()["id"]

    db = SessionLocal()
    try:
        db.add(
            AgentExchangeLog(
                project_id=pid,
                source="test",
                kind="tz.get",
                decision="accepted",
            )
        )
        db.add(AgentQuestion(project_id=pid, request_id="req-1", source="test", question="?"))
        db.add(ContractItem(project_id=pid, kind="req", key="k1", seq=1))
        db.commit()
    finally:
        db.close()

    r = client.delete(f"/projects/{pid}", headers={"Authorization": f"Bearer {manager_token}"})
    assert r.status_code == 204


def test_delete_project_with_mcp_exchange_bundle(client, manager_token):
    """Регресс (проект 54): exchange_bundles.token_id → project_mcp_tokens; если бандлы
    чистить после ключей, DELETE проекта падает с IntegrityError."""
    from app.app.db import SessionLocal
    from app.app.models.exchange_file import ExchangeBundle, ExchangeFile
    from app.app.models.project_mcp_token import McpCallStat
    from app.app.services.project_mcp_tokens import create_project_mcp_token

    pid = client.post(
        "/projects",
        json={"name": "MCP bundles"},
        headers={"Authorization": f"Bearer {manager_token}"},
    ).json()["id"]

    db = SessionLocal()
    try:
        token, _plain = create_project_mcp_token(db, project_id=pid, label="agent", user_id=1)
        bundle = ExchangeBundle(
            project_id=pid,
            name="cfg",
            kind="other",
            status="complete",
            manifest_sha="a" * 64,
            total_bytes=3,
            file_count=1,
            token_id=token.id,
            source="test",
        )
        db.add(bundle)
        db.flush()
        db.add(
            ExchangeFile(
                bundle_id=bundle.id,
                path="map.json",
                size=3,
                sha256="b" * 64,
                received_bytes=3,
                status="complete",
            )
        )
        db.add(
            McpCallStat(
                project_id=pid,
                token_id=token.id,
                day="2026-11-07",
                tool="files_catalog",
                calls=1,
                bytes_out=10,
                errors=0,
            )
        )
        db.commit()
    finally:
        db.close()

    r = client.delete(f"/projects/{pid}", headers={"Authorization": f"Bearer {manager_token}"})
    assert r.status_code == 204


def test_delete_project_keeps_activity_log(client, manager_token):
    """Регресс (проект 58): создание пишет user_activity_log; purge не удаляет журнал,
    но строка projects должна уйти (раньше FK журнала на projects давал IntegrityError)."""
    from sqlalchemy import select

    from app.app.db import SessionLocal
    from app.app.models.user_activity_log import UserActivityLog

    pid = client.post(
        "/projects",
        json={"name": "Audit kept"},
        headers={"Authorization": f"Bearer {manager_token}"},
    ).json()["id"]

    db = SessionLocal()
    try:
        before = list(
            db.scalars(select(UserActivityLog.id).where(UserActivityLog.project_id == pid))
        )
        assert before, "создание проекта должно писать в журнал"
    finally:
        db.close()

    r = client.delete(f"/projects/{pid}", headers={"Authorization": f"Bearer {manager_token}"})
    assert r.status_code == 204

    db = SessionLocal()
    try:
        left = list(
            db.scalars(select(UserActivityLog).where(UserActivityLog.project_id == pid))
        )
        assert left, "журнал аудита должен остаться с project_id удалённого проекта"
    finally:
        db.close()


def test_ensure_user_activity_log_no_fk_strips_legacy_fk(tmp_path):
    """Миграция снимает унаследованные FK журнала (как в боевой app.db до фикса)."""
    import sqlite3

    from sqlalchemy import create_engine

    from app.app.db import ensure_user_activity_log_no_fk

    db_path = tmp_path / "audit.db"
    raw = sqlite3.connect(db_path)
    raw.execute("PRAGMA foreign_keys=ON")
    raw.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE projects (id INTEGER PRIMARY KEY);
        INSERT INTO users(id) VALUES (1);
        INSERT INTO projects(id) VALUES (1);
        CREATE TABLE user_activity_log (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            project_id INTEGER,
            block VARCHAR(32) NOT NULL,
            action VARCHAR(48) NOT NULL,
            title VARCHAR(255) NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(project_id) REFERENCES projects(id)
        );
        INSERT INTO user_activity_log(id, user_id, project_id, block, action, title)
        VALUES (1, 1, 1, 'project', 'create', 'x');
        """
    )
    raw.commit()
    assert any(r[2] == "projects" for r in raw.execute("PRAGMA foreign_key_list(user_activity_log)"))
    raw.close()

    eng = create_engine(f"sqlite:///{db_path.as_posix()}")
    with eng.connect() as conn:
        ensure_user_activity_log_no_fk(conn)
        fks = conn.exec_driver_sql("PRAGMA foreign_key_list(user_activity_log)").fetchall()
        assert fks == []
        n = conn.exec_driver_sql("SELECT COUNT(*) FROM user_activity_log").scalar_one()
        assert n == 1
        # После снятия FK удаление проекта не блокируется журналом.
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        conn.exec_driver_sql("DELETE FROM projects WHERE id=1")
        conn.commit()
        left = conn.exec_driver_sql(
            "SELECT project_id FROM user_activity_log WHERE id=1"
        ).scalar_one()
        assert left == 1


def test_delete_project_with_decision_memory_rows(client, manager_token):
    """Регресс: память решений (subjects/speech_acts/decision_events/…) ссылается на artifacts/projects
    (FK) и раньше не чистилась в purge_project — delete падал с IntegrityError после разбора материала."""
    import io

    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.decision_memory import DecisionEvent, MemoryShadowReview, Subject, SubjectPosition
    from app.app.models.speech_act import SpeechAct
    from sqlalchemy import select

    pid = client.post(
        "/projects",
        json={"name": "Memory touched"},
        headers={"Authorization": f"Bearer {manager_token}"},
    ).json()["id"]
    up = client.post(
        f"/projects/{pid}/upload",
        headers={"Authorization": f"Bearer {manager_token}"},
        data={"source_type": "note"},
        files=[("files", ("acted.txt", io.BytesIO(b"some dialogue"), "text/plain"))],
    )
    assert up.status_code == 200, up.text
    aid = up.json()[0]["artifact_id"]

    db = SessionLocal()
    try:
        art = db.scalar(select(Artifact).where(Artifact.artifact_id == aid))
        assert art is not None
        subject = Subject(project_id=pid, title="Предмет спора")
        db.add(subject)
        db.flush()
        db.add(SubjectPosition(subject_id=subject.id, label="Вариант А"))
        act = SpeechAct(
            project_id=pid,
            artifact_id=art.id,
            unit_ref="M1",
            unit_key=1,
            act="proposed",
            quote="цитата реплики",
            prompt_version="v-test",
        )
        db.add(act)
        db.flush()
        db.add(
            DecisionEvent(
                project_id=pid,
                subject_id=subject.id,
                kind="proposed",
                occurred_at="2026-10-01T10:00:00",
                artifact_id=art.id,
                speech_act_id=act.id,
                decided_by="rule",
            )
        )
        db.add(
            MemoryShadowReview(
                project_id=pid,
                entity_id=1,
                subject_id=subject.id,
                old_state="current",
                new_state="agreed",
                verdict="new",
            )
        )
        db.commit()
    finally:
        db.close()

    r = client.delete(f"/projects/{pid}", headers={"Authorization": f"Bearer {manager_token}"})
    assert r.status_code == 204


def test_create_project_makes_directory_tree(client, manager_token, tmp_data):
    r = client.post(
        "/projects",
        json={"name": "Tree"},
        headers={"Authorization": f"Bearer {manager_token}"},
    )
    pid = r.json()["id"]
    settings = get_settings()
    root = project_root(settings, pid)
    for sub in (
        "raw_inputs/calls",
        "raw_inputs/documents",
        "raw_inputs/notes",
        "parsed/calls",
        "parsed/documents",
        "parsed/notes",
        "structured",
        "project_notes",
        "project_state/versions",
        "tmp",
    ):
        assert (root / sub).is_dir()


def test_two_projects_same_name_get_isolated_folders(client, manager_token, tmp_data):
    headers = {"Authorization": f"Bearer {manager_token}"}
    a = client.post("/projects", json={"name": "Same Name"}, headers=headers).json()
    b = client.post("/projects", json={"name": "Same Name"}, headers=headers).json()
    assert a["id"] != b["id"]
    assert a["name"] == b["name"] == "Same Name"

    settings = get_settings()
    root_a = project_root(settings, a["id"])
    root_b = project_root(settings, b["id"])
    assert root_a != root_b
    assert root_a.is_dir() and root_b.is_dir()
    assert root_a.parent == root_b.parent == settings.data_dir / "projects"

    marker = root_a / "raw_inputs" / "notes" / "only_in_a.txt"
    marker.write_text("secret-a", encoding="utf-8")
    assert not (root_b / "raw_inputs" / "notes" / "only_in_a.txt").exists()
    assert (root_b / "raw_inputs" / "notes").is_dir()


def test_new_project_does_not_reuse_existing_folder(client, manager_token, tmp_data):
    headers = {"Authorization": f"Bearer {manager_token}"}
    first = client.post("/projects", json={"name": "First"}, headers=headers).json()
    settings = get_settings()
    first_root = project_root(settings, first["id"])
    (first_root / "raw_inputs" / "documents" / "keep.bin").write_bytes(b"keep")

    second = client.post("/projects", json={"name": "Second"}, headers=headers).json()
    second_root = project_root(settings, second["id"])
    assert second_root != first_root
    assert (first_root / "raw_inputs" / "documents" / "keep.bin").read_bytes() == b"keep"
    assert list(second_root.rglob("keep.bin")) == []


def test_project_name_with_path_chars_is_sanitized(client, manager_token):
    r = client.post(
        "/projects",
        json={"name": "../../etc"},
        headers={"Authorization": f"Bearer {manager_token}"},
    )
    assert r.status_code == 201
    assert ".." not in r.json()["name"]


def test_list_projects_visible_to_all_roles(client, admin_token, user_token):
    assert client.get("/projects", headers={"Authorization": f"Bearer {admin_token}"}).status_code == 200
    assert client.get("/projects", headers={"Authorization": f"Bearer {user_token}"}).status_code == 200


def test_projects_page_asks_delete_confirmation(client, manager_token):
    r = client.get("/", headers={"Authorization": f"Bearer {manager_token}"})
    assert r.status_code == 200
    html = r.text
    assert 'id="deleteProjectModal"' in html
    assert "Подтверждаю полное удаление проекта" in html
    assert "Подтверждение удаления" in html
    assert 'id="projects-board"' in html
    assert 'id="board-tasks"' in html
    assert 'id="projects-board-split"' in html
    assert 'id="board-tasks-split"' in html
    assert 'project-card-ghost' in html
    assert ">Задачи<" in html
    assert ">Сделано<" in html
    assert 'id="projects-list"' in html
    assert 'id="projects-board-list"' in html
    assert "Все проекты" in html
    assert "Доска" in html
    assert ">Настройки<" in html
    assert "Настройки интерфейса" not in html
    assert 'id="jira-card"' not in html
    assert 'id="jira-settings"' in html
    assert 'id="jira-token"' in html
    assert 'id="nav-ui"' in html
    assert 'id="ui-home-list"' in html
    assert 'id="ui-home-dashboard"' in html
    assert 'id="projects-classic"' in html
    assert 'class="bg-light projects-dashboard"' not in html
    assert 'class="projects-board d-none" id="projects-board"' in html
    assert "data-dashboard-move" not in html
    assert "/me/dashboard" in html


def test_list_projects_returns_description(client, manager_token):
    client.post(
        "/projects",
        json={"name": "WithDesc", "description": "Кратко о проекте"},
        headers={"Authorization": f"Bearer {manager_token}"},
    )
    rows = client.get("/projects", headers={"Authorization": f"Bearer {manager_token}"}).json()
    match = next(p for p in rows if p["name"] == "WithDesc")
    assert match["description"] == "Кратко о проекте"


def test_delete_preview_counts(client, manager_token):
    h = {"Authorization": f"Bearer {manager_token}"}
    pid = client.post("/projects", json={"name": "Preview"}, headers=h).json()["id"]
    client.post(f"/projects/{pid}/meetings", headers=h)
    client.post(f"/projects/{pid}/meetings", headers=h)
    r = client.get(f"/projects/{pid}/delete-preview", headers=h)
    assert r.status_code == 200
    assert r.json() == {"documents": 0, "calls": 0, "meetings": 2, "notes": 0}
    assert client.get("/projects/999999/delete-preview", headers=h).status_code == 404


def test_project_name_is_not_mangled_and_empty_rejected(client, manager_token):
    h = {"Authorization": f"Bearer {manager_token}"}
    ok = client.post("/projects", json={"name": "  ООО «Ромашка» №1 & Co  "}, headers=h)
    assert ok.status_code in (200, 201) and ok.json()["name"] == "ООО «Ромашка» №1 & Co"
    assert client.post("/projects", json={"name": "   "}, headers=h).status_code == 400
    assert client.post("/projects", json={"name": "x" * 300}, headers=h).status_code == 422


def test_rename_project(client, admin_token, manager_token, user_token):
    h = {"Authorization": f"Bearer {manager_token}"}
    pid = client.post("/projects", json={"name": "Старое"}, headers=h).json()["id"]
    r = client.patch(f"/projects/{pid}", json={"name": "  Новое «имя»  "}, headers=h)
    assert r.status_code == 200 and r.json()["name"] == "Новое «имя»"
    assert next(p for p in client.get("/projects", headers=h).json() if p["id"] == pid)["name"] == "Новое «имя»"
    assert client.patch(f"/projects/{pid}", json={"name": "   "}, headers=h).status_code == 400
    assert client.patch(f"/projects/{pid}", json={"name": "x" * 300}, headers=h).status_code == 422
    assert client.patch(f"/projects/{pid}", json={"name": "Чужое"}, headers={"Authorization": f"Bearer {user_token}"}).status_code == 403
    ra = client.patch(f"/projects/{pid}", json={"name": "От админа"}, headers={"Authorization": f"Bearer {admin_token}"})
    assert ra.status_code == 200 and ra.json()["name"] == "От админа"
    page = client.get("/", headers=h).text
    assert 'id="renameProjectModal"' in page


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_dashboard_requires_login(client):
    assert client.get("/me/dashboard").status_code == 401
    assert client.put("/me/dashboard", json={"project_ids": []}).status_code == 401
    assert client.get("/me/ui").status_code == 401
    assert client.put("/me/ui", json={"home": "list"}).status_code == 401
    assert client.get("/me/board").status_code == 401
    assert client.post("/me/board/dismiss", json={"kind": "speakers", "ref": "x"}).status_code == 401


def test_home_layout_is_personal_and_switches_the_page(client, admin_token, manager_token):
    h = _auth(manager_token)
    assert client.get("/me/ui", headers=h).json() == {"home": "list", "list_width": None, "tasks_height": None}
    listed = client.get("/", headers=h).text
    assert 'class="bg-light projects-dashboard"' not in listed
    assert 'class="projects-board d-none" id="projects-board"' in listed
    assert 'id="projects-classic" class="list-group"' in listed
    saved = client.put("/me/ui", json={"home": "dashboard"}, headers=h)
    assert saved.status_code == 200 and saved.json() == {"home": "dashboard", "list_width": None, "tasks_height": None}
    assert 'class="bg-light projects-dashboard"' in client.get("/", headers=h).text
    assert client.get("/me/ui", headers=_auth(admin_token)).json() == {"home": "list", "list_width": None, "tasks_height": None}
    assert client.put("/me/ui", json={"home": "grid"}, headers=h).status_code == 422
    assert client.get("/me/ui", headers=h).json() == {"home": "dashboard", "list_width": None, "tasks_height": None}
    assert client.put("/me/ui", json={"home": "list"}, headers=h).status_code == 200


def test_list_width_is_personal_and_keeps_the_home_choice(client, admin_token, manager_token):
    h = _auth(manager_token)
    saved = client.put("/me/ui", json={"list_width": 420}, headers=h)
    assert saved.status_code == 200
    assert saved.json() == {"home": "list", "list_width": 420, "tasks_height": None}
    page = client.get("/", headers=h).text
    assert 'class="bg-light projects-dashboard"' not in page
    assert 'style="--board-list-width: 420px"' in page
    assert client.put("/me/ui", json={"home": "dashboard"}, headers=h).json()["list_width"] == 420
    assert client.put("/me/ui", json={"home": "list"}, headers=h).json()["list_width"] == 420
    assert client.put("/me/ui", json={"list_width": 100}, headers=h).status_code == 422
    assert client.get("/me/ui", headers=h).json()["list_width"] == 420
    assert client.get("/me/ui", headers=_auth(admin_token)).json()["list_width"] is None


def test_tasks_height_is_personal_and_keeps_other_settings(client, admin_token, manager_token):
    h = _auth(manager_token)
    saved = client.put("/me/ui", json={"tasks_height": 320}, headers=h)
    assert saved.status_code == 200
    assert saved.json() == {"home": "list", "list_width": None, "tasks_height": 320}
    page = client.get("/", headers=h).text
    assert 'style="--board-tasks-height: 320px"' in page
    assert client.put("/me/ui", json={"list_width": 420}, headers=h).json()["tasks_height"] == 320
    assert client.put("/me/ui", json={"home": "dashboard"}, headers=h).json()["tasks_height"] == 320
    assert client.put("/me/ui", json={"tasks_height": 80}, headers=h).status_code == 422
    assert client.get("/me/ui", headers=h).json()["tasks_height"] == 320
    assert client.get("/me/ui", headers=_auth(admin_token)).json()["tasks_height"] is None


def test_dashboard_is_personal_and_keeps_order(client, admin_token, manager_token, user_token, member_card):
    h = _auth(manager_token)
    first = client.post("/projects", json={"name": "Доска-1"}, headers=h).json()["id"]
    second = client.post("/projects", json={"name": "Доска-2"}, headers=h).json()["id"]
    third = client.post("/projects", json={"name": "Доска-3"}, headers=h).json()["id"]
    assert client.get("/me/dashboard", headers=h).json() == {"project_ids": []}

    saved = client.put("/me/dashboard", json={"project_ids": [third, first]}, headers=h)
    assert saved.status_code == 200
    assert saved.json()["project_ids"] == [third, first]
    assert client.get("/me/dashboard", headers=h).json()["project_ids"] == [third, first]

    assert client.get("/me/dashboard", headers=_auth(admin_token)).json()["project_ids"] == []
    admin_saved = client.put("/me/dashboard", json={"project_ids": [second]}, headers=_auth(admin_token))
    assert admin_saved.status_code == 200
    assert client.get("/me/dashboard", headers=h).json()["project_ids"] == [third, first]

    denied = client.put("/me/dashboard", json={"project_ids": [first]}, headers=_auth(user_token))
    assert denied.status_code == 400
    from app.app.db import SessionLocal
    from app.app.models.user import User

    db = SessionLocal()
    try:
        uid = db.query(User).filter(User.telegram_id == "tg-user").one().id
    finally:
        db.close()
    granted = client.post(
        f"/projects/{first}/members",
        json={"user_id": uid, "role": "user", **member_card},
        headers=h,
    )
    assert granted.status_code == 200
    assert client.put("/me/dashboard", json={"project_ids": [first]}, headers=_auth(user_token)).status_code == 200
    assert client.put("/me/dashboard", json={"project_ids": [second]}, headers=_auth(user_token)).status_code == 400
    assert client.put("/me/dashboard", json={"project_ids": [first, first]}, headers=h).status_code == 400
    assert client.get("/me/dashboard", headers=h).json()["project_ids"] == [third, first]

    cleared = client.put("/me/dashboard", json={"project_ids": []}, headers=h)
    assert cleared.status_code == 200 and cleared.json()["project_ids"] == []


def test_dashboard_drops_deleted_and_inactive_project(client, manager_token):
    from app.app.db import SessionLocal
    from app.app.models.project import Project
    from app.app.models.user_dashboard import UserDashboardProject

    h = _auth(manager_token)
    pid = client.post("/projects", json={"name": "На доске"}, headers=h).json()["id"]
    other = client.post("/projects", json={"name": "Рядом"}, headers=h).json()["id"]
    assert client.put("/me/dashboard", json={"project_ids": [pid, other]}, headers=h).status_code == 200

    db = SessionLocal()
    try:
        row = db.get(Project, other)
        row.is_active = False
        db.commit()
    finally:
        db.close()
    assert client.get("/me/dashboard", headers=h).json()["project_ids"] == [pid]
    assert client.put("/me/dashboard", json={"project_ids": [other]}, headers=h).status_code == 400

    assert client.delete(f"/projects/{pid}", headers=h).status_code == 204
    assert client.get("/me/dashboard", headers=h).json()["project_ids"] == []
    db = SessionLocal()
    try:
        left = db.query(UserDashboardProject).filter(UserDashboardProject.project_id == pid).count()
    finally:
        db.close()
    assert left == 0


def test_list_projects_creator_only_for_admin(client, admin_token, manager_token):
    from app.app.db import SessionLocal
    from app.app.models.project import Project
    from app.app.models.user_profile import UserProfile

    client.post("/projects", json={"name": "ByManager"}, headers={"Authorization": f"Bearer {manager_token}"})
    db = SessionLocal()
    try:
        owner = db.query(Project).filter(Project.name == "ByManager").one().created_by
        db.merge(UserProfile(user_id=owner, last_name="Петров", first_name="Олег"))
        db.commit()
    finally:
        db.close()
    rows = client.get("/projects", headers={"Authorization": f"Bearer {admin_token}"}).json()
    assert next(p for p in rows if p["name"] == "ByManager")["creator_name"] == "Петров Олег"
    rows = client.get("/projects", headers={"Authorization": f"Bearer {manager_token}"}).json()
    assert next(p for p in rows if p["name"] == "ByManager")["creator_name"] is None
