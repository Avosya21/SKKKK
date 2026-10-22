"""Общее для опыта EmbeddingGemma 2: пути, загрузка модели, выбор устройства.

Опыт изолирован от портала: свой venv (.venv), своя копия базы (out/app_snapshot.db),
портал отсюда не импортируется и в его базу ничего не пишется.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = HERE / "out"
MODEL_DIR = HERE / "model"
SNAPSHOT_DB = OUT / "app_snapshot.db"
LIVE_DB = ROOT / "data" / "app.db"
EVAL_DIR = ROOT / "docs" / "eval"

# Сколько свободной видеопамяти нужно, чтобы считать на GPU (портал держит там Qwen и ASR).
GPU_MIN_FREE_GB = 4.0


def pick_device(requested: str = "auto") -> str:
    import torch

    if requested != "auto":
        return requested
    if not torch.cuda.is_available():
        return "cpu"
    free, _total = torch.cuda.mem_get_info()
    return "cuda" if free / 1024**3 >= GPU_MIN_FREE_GB else "cpu"


def load_model(device: str = "auto", *, modalities: str = "text", threads: int = 4):
    """modalities: text | image | all. Ненужные энкодеры не грузим — меньше памяти."""
    import torch
    from sentence_transformers import SentenceTransformer

    dev = pick_device(device)
    if dev == "cpu":
        # Не отнимать все ядра у воркера портала.
        torch.set_num_threads(threads)
    config_kwargs = {
        "text": {"vision_config": None, "audio_config": None},
        "image": {"audio_config": None},
        "all": {},
    }[modalities]
    dtype = torch.bfloat16 if dev == "cuda" and torch.cuda.is_bf16_supported() else torch.float32
    model = SentenceTransformer(
        str(MODEL_DIR),
        device=dev,
        model_kwargs={"torch_dtype": dtype},
        config_kwargs=config_kwargs,
    )
    print(f"[model] device={dev} dtype={dtype} modalities={modalities}")
    return model


def read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def ollama_url() -> str:
    return os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
