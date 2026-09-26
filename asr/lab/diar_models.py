"""Движки диаризации лаборатории: Nemotron 3 (по умолчанию) и Sortformer 4spk (откат).

Оба отдают одно и то же: матрицу вероятностей ``(T, S)`` с шагом 80 мс.
``S`` — число каналов модели (8 у Nemotron, 4 у Sortformer); дальше по
конвейеру число спикеров берётся из формы матрицы, а не из константы.

Пороги у моделей разные и живут здесь, рядом с моделью:

- сегменты ``diar.json`` (onset/offset) у Nemotron — 0.5/0.5 без добавок, как в
  замерах NVIDIA; у сегментов нет потребителей по конвейеру, только сводка;
- покраска слов ``min_prob`` у Nemotron — 0.20: модель менее уверена на коротких
  словах («Да», «в», «мы»), при 0.35 они теряют спикера. Подобрано на созвонах
  проектов 35 и 23 (2026-09-25), согласие со Sortformer по словам не падает.

Выбор — переменная окружения ``DIAR_MODEL`` (``nemotron3`` | ``sortformer4``).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import sortformer
from .onnx_env import ROOT, providers_for
from .sortformer import Binarization, DiarizationResult

NEMOTRON = "nemotron3"
SORTFORMER = "sortformer4"
DEFAULT_MODEL = NEMOTRON

NEMOTRON_PYTHON = ROOT / "venvs" / "nemo-diar" / "Scripts" / "python.exe"
NEMOTRON_RUNNER = ROOT / "asr" / "lab" / "nemotron_runner.py"
NEMOTRON_FILE = ROOT / "Nemotron-3-Diarization" / "Nemotron-3-Diarization.nemo"
NEMOTRON_SHA256 = "867c53f552998f772e5b5e5c082962ae85ee7ca5669c2bc17d7f615133d4e96d"
NEMOTRON_TIMEOUT_SEC = 1800
# bf16 — как в замерах NVIDIA; fp32 на созвоне проекта 35 слил короткого спикера с основным.
NEMOTRON_PRECISION = "bf16"


@dataclass(frozen=True)
class DiarModel:
    key: str
    label: str
    num_speakers: int
    binarization: Binarization
    min_prob: float
    min_margin: float


MODELS: dict[str, DiarModel] = {
    NEMOTRON: DiarModel(
        key=NEMOTRON,
        label="Nemotron 3 Diarization 8spk",
        num_speakers=8,
        binarization=Binarization(
            onset=0.5, offset=0.5, pad_onset=0.0, pad_offset=0.0, min_duration_on=0.0, min_duration_off=0.0
        ),
        min_prob=0.20,
        min_margin=0.15,
    ),
    SORTFORMER: DiarModel(
        key=SORTFORMER,
        label="Sortformer 4spk v2.1",
        num_speakers=sortformer.NUM_SPEAKERS,
        binarization=Binarization(),
        min_prob=0.35,
        min_margin=0.15,
    ),
}


def selected_model(name: str | None = None) -> DiarModel:
    key = (name or os.environ.get("DIAR_MODEL") or DEFAULT_MODEL).strip().lower()
    if key not in MODELS:
        raise ValueError(f"неизвестная модель диаризации {key!r}; есть: {', '.join(MODELS)}")
    return MODELS[key]


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def nemotron_model_path() -> Path:
    path = Path(os.environ.get("NEMOTRON_DIAR_MODEL") or NEMOTRON_FILE)
    if not path.is_file():
        raise FileNotFoundError(f"нет модели Nemotron: {path} (скачайте nvidia/Nemotron-3-Diarization)")
    if not NEMOTRON_PYTHON.is_file():
        raise FileNotFoundError(f"нет {NEMOTRON_PYTHON}: Nemotron исполняется в venvs\\nemo-diar")
    actual = sha256_of(path)
    if actual != NEMOTRON_SHA256:
        raise RuntimeError(
            f"контрольная сумма Nemotron не совпала: ожидалось {NEMOTRON_SHA256[:12]}, получено {actual[:12]}"
        )
    return path


def _nemotron(wav: Path, *, device: str, workdir: Path, log: Callable[[str], None]) -> tuple[DiarizationResult, dict]:
    model_path = nemotron_model_path()
    out = workdir / "nemotron_probs.npy"
    meta_path = workdir / "nemotron_meta.json"
    cmd = [
        str(NEMOTRON_PYTHON), str(NEMOTRON_RUNNER),
        "--model", str(model_path), "--wav", str(wav),
        "--out", str(out), "--meta", str(meta_path), "--device", device,
        "--precision", NEMOTRON_PRECISION,
    ]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    log(f"Nemotron {model_path.name} в venvs\\nemo-diar, устройство {device}")
    proc = subprocess.run(
        cmd, cwd=str(ROOT), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=NEMOTRON_TIMEOUT_SEC,
    )
    if proc.returncode != 0 or not out.is_file():
        tail = (proc.stderr or proc.stdout or "")[-1500:]
        raise RuntimeError(f"Nemotron упал (код {proc.returncode}): {tail}")
    probabilities = np.load(out)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    out.unlink(missing_ok=True)
    meta_path.unlink(missing_ok=True)
    import soundfile as sf

    duration = float(sf.info(str(wav)).duration)
    result = DiarizationResult(
        probabilities=probabilities,
        frame_duration=sortformer.FRAME_DURATION,
        audio_duration=duration,
        model_path=model_path,
        providers=[f"torch-{meta.get('device')}"],
    )
    tool = {"name": "nemotron3-nemo", **{k: meta[k] for k in ("device", "gpu", "precision", "nemo", "torch", "infer_sec")}}
    tool["sha256"] = NEMOTRON_SHA256
    return result, tool


def diarize(
    model: DiarModel, wav: Path, *, device: str, workdir: Path, log: Callable[[str], None]
) -> tuple[DiarizationResult, dict]:
    """Прогнать запись выбранной моделью. Возвращает результат и описание инструмента."""
    if model.key == NEMOTRON:
        return _nemotron(wav, device=device, workdir=workdir, log=log)
    from .onnx_env import runtime_info

    providers = providers_for(device)
    model_path = sortformer.resolve_model()
    log(f"Sortformer {model_path.name}, провайдеры {providers}")
    engine = sortformer.SortformerOnnx(model_path, providers)
    result = engine.diarize_file(wav)
    return result, {"name": "sortformer-onnx", "providers": engine.providers, **runtime_info()}
