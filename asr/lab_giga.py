"""Этап 3a — прогон GigaAM (Docker) на аудио того же лабораторного прогона.

Забираем всё, что контейнер готов отдать: обычный текст, текст с таймкодами и
спикерами, сырой ответ API и все файлы результата. Ничего не решаем: снимок
01–07 и оверлей ``sense/`` остаются как есть, пишем только в ``giga/``.

    venv\\Scripts\\python.exe asr\\lab_giga.py --project-id 1 --artifact-id art_... --run-id 20260826T174104Z_48a796
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from asr.lab.giga import GIGA_DIR, parse_turns, summarize  # noqa: E402
from asr.lab.paths import LabRun  # noqa: E402
from asr.lab.stages.s01_audio import clean_wav  # noqa: E402

GIGAAM_URL = os.environ.get("GIGAAM_URL", "http://127.0.0.1:18102")
RESULTS_DIR = ROOT / "asr" / "gigaam-results"
POLL_SEC = 5
UPLOAD_TIMEOUT = 600
TASK_TIMEOUT = 7200


def api_key() -> str:
    """Ключ из окружения, иначе из `.env`. В лог не попадает."""
    key = os.environ.get("GIGAAM_API_KEY", "").strip()
    if key:
        return key
    env_file = ROOT / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("GIGAAM_API_KEY="):
                return line.split("=", 1)[1].strip()
    return ""


def get_json(url: str, headers: dict[str, str], timeout: int) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def post_wav(url: str, path: Path, headers: dict[str, str], *, filename: str) -> dict[str, Any]:
    boundary = "----skkkkLabGigaBoundary"
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
            b"Content-Type: audio/wav\r\n\r\n",
            path.read_bytes(),
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )
    request = urllib.request.Request(
        url,
        data=body,
        headers={**headers, "Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=UPLOAD_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def _wait(task_id: str, headers: dict[str, str]) -> dict[str, Any]:
    deadline = time.time() + TASK_TIMEOUT
    last = ""
    while time.time() < deadline:
        status = get_json(f"{GIGAAM_URL}/api/v1/tasks/{task_id}", headers, 60)
        state = status.get("status")
        stamp = f"{status.get('stage') or ''} {status.get('progress')}%"
        if stamp != last:
            print(f"[giga] {stamp}", flush=True)
            last = stamp
        if state == "completed":
            return status
        if state == "failed":
            raise RuntimeError(status.get("error") or status)
        time.sleep(POLL_SEC)
    raise TimeoutError(f"задача {task_id} не закончилась за {TASK_TIMEOUT} с")


def collect_result_files(task_id: str, target: Path) -> list[dict[str, Any]]:
    """Все файлы результата контейнера — внутрь прогона, без разбора важности."""
    source = RESULTS_DIR / task_id
    if not source.is_dir():
        return []
    target.mkdir(parents=True, exist_ok=True)
    rows = []
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        copy = target / path.name
        shutil.copy2(path, copy)
        rows.append({"name": path.name, "bytes": copy.stat().st_size})
    return rows


def pick_text(files_dir: Path, key: str) -> str:
    for path in sorted(files_dir.glob(f"*{key}*")):
        if path.suffix.lower() == ".txt":
            return path.read_text(encoding="utf-8")
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Этап 3a: GigaAM на аудио лабораторного прогона")
    parser.add_argument("--project-id", type=int, required=True)
    parser.add_argument("--artifact-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--num-speakers", type=int, help="подсказка pyannote; по умолчанию авто")
    args = parser.parse_args()

    key = api_key()
    if not key:
        print("нет GIGAAM_API_KEY (окружение или .env)", file=sys.stderr)
        return 3

    data_dir = Path(os.environ.get("DATA_DIR") or ROOT / "data")
    artifact_root = data_dir / "projects" / str(args.project_id) / "artifacts" / args.artifact_id
    run = LabRun(artifact_root, args.run_id)
    audio = clean_wav(run)
    if not audio.is_file():
        print(f"нет аудио прогона {audio}", file=sys.stderr)
        return 3

    headers = {"X-API-Key": key}
    health = get_json(f"{GIGAAM_URL}/health", headers, 15)
    if health.get("status") != "healthy":
        print(f"контейнер не готов: {health}", file=sys.stderr)
        return 4

    params = {
        "asr_backend": "pytorch",
        "asr_model": "v3_e2e_rnnt",
        "enable_diarization": "true",
        "diarization_backend": "pyannote",
    }
    if args.num_speakers:
        params["num_speakers"] = str(args.num_speakers)
    query = "&".join(f"{name}={value}" for name, value in params.items())

    started = time.time()
    upload_name = f"lab_{run.run_id}_clean_16k.wav"
    print(f"[giga] отправляю {audio.name} как {upload_name}", flush=True)
    uploaded = post_wav(
        f"{GIGAAM_URL}/api/v1/transcribe?{query}",
        audio,
        headers,
        filename=upload_name,
    )
    task_id = uploaded["task_id"]
    print(f"[giga] задача {task_id}", flush=True)

    status = _wait(task_id, headers)
    result = get_json(f"{GIGAAM_URL}/api/v1/tasks/{task_id}/result", headers, 300)
    elapsed = round(time.time() - started, 1)

    giga_dir = run.resolve(GIGA_DIR)
    raw_dir = giga_dir / "raw"
    giga_dir.mkdir(parents=True, exist_ok=True)
    (giga_dir / "api_result.json").write_text(
        json.dumps({"task": status, "result": result}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    files = collect_result_files(task_id, raw_dir)

    timecoded = pick_text(raw_dir, "diarize_timecodes") or (
        result.get("transcription_with_timecodes") or ""
    )
    plain = pick_text(raw_dir, "diarize") or (result.get("transcription") or "")
    if timecoded:
        (giga_dir / "timecodes.txt").write_text(timecoded, encoding="utf-8")
    if plain:
        (giga_dir / "plain.txt").write_text(plain, encoding="utf-8")

    turns = parse_turns(timecoded)
    doc = {
        "stage": "giga",
        "overlay": True,
        "does_not_replace": [
            "02_asr/words.json",
            "04_paint/paint.json",
            "06_transcript/transcript.json",
            "sense/sense.json",
            "sense/transcript.json",
        ],
        "meta": {
            "run_id": run.run_id,
            "artifact_id": args.artifact_id,
            "project_id": args.project_id,
            "audio": run.relative(audio),
            "upload_name": upload_name,
            "task_id": task_id,
            "params": params,
            "elapsed_sec": elapsed,
            "processing_time": result.get("processing_time"),
            "media_duration": result.get("media_duration"),
            "asr": health.get("asr"),
            "files": files,
        },
        "summary": summarize(turns, plain_text=plain),
        "turns": turns,
    }
    (giga_dir / "giga.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(
        json.dumps(
            {
                "run_id": run.run_id,
                "task_id": task_id,
                "elapsed_sec": elapsed,
                **doc["summary"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    os.chdir(ROOT)
    raise SystemExit(main())
