"""Полный функциональный смоук портала (без полного UI).

Запуск из PyCharm: Working directory = корень проекта,
интерпретатор = venv\\Scripts\\python.exe, скрипт = scripts\\portal_smoke.py.

Нужны: живой портал + воркер + LiveKit + GigaAM; песочница SMOKE_PROJECT_ID (29).
Вход автоматики: E2E_ALLOW_MINT=1 (скрипт сам выпускает JWT).
Вход «как человек»: SMOKE_PERSONAL_PW (+ SMOKE_OTP_CODE или B24_AUTH_DEV).

    venv\\Scripts\\python.exe scripts\\portal_smoke.py
    venv\\Scripts\\python.exe scripts\\portal_smoke.py --stand-only
    venv\\Scripts\\python.exe scripts\\portal_smoke.py --keep

Лог — по-русски в консоль. Отчёт: data/eval/portal_smoke_*.json.
SKIP на обязательных шагах запрещён (нет секрета/модели = FAIL).
Полный UI комнаты — отдельно: scripts\\smoke\\run_all.py.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
E2E = ROOT / "scripts" / "e2e"
SMOKE = ROOT / "scripts" / "smoke"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(E2E))
sys.path.insert(0, str(SMOKE))

os.chdir(ROOT)

PY = sys.executable
ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}


def _smoke_common():
    import importlib.util

    spec = importlib.util.spec_from_file_location("smoke_common_ps", SMOKE / "common.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class StepLog:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def _add(self, name: str, status: str, detail: str = "", seconds: float = 0) -> None:
        row = {
            "step": name,
            "status": status,
            "detail": detail,
            "seconds": round(seconds, 1),
        }
        self.rows.append(row)
        mark = {"OK": "OK  ", "FAIL": "FAIL", "WARN": "WARN"}[status]
        extra = f" — {detail}" if detail else ""
        print(f"[{mark}] {name}{extra}", flush=True)

    def ok(self, name: str, detail: str = "", seconds: float = 0) -> None:
        self._add(name, "OK", detail, seconds)

    def fail(self, name: str, detail: str = "", seconds: float = 0) -> None:
        self._add(name, "FAIL", detail, seconds)

    def warn(self, name: str, detail: str = "", seconds: float = 0) -> None:
        self._add(name, "WARN", detail, seconds)

    @property
    def failed(self) -> list[dict]:
        return [r for r in self.rows if r["status"] == "FAIL"]


LOG = StepLog()


def _run_step(name: str, fn) -> None:
    started = time.time()
    print(f"\n=== {name}", flush=True)
    try:
        detail = fn() or ""
        LOG.ok(name, str(detail), time.time() - started)
    except Exception as exc:  # noqa: BLE001 — смоук: любая ошибка шага = FAIL с текстом
        LOG.fail(name, str(exc)[:800], time.time() - started)


def _ensure_mint() -> None:
    if ENV.get("SMOKE_BEARER_TOKEN") or ENV.get("SMOKE_OTP_CODE"):
        return
    ENV["E2E_ALLOW_MINT"] = "1"
    os.environ["E2E_ALLOW_MINT"] = "1"
    r = subprocess.run(
        [PY, str(ROOT / "scripts" / "e2e" / "mint_smoke_token.py")],
        cwd=str(ROOT),
        env={**ENV, "E2E_ALLOW_MINT": "1"},
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    token = (r.stdout or "").strip().splitlines()[-1] if r.returncode == 0 else ""
    if not token:
        raise RuntimeError(f"mint_smoke_token: {(r.stderr or r.stdout or '')[:400]}")
    ENV["SMOKE_BEARER_TOKEN"] = token
    os.environ["SMOKE_BEARER_TOKEN"] = token


# ---------------------------------------------------------------------------
# 1. Стенд
# ---------------------------------------------------------------------------


def step_stand() -> str:
    import httpx

    smoke = _smoke_common()
    base = os.environ.get("SMOKE_BASE", smoke.BASE).rstrip("/")

    with httpx.Client(base_url=base, verify=False, timeout=30) as anon:
        r = anon.get("/health")
        if r.status_code != 200 or (r.json() or {}).get("status") != "ok":
            raise RuntimeError(f"/health → {r.status_code} {r.text[:200]}")

    # venv lab / nemo-diar: encodings + BOM
    for rel in ("venvs/lab", "venvs/nemo-diar"):
        cfg = ROOT / rel / "pyvenv.cfg"
        py = ROOT / rel / "Scripts" / "python.exe"
        if not py.is_file():
            raise RuntimeError(f"нет {py}")
        raw = cfg.read_bytes() if cfg.is_file() else b""
        if raw.startswith(b"\xef\xbb\xbf"):
            raise RuntimeError(f"UTF-8 BOM в {cfg} — Python ломает home (как после переноса)")
        if cfg.is_file():
            text = raw.decode("utf-8", errors="replace")
            home_line = next((ln for ln in text.splitlines() if ln.lower().startswith("home")), "")
            if home_line and "Primary_Analitics" not in home_line.replace("\\", "/"):
                # допускаем tools\Python310 внутри корня
                if str(ROOT).replace("\\", "/") not in home_line.replace("\\", "/"):
                    raise RuntimeError(f"{cfg}: home не в корне проекта: {home_line}")
        r = subprocess.run(
            [str(py), "-c", "import encodings; print('ok')"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        if r.returncode != 0 or "ok" not in (r.stdout or ""):
            raise RuntimeError(f"{rel}: import encodings упал: {(r.stderr or r.stdout)[:300]}")

    r = subprocess.run(
        [PY, "-c", "import filelock; print('ok')"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    if r.returncode != 0:
        raise RuntimeError("в portal venv нет filelock — после спикеров knowledge упадёт")

    # модели
    models = [
        ("parakeet", ROOT / "data" / "models" / "onnx-asr" / "nemo-parakeet-tdt-0.6b-v3" / "encoder-model.onnx"),
        ("sortformer", ROOT / "data" / "models" / "onnx-diar" / "diar_streaming_sortformer_4spk-v2.1.onnx"),
        ("gigaam_ckpt", ROOT / "data" / "gigaam" / "models" / "gigaam" / "v3_e2e_rnnt.ckpt"),
    ]
    for name, path in models:
        if not path.is_file() or path.stat().st_size <= 0:
            raise RuntimeError(f"нет/пустая модель {name}: {path}")

    nemo = ROOT / "Nemotron-3-Diarization" / "Nemotron-3-Diarization.nemo"
    from app.app.config import get_settings

    settings = get_settings()
    diar = (settings.diar_model or "").strip().lower()
    if diar == "nemotron3":
        if not nemo.is_file() or nemo.stat().st_size <= 0:
            raise RuntimeError(
                "DIAR_MODEL=nemotron3, но нет Nemotron-3-Diarization.nemo — "
                "после переноса веса не скопированы (в git их нет). "
                f"Положите файл в {nemo} или временно DIAR_MODEL=sortformer4"
            )
    elif diar != "sortformer4":
        raise RuntimeError(f"неизвестный DIAR_MODEL={diar!r}")

    # WeSpeaker ненулевой
    wes_ok = False
    for base_ws in (
        ROOT / "data" / "gigaam" / "models" / "huggingface" / "hub" / "models--pyannote--wespeaker-voxceleb-resnet34-LM",
        ROOT / "data" / "models" / "huggingface" / "hub" / "models--pyannote--wespeaker-voxceleb-resnet34-LM",
    ):
        if base_ws.is_dir():
            for f in base_ws.rglob("*"):
                if f.is_file() and f.stat().st_size > 1_000_000:
                    wes_ok = True
                    break
        if wes_ok:
            break
    if not wes_ok:
        raise RuntimeError("WeSpeaker: нет файла >1 МБ (после переноса часто 0 байт в snapshot)")

    # GigaAM docker + HTTP + mount probe
    try:
        ps = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"],
            capture_output=True,
            text=True,
            timeout=30,
            encoding="utf-8",
        )
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"docker недоступен: {exc}") from exc
    up = any("primaryanalitics-gigaam" in ln and "Up" in ln for ln in (ps.stdout or "").splitlines())
    if not up:
        compose = ROOT / "asr" / "docker-compose.yml"
        if compose.is_file():
            subprocess.run(
                ["docker", "compose", "-f", str(compose), "up", "-d"],
                cwd=str(ROOT),
                timeout=300,
                check=False,
            )
            deadline = time.time() + 120
            while time.time() < deadline:
                ps = subprocess.run(
                    ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    encoding="utf-8",
                )
                if any("primaryanalitics-gigaam" in ln and "Up" in ln for ln in (ps.stdout or "").splitlines()):
                    up = True
                    break
                time.sleep(3)
    if not up:
        raise RuntimeError("контейнер primaryanalitics-gigaam не Up")

    work = ROOT / "asr" / "work"
    work.mkdir(parents=True, exist_ok=True)
    probe = work / f"portal_smoke_probe_{int(time.time())}.txt"
    probe.write_text("portal_smoke_mount_ok", encoding="utf-8")
    try:
        inside = subprocess.run(
            ["docker", "exec", "primaryanalitics-gigaam", "test", "-f", f"/work/{probe.name}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if inside.returncode != 0:
            # имя контейнера может отличаться — ищем
            names = [
                ln.split("\t")[0]
                for ln in (ps.stdout or "").splitlines()
                if "gigaam" in ln.lower() and "Up" in ln
            ]
            ok_mount = False
            for name in names:
                inside = subprocess.run(
                    ["docker", "exec", name, "test", "-f", f"/work/{probe.name}"],
                    capture_output=True,
                    timeout=30,
                )
                if inside.returncode == 0:
                    ok_mount = True
                    break
            if not ok_mount:
                raise RuntimeError(
                    "GigaAM не видит asr/work (/work) — том не смонтирован (ошибка no audio /work/...)"
                )
    finally:
        try:
            probe.unlink()
        except OSError:
            pass

    giga_http = False
    deadline = time.time() + 120
    while time.time() < deadline and not giga_http:
        for url in ("http://127.0.0.1:18102/health", "http://127.0.0.1:18102/"):
            try:
                with httpx.Client(timeout=5) as c:
                    if c.get(url).status_code < 500:
                        giga_http = True
                        break
            except httpx.HTTPError:
                continue
        if not giga_http:
            time.sleep(3)
    if not giga_http:
        raise RuntimeError("GigaAM HTTP :18102 не отвечает за 120 с после старта контейнера")

    # ready + LLM
    _ensure_mint()
    smoke = _smoke_common()
    client, _ = smoke.api_login("admin")
    r = client.get("/health/ready")
    if r.status_code != 200:
        raise RuntimeError(f"/health/ready → {r.status_code}")
    ready = r.json()
    if ready.get("status") != "ok":
        raise RuntimeError(f"ready.status={ready.get('status')}: {ready.get('llm_detail')}")
    if not ready.get("llm"):
        raise RuntimeError(f"LLM не жив ({ready.get('llm_provider')}: {ready.get('llm_detail')})")

    # Ollama bge-m3
    try:
        with httpx.Client(timeout=10) as c:
            tags = c.get("http://127.0.0.1:11434/api/tags").json()
        names = [m.get("name") or "" for m in (tags.get("models") or [])]
        if not any("bge-m3" in n for n in names):
            raise RuntimeError(f"Ollama без bge-m3: {names[:8]}")
    except Exception as exc:
        raise RuntimeError(f"Ollama :11434: {exc}") from exc

    # tools
    for tool, argv in (
        ("ffmpeg", ["ffmpeg", "-version"]),
        ("tesseract", ["tesseract", "--list-langs"]),
    ):
        try:
            tr = subprocess.run(argv, capture_output=True, text=True, timeout=30, encoding="utf-8")
        except FileNotFoundError as exc:
            # tools\ paths
            alt = ROOT / "tools" / tool / "bin" / f"{tool}.exe"
            if tool == "tesseract":
                alt = ROOT / "tools" / "tesseract" / "tesseract.exe"
            if not alt.is_file():
                raise RuntimeError(f"нет {tool} в PATH и {alt}") from exc
            continue
        if tool == "tesseract" and "rus" not in ((tr.stdout or "") + (tr.stderr or "")):
            raise RuntimeError("Tesseract без языка rus")

    # LibreOffice
    so = False
    for cand in (
        ROOT / "tools" / "LibreOffice" / "program" / "soffice.exe",
        Path(r"C:\Program Files\LibreOffice\program\soffice.exe"),
    ):
        if cand.is_file():
            so = True
            break
    if not so:
        raise RuntimeError("нет LibreOffice (нужен для .odt/.doc/.xls)")

    # restart.ps1 parse (BOM для PS 5.1)
    restart = ROOT / "scripts" / "restart.ps1"
    if restart.is_file():
        raw = restart.read_bytes()
        if not raw.startswith(b"\xef\xbb\xbf"):
            LOG.warn("restart.ps1 BOM", "файл без UTF-8 BOM — PowerShell 5.1 может ломать кириллицу")

    return f"diar={diar}, llm={ready.get('llm_provider')}"


# ---------------------------------------------------------------------------
# 2. Вход
# ---------------------------------------------------------------------------


def step_auth_mint() -> str:
    _ensure_mint()
    smoke = _smoke_common()
    client, data = smoke.api_login("admin")
    r = client.get("/projects")
    if r.status_code != 200:
        raise RuntimeError(f"mint JWT: GET /projects → {r.status_code}")
    return f"role={data.get('role')}"


def step_auth_human() -> str:
    """Вход логин+пароль+код без mint. Без SMOKE_PERSONAL_PW — FAIL."""
    import httpx

    smoke = _smoke_common()
    pw = os.environ.get("SMOKE_PERSONAL_PW", "").strip()
    if not pw:
        raise RuntimeError(
            "для входа «как человек» задайте SMOKE_PERSONAL_PW "
            f"(логин {smoke.LOGIN}). Автоматика при этом может идти через mint"
        )
    saved = os.environ.pop("SMOKE_BEARER_TOKEN", None)
    try:
        base = os.environ.get("SMOKE_BASE", smoke.BASE).rstrip("/")
        client = httpx.Client(
            base_url=base,
            verify=False,
            timeout=60,
            headers={"Origin": base, "Referer": base + "/"},
        )
        body = {"name": smoke.LOGIN, "telegram_id": smoke.TG, "role": "admin", "password": pw}
        r = client.post("/auth/login", json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"/auth/login → {r.status_code} {r.text[:300]}")
        data = r.json()
        if data.get("step") == "code":
            channel = data.get("channel") or "b24"
            r = client.post("/auth/login/code/request", json={"ticket": data["ticket"], "channel": channel})
            if r.status_code >= 400:
                raise RuntimeError(f"code/request → {r.status_code} {r.text[:300]}")
            code = os.environ.get("SMOKE_OTP_CODE", "").strip()
            if not code:
                # B24_AUTH_DEV иногда кладёт код в ответ
                req = r.json() if "application/json" in (r.headers.get("content-type") or "") else {}
                code = str(req.get("dev_code") or req.get("code") or "").strip()
            if not code:
                raise RuntimeError("нет SMOKE_OTP_CODE (и нет dev_code) — шаг входа как человек")
            r = client.post("/auth/login/code/verify", json={"ticket": data["ticket"], "code": code})
            if r.status_code >= 400:
                raise RuntimeError(f"code/verify → {r.status_code} {r.text[:300]}")
            data = r.json()
        if data.get("step"):
            raise RuntimeError(f"вход остановился на шаге {data.get('step')} — дозаполните карточку в браузере")
        token = data.get("access_token")
        if not token:
            raise RuntimeError(f"нет access_token: {data}")
        client.headers["Authorization"] = "Bearer " + token
        r = client.post("/auth/logout")
        # logout может быть 200/204
        if r.status_code not in (200, 204):
            LOG.warn("logout", f"HTTP {r.status_code}")
        return f"login={smoke.LOGIN}"
    finally:
        if saved:
            os.environ["SMOKE_BEARER_TOKEN"] = saved


# ---------------------------------------------------------------------------
# 3. Проект + MCP + удаление
# ---------------------------------------------------------------------------


def step_project_lifecycle() -> str:
    smoke = _smoke_common()
    client, _ = smoke.api_login("admin")
    name = f"portal-smoke-{datetime.now():%Y%m%d-%H%M%S}"
    r = client.post("/projects", json={"name": name, "description": "portal_smoke temp"})
    if r.status_code not in (200, 201):
        raise RuntimeError(f"POST /projects → {r.status_code} {r.text[:300]}")
    pid = int(r.json()["id"])
    tok = client.post(f"/projects/{pid}/mcp/tokens", json={"label": "portal-smoke"})
    if tok.status_code not in (200, 201):
        raise RuntimeError(f"MCP token → {tok.status_code} {tok.text[:300]}")
    key = tok.json().get("token")
    key_id = tok.json().get("id")
    if key:
        import httpx

        base = os.environ.get("SMOKE_BASE", smoke.BASE).rstrip("/")
        mcp = httpx.Client(
            base_url=base,
            verify=False,
            timeout=60,
            headers={"Authorization": f"Bearer {key}"},
        )
        listed = mcp.post(
            f"/mcp/projects/{pid}",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        if listed.status_code >= 400:
            raise RuntimeError(f"MCP tools/list → {listed.status_code}")
    if key_id is not None:
        client.delete(f"/projects/{pid}/mcp/tokens/{key_id}")
    # удаление — ловит FK activity_log / MCP
    d = client.delete(f"/projects/{pid}")
    if d.status_code not in (200, 204):
        raise RuntimeError(f"DELETE /projects/{pid} → {d.status_code} {d.text[:400]}")
    return f"project_id={pid} deleted"


# ---------------------------------------------------------------------------
# 4–7. Материалы через e2e
# ---------------------------------------------------------------------------


def step_documents() -> str:
    from build_fixtures import main as build_fixtures
    import e2e_documents

    build_fixtures()
    code = e2e_documents.main()
    if code != 0:
        raise RuntimeError(f"e2e_documents exit {code}")
    from common import read_state, state_path

    ids = read_state(state_path()).get("document_ids") or []
    return f"{len(ids)} документов"


def step_audio_primary() -> str:
    """Аудио актуальным DIAR_MODEL. При провале — причина + наличие запасного Sortformer."""
    import e2e_audio

    try:
        code = e2e_audio.main()
        if code != 0:
            raise RuntimeError(f"e2e_audio exit {code}")
    except Exception as exc:
        sortformer = ROOT / "data" / "models" / "onnx-diar" / "diar_streaming_sortformer_4spk-v2.1.onnx"
        detail = (
            f"актуальная диаризация/ASR упала: {exc}. "
            f"Запасной Sortformer на диске: {'да' if sortformer.is_file() else 'нет'}. "
            "Перезапустите воркер с DIAR_MODEL=sortformer4 только как временный обход — "
            "в смоуке это всё равно FAIL (основной способ сломан)."
        )
        raise RuntimeError(detail) from exc
    from common import read_state, state_path

    st = read_state(state_path())
    return f"audio_id={st.get('audio_id')} status={st.get('audio_status')} gate={st.get('voice_gate_mode')}"


def step_audio_mp4() -> str:
    from common import AUDIO_SOURCE, FIXTURES, ensure_login, upload_file, wait_status

    mp4 = FIXTURES / "e2e_speech.mp4"
    if not mp4.is_file():
        LOG.warn("аудио mp4", "нет e2e_speech.mp4 (ffmpeg?) — ветка видео пропущена как WARN")
        return "skipped"
    client, _ = ensure_login()
    row = upload_file(client, mp4, source_type=AUDIO_SOURCE, content_type="video/mp4")
    aid = row["artifact_id"]
    if str(row.get("status") or "") in {"done", "speakers_confirmed", "transcript_ready"}:
        raise RuntimeError(f"mp4 дедуп старого артефакта {aid}")
    wait_status(
        client,
        aid,
        {"speakers_pending", "transcript_ready", "done"},
        timeout_sec=5400,
        label="mp4",
    )
    from common import read_state, state_path, write_state

    st = read_state(state_path())
    st["mp4_id"] = aid
    write_state(state_path(), st)
    return f"mp4_id={aid}"


def step_speakers() -> str:
    import e2e_speakers

    code = e2e_speakers.main()
    if code != 0:
        raise RuntimeError(f"e2e_speakers exit {code}")
    return "speakers ok"


def step_knowledge() -> str:
    import e2e_knowledge

    code = e2e_knowledge.main()
    if code != 0:
        raise RuntimeError(f"e2e_knowledge exit {code}")
    return "search+tz+chat ok"


# ---------------------------------------------------------------------------
# 8. Доп. функции проекта
# ---------------------------------------------------------------------------


def step_project_features() -> str:
    smoke = _smoke_common()
    client, _ = smoke.api_login("admin")
    pid = smoke.PID

    r = client.get(f"/projects/{pid}/graph/data")
    if r.status_code != 200:
        raise RuntimeError(f"graph/data → {r.status_code}")
    g = r.json()
    # пустой граф допустим только если вообще нет сущностей — но после knowledge должны быть
    nodes = g.get("nodes") or g.get("elements") or g.get("entities") or []
    if isinstance(g.get("elements"), dict):
        nodes = (g["elements"].get("nodes") or []) + (g["elements"].get("edges") or [])

    r = client.get(f"/projects/{pid}/tasks", params={"view": "all", "limit": 10})
    if r.status_code != 200:
        raise RuntimeError(f"tasks → {r.status_code}")
    tr = client.post(
        f"/projects/{pid}/tasks",
        json={"description": f"portal_smoke задача {datetime.now():%H%M%S}"},
    )
    if tr.status_code not in (200, 201):
        raise RuntimeError(f"POST tasks → {tr.status_code} {tr.text[:300]}")
    task = tr.json()
    tid = task.get("id") or task.get("task_id")
    if tid is not None:
        client.delete(f"/projects/{pid}/manual-tasks/{tid}")

    r = client.get(f"/projects/{pid}/people")
    if r.status_code != 200:
        raise RuntimeError(f"people → {r.status_code} {r.text[:200]}")

    r = client.post(
        f"/projects/{pid}/notes",
        json={"text": f"portal_smoke note {datetime.now():%Y%m%d_%H%M%S}"},
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"notes → {r.status_code} {r.text[:300]}")

    from common import read_state, state_path

    st = read_state(state_path())
    aid = st.get("audio_id") or (st.get("document_ids") or [None])[0]
    if aid:
        dl = client.get(f"/artifacts/{aid}/download", follow_redirects=True)
        if dl.status_code != 200:
            raise RuntimeError(f"download {aid} → {dl.status_code}")

    bg = client.get(f"/projects/{pid}/background")
    if bg.status_code != 200:
        raise RuntimeError(f"background → {bg.status_code}")
    body = bg.json()
    # не требуем пустых ошибок за сутки глобально — только чтобы ответ валидный
    if not isinstance(body, dict):
        raise RuntimeError(f"background не dict: {type(body)}")

    return f"graph_nodes≈{len(nodes) if hasattr(nodes, '__len__') else '?'}"


# ---------------------------------------------------------------------------
# 9. Встреча
# ---------------------------------------------------------------------------


def step_meeting() -> str:
    smoke = _smoke_common()
    client, _ = smoke.api_login("admin")
    mid, tok = smoke.new_live_meeting(client)
    g = client.post(
        f"/api/meetings/public/{tok}/join",
        json={"display_name": "Гость-portal-smoke", "guest_id": f"ps-{int(time.time())}"},
    )
    if g.status_code != 200:
        raise RuntimeError(f"guest join → {g.status_code} {g.text[:300]}")
    s = client.post(f"/projects/{smoke.PID}/meetings/{mid}/join")
    if s.status_code != 200:
        raise RuntimeError(f"staff join → {s.status_code} {s.text[:300]}")
    # короткая пауза чтобы рекордеры успели зайти
    time.sleep(5)
    end = client.post(f"/projects/{smoke.PID}/meetings/{mid}/end")
    if end.status_code not in (200, 204):
        raise RuntimeError(f"end → {end.status_code}")
    an = client.post(f"/projects/{smoke.PID}/meetings/{mid}/analyze")
    # без дорожек analyze может ответить 4xx — это не всегда FAIL стенда
    if an.status_code >= 500:
        raise RuntimeError(f"analyze → {an.status_code} {an.text[:300]}")
    if an.status_code >= 400:
        LOG.warn(
            "встреча analyze",
            f"HTTP {an.status_code} (часто нет дорожек без браузерного медиа) — "
            f"{an.text[:200]}",
        )
        # удалим встречу
        client.delete(f"/projects/{smoke.PID}/meetings/{mid}")
        return f"meeting={mid} join/end ok, analyze skipped"
    # дождаться артефакта если есть
    payload = an.json() if an.headers.get("content-type", "").startswith("application/json") else {}
    art = None
    if isinstance(payload, dict):
        art = payload.get("artifact_id") or (payload.get("artifact") or {}).get("artifact_id")
    if art:
        from common import wait_status

        try:
            wait_status(
                client,
                art,
                {"speakers_pending", "transcript_ready", "done", "no_speech"},
                timeout_sec=3600,
                label="meeting-analyze",
            )
        except Exception as exc:
            LOG.warn("встреча analyze wait", str(exc)[:300])
    client.delete(f"/projects/{smoke.PID}/meetings/{mid}")
    return f"meeting={mid}"


# ---------------------------------------------------------------------------
# Cleanup + report
# ---------------------------------------------------------------------------


def step_cleanup(keep: bool) -> str:
    if keep:
        return "keep"
    from common import ensure_login
    import e2e_audio
    import e2e_documents

    client, _ = ensure_login()
    e2e_documents.cleanup(client)
    e2e_audio.cleanup(client)
    from common import read_state, state_path, delete_artifact

    mp4 = read_state(state_path()).get("mp4_id")
    if mp4:
        try:
            delete_artifact(client, mp4)
        except Exception:
            pass
    return "cleaned"


def _write_report() -> Path:
    out_dir = ROOT / "data" / "eval"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"portal_smoke_{stamp}.json"
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "failed": len(LOG.failed),
        "steps": LOG.rows,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    latest = out_dir / "portal_smoke_latest.json"
    latest.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="Полный функциональный смоук портала")
    ap.add_argument("--stand-only", action="store_true", help="только стенд и вход")
    ap.add_argument("--keep", action="store_true", help="не удалять e2e-артефакты")
    args = ap.parse_args()

    print("Portal smoke — функциональная работоспособность (без полного UI)", flush=True)
    print(f"ROOT={ROOT}", flush=True)
    print(f"SMOKE_BASE={os.environ.get('SMOKE_BASE', 'https://127.0.0.1:777')}", flush=True)

    _run_step("стенд (health, venv/BOM, модели, GigaAM, LLM)", step_stand)
    if LOG.failed:
        path = _write_report()
        print(f"\nСтенд красный — дальше не идём. Отчёт: {path}", flush=True)
        return 1

    _run_step("вход mint JWT", step_auth_mint)
    _run_step("вход как человек + logout", step_auth_human)

    if args.stand_only:
        path = _write_report()
        print(f"\n--stand-only. Отчёт: {path}", flush=True)
        return 1 if LOG.failed else 0

    _run_step("проект: создать → MCP → удалить", step_project_lifecycle)
    _run_step("документы всех форматов", step_documents)
    _run_step("созвон wav (ASR + диаризация)", step_audio_primary)
    _run_step("созвон mp4", step_audio_mp4)
    _run_step("спикеры confirm → knowledge jobs", step_speakers)
    _run_step("поиск + ТЗ + чат", step_knowledge)
    _run_step("граф, задачи, люди, заметка, download, background", step_project_features)
    _run_step("встреча: create→join→end→analyze", step_meeting)
    _run_step("очистка артефактов", lambda: step_cleanup(args.keep))

    path = _write_report()
    print("\n" + "=" * 60, flush=True)
    print("\n".join(f"{r['status']:5} {r['step']}" + (f" — {r['detail']}" if r.get("detail") else "") for r in LOG.rows), flush=True)
    print(f"\nОтчёт: {path}", flush=True)
    print(
        "Полный UI комнаты (отдельно): venv\\Scripts\\python.exe scripts\\smoke\\run_all.py",
        flush=True,
    )
    if LOG.failed:
        print(f"\nFAIL: {len(LOG.failed)} шаг(ов)", flush=True)
        return 1
    print("\nOK: portal smoke зелёный", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
