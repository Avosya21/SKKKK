"""REST поверх официального образа tinkoffcreditsystems/t-one."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from tone import StreamingCTCPipeline, read_audio

ENGINE = "tone"
OUT_DIR = Path(os.environ.get("OUT_DIR", "/out"))
LOAD_FROM = os.environ.get("LOAD_FROM_FOLDER", "/models")

app = FastAPI(title="SKKKK T-one", version="1.0")
pipeline: StreamingCTCPipeline | None = None


@app.on_event("startup")
def startup() -> None:
    global pipeline
    folder = Path(LOAD_FROM) if LOAD_FROM else None
    if folder and folder.exists() and any(folder.iterdir()):
        pipeline = StreamingCTCPipeline.from_local(folder)
    else:
        pipeline = StreamingCTCPipeline.from_hugging_face()


class TranscribeRequest(BaseModel):
    audio: str
    out_dir: str | None = None


@app.get("/")
def root():
    return {
        "service": "skkkk-asr",
        "engine": ENGINE,
        "health": "/health",
        "transcribe": "POST /transcribe {\"audio\": \"/data/input_8k.wav\"}",
    }


@app.get("/health")
def health():
    return {
        "status": "ok" if pipeline is not None else "loading",
        "engine": ENGINE,
        "ready": pipeline is not None,
    }


@app.post("/transcribe")
def transcribe(req: TranscribeRequest):
    if pipeline is None:
        raise HTTPException(503, "pipeline not ready")
    audio = Path(req.audio)
    if not audio.exists():
        raise HTTPException(400, f"file not found: {audio}")
    phrases = pipeline.forward_offline(read_audio(str(audio)))
    segments = []
    texts: list[str] = []
    for ph in phrases:
        text = (getattr(ph, "text", None) or "").strip()
        segments.append(
            {
                "start": getattr(ph, "start_time", None),
                "end": getattr(ph, "end_time", None),
                "text": text,
            }
        )
        if text:
            texts.append(text)
    full = " ".join(texts)
    txt_path = None
    try:
        out_dir = Path(req.out_dir) if req.out_dir else OUT_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = audio.stem.replace("_16k", "").replace("_8k", "")
        txt_path = out_dir / f"{stem}_raw_{ENGINE}.txt"
        txt_path.write_text(full, encoding="utf-8")
    except OSError:
        txt_path = None
    return {
        "engine": ENGINE,
        "audio": str(audio),
        "text": full,
        "chars": len(full),
        "segments": segments,
        "txt": str(txt_path) if txt_path else None,
    }
