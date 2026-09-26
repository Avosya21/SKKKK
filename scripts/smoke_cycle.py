"""Прогон полного цикла через HTTP API: вход, проект, загрузка, обработка.

Пароль берётся из переменной окружения `SMOKE_PASSWORD`, чтобы не попадать в
историю команд. Скрипт ничего не пишет в боевую БД, если запущен против API,
поднятого с отдельными `DATA_DIR` и `DATABASE_URL`.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import httpx

TERMINAL_STATUSES = {"done", "error"}


def _say(message: str) -> None:
    print(f"[cycle] {message}", flush=True)


def login(client: httpx.Client, base_url: str, name: str, telegram_id: str, password: str) -> str:
    response = client.post(
        f"{base_url}/auth/login",
        json={"name": name, "telegram_id": telegram_id, "password": password},
    )
    response.raise_for_status()
    data = response.json()
    if data.get("step") == "personal":
        personal = os.environ.get("SMOKE_PERSONAL_PW", "")
        if not personal:
            raise SystemExit("вход требует личный пароль: задайте SMOKE_PERSONAL_PW")
        response = client.post(f"{base_url}/auth/login/personal", json={"ticket": data["ticket"], "password": personal})
        response.raise_for_status()
        data = response.json()
    if data.get("step"):
        raise SystemExit(f"у логина {name} нет личного пароля или карточки: войдите им один раз в браузере")
    _say(f"вход выполнен: {data['name']} role={data['role']}")
    return data["access_token"]


def create_project(client: httpx.Client, base_url: str, headers: dict, name: str) -> int:
    response = client.post(
        f"{base_url}/projects",
        headers=headers,
        json={"name": name, "description": "smoke cycle"},
    )
    response.raise_for_status()
    project = response.json()
    _say(f"проект создан: id={project['id']} name={project['name']}")
    return project["id"]


def upload(client: httpx.Client, base_url: str, headers: dict, project_id: int, path: Path) -> None:
    with path.open("rb") as fh:
        response = client.post(
            f"{base_url}/projects/{project_id}/upload",
            headers=headers,
            files={"files": (path.name, fh, "text/plain")},
            data={"source_type": "document"},
        )
    response.raise_for_status()
    for item in response.json():
        _say(f"загружен {path.name}: artifact={item['artifact_id']} status={item['status']}")


def wait_for_processing(
    client: httpx.Client,
    base_url: str,
    headers: dict,
    project_id: int,
    expected: int,
    timeout_sec: int,
) -> list[dict]:
    deadline = time.time() + timeout_sec
    seen: dict[str, str] = {}
    while time.time() < deadline:
        response = client.get(f"{base_url}/projects/{project_id}/artifacts", headers=headers)
        response.raise_for_status()
        artifacts = response.json()
        for art in artifacts:
            if seen.get(art["artifact_id"]) != art["status"]:
                seen[art["artifact_id"]] = art["status"]
                _say(f"{art['original_filename']}: {art['status']}")
        finished = [a for a in artifacts if a["status"] in TERMINAL_STATUSES]
        if len(artifacts) >= expected and len(finished) == len(artifacts):
            return artifacts
        time.sleep(2)
    raise TimeoutError(f"обработка не завершилась за {timeout_sec} с")


def wait_for_tz(client: httpx.Client, base_url: str, headers: dict, project_id: int, timeout_sec: int) -> str:
    """Дождаться пересборки ТЗ.

    Она стартует уже после того, как последний артефакт получил `done`, и Gemma
    пишет её несколько минут, поэтому мгновенный запрос всегда возвращал пустоту.
    """
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        response = client.get(f"{base_url}/projects/{project_id}/tz/content", headers=headers)
        if response.status_code == 200:
            body = response.json().get("content", "")
            if body.strip():
                return body
        time.sleep(5)
    return ""


def report(client: httpx.Client, base_url: str, headers: dict, project_id: int, artifacts: list[dict]) -> int:
    _say("--- итог ---")
    failed = 0
    for art in artifacts:
        marker = "OK " if art["status"] == "done" else "FAIL"
        note = f" error={art.get('error_message')}" if art.get("error_message") else ""
        _say(f"{marker} {art['original_filename']} status={art['status']}{note}")
        if art["status"] != "done":
            failed += 1

    total_entities = 0
    for art in artifacts:
        response = client.get(
            f"{base_url}/projects/{project_id}/artifacts/{art['id']}/entities",
            headers=headers,
        )
        if response.status_code == 200:
            rows = response.json()
            total_entities += len(rows)
            _say(f"{art['original_filename']}: сущностей {len(rows)}")
    _say(f"всего сущностей в базе знаний проекта: {total_entities}")

    graph = client.get(f"{base_url}/projects/{project_id}/graph/data", headers=headers)
    if graph.status_code == 200:
        payload = graph.json()
        _say(f"граф проекта: узлов {len(payload['nodes'])}, связей {len(payload['edges'])}")

    conflicts = client.get(f"{base_url}/projects/{project_id}/conflicts", headers=headers)
    if conflicts.status_code == 200:
        _say(f"конфликтов на разбор: {len(conflicts.json())}")

    search = client.get(
        f"{base_url}/projects/{project_id}/search",
        headers=headers,
        params={"q": "срок сдачи проекта"},
    )
    if search.status_code == 200:
        hits = search.json().get("results", [])
        _say(f"семантический поиск вернул совпадений: {len(hits)}")
        for hit in hits[:3]:
            snippet = hit.get("text", "")[:80].replace("\n", " ")
            _say(f"  score={hit.get('score'):.3f} {snippet}")

    return failed


def main() -> int:
    parser = argparse.ArgumentParser(description="Полный цикл SKKKK через API")
    parser.add_argument("--base-url", default="http://127.0.0.1:1113")
    parser.add_argument("--project-name", default="Smoke Cycle")
    parser.add_argument("--telegram-id", default="tg-smoke")
    parser.add_argument("--name", default="Smoke Runner")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--tz-timeout", type=int, default=900, help="ожидание пересборки ТЗ")
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args()

    password = os.environ.get("SMOKE_PASSWORD")
    if not password:
        print("SMOKE_PASSWORD не задан", file=sys.stderr)
        return 2

    with httpx.Client(timeout=120) as client:
        token = login(client, args.base_url, args.name, args.telegram_id, password)
        headers = {"Authorization": f"Bearer {token}"}
        project_id = create_project(client, args.base_url, headers, args.project_name)
        for path in args.files:
            upload(client, args.base_url, headers, project_id, path)
        artifacts = wait_for_processing(
            client, args.base_url, headers, project_id, len(args.files), args.timeout
        )
        failed = report(client, args.base_url, headers, project_id, artifacts)

        _say("жду пересборку ТЗ...")
        body = wait_for_tz(client, args.base_url, headers, project_id, args.tz_timeout)
        if body:
            _say(f"ТЗ проекта: {len(body)} символов")
        else:
            _say("ТЗ пустое: пересборка не завершилась за отведённое время")
            failed += 1

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
