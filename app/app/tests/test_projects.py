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


def test_list_projects_returns_description(client, manager_token):
    client.post(
        "/projects",
        json={"name": "WithDesc", "description": "Кратко о проекте"},
        headers={"Authorization": f"Bearer {manager_token}"},
    )
    rows = client.get("/projects", headers={"Authorization": f"Bearer {manager_token}"}).json()
    match = next(p for p in rows if p["name"] == "WithDesc")
    assert match["description"] == "Кратко о проекте"
