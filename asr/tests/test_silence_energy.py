"""Тесты оценки речи по энергии WAV (без GPU)."""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

from silence_energy import effective_speech_sec, resolve_speech_budget_sec, rms_speech_sec


def _write_wav(path: Path, samples: list[int], rate: int = 16000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(rate)
        fh.writeframes(b"".join(struct.pack("<h", max(-32767, min(32767, s))) for s in samples))


def test_rms_speech_sec_silence_vs_tone(tmp_path: Path):
    rate = 16000
    silence = [0] * (rate * 2)
    tone = [int(8000 * math.sin(2 * math.pi * 220 * i / rate)) for i in range(rate * 2)]
    silent = tmp_path / "silent.wav"
    voiced = tmp_path / "voiced.wav"
    _write_wav(silent, silence, rate)
    _write_wav(voiced, tone, rate)
    assert rms_speech_sec(silent, 0.0, 2.0) < 0.2
    assert rms_speech_sec(voiced, 0.0, 2.0) > 1.0


def test_effective_speech_takes_min_of_diar_and_energy(tmp_path: Path):
    rate = 16000
    silence = [0] * (rate * 3)
    path = tmp_path / "quiet.wav"
    _write_wav(path, silence, rate)
    # диаризация врёт «30 с речи», энергия — тишина
    assert effective_speech_sec(30.0, path, 0.0, 3.0) < 0.5


def test_resolve_budget_rescues_soft_voip_with_consensus():
    # энергия занизила, двое движков живы — берём долю диаризации
    got = resolve_speech_budget_sec(diar_sec=100.0, energy_aware_sec=21.0, engine_alive=2)
    assert got >= 60.0
    # один движок — не раздуваем
    got_one = resolve_speech_budget_sec(diar_sec=100.0, energy_aware_sec=5.0, engine_alive=1)
    assert got_one == 5.0
