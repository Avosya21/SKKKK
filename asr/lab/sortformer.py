"""Streaming Sortformer 4spk v2.1 на ONNX Runtime, без NeMo и без torch.

Потоковый алгоритм (spkcache + fifo + сжатие кеша) перенесён из
``vendor/GigaAMGUI/src/core/diarization/sortformer_onnx.py``, который в свою
очередь следует публичной модели NeMo и MIT-реализации parakeet-rs. Отличие
одно и оно принципиальное: наружу отдаётся **сырая матрица вероятностей**
T x 4, а не готовые сегменты. Пороги бинаризации — дело вызывающей стороны,
иначе спорные места невозможно ни увидеть, ни перенастроить.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .onnx_env import DIAR_MODELS_DIR

ONNX_REPO_ID = "Scrybl/diar_streaming_sortformer_4spk-v2.1"
ONNX_REVISION = "3be92087de17e4e9063c9cfabc104f9809b2d037"
ONNX_FILENAME = "diar_streaming_sortformer_4spk-v2.1.onnx"
ONNX_SHA256 = "0182539aa7f8ecfc446ec56eb320b6f9131d686f98ca8f40644830af7d4d81c8"

SAMPLE_RATE = 16_000
FRAME_DURATION = 0.08
NUM_SPEAKERS = 4

_N_FFT = 512
_WIN_LENGTH = 400
_HOP_LENGTH = 160
_N_MELS = 128
_PREEMPHASIS = 0.97
_LOG_ZERO_GUARD = 2.0**-24
_SUBSAMPLING = 8
_EMBEDDING_DIM = 512

_SILENCE_FRAMES_PER_SPEAKER = 3
_PRED_SCORE_THRESHOLD = 0.25
_STRONG_BOOST_RATE = 0.75
_WEAK_BOOST_RATE = 1.5
_MIN_POS_SCORES_RATE = 0.5
_SILENCE_THRESHOLD = 0.2

_INPUTS = ("chunk", "chunk_lengths", "spkcache", "spkcache_lengths", "fifo", "fifo_lengths")
_OUTPUTS = ("spkcache_fifo_chunk_preds", "chunk_pre_encode_embs", "chunk_pre_encode_lengths")


def resolve_model(*, allow_download: bool = True) -> Path:
    """Найти ONNX локально или скачать в data/models/onnx-diar с проверкой sha256."""
    DIAR_MODELS_DIR.mkdir(parents=True, exist_ok=True)
    local = DIAR_MODELS_DIR / ONNX_FILENAME
    if local.is_file():
        _verify(local)
        return local
    if not allow_download:
        raise FileNotFoundError(
            f"нет {local}; скачайте {ONNX_REPO_ID}/{ONNX_FILENAME} или разрешите загрузку"
        )

    from huggingface_hub import hf_hub_download

    downloaded = Path(
        hf_hub_download(
            ONNX_REPO_ID,
            ONNX_FILENAME,
            revision=ONNX_REVISION,
            local_dir=str(DIAR_MODELS_DIR),
        )
    )
    _verify(downloaded)
    return downloaded


def _verify(path: Path) -> None:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    actual = digest.hexdigest()
    if actual != ONNX_SHA256:
        raise RuntimeError(
            f"контрольная сумма Sortformer не совпала: ожидалось {ONNX_SHA256[:12]}, "
            f"получено {actual[:12]} — удалите {path} и скачайте заново"
        )


@dataclass
class DiarizationResult:
    probabilities: np.ndarray  # (T, 4) float32
    frame_duration: float
    audio_duration: float
    model_path: Path
    providers: list[str]

    @property
    def frames(self) -> int:
        return int(self.probabilities.shape[0])


class SortformerOnnx:
    """Одна ORT-сессия. Состояние сбрасывается перед каждым файлом."""

    def __init__(self, model_path: Path, providers: list[str]) -> None:
        import onnxruntime as ort

        self.model_path = Path(model_path)
        self.session = ort.InferenceSession(str(self.model_path), providers=providers)
        names = {item.name for item in self.session.get_inputs()}
        missing = sorted(set(_INPUTS) - names)
        if missing:
            raise RuntimeError(f"ONNX Sortformer: нет входов {missing}")
        out_names = {item.name for item in self.session.get_outputs()}
        missing_out = sorted(set(_OUTPUTS) - out_names)
        if missing_out:
            raise RuntimeError(f"ONNX Sortformer: нет выходов {missing_out}")

        meta = self.session.get_modelmeta().custom_metadata_map
        self.chunk_len = _meta_int(meta, "chunk_len", 124)
        self.fifo_len = _meta_int(meta, "fifo_len", 124)
        self.spkcache_len = _meta_int(meta, "spkcache_len", 188)
        self.right_context = _meta_int(meta, "right_context", 1)
        self._reset()

    @property
    def providers(self) -> list[str]:
        return [str(p) for p in self.session.get_providers()]

    def _reset(self) -> None:
        self._spkcache = np.zeros((1, 0, _EMBEDDING_DIM), dtype=np.float32)
        self._spkcache_predictions: np.ndarray | None = None
        self._fifo = np.zeros((1, 0, _EMBEDDING_DIM), dtype=np.float32)
        self._fifo_predictions = np.zeros((1, 0, NUM_SPEAKERS), dtype=np.float32)
        self._mean_silence_embedding = np.zeros((1, _EMBEDDING_DIM), dtype=np.float32)
        self._silence_frames = 0

    def diarize_file(self, wav_path: Path) -> DiarizationResult:
        import librosa
        import soundfile as sf

        samples, sample_rate = sf.read(str(wav_path), dtype="float32", always_2d=True)
        waveform = samples.mean(axis=1).astype(np.float32, copy=False)
        if sample_rate != SAMPLE_RATE:
            waveform = librosa.resample(
                waveform, orig_sr=sample_rate, target_sr=SAMPLE_RATE
            ).astype(np.float32, copy=False)
        return self.diarize_waveform(waveform)

    def diarize_waveform(self, waveform: np.ndarray) -> DiarizationResult:
        features = _extract_features(waveform)
        self._reset()
        probabilities = self._process(features)
        self._reset()
        return DiarizationResult(
            probabilities=probabilities,
            frame_duration=FRAME_DURATION,
            audio_duration=float(len(waveform)) / SAMPLE_RATE,
            model_path=self.model_path,
            providers=self.providers,
        )

    def _process(self, features: np.ndarray) -> np.ndarray:
        total_frames = int(features.shape[1])
        stride = self.chunk_len * _SUBSAMPLING
        feed_size = (self.chunk_len + self.right_context) * _SUBSAMPLING
        chunks = math.ceil(total_frames / stride) if total_frames else 0
        collected: list[np.ndarray] = []
        for index in range(chunks):
            start = index * stride
            end = min(start + feed_size, total_frames)
            current_len = end - start
            chunk = np.zeros((1, feed_size, _N_MELS), dtype=np.float32)
            chunk[:, :current_len, :] = features[:, start:end, :]
            collected.append(self._step(chunk, current_len))
        if not collected:
            return np.zeros((0, NUM_SPEAKERS), dtype=np.float32)
        return np.concatenate(collected, axis=0)

    def _step(self, chunk: np.ndarray, current_len: int) -> np.ndarray:
        cache_len = self._spkcache.shape[1]
        fifo_len = self._fifo.shape[1]
        raw_predictions, raw_embeddings, _ = self.session.run(
            list(_OUTPUTS),
            {
                "chunk": np.asarray(chunk, dtype=np.float32),
                "chunk_lengths": np.asarray([current_len], dtype=np.int64),
                "spkcache": self._spkcache,
                "spkcache_lengths": np.asarray([cache_len], dtype=np.int64),
                "fifo": self._fifo,
                "fifo_lengths": np.asarray([fifo_len], dtype=np.int64),
            },
        )
        raw_predictions = np.asarray(raw_predictions, dtype=np.float32)
        raw_embeddings = np.asarray(raw_embeddings, dtype=np.float32)
        valid_frames = math.ceil(current_len / _SUBSAMPLING)
        keep = min(self.chunk_len, valid_frames, raw_embeddings.shape[1])
        chunk_start = cache_len + fifo_len
        chunk_predictions = raw_predictions[0, chunk_start : chunk_start + keep, :]
        chunk_embeddings = raw_embeddings[:, :keep, :]
        previous_fifo_predictions = raw_predictions[0, cache_len : cache_len + fifo_len, :]

        self._fifo = np.concatenate((self._fifo, chunk_embeddings), axis=1)
        self._fifo_predictions = np.concatenate(
            (previous_fifo_predictions, chunk_predictions), axis=0
        )[None, :, :]

        if self._fifo.shape[1] > self.fifo_len:
            pop_count = max(self.chunk_len, max(0, valid_frames - self.fifo_len) + fifo_len)
            pop_count = min(pop_count, self._fifo.shape[1])
            popped_embeddings = self._fifo[:, :pop_count, :].copy()
            popped_predictions = self._fifo_predictions[:, :pop_count, :].copy()
            self._absorb_silence(popped_embeddings, popped_predictions)
            self._fifo = self._fifo[:, pop_count:, :].copy()
            self._fifo_predictions = self._fifo_predictions[:, pop_count:, :].copy()
            self._spkcache = np.concatenate((self._spkcache, popped_embeddings), axis=1)
            if self._spkcache_predictions is not None:
                self._spkcache_predictions = np.concatenate(
                    (self._spkcache_predictions, popped_predictions), axis=1
                )
            if self._spkcache.shape[1] > self.spkcache_len:
                if self._spkcache_predictions is None:
                    self._spkcache_predictions = np.concatenate(
                        (raw_predictions[:, :cache_len, :].copy(), popped_predictions), axis=1
                    )
                self._compress_cache()

        return chunk_predictions.copy()

    def _absorb_silence(self, embeddings: np.ndarray, predictions: np.ndarray) -> None:
        silent = predictions[0].sum(axis=1) < _SILENCE_THRESHOLD
        for embedding in embeddings[0, silent, :]:
            total = self._mean_silence_embedding[0] * self._silence_frames + embedding
            self._silence_frames += 1
            self._mean_silence_embedding[0] = total / self._silence_frames

    def _compress_cache(self) -> None:
        if self._spkcache_predictions is None:
            return
        predictions = self._spkcache_predictions[0]
        frames = predictions.shape[0]
        per_speaker = self.spkcache_len // NUM_SPEAKERS
        if per_speaker <= _SILENCE_FRAMES_PER_SPEAKER:
            self._spkcache = self._spkcache[:, : self.spkcache_len, :].copy()
            self._spkcache_predictions = self._spkcache_predictions[:, : self.spkcache_len, :].copy()
            return

        usable = per_speaker - _SILENCE_FRAMES_PER_SPEAKER
        strong = int(usable * _STRONG_BOOST_RATE)
        weak = int(usable * _WEAK_BOOST_RATE)
        minimum_positive = int(usable * _MIN_POS_SCORES_RATE)
        probability = np.maximum(predictions, _PRED_SCORE_THRESHOLD)
        inverse = np.maximum(1.0 - predictions, _PRED_SCORE_THRESHOLD)
        scores = (
            np.log(probability)
            - np.log(inverse)
            + np.log(inverse).sum(axis=1, keepdims=True)
            - math.log(0.5)
        )
        positive_count = (scores > 0.0).sum(axis=0)
        disabled = (predictions <= 0.5) | (
            (scores <= 0.0) & (positive_count[None, :] >= minimum_positive)
        )
        scores[disabled] = -np.inf
        for speaker in range(NUM_SPEAKERS):
            order = np.argsort(-scores[:, speaker], kind="stable")
            for count, scale in ((strong, 2.0), (weak, 1.0)):
                indices = order[: min(count, len(order))]
                valid = np.isfinite(scores[indices, speaker])
                scores[indices[valid], speaker] -= scale * math.log(0.5)

        padded = np.full((frames + _SILENCE_FRAMES_PER_SPEAKER, NUM_SPEAKERS), -np.inf, dtype=np.float32)
        padded[:frames] = scores
        padded[frames:] = np.inf
        flat_scores = padded.T.reshape(-1)
        selected = np.argsort(-flat_scores, kind="stable")[: self.spkcache_len]
        selected = np.where(np.isneginf(flat_scores[selected]), 99_999, selected)
        selected.sort()
        frame_indices = selected % padded.shape[0]
        is_disabled = (selected == 99_999) | (frame_indices >= frames)
        frame_indices = np.where(is_disabled, 0, frame_indices)

        new_embeddings = np.zeros((1, self.spkcache_len, _EMBEDDING_DIM), dtype=np.float32)
        new_predictions = np.zeros((1, self.spkcache_len, NUM_SPEAKERS), dtype=np.float32)
        for index, (frame, skip) in enumerate(zip(frame_indices, is_disabled, strict=True)):
            if skip:
                new_embeddings[0, index] = self._mean_silence_embedding[0]
            else:
                new_embeddings[0, index] = self._spkcache[0, frame]
                new_predictions[0, index] = self._spkcache_predictions[0, frame]
        self._spkcache = new_embeddings
        self._spkcache_predictions = new_predictions


def _meta_int(meta: dict[str, str], name: str, default: int) -> int:
    try:
        value = int(meta.get(name, ""))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _extract_features(audio: np.ndarray) -> np.ndarray:
    import librosa

    samples = np.asarray(audio, dtype=np.float32).reshape(-1)
    if samples.size == 0:
        return np.zeros((1, 0, _N_MELS), dtype=np.float32)
    emphasized = samples.copy()
    emphasized[1:] -= _PREEMPHASIS * samples[:-1]
    spectrum = (
        np.abs(
            librosa.stft(
                emphasized,
                n_fft=_N_FFT,
                hop_length=_HOP_LENGTH,
                win_length=_WIN_LENGTH,
                window="hann",
                center=True,
                pad_mode="constant",
            )
        )
        ** 2
    )
    mel_basis = librosa.filters.mel(
        sr=SAMPLE_RATE,
        n_fft=_N_FFT,
        n_mels=_N_MELS,
        fmin=0.0,
        fmax=SAMPLE_RATE / 2,
        htk=False,
        norm="slaney",
        dtype=np.float32,
    )
    with np.errstate(divide="ignore", over="ignore", invalid="ignore"):
        features = np.log(mel_basis @ spectrum + _LOG_ZERO_GUARD)
    return np.asarray(features.T[None, :, :], dtype=np.float32)


@dataclass(frozen=True)
class Binarization:
    onset: float = 0.64
    offset: float = 0.74
    pad_onset: float = 0.06
    pad_offset: float = 0.0
    min_duration_on: float = 0.10
    min_duration_off: float = 0.15

    def as_dict(self) -> dict[str, float]:
        return {
            "onset": self.onset,
            "offset": self.offset,
            "pad_onset": self.pad_onset,
            "pad_offset": self.pad_offset,
            "min_duration_on": self.min_duration_on,
            "min_duration_off": self.min_duration_off,
        }


def binarize(
    probabilities: np.ndarray,
    *,
    rules: Binarization | None = None,
    audio_duration: float | None = None,
) -> list[dict[str, Any]]:
    """Матрица вероятностей → сегменты. Гистерезис onset/offset как в NeMo.

    Число спикеров — по ширине матрицы: у Nemotron 8 каналов, у Sortformer 4.
    """
    rules = rules or Binarization()
    total = probabilities.shape[0] * FRAME_DURATION
    limit = audio_duration if audio_duration is not None else total
    segments: list[dict[str, Any]] = []
    for speaker in range(probabilities.shape[1]):
        for start, end in _speaker_spans(probabilities[:, speaker], rules):
            stop = min(end, limit)
            if stop > start:
                segments.append(
                    {
                        "speaker": f"speaker_{speaker}",
                        "speaker_index": speaker,
                        "start": round(float(start), 3),
                        "end": round(float(stop), 3),
                    }
                )
    segments.sort(key=lambda row: (row["start"], row["end"], row["speaker_index"]))
    return segments


def _speaker_spans(scores: np.ndarray, rules: Binarization) -> list[tuple[float, float]]:
    raw: list[tuple[float, float]] = []
    start: int | None = None
    for frame, probability in enumerate(scores):
        if start is None and probability >= rules.onset:
            start = frame
        elif start is not None and probability < rules.offset:
            begin = max(0.0, start * FRAME_DURATION - rules.pad_onset)
            end = frame * FRAME_DURATION + rules.pad_offset
            if end - begin >= rules.min_duration_on:
                raw.append((begin, end))
            start = None
    if start is not None:
        begin = max(0.0, start * FRAME_DURATION - rules.pad_onset)
        end = len(scores) * FRAME_DURATION + rules.pad_offset
        if end - begin >= rules.min_duration_on:
            raw.append((begin, end))

    merged: list[tuple[float, float]] = []
    for span in raw:
        if merged and span[0] - merged[-1][1] < rules.min_duration_off:
            merged[-1] = (merged[-1][0], span[1])
        else:
            merged.append(span)
    return merged
