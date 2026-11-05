"""Клиент PrimaryAnalitics к трём ASR-контейнерам."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import httpx

WHISPER_URL = os.environ.get("WHISPER_URL", "http://127.0.0.1:18101")
GIGAAM_URL = os.environ.get("GIGAAM_URL", "http://127.0.0.1:18102")
TONE_URL = os.environ.get("TONE_URL", "http://127.0.0.1:18103")
GIGAAM_API_KEY = os.environ.get("GIGAAM_API_KEY", "")

ENGINES = {"whisper": WHISPER_URL, "gigaam": GIGAAM_URL, "tone": TONE_URL}

WORK_DIR = Path(__file__).resolve().parent / "work"
DEFAULT_AUDIO = {
    "whisper": WORK_DIR / "input_16k.wav",
    "gigaam": WORK_DIR / "input_16k.wav",
    "tone": "/data/input_8k.wav",
}


def health(engine: str | None = None) -> dict[str, Any]:
    targets = {engine: ENGINES[engine]} if engine else ENGINES
    out: dict[str, Any] = {}
    for name, base in targets.items():
        try:
            path = "/health"
            r = httpx.get(f"{base}{path}", timeout=10.0)
            r.raise_for_status()
            out[name] = {"status": "ok", "data": r.json()}
        except Exception as exc:  # noqa: BLE001
            out[name] = {"status": "down", "error": str(exc)}
    return out


def _gigaam_headers() -> dict[str, str]:
    if not GIGAAM_API_KEY:
        raise RuntimeError("Задайте GIGAAM_API_KEY в .env")
    return {"X-API-Key": GIGAAM_API_KEY}


def _format_whisper_text(body: dict[str, Any]) -> str:
    """Склеить сегменты в реплики [SPEAKER_xx], если диаризация есть."""
    segs = body.get("segments") or []
    if not segs or not any(isinstance(s, dict) and s.get("speaker") for s in segs):
        return body.get("text") or ""
    lines: list[str] = []
    prev: str | None = None
    buf: list[str] = []
    for seg in segs:
        if not isinstance(seg, dict):
            continue
        speaker = str(seg.get("speaker") or "SPEAKER_00")
        chunk = (seg.get("text") or "").strip()
        if not chunk:
            continue
        if prev is None:
            prev = speaker
        if speaker != prev:
            lines.append(f"[{prev}] {' '.join(buf)}")
            buf = [chunk]
            prev = speaker
        else:
            buf.append(chunk)
    if prev is not None and buf:
        lines.append(f"[{prev}] {' '.join(buf)}")
    return "\n".join(lines)


def _transcribe_whisper(base: str, host_file: Path, timeout: httpx.Timeout) -> dict[str, Any]:
    with host_file.open("rb") as fh:
        r = httpx.post(
            f"{base}/v1/audio/transcriptions",
            files={"file": (host_file.name, fh, "audio/wav")},
            data={
                "language": "ru",
                "response_format": "verbose_json",
                "timestamp_granularities[]": "segment",
            },
            timeout=timeout,
        )
    r.raise_for_status()
    body = r.json()
    text = _format_whisper_text(body)
    return {
        "engine": "whisper",
        "audio": str(host_file),
        "text": text,
        "chars": len(text),
        "raw": body,
    }


def _transcribe_gigaam(base: str, host_file: Path, timeout: httpx.Timeout) -> dict[str, Any]:
    headers = _gigaam_headers()
    with host_file.open("rb") as fh:
        r = httpx.post(
            f"{base}/api/v1/transcribe",
            params={
                "asr_backend": "pytorch",
                "asr_model": "v3_e2e_rnnt",
                "enable_diarization": "false",
            },
            files={"file": (host_file.name, fh, "audio/wav")},
            headers=headers,
            timeout=timeout,
        )
    r.raise_for_status()
    task_id = r.json()["task_id"]
    deadline = time.time() + 3600
    while time.time() < deadline:
        st = httpx.get(f"{base}/api/v1/tasks/{task_id}", headers=headers, timeout=30.0)
        st.raise_for_status()
        body = st.json()
        status = body.get("status")
        if status == "completed":
            res = httpx.get(
                f"{base}/api/v1/tasks/{task_id}/result",
                headers=headers,
                timeout=60.0,
            )
            res.raise_for_status()
            data = res.json()
            text = data.get("transcription") or ""
            return {
                "engine": "gigaam",
                "audio": str(host_file),
                "text": text,
                "chars": len(text),
                "task_id": task_id,
                "raw": data,
            }
        if status == "failed":
            raise RuntimeError(body.get("error") or body)
        time.sleep(2)
    raise TimeoutError(f"GigaAM task {task_id} timed out")


def transcribe(engine: str, audio: str | None = None, out_dir: str = "/out") -> dict[str, Any]:
    base = ENGINES[engine]
    timeout = httpx.Timeout(3600.0, connect=10.0)

    if engine == "whisper":
        host_file = Path(audio) if audio else DEFAULT_AUDIO["whisper"]
        return _transcribe_whisper(base, host_file, timeout)

    if engine == "gigaam":
        host_file = Path(audio) if audio else DEFAULT_AUDIO["gigaam"]
        return _transcribe_gigaam(base, host_file, timeout)

    container_path = audio or DEFAULT_AUDIO["tone"]
    r = httpx.post(
        f"{base}/transcribe",
        json={"audio": container_path, "out_dir": out_dir},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()
