"""Грубая оценка «есть ли речь» по энергии WAV без сдвига таймлайна.

Не вырезает паузы из файла (плеер должен совпадать с исходником).
Нужна только как сигнал: окно почти тихое → текст ASR в склейку не пускать.
"""

from __future__ import annotations

import array
import wave
from pathlib import Path


def rms_speech_sec(
    wav_path: Path,
    start: float,
    end: float,
    *,
    frame_ms: int = 30,
    silence_rms: float = 350.0,
    min_run_ms: int = 200,
) -> float:
    """Сколько секунд внутри [start, end] похожи на речь по RMS (16-bit PCM)."""
    if end <= start or not wav_path.is_file():
        return 0.0
    with wave.open(str(wav_path), "rb") as fh:
        channels = fh.getnchannels()
        width = fh.getsampwidth()
        rate = fh.getframerate()
        nframes = fh.getnframes()
        if width != 2 or rate <= 0 or nframes <= 0:
            return 0.0
        frame_samples = max(1, int(rate * frame_ms / 1000))
        start_frame = max(0, int(start * rate))
        end_frame = min(nframes, int(end * rate))
        if end_frame <= start_frame:
            return 0.0
        fh.setpos(start_frame)
        raw = fh.readframes(end_frame - start_frame)
    samples = array.array("h")
    samples.frombytes(raw)
    if channels > 1:
        mono = array.array("h")
        for i in range(0, len(samples), channels):
            mono.append(int(sum(samples[i : i + channels]) / channels))
        samples = mono
    if not samples:
        return 0.0

    speech_frames = 0
    run = 0
    need = max(1, int(min_run_ms / frame_ms))
    for i in range(0, len(samples), frame_samples):
        chunk = samples[i : i + frame_samples]
        if not chunk:
            break
        acc = 0.0
        for s in chunk:
            acc += float(s) * float(s)
        rms = (acc / len(chunk)) ** 0.5
        if rms >= silence_rms:
            run += 1
            if run >= need:
                speech_frames += 1
        else:
            run = 0
    return speech_frames * (frame_ms / 1000.0)


def effective_speech_sec(
    diar_speech_sec: float,
    wav_path: Path | None,
    start: float,
    end: float,
) -> float:
    """Согласует диаризацию и энергию WAV.

    Чистый min() на тихом VoIP занижает речь (окно 804–965 потеряло живой текст).
    Энергия ≈ 0 при длинной «диаризации» — признак тишины/галлюцинации.
    """
    diar = max(0.0, float(diar_speech_sec))
    if wav_path is None or not wav_path.is_file():
        return diar
    energy = rms_speech_sec(wav_path, start, end)
    if diar <= 0:
        return energy
    if energy <= 0.35:
        # почти полная тишина по микрофону — не раздуваем бюджет чанками ASR
        return energy
    # мягкий пол: не даём энергии срезать больше чем до ~45% диаризации
    floor = diar * 0.45
    return min(diar, max(energy, floor))


def resolve_speech_budget_sec(
    *,
    diar_sec: float,
    energy_aware_sec: float,
    engine_alive: int,
) -> float:
    """Бюджет для Gemma: при согласии 2+ движков не убиваем окно из‑за soft energy."""
    diar = max(0.0, float(diar_sec))
    speech = max(0.0, float(energy_aware_sec))
    if engine_alive >= 2 and diar > speech:
        return max(speech, diar * 0.6)
    return speech
