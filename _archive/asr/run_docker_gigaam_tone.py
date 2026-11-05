"""Прогон Docker GigaAM + T-one на тестовой записи. Whisper не трогаем."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "asr" / "work"
OUT = ROOT / "asr" / "out"
ENV = ROOT / ".env"

GIGAAM = "http://127.0.0.1:18102"
TONE = "http://127.0.0.1:18103"


def load_env() -> dict[str, str]:
    data: dict[str, str] = {}
    for line in ENV.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip()
    return data


def post_json(url: str, payload: dict, timeout: int = 3600) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def get_json(url: str, headers: dict[str, str] | None = None, timeout: int = 60) -> dict:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def post_file(url: str, path: Path, headers: dict[str, str], timeout: int = 3600) -> dict:
    boundary = "----primaryanaliticsBoundary7MA4YWxkTrZu0gW"
    data = path.read_bytes()
    parts = [
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode(),
        b"Content-Type: audio/wav\r\n\r\n",
        data,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    body = b"".join(parts)
    hdrs = {
        **headers,
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def run_tone() -> dict:
    t0 = time.time()
    result = post_json(
        f"{TONE}/transcribe",
        {"audio": "/data/input_8k.wav", "out_dir": "/out"},
        timeout=3600,
    )
    result["elapsed_sec"] = round(time.time() - t0, 1)
    text = result.get("text") or ""
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "docker_tone.txt").write_text(text, encoding="utf-8")
    (OUT / "docker_tone.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def run_gigaam(api_key: str) -> dict:
    t0 = time.time()
    wav = WORK / "input_16k.wav"
    headers = {"X-API-Key": api_key}
    uploaded = post_file(
        f"{GIGAAM}/api/v1/transcribe?asr_backend=pytorch&asr_model=v3_e2e_rnnt&enable_diarization=false",
        wav,
        headers,
        timeout=120,
    )
    task_id = uploaded["task_id"]
    while True:
        st = get_json(f"{GIGAAM}/api/v1/tasks/{task_id}", headers=headers, timeout=30)
        status = st.get("status")
        if status == "completed":
            data = get_json(f"{GIGAAM}/api/v1/tasks/{task_id}/result", headers=headers, timeout=60)
            data["elapsed_sec"] = round(time.time() - t0, 1)
            data["task_id"] = task_id
            text = data.get("transcription") or ""
            OUT.mkdir(parents=True, exist_ok=True)
            (OUT / "docker_gigaam.txt").write_text(text, encoding="utf-8")
            (OUT / "docker_gigaam.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            return data
        if status == "failed":
            raise RuntimeError(st)
        time.sleep(2)


def main() -> None:
    env = load_env()
    key = env.get("GIGAAM_API_KEY") or os.environ.get("GIGAAM_API_KEY", "")
    if not key:
        raise SystemExit("GIGAAM_API_KEY empty")
    if not (WORK / "input_16k.wav").is_file():
        raise SystemExit(f"missing {WORK / 'input_16k.wav'}")

    from concurrent.futures import ThreadPoolExecutor, as_completed

    summary: dict[str, object] = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = {
            pool.submit(run_gigaam, key): "gigaam",
            pool.submit(run_tone): "tone",
        }
        for fut in as_completed(futs):
            name = futs[fut]
            try:
                result = fut.result()
                text = result.get("transcription") or result.get("text") or ""
                summary[name] = {
                    "ok": True,
                    "chars": len(text),
                    "elapsed_sec": result.get("elapsed_sec"),
                    "preview": text[:400].replace("\n", " "),
                }
                print(f"OK {name}: {len(text)} chars in {result.get('elapsed_sec')}s")
            except Exception as exc:  # noqa: BLE001
                summary[name] = {"ok": False, "error": str(exc)}
                print(f"FAIL {name}: {exc}")
    (OUT / "docker_gigaam_tone_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
