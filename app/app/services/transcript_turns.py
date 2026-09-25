"""Реплики созвона из канона ``_final.txt``: кто, с какой по какую секунду, что сказал.

Реплики в БД не копируются (DECISIONS: провенанс — в ``Entity.value_json``, отдельной таблицы нет):
единственный источник — ``_final.txt``, его переписывают конвейер и кнопки переименования спикеров,
поэтому разбор всегда совпадает с каноном. Кэш — по (путь, mtime, размер).
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

KIND_SPEECH = "speech"
KIND_SCREEN = "screen"
KIND_SCREEN_SPEECH = "screen_speech"
UNKNOWN_SPEAKERS = frozenset({"unknown", "неизвестно", "неизвестный"})
# Метки неназванного голоса (lab speaker_N, Sortformer SPEAKER_NN): голос есть, имени пока нет — это не имя человека.
PLACEHOLDER_SPEAKER = re.compile(r"^(speaker_\d+|SPEAKER_\d+)$")

# [05:51 - 07:34] / [01:00:50 - 01:02:10]; у экранных блоков минуты бывают > 59 ([75:10 - 75:20]).
_TIME = r"\d+:\d{2}(?::\d{2})?"
_TAGGED = re.compile(rf"^\[(?P<start>{_TIME})\s*[-–—]\s*(?P<end>{_TIME})\]\s*(?P<rest>.*)$")
_SPEAKER = re.compile(r"^(?P<speaker>Спикер\s*№\d+|[^:\[\]]{1,80}?):\s*(?P<text>.*)$", re.I)
_SCREEN = re.compile(r"^экран\b", re.I)
_SCREEN_SPEECH = re.compile(r"^речь на экране$", re.I)


@dataclass(frozen=True)
class Turn:
    index: int
    kind: str
    speaker: str | None
    start_sec: float | None
    end_sec: float | None
    text: str
    line: str

    @property
    def is_speech(self) -> bool:
        return self.kind == KIND_SPEECH

    @property
    def known_speaker(self) -> str | None:
        """Имя говорящего, если оно есть; ``unknown`` именем не считается."""
        name = (self.speaker or "").strip()
        if not name or name.casefold() in UNKNOWN_SPEAKERS or PLACEHOLDER_SPEAKER.match(name):
            return None
        return name


def parse_timecode(raw: str) -> float:
    parts = [int(p) for p in raw.split(":")]
    if len(parts) == 3:
        return float(parts[0] * 3600 + parts[1] * 60 + parts[2])
    return float(parts[0] * 60 + parts[1])


def format_timecode(sec: float | None) -> str:
    if sec is None:
        return ""
    total = int(sec)
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _kind_for(speaker: str | None) -> str:
    name = (speaker or "").strip()
    if _SCREEN_SPEECH.match(name):
        return KIND_SCREEN_SPEECH
    if _SCREEN.match(name):
        return KIND_SCREEN
    return KIND_SPEECH


def parse_final_turns(text: str) -> list[Turn]:
    """Разобрать стенограмму: строки ``[таймкод] Имя: текст``; строка без метки дописывается к предыдущей реплике."""
    rows: list[dict[str, Any]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        tagged = _TAGGED.match(line)
        if tagged:
            rest = tagged.group("rest").strip()
            m = _SPEAKER.match(rest)
            speaker = m.group("speaker").strip() if m else None
            body = (m.group("text") if m else rest).strip()
            rows.append(
                {
                    "speaker": speaker,
                    "start": parse_timecode(tagged.group("start")),
                    "end": parse_timecode(tagged.group("end")),
                    "text": body,
                    "line": line,
                }
            )
            continue
        m = _SPEAKER.match(line)
        if m and not rows:
            rows.append({"speaker": m.group("speaker").strip(), "start": None, "end": None, "text": m.group("text").strip(), "line": line})
            continue
        if m and rows and rows[-1]["start"] is None:
            # Старый формат без таймкодов: «Спикер: текст» — отдельная реплика.
            rows.append({"speaker": m.group("speaker").strip(), "start": None, "end": None, "text": m.group("text").strip(), "line": line})
            continue
        if rows:
            rows[-1]["text"] = f"{rows[-1]['text']} {line}".strip()
            rows[-1]["line"] = f"{rows[-1]['line']}\n{line}"
        else:
            rows.append({"speaker": None, "start": None, "end": None, "text": line, "line": line})
    return [
        Turn(
            index=i,
            kind=_kind_for(row["speaker"]),
            speaker=row["speaker"],
            start_sec=row["start"],
            end_sec=row["end"],
            text=row["text"],
            line=row["line"],
        )
        for i, row in enumerate(rows)
    ]


def speech_turns(turns: list[Turn]) -> list[Turn]:
    return [t for t in turns if t.is_speech]


def turn_at(turns: list[Turn], sec: float | None) -> Turn | None:
    """Реплика речи, которая покрывает момент ``sec``; иначе ближайшая по началу."""
    if sec is None:
        return None
    timed = [t for t in turns if t.is_speech and t.start_sec is not None]
    if not timed:
        return None
    for turn in timed:
        end = turn.end_sec if turn.end_sec is not None else turn.start_sec
        if turn.start_sec <= sec <= end:
            return turn
    return min(timed, key=lambda t: abs((t.start_sec or 0.0) - sec))


def turns_in_range(turns: list[Turn], start: float | None, end: float | None) -> list[Turn]:
    """Реплики речи, пересекающие отрезок [start, end]."""
    if start is None:
        return []
    stop = end if end is not None else start
    out = []
    for t in turns:
        if not t.is_speech or t.start_sec is None:
            continue
        t_end = t.end_sec if t.end_sec is not None else t.start_sec
        if t_end >= start and t.start_sec <= stop:
            out.append(t)
    return out


_cache: dict[str, tuple[int, int, list[Turn]]] = {}
_cache_lock = threading.Lock()


def final_path_for(settings: Any, artifact: Any) -> Path:
    from app.app.services.storage import parsed_dir

    return parsed_dir(settings, artifact.project_id, artifact.source_type) / f"{artifact.artifact_id}_final.txt"


def load_turns(settings: Any, artifact: Any) -> list[Turn]:
    """Реплики созвона. Документам — пустой список. Нет ``_final.txt`` — разбор ``transcription_text`` без кэша."""
    if getattr(artifact, "file_type", "") != "audio":
        return []
    path = final_path_for(settings, artifact)
    try:
        stat = path.stat()
    except OSError:
        return parse_final_turns(getattr(artifact, "transcription_text", None) or "")
    key = str(path)
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] == stat.st_mtime_ns and hit[1] == stat.st_size:
            return hit[2]
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    turns = parse_final_turns(text)
    if turns:  # пустой разбор (файл в момент перезаписи) не кэшируем
        with _cache_lock:
            _cache[key] = (stat.st_mtime_ns, stat.st_size, turns)
    return turns
