"""Разбор ответа GigaAM (Docker) для лаборатории.

Этап 3 — независимое мнение GigaAM о том же аудио прогона. Модуль только
разбирает и нормализует то, что отдал контейнер: реплики, спикеров, таймкоды.
Ничего не решает и не переписывает — решения принимает сверка (``reconcile``).

Особенность формата: контейнер отдаёт таймкоды с точностью до секунды, а
реплики разных спикеров **могут перекрываться по времени**. Ни то, ни другое
не сглаживаем: сверка обязана видеть перекрытие как есть.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
for candidate in (ROOT, ROOT / "asr"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

GIGA_DIR = "giga"
SPEAKER_NUM_RE = re.compile(r"(?:Спикер\s*№?\s*|SPEAKER[_\s]*)(\d+)", re.IGNORECASE)


def speaker_key(label: str) -> str:
    """``Спикер №2`` → ``giga_2``. Своя нумерация, не путать с лабораторной."""
    match = SPEAKER_NUM_RE.search(label or "")
    if match:
        return f"giga_{int(match.group(1))}"
    cleaned = (label or "").strip()
    return f"giga:{cleaned}" if cleaned else "giga:?"


def parse_turns(timecoded_text: str) -> list[dict[str, Any]]:
    """Строки ``[MM:SS - MM:SS] Спикер №N: текст`` → реплики as-is."""
    from speakers import parse_timecode_lines

    turns: list[dict[str, Any]] = []
    for index, turn in enumerate(parse_timecode_lines(timecoded_text or "")):
        text = (turn.text or "").strip()
        turns.append(
            {
                "i": index,
                "start": round(float(turn.start), 3),
                "end": round(float(turn.end), 3),
                "speaker_label": turn.speaker or "",
                "speaker": speaker_key(turn.speaker),
                "text": text,
                "chars": len(text),
            }
        )
    return turns


def speaker_stats(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seconds: dict[str, float] = {}
    chars: dict[str, int] = {}
    for turn in turns:
        key = turn["speaker"]
        seconds[key] = seconds.get(key, 0.0) + max(0.0, float(turn["end"]) - float(turn["start"]))
        chars[key] = chars.get(key, 0) + int(turn["chars"])
    total = sum(seconds.values()) or 1.0
    rows = [
        {
            "speaker": key,
            "label": next(t["speaker_label"] for t in turns if t["speaker"] == key),
            "seconds": round(value, 1),
            "share": round(value / total, 3),
            "chars": chars.get(key, 0),
            "turns": sum(1 for t in turns if t["speaker"] == key),
        }
        for key, value in seconds.items()
    ]
    rows.sort(key=lambda row: -row["seconds"])
    return rows


def overlap_seconds(turns: list[dict[str, Any]]) -> float:
    """Сколько секунд реплики накладываются друг на друга — это норма для pyannote."""
    ordered = sorted(turns, key=lambda row: (float(row["start"]), float(row["end"])))
    total = 0.0
    for index, row in enumerate(ordered):
        for other in ordered[index + 1 :]:
            if float(other["start"]) >= float(row["end"]):
                break
            total += min(float(row["end"]), float(other["end"])) - float(other["start"])
    return round(total, 1)


def summarize(turns: list[dict[str, Any]], *, plain_text: str = "") -> dict[str, Any]:
    speakers = speaker_stats(turns)
    return {
        "turn_count": len(turns),
        "speaker_count": len(speakers),
        "speakers": speakers,
        "chars_timecoded": sum(int(t["chars"]) for t in turns),
        "chars_plain": len(plain_text or ""),
        "overlap_sec": overlap_seconds(turns),
        "span": {
            "start": min((float(t["start"]) for t in turns), default=0.0),
            "end": max((float(t["end"]) for t in turns), default=0.0),
        },
    }
