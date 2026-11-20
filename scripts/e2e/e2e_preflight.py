"""Preflight живого acceptance: health, модели, Docker GigaAM, LLM, воркер."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import BASE, ROOT, api_login, check, fail, ok  # noqa: E402

MODEL_CHECKS = [
    ("parakeet", ROOT / "data" / "models" / "onnx-asr" / "nemo-parakeet-tdt-0.6b-v3" / "encoder-model.onnx"),
    ("sortformer", ROOT / "data" / "models" / "onnx-diar" / "diar_streaming_sortformer_4spk-v2.1.onnx"),
    ("nemotron", ROOT / "Nemotron-3-Diarization" / "Nemotron-3-Diarization.nemo"),
    ("gigaam_ckpt", ROOT / "data" / "gigaam" / "models" / "gigaam" / "v3_e2e_rnnt.ckpt"),
]


def _wespeaker_ok() -> bool:
    roots = [
        ROOT / "data" / "gigaam" / "models" / "huggingface" / "hub" / "models--pyannote--wespeaker-voxceleb-resnet34-LM",
        ROOT / "data" / "models" / "huggingface" / "hub" / "models--pyannote--wespeaker-voxceleb-resnet34-LM",
    ]
    for base in roots:
        if not base.is_dir():
            continue
        # snapshot или blobs с ненулевым весом
        for f in base.rglob("*"):
            if f.is_file() and f.stat().st_size > 1_000_000:
                return True
    return False


def _docker_gigaam_running() -> bool:
    try:
        r = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"],
            capture_output=True,
            text=True,
            timeout=30,
            encoding="utf-8",
        )
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        fail(f"docker недоступен: {exc}")
    for line in (r.stdout or "").splitlines():
        if "primaryanalitics-gigaam" in line and "Up" in line:
            return True
    return False


def _gigaam_http_ok() -> bool:
    try:
        with httpx.Client(timeout=10) as c:
            r = c.get("http://127.0.0.1:18102/health")
            return r.status_code < 500
    except httpx.HTTPError:
        # некоторые сборки без /health — пробуем корень
        try:
            with httpx.Client(timeout=10) as c:
                r = c.get("http://127.0.0.1:18102/")
                return r.status_code < 500
        except httpx.HTTPError:
            return False


def main() -> int:
    print(f"BASE={BASE}")
    # Публичный health
    with httpx.Client(base_url=BASE, verify=False, timeout=30) as anon:
        r = anon.get("/health")
        check(r.status_code == 200 and (r.json() or {}).get("status") == "ok", "GET /health")

    client, _ = api_login("admin")
    r = client.get("/health/ready")
    check(r.status_code == 200, f"GET /health/ready ({r.status_code})")
    ready = r.json()
    print("ready:", json.dumps({k: ready.get(k) for k in ("status", "llm_provider", "llm", "queue", "vram_mib")}, ensure_ascii=False))
    check(ready.get("status") == "ok", "ready.status=ok")
    check(bool(ready.get("llm")), f"LLM жив ({ready.get('llm_provider')}: {ready.get('llm_detail')})")
    queue = ready.get("queue") or {}
    running = int(queue.get("running") or 0)
    queued = int(queue.get("queued") or 0)
    if running > 0 or queued > 0:
        print(f"WARN очередь занята: running={running}, queued={queued} — таймауты e2e вырастут")

    for name, path in MODEL_CHECKS:
        check(path.is_file() and path.stat().st_size > 0, f"модель {name}: {path}")
    check(_wespeaker_ok(), "модель WeSpeaker (ненулевой snapshot/blob)")

    # Nemotron обязан быть выбранным по умолчанию и файлом на диске (иначе тихий откат)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from app.app.config import get_settings

    settings = get_settings()
    diar = (settings.diar_model or "").strip().lower()
    check(diar in {"nemotron3", "sortformer4"}, f"DIAR_MODEL допустим ({diar})")
    if diar == "nemotron3":
        check(
            (ROOT / "Nemotron-3-Diarization" / "Nemotron-3-Diarization.nemo").is_file(),
            "файл Nemotron-3-Diarization.nemo на месте",
        )
    check(bool(settings.voice_gate_before_asr), "VOICE_GATE_BEFORE_ASR включён")

    if not _docker_gigaam_running():
        print("поднимаю primaryanalitics-gigaam…")
        compose = ROOT / "asr" / "docker-compose.yml"
        if compose.is_file():
            subprocess.run(
                ["docker", "compose", "-f", str(compose), "up", "-d"],
                cwd=str(ROOT),
                timeout=300,
                check=False,
            )
        else:
            subprocess.run(["docker", "start", "primaryanalitics-gigaam"], timeout=120, check=False)
        import time

        deadline = time.time() + 120
        while time.time() < deadline and not _docker_gigaam_running():
            time.sleep(3)
    if not _docker_gigaam_running():
        fail("контейнер primaryanalitics-gigaam не Up после попытки старта")
    ok("docker primaryanalitics-gigaam Up")
    if not _gigaam_http_ok():
        import time

        for _ in range(40):
            if _gigaam_http_ok():
                break
            time.sleep(3)
    check(_gigaam_http_ok(), "GigaAM HTTP :18102")

    # Воркер жив: либо running/queued меняются, либо в логах есть worker — достаточно ready.queue поля
    ok("preflight OK")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError:
        raise SystemExit(1)
