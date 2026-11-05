"""HTTP-сервис ASR-движка. Контейнер живёт постоянно; модель грузится при первом /transcribe."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

ENGINE = os.environ.get("ASR_ENGINE", "whisper")
OUT_DIR = Path(os.environ.get("OUT_DIR", "/out"))
APP_DIR = Path(os.environ.get("APP_DIR", "/app"))

app = FastAPI(title=f"PrimaryAnalitics ASR {ENGINE}", version="1.0")


class TranscribeRequest(BaseModel):
    audio: str
    out_dir: str | None = None


def _cli(audio: str, out_dir: Path) -> list[str]:
    py = sys.executable
    if ENGINE == "whisper":
        return [py, str(APP_DIR / "run_whisper.py"), "--audio", audio, "--out-dir", str(out_dir)]
    if ENGINE == "gigaam":
        return [py, str(APP_DIR / "main.py"), audio, "--out-dir", str(out_dir)]
    if ENGINE == "tone":
        return [py, str(APP_DIR / "run_tone.py"), "--audio", audio, "--out-dir", str(out_dir)]
    raise HTTPException(500, f"unknown ASR_ENGINE={ENGINE}")


@app.get("/")
def root():
    return {
        "service": "primaryanalitics-asr",
        "engine": ENGINE,
        "health": "/health",
        "transcribe": "POST /transcribe {\"audio\": \"/data/file.wav\"}",
    }


@app.get("/health")
def health():
    return {"status": "ok", "engine": ENGINE}


@app.post("/transcribe")
def transcribe(req: TranscribeRequest):
    audio = Path(req.audio)
    if not audio.exists():
        raise HTTPException(400, f"file not found: {audio}")
    out_dir = Path(req.out_dir) if req.out_dir else OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = _cli(str(audio), out_dir)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise HTTPException(
            500,
            detail={"returncode": proc.returncode, "stderr": proc.stderr[-4000:], "stdout": proc.stdout[-2000:]},
        )
    stem = audio.stem.replace("_16k", "").replace("_8k", "")
    txt = out_dir / f"{stem}_raw_{ENGINE}.txt"
    text = txt.read_text(encoding="utf-8") if txt.exists() else ""
    return {
        "engine": ENGINE,
        "audio": str(audio),
        "text": text,
        "chars": len(text),
        "txt": str(txt) if txt.exists() else None,
        "log": proc.stdout[-2000:],
    }
