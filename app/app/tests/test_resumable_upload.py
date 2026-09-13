from __future__ import annotations

import hashlib

from app.app.config import get_settings


def _project(client, manager_token):
    r = client.post(
        "/projects",
        json={"name": "Resumable upload"},
        headers={"Authorization": f"Bearer {manager_token}"},
    )
    return r.json()["id"]


def _auth(user_token):
    return {"Authorization": f"Bearer {user_token}"}


def test_resumable_upload_full_flow(client, manager_token, user_token, monkeypatch):
    monkeypatch.setenv("UPLOAD_CHUNK_SIZE", "1024")
    get_settings.cache_clear()
    pid = _project(client, manager_token)
    payload = b"a" * 2048  # 2 chunks
    init = client.post(
        f"/projects/{pid}/uploads/init",
        headers=_auth(manager_token),
        json={
            "filename": "clip.bin",
            "size": len(payload),
            "source_type": "call",
            "client_key": "clip.bin:2048:1",
        },
    )
    assert init.status_code == 200, init.text
    session = init.json()
    assert session["total_chunks"] == 2
    assert session["missing"] == [0, 1]
    upload_id = session["upload_id"]
    assert session["chunk_size"] == 1024

    r0 = client.put(
        f"/projects/{pid}/uploads/{upload_id}/chunks/0",
        headers=_auth(manager_token),
        content=payload[:1024],
    )
    assert r0.status_code == 200
    assert r0.json()["received"] == [0]

    r1 = client.put(
        f"/projects/{pid}/uploads/{upload_id}/chunks/1",
        headers=_auth(manager_token),
        content=payload[1024:],
    )
    assert r1.status_code == 200
    assert r1.json()["missing"] == []

    done = client.post(
        f"/projects/{pid}/uploads/{upload_id}/complete",
        headers=_auth(manager_token),
    )
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["original_filename"] == "clip.bin"
    assert body["status"] == "uploaded"
    assert body["source_type"] == "call"
    assert hashlib.sha256(payload).hexdigest()


def test_resumable_upload_resumes_by_client_key(client, manager_token, user_token, monkeypatch):
    monkeypatch.setenv("UPLOAD_CHUNK_SIZE", "1024")
    get_settings.cache_clear()
    pid = _project(client, manager_token)
    payload = b"b" * 2048
    key = "movie.mp4:2048:99"
    init1 = client.post(
        f"/projects/{pid}/uploads/init",
        headers=_auth(manager_token),
        json={"filename": "movie.mp4", "size": 2048, "source_type": "call", "client_key": key},
    )
    assert init1.status_code == 200, init1.text
    init1 = init1.json()
    upload_id = init1["upload_id"]
    client.put(
        f"/projects/{pid}/uploads/{upload_id}/chunks/0",
        headers=_auth(manager_token),
        content=payload[:1024],
    )
    init2 = client.post(
        f"/projects/{pid}/uploads/init",
        headers=_auth(manager_token),
        json={"filename": "movie.mp4", "size": 2048, "source_type": "call", "client_key": key},
    ).json()
    assert init2["upload_id"] == upload_id
    assert init2["received"] == [0]
    assert init2["missing"] == [1]

    client.put(
        f"/projects/{pid}/uploads/{upload_id}/chunks/1",
        headers=_auth(manager_token),
        content=payload[1024:],
    )
    done = client.post(
        f"/projects/{pid}/uploads/{upload_id}/complete",
        headers=_auth(manager_token),
    )
    assert done.status_code == 200


def test_resumable_complete_rejects_missing_chunks(client, manager_token, user_token, monkeypatch):
    monkeypatch.setenv("UPLOAD_CHUNK_SIZE", "1024")
    get_settings.cache_clear()
    pid = _project(client, manager_token)
    init = client.post(
        f"/projects/{pid}/uploads/init",
        headers=_auth(manager_token),
        json={"filename": "a.bin", "size": 2048, "source_type": "note"},
    )
    assert init.status_code == 200, init.text
    init = init.json()
    upload_id = init["upload_id"]
    client.put(
        f"/projects/{pid}/uploads/{upload_id}/chunks/0",
        headers=_auth(manager_token),
        content=b"1" * 1024,
    )
    r = client.post(
        f"/projects/{pid}/uploads/{upload_id}/complete",
        headers=_auth(manager_token),
    )
    assert r.status_code == 409
    assert r.json()["detail"]["missing"] == [1]


def test_resumable_rejects_oversized_init(client, manager_token, user_token, monkeypatch):
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "10")
    get_settings.cache_clear()
    pid = _project(client, manager_token)
    r = client.post(
        f"/projects/{pid}/uploads/init",
        headers=_auth(manager_token),
        json={"filename": "big.bin", "size": 20, "source_type": "note"},
    )
    assert r.status_code == 413
