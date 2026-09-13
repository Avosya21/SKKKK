from __future__ import annotations

import io

from app.app.config import get_settings
from app.app.services.storage import raw_input_dir


def _project(client, manager_token):
    r = client.post(
        "/projects",
        json={"name": "Upload test"},
        headers={"Authorization": f"Bearer {manager_token}"},
    )
    return r.json()["id"]


def test_upload_creates_artifact_and_job(client, manager_token, user_token):
    pid = _project(client, manager_token)
    data = {"source_type": (None, "note")}
    files = {"files": ("hello.txt", io.BytesIO(b"hello world"), "text/plain")}
    r = client.post(
        f"/projects/{pid}/upload",
        headers={"Authorization": f"Bearer {manager_token}"},
        data={"source_type": "note"},
        files=[("files", ("hello.txt", io.BytesIO(b"hello world"), "text/plain"))],
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["status"] == "uploaded"


def test_upload_rejects_oversized_file_413(client, manager_token, user_token, monkeypatch):
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "10")
    from app.app.config import get_settings

    get_settings.cache_clear()
    pid = _project(client, manager_token)
    r = client.post(
        f"/projects/{pid}/upload",
        headers={"Authorization": f"Bearer {manager_token}"},
        data={"source_type": "note"},
        files=[("files", ("big.bin", io.BytesIO(b"x" * 20), "application/octet-stream"))],
    )
    assert r.status_code == 413


def test_upload_detects_duplicate_by_sha256(client, manager_token, user_token):
    pid = _project(client, manager_token)
    payload = b"same content"
    for _ in range(2):
        r = client.post(
            f"/projects/{pid}/upload",
            headers={"Authorization": f"Bearer {manager_token}"},
            data={"source_type": "note"},
            files=[("files", ("a.txt", io.BytesIO(payload), "text/plain"))],
        )
        assert r.status_code == 200
    arts = client.get(
        f"/projects/{pid}/artifacts",
        headers={"Authorization": f"Bearer {manager_token}"},
    ).json()
    assert len(arts) == 1
    assert arts[0]["speakers_match_available"] is False
    assert arts[0]["speakers_confirm_ready"] is False
    r2 = client.post(
        f"/projects/{pid}/upload",
        headers={"Authorization": f"Bearer {manager_token}"},
        data={"source_type": "note"},
        files=[("files", ("b.txt", io.BytesIO(payload), "text/plain"))],
    )
    assert r2.json()[0]["duplicate"] is True


def test_upload_generates_safe_stored_filename(client, manager_token, user_token, tmp_data):
    pid = _project(client, manager_token)
    name = "отчёт (финал)#1.pdf"
    client.post(
        f"/projects/{pid}/upload",
        headers={"Authorization": f"Bearer {manager_token}"},
        data={"source_type": "document"},
        files=[("files", (name, io.BytesIO(b"%PDF-1.4"), "application/pdf"))],
    )
    settings = get_settings()
    folder = raw_input_dir(settings, pid, "document")
    files = list(folder.glob("art_*"))
    assert len(files) == 1
    assert "#" not in files[0].name
    assert "отч" not in files[0].name


def test_upload_path_traversal_blocked(client, manager_token, user_token, tmp_data):
    pid = _project(client, manager_token)
    client.post(
        f"/projects/{pid}/upload",
        headers={"Authorization": f"Bearer {manager_token}"},
        data={"source_type": "note"},
        files=[("files", ("../../evil.txt", io.BytesIO(b"ok"), "text/plain"))],
    )
    settings = get_settings()
    evil = settings.data_dir / "evil.txt"
    assert not evil.exists()


def test_upload_sets_source_type_and_weight(client, manager_token, user_token):
    pid = _project(client, manager_token)
    r = client.post(
        f"/projects/{pid}/upload",
        headers={"Authorization": f"Bearer {manager_token}"},
        data={"source_type": "call"},
        files=[("files", ("x.wav", io.BytesIO(b"RIFF"), "audio/wav"))],
    )
    assert r.json()[0]["source_type"] == "call"


def test_user_cannot_upload_to_foreign_project(client, manager_token, user_token):
    pid = _project(client, manager_token)
    r = client.post(
        f"/projects/{pid}/upload",
        headers={"Authorization": f"Bearer {user_token}"},
        data={"source_type": "note"},
        files=[("files", ("n.txt", io.BytesIO(b"1"), "text/plain"))],
    )
    assert r.status_code == 404
