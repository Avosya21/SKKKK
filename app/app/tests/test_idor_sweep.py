"""Обход всех маршрутов с чужими идентификаторами (пункт 4.7): пользователь без доступа к проекту не получает 2xx."""
from __future__ import annotations

import re
import uuid

from app.app.tests.conftest import MANAGER_PASS, USER_PASS

PUBLIC_PREFIXES = ("/api/meetings/public", "/m/", "/health", "/login", "/static", "/auth", "/docs", "/openapi", "/redoc",
                   "/meeting.webmanifest", "/.well-known")
# Маршруты, где 2xx для постороннего допустим по замыслу (нет данных проекта / собственные данные пользователя):
ALLOWED = {("GET", "/projects"), ("POST", "/projects"), ("GET", "/api/chat-status")}


def _login(client, name, tg, pw):
    r = client.post("/auth/login", json={"name": name, "telegram_id": tg, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def _seed(client, manager):
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.user import User

    pid = client.post("/projects", json={"name": "Idor-" + uuid.uuid4().hex[:6]}, headers=manager).json()["id"]
    mid = client.post(f"/projects/{pid}/meetings", headers=manager).json()["id"]
    db = SessionLocal()
    try:
        owner = db.query(User).filter(User.role != "user").first()
        tag = uuid.uuid4().hex[:10]
        art = Artifact(project_id=pid, artifact_id=f"idor-{tag}", file_type="audio", status="done", original_filename="a.wav",
                       stored_filename="a.wav", file_path="a.wav", source_type="call", sha256=tag.ljust(64, "0"), user_id=owner.id)
        db.add(art)
        db.commit()
        return pid, mid, art.artifact_id, art.id
    finally:
        db.close()


def test_foreign_ids_never_return_2xx_for_outsider(client, manager_token):
    manager = {"Authorization": f"Bearer {manager_token}"}
    pid, mid, art_id, art_pk = _seed(client, manager)
    for label, outsider in (
        ("user", _login(client, "User", "tg-outsider", USER_PASS)),
        ("чужой менеджер", _login(client, "Bootstrap manager", "tg-other-mgr", MANAGER_PASS)),
    ):
        _sweep(client, outsider, label, pid, mid, art_id)


def _sweep(client, outsider, label, pid, mid, art_id):
    values = {"project_id": pid, "meeting_id": mid, "artifact_id": art_id, "upload_id": "x", "token_id": 1, "entity_id": 1,
              "conflict_id": 1, "link_id": 1, "note_id": 1, "member_id": 1, "user_id": 1, "job_id": 1, "task_id": 1,
              "frame_id": 1, "profile_id": 1, "speaker_id": 1, "identity": "member:1", "path": "x", "kind": "x", "name": "x",
              "filename": "a.wav", "track": "x", "version": "x", "stamp": "x", "run_id": "x", "file_name": "a.wav"}
    leaks = []
    checked = 0
    from app.app.main import app as fastapi_app

    for path, ops in fastapi_app.openapi()["paths"].items():
        methods = {m.upper() for m in ops}
        if any(path.startswith(p) for p in PUBLIC_PREFIXES):
            continue
        params = re.findall(r"\{(\w+)(?::[^}]*)?\}", path)
        if not params:
            continue
        url = re.sub(r"\{(\w+)(?::[^}]*)?\}", lambda m: str(values.get(m.group(1), 1)), path)
        for method in methods - {"HEAD", "OPTIONS"}:
            if (method, path) in ALLOWED:
                continue
            checked += 1
            kwargs = {"headers": outsider}
            if method in ("POST", "PUT", "PATCH"):
                kwargs["json"] = {}
            resp = client.request(method, url, **kwargs)
            if 200 <= resp.status_code < 300 and any(p in ("project_id", "meeting_id", "artifact_id") for p in params):
                leaks.append(f"[{label}] {method} {path} -> {resp.status_code}")
    assert checked > 30, f"слишком мало проверенных маршрутов: {checked}"
    assert not leaks, "Посторонний получил 2xx на чужие данные:\n" + "\n".join(leaks)
