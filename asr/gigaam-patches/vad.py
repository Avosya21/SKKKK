"""VAD-driven segmentation helpers shared by PyTorch and MLX ASR.

PrimaryAnalitics patch: load pyannote/segmentation-3.0 from local checkpoint when CDN/cache fails.
"""

from __future__ import annotations

import inspect
import os
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Protocol


class SpeechRegion(Protocol):
    start: float
    end: float


class SpeechTimeline(Protocol):
    def support(self) -> Iterable[SpeechRegion]: ...


class VadAnnotation(Protocol):
    def get_timeline(self) -> SpeechTimeline: ...


class VadPipeline(Protocol):
    def __call__(self, audio_path: str) -> VadAnnotation: ...


class VadSegmenter(Protocol):
    def segment_file(
        self,
        audio_path: str,
        *,
        audio_duration: float,
    ) -> list[tuple[float, float]]: ...


class VadUnavailableError(RuntimeError):
    """The VAD model could not be initialized in the current runtime."""


def resolve_vad_device(requested: str, *, torch_module=None) -> str:
    """Resolve ``auto`` lazily so startup remains free of torch imports."""

    normalized = (requested or "auto").strip().lower() or "auto"
    if normalized != "auto":
        return normalized
    try:
        torch = torch_module or __import__("torch")
        if torch.cuda.is_available():
            return "cuda"
        mps = getattr(getattr(torch, "backends", None), "mps", None)
        if mps is not None and mps.is_available():
            return "mps"
    except (ImportError, AttributeError):
        pass
    return "cpu"


def _resolve_segmentation_checkpoint() -> Path | None:
    candidates: list[Path] = []
    for key in ("PYANNOTE_SEGMENTATION_MODEL", "PYANNOTE_SEGMENTATION_LOCAL"):
        raw = os.getenv(key, "").strip()
        if not raw:
            continue
        p = Path(raw)
        if p.is_file():
            candidates.append(p)
        else:
            candidates.append(p / "pytorch_model.bin")

    candidates.extend(
        [
            Path("/data/models/pyannote/segmentation-3.0/pytorch_model.bin"),
            Path(
                "/data/models/huggingface/hub/models--pyannote--segmentation-3.0/"
                "snapshots/e66f3d3b9eb0873085418a7b813d3b369bf160bb/pytorch_model.bin"
            ),
        ]
    )

    for candidate in candidates:
        try:
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
        except OSError:
            continue
    return None


def load_pyannote_vad_pipeline(*, token: str | None, device: str) -> VadPipeline:
    """Load segmentation-3.0 through the API exposed by the installed pyannote."""

    from ...utils.pyannote_patch import apply_pyannote_patch

    apply_pyannote_patch()

    import torch
    from pyannote.audio import Model
    from pyannote.audio.pipelines import VoiceActivityDetection

    local_checkpoint = _resolve_segmentation_checkpoint()
    if local_checkpoint is not None:
        model = Model.from_pretrained(str(local_checkpoint))
    else:
        parameters = inspect.signature(Model.from_pretrained).parameters
        token_parameter = "token" if "token" in parameters else "use_auth_token"
        model = Model.from_pretrained(
            "pyannote/segmentation-3.0",
            **{token_parameter: token},
        )
    if model is None:
        raise ValueError("pyannote/segmentation-3.0: from_pretrained вернул None")

    pipeline = VoiceActivityDetection(segmentation=model)
    pipeline.instantiate({"min_duration_on": 0.0, "min_duration_off": 0.0})
    pipeline.to(torch.device(device))
    return pipeline


class PyannoteVadSegmenter:
    """Lazily detect speech boundaries through a pyannote VAD pipeline."""

    def __init__(
        self,
        *,
        token: str | None,
        device: str,
        pipeline_loader: Callable[..., VadPipeline] | None = None,
    ):
        self.token = token
        self.device = device
        self._pipeline_loader = pipeline_loader
        self._pipeline: VadPipeline | None = None

    @property
    def pipeline(self) -> VadPipeline:
        if self._pipeline is None:
            loader = self._pipeline_loader or load_pyannote_vad_pipeline
            try:
                self._pipeline = loader(token=self.token, device=self.device)
            except Exception as exc:
                raise VadUnavailableError("VAD pipeline initialization failed") from exc
        return self._pipeline

    def segment_file(
        self,
        audio_path: str,
        *,
        audio_duration: float,
    ) -> list[tuple[float, float]]:
        annotation = self.pipeline(audio_path)
        timeline = annotation.get_timeline().support()
        regions = [(segment.start, segment.end) for segment in timeline]
        return merge_speech_regions(regions, audio_duration=audio_duration)


def merge_speech_regions(
    regions: Iterable[tuple[float, float]],
    *,
    audio_duration: float,
    max_duration: float = 22.0,
    min_duration: float = 15.0,
    strict_limit_duration: float = 30.0,
    new_chunk_threshold: float = 0.2,
) -> list[tuple[float, float]]:
    """Merge detected speech regions without inventing cuts inside speech."""

    valid = sorted(
        (
            max(0.0, float(start)),
            min(float(audio_duration), float(end)),
        )
        for start, end in regions
        if float(end) > float(start)
    )
    valid = [(start, end) for start, end in valid if end > start]
    if not valid:
        return []

    boundaries: list[tuple[float, float]] = []

    _ = strict_limit_duration

    def append_boundary(start: float, end: float) -> None:
        boundaries.append((start, end))

    current_start = valid[0][0]
    current_end = valid[0][1]

    for start, end in valid[1:]:
        if end <= current_end:
            continue
        current_duration = current_end - current_start
        should_flush = current_duration > new_chunk_threshold and (
            current_duration + (end - current_end) > max_duration
            or current_duration > min_duration
        )
        if should_flush:
            append_boundary(current_start, current_end)
            current_start = start
        current_end = end

    if current_end - current_start > new_chunk_threshold:
        append_boundary(current_start, current_end)
    return boundaries
