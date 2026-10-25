"""Прогон Docker GigaAM с диаризацией pyannote на тестовой записи."""

from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "asr" / "work"
OUT = ROOT / "asr" / "out"
RESULTS = ROOT / "asr" / "gigaam-results"
ENV = ROOT / ".env"
GIGAAM = "http://127.0.0.1:18102"


def load_env() -> dict[str, str]:
    data: dict[str, str] = {}
    for line in ENV.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip()
    return data


def get_json(url: str, headers: dict[str, str] | None = None, timeout: int = 60) -> dict:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def post_file(url: str, path: Path, headers: dict[str, str], timeout: int = 3600) -> dict:
    boundary = "----primaryanaliticsBoundaryDiar7MA4YWxk"
    data = path.read_bytes()
    parts = [
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode(),
        b"Content-Type: audio/wav\r\n\r\n",
        data,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    hdrs = {**headers, "Content-Type": f"multipart/form-data; boundary={boundary}"}
    req = urllib.request.Request(url, data=b"".join(parts), headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    env = load_env()
    key = env.get("GIGAAM_API_KEY") or os.environ.get("GIGAAM_API_KEY", "")
    if not key:
        raise SystemExit("GIGAAM_API_KEY empty")
    wav = WORK / "input_16k.wav"
    if not wav.is_file():
        raise SystemExit(f"missing {wav}")

    headers = {"X-API-Key": key}
    health = get_json(f"{GIGAAM}/health", timeout=10)
    print("health", health.get("status") or health)

    t0 = time.time()
    qs = (
        "asr_backend=pytorch&asr_model=v3_e2e_rnnt"
        "&enable_diarization=true&diarization_backend=pyannote"
    )
    uploaded = post_file(f"{GIGAAM}/api/v1/transcribe?{qs}", wav, headers, timeout=120)
    task_id = uploaded["task_id"]
    print("task", task_id)

    last_stage = ""
    while True:
        st = get_json(f"{GIGAAM}/api/v1/tasks/{task_id}", headers=headers, timeout=30)
        status = st.get("status")
        stage = st.get("stage") or ""
        if stage and stage != last_stage:
            print("stage", stage, "progress", st.get("progress"))
            last_stage = stage
        if status == "completed":
            data = get_json(f"{GIGAAM}/api/v1/tasks/{task_id}/result", headers=headers, timeout=60)
            elapsed = round(time.time() - t0, 1)
            result_dir = RESULTS / task_id
            diar_txt = result_dir / "input_16k_diarize.txt"
            diar_tc = result_dir / "input_16k_diarize_timecodes.txt"
            # API отдаёт обычный TXT без меток; кто говорит — в *_diarize*.
            if diar_txt.is_file():
                text = diar_txt.read_text(encoding="utf-8")
            else:
                text = data.get("transcription") or ""
            if diar_tc.is_file():
                timed = diar_tc.read_text(encoding="utf-8")
            else:
                timed = data.get("transcription_with_timecodes") or data.get("transcription_timecoded") or ""
            OUT.mkdir(parents=True, exist_ok=True)
            (OUT / "docker_gigaam_diar.txt").write_text(text, encoding="utf-8")
            if timed:
                (OUT / "docker_gigaam_diar_timecodes.txt").write_text(timed, encoding="utf-8")
            payload = {**data, "elapsed_sec": elapsed, "task_id": task_id}
            (OUT / "docker_gigaam_diar.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            speakers = sorted(
                {line.strip("[]") for line in text.splitlines() if line.startswith("[Спикер")}
            )
            summary = {
                "ok": True,
                "elapsed_sec": elapsed,
                "chars": len(text),
                "speakers": speakers,
                "speaker_count": len(speakers),
                "min_speakers": 2,
                "max_speakers": 15,
                "preview": text[:500].replace("\n", " | "),
            }
            (OUT / "docker_gigaam_diar_summary.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print("OK chars", len(text), "elapsed", elapsed, "speakers", speakers)
            print(text[:600])
            try:
                from build_transcript_view import build as build_view

                view = build_view(None)
                print("view", view.get("html"), "speakers", view.get("speakers"))
            except Exception as exc:  # noqa: BLE001
                print("view skipped:", exc)
            return 0
        if status == "failed":
            err = st.get("error") or st.get("message") or st
            (OUT / "docker_gigaam_diar_summary.json").write_text(
                json.dumps({"ok": False, "task_id": task_id, "error": err}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            raise SystemExit(f"FAIL {err}")
        time.sleep(3)


if __name__ == "__main__":
    raise SystemExit(main())
