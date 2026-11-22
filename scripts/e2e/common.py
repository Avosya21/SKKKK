"""Общие хелперы живого e2e (песочница). Логин — через scripts/smoke/common."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
FIXTURES = HERE / "fixtures"
SMOKE_DIR = ROOT / "scripts" / "smoke"

import importlib.util

_spec = importlib.util.spec_from_file_location("smoke_common", SMOKE_DIR / "common.py")
assert _spec and _spec.loader
_smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_smoke)

BASE = _smoke.BASE
LOGIN = _smoke.LOGIN
PID = _smoke.PID
api_login = _smoke.api_login
check = _smoke.check
personal_password = _smoke.personal_password
wait_until = _smoke.wait_until

# Уникальный якорь в фикстурах документов — для search/TZ/chat.
ANCHOR = "E2E_ACCEPTANCE_TOKEN_7F3A"
DOC_SOURCE = "document"
AUDIO_SOURCE = "call"

TERMINAL_OK = frozenset({"done", "speakers_pending", "speakers_confirmed", "transcript_ready"})
TERMINAL_FAIL = frozenset(
    {
        "error",
        "failed",
        "asr_failed",
        "cancelled",
        "canceled",
    }
)


def fail(message: str) -> None:
    print("FAIL " + message)
    raise AssertionError(message)


def ok(message: str) -> None:
    print("PASS " + message)


def list_artifacts(client: httpx.Client) -> list[dict]:
    r = client.get(f"/projects/{PID}/artifacts")
    r.raise_for_status()
    data = r.json()
    if isinstance(data, list):
        return data
    return data.get("artifacts") or data.get("items") or []


def get_artifact(client: httpx.Client, artifact_id: str) -> dict | None:
    for row in list_artifacts(client):
        if row.get("artifact_id") == artifact_id or str(row.get("id")) == artifact_id:
            return row
    return None


def upload_file(
    client: httpx.Client,
    path: Path,
    *,
    source_type: str,
    content_type: str | None = None,
) -> dict:
    mime = content_type or "application/octet-stream"
    with path.open("rb") as fh:
        r = client.post(
            f"/projects/{PID}/upload",
            files=[("files", (path.name, fh, mime))],
            data={"source_type": source_type},
            timeout=120,
        )
    if r.status_code not in (200, 201, 202):
        fail(f"upload {path.name}: HTTP {r.status_code} {r.text[:400]}")
    body = r.json()
    row = body[0] if isinstance(body, list) else body
    ok(f"upload {path.name} → {row.get('artifact_id')} ({row.get('status')})")
    return row


def delete_artifact(client: httpx.Client, artifact_id: str) -> None:
    r = client.delete(f"/projects/{PID}/artifacts/{artifact_id}")
    if r.status_code not in (200, 204, 404):
        fail(f"delete {artifact_id}: HTTP {r.status_code} {r.text[:200]}")
    ok(f"delete {artifact_id}")


def wait_status(
    client: httpx.Client,
    artifact_id: str,
    want: set[str] | frozenset[str],
    *,
    timeout_sec: float,
    label: str,
) -> dict:
    """Ждём один из ``want``. Любой другой терминальный статус (error/done вне want) — FAIL сразу."""
    deadline = time.time() + timeout_sec
    last = ""
    terminal_other = TERMINAL_OK | TERMINAL_FAIL
    while time.time() < deadline:
        row = get_artifact(client, artifact_id) or {}
        status = str(row.get("status") or "")
        if status != last:
            print(f"  … {label} {artifact_id}: {status}")
            last = status
        if status in TERMINAL_FAIL:
            fail(f"{label} {artifact_id} → {status}: {row.get('error_message') or row}")
        if status in want:
            ok(f"{label} {artifact_id} → {status}")
            return row
        # done/speakers_* не из want — дыра или дедуп старого артефакта, не крутить 90 мин
        if status in terminal_other:
            fail(
                f"{label} {artifact_id} → неожиданный терминальный статус {status!r}, "
                f"ждали {sorted(want)}; возможно дедуп старого файла или сломанный шлюз"
            )
        time.sleep(5)
    fail(f"{label} {artifact_id}: timeout {timeout_sec:.0f}s, last={last}")


def wait_project_task_idle(client: httpx.Client, *, timeout_sec: float = 900) -> None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        r = client.get(f"/projects/{PID}/tasks/active")
        r.raise_for_status()
        tasks = r.json().get("tasks") or []
        if not tasks:
            ok("project tasks idle")
            return
        kinds = [t.get("task_type") or t.get("type") or "?" for t in tasks]
        print(f"  … active tasks: {kinds}")
        time.sleep(3)
    fail(f"project tasks still active after {timeout_sec:.0f}s")


def artifact_dir(artifact_id: str) -> Path:
    return ROOT / "data" / "projects" / str(PID) / "artifacts" / artifact_id


def write_state(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_state(path: Path) -> dict:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def state_path() -> Path:
    name = os.environ.get("E2E_STATE", "e2e_state.json")
    return HERE / name


def ensure_login(role: str = "admin") -> tuple[httpx.Client, dict]:
    return api_login(role)
