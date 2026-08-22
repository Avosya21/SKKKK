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


def test_only_admin_can_archive_project(client, admin_token, manager_token, user_token):
    r = client.post(
        "/projects",
        json={"name": "Archive me"},
        headers={"Authorization": f"Bearer {manager_token}"},
    )
    pid = r.json()["id"]
    assert client.delete(f"/projects/{pid}", headers={"Authorization": f"Bearer {manager_token}"}).status_code == 403
    assert client.delete(f"/projects/{pid}", headers={"Authorization": f"Bearer {user_token}"}).status_code == 403
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


def test_list_projects_returns_description(client, manager_token):
    client.post(
        "/projects",
        json={"name": "WithDesc", "description": "Кратко о проекте"},
        headers={"Authorization": f"Bearer {manager_token}"},
    )
    rows = client.get("/projects", headers={"Authorization": f"Bearer {manager_token}"}).json()
    match = next(p for p in rows if p["name"] == "WithDesc")
    assert match["description"] == "Кратко о проекте"
