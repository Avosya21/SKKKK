"""Merge per-speaker meeting words onto one clock. No LLM."""

from __future__ import annotations

import subprocess
from pathlib import Path

from app.app.services.ffmpeg_tools import resolve_ffmpeg_tool

MS_PER_SEC = 1000
TIME_ROUND = 3
REPLICA_MAX_GAP_SEC = 1.2
SEC_PER_MIN = 60
SEC_PER_HOUR = 3600


TURN_ID_PREFIX = "turn"


def source_turn_id(artifact_id: str, speaker: str, start_sec: float, ordinal: int) -> str:
    start_ms = int(round(float(start_sec) * MS_PER_SEC))
    return f"{TURN_ID_PREFIX}:{artifact_id}:{speaker}:{start_ms}:{ordinal}"


def attach_turn_ids(replicas: list[dict], artifact_id: str) -> list[dict]:
    rows = []
    for index, replica in enumerate(replicas):
        item = dict(replica)
        item["source_span_id"] = source_turn_id(
            artifact_id,
            str(replica.get("speaker") or ""),
            float(replica.get("start") or 0.0),
            index,
        )
        rows.append(item)
    return rows


def format_meeting_clock(seconds: float) -> str:
    total = max(0, int(round(float(seconds))))
    hours, rem = divmod(total, SEC_PER_HOUR)
    minutes, secs = divmod(rem, SEC_PER_MIN)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def word_text(row: dict) -> str:
    if "final" not in row or row["final"] is None:
        return str(row.get("word") or row.get("text") or "").strip()
    return str(row["final"] or "").strip()


def words_from_replicas(replicas: list[dict]) -> list[dict]:
    out: list[dict] = []
    for replica in replicas:
        speaker = str(replica.get("speaker") or "")
        nested = [row for row in (replica.get("words") or []) if isinstance(row, dict)]
        if nested:
            out.extend(_inherit_speaker(row, speaker) for row in nested)
            continue
        text = word_text(replica)
        if not text:
            continue
        start = float(replica.get("start") or 0.0)
        out.append(
            {
                "word": text,
                "final": text,
                "start": start,
                "end": float(replica.get("end") or start),
                "speaker": speaker,
            }
        )
    return out


def _inherit_speaker(row: dict, speaker: str) -> dict:
    item = dict(row)
    if speaker and not item.get("speaker"):
        item["speaker"] = speaker
    return item


def shift_words(words: list[dict], offset_sec: float, speaker: str) -> list[dict]:
    shifted: list[dict] = []
    for index, row in enumerate(words):
        text = word_text(row)
        if not text:
            continue
        start = float(row.get("start") or 0.0) + offset_sec
        end = float(row.get("end") or start) + offset_sec
        item = dict(row)
        item["i"] = int(row.get("i") or index)
        item["start"] = round(start, TIME_ROUND)
        item["end"] = round(max(end, start), TIME_ROUND)
        item["speaker"] = speaker
        item["word"] = str(row.get("word") or text)
        item["final"] = text
        item["text"] = text
        shifted.append(item)
    return shifted


def merge_words(*groups: list[dict]) -> list[dict]:
    rows = [row for group in groups for row in group]
    rows.sort(key=lambda row: (float(row.get("start") or 0.0), str(row.get("speaker") or "")))
    for index, row in enumerate(rows):
        row["i"] = index
    return rows


def replicas_from_words(words: list[dict], *, max_gap: float = REPLICA_MAX_GAP_SEC) -> list[dict]:
    from asr.lab.proof import final_replicas

    return final_replicas(words, max_gap=max_gap)


def shift_replicas(replicas: list[dict], offset_sec: float, speaker: str) -> list[dict]:
    return replicas_from_words(shift_words(words_from_replicas(replicas), offset_sec, speaker))


def merge_track_replicas(*groups: list[dict]) -> list[dict]:
    return replicas_from_words(merge_words(*groups))


def merge_replicas(*groups: list[dict]) -> list[dict]:
    return merge_track_replicas(*[words_from_replicas(group) for group in groups])


LEAK_WINDOW_SEC = 0.4
LEAK_MIN_TOKENS = 2
LEAK_BY_RANK = "rank"
LEAK_BY_RMS = "rms"
LEAK_RANK_WINDOW_SEC = 1.0


def _bigrams(words: list[dict]) -> list[tuple[tuple[str, str], dict, dict]]:
    rows = [row for row in words if word_text(row)]
    out = []
    for left, right in zip(rows, rows[1:]):
        key = (word_text(left).casefold(), word_text(right).casefold())
        out.append((key, left, right))
    return out


def _rank_winner(events: list[dict] | None, when: float, first: str, second: str) -> str:
    for event in events or []:
        if event.get("kind") != "speakers":
            continue
        if abs(float(event.get("t") or 0.0) - when) > LEAK_RANK_WINDOW_SEC:
            continue
        active = [str(name) for name in (event.get("active") or [])]
        if first in active and second not in active:
            return first
        if second in active and first not in active:
            return second
        if first in active and second in active:
            return ""
    return ""


def find_leaks(
    groups: list[list[dict]],
    events: list[dict] | None = None,
    *,
    window_sec: float = LEAK_WINDOW_SEC,
) -> list[dict]:
    """Одинаковые биграммы в одно время на разных людях — кандидат протечки."""
    labeled = [(str(group[0].get("speaker") or ""), group) for group in groups if group]
    leaks: list[dict] = []
    for i, (name_a, group_a) in enumerate(labeled):
        for j, (name_b, group_b) in enumerate(labeled):
            if i >= j or name_a == name_b:
                continue
            index: dict[tuple[str, str], list[tuple[dict, dict]]] = {}
            for key, left, right in _bigrams(group_b):
                index.setdefault(key, []).append((left, right))
            for key, left_a, right_a in _bigrams(group_a):
                for left_b, right_b in index.get(key, []):
                    start_a = float(left_a.get("start") or 0.0)
                    start_b = float(left_b.get("start") or 0.0)
                    if abs(start_a - start_b) > window_sec:
                        continue
                    winner = _rank_winner(events, start_a, name_a, name_b)
                    if winner == name_a:
                        start, end = start_b, float(right_b.get("end") or start_b)
                        text = f"{word_text(left_b)} {word_text(right_b)}".strip()
                        loser, method = name_b, LEAK_BY_RANK
                    elif winner == name_b:
                        start, end = start_a, float(right_a.get("end") or start_a)
                        text = f"{word_text(left_a)} {word_text(right_a)}".strip()
                        loser, method = name_a, LEAK_BY_RANK
                    else:
                        start, end = start_a, float(right_a.get("end") or start_a)
                        text = f"{word_text(left_a)} {word_text(right_a)}".strip()
                        loser, method = name_b, ""
                    leaks.append(
                        {
                            "speaker": loser,
                            "from_speaker": winner,
                            "left": name_a,
                            "right": name_b,
                            "start": start,
                            "end": end,
                            "text": text,
                            "by": method,
                            "rank": winner,
                        }
                    )
    return leaks


def apply_leaks(groups: list[list[dict]], leaks: list[dict]) -> list[list[dict]]:
    from app.app.services.meeting_overlap import DROP_METHODS

    spans: dict[str, list[tuple[float, float]]] = {}
    for leak in leaks:
        if leak.get("by") not in DROP_METHODS:
            continue
        speaker = str(leak.get("speaker") or "")
        spans.setdefault(speaker, []).append(
            (float(leak.get("start") or 0.0), float(leak.get("end") or 0.0))
        )
    cleaned: list[list[dict]] = []
    for group in groups:
        if not group:
            cleaned.append(group)
            continue
        speaker = str(group[0].get("speaker") or "")
        lost = spans.get(speaker) or []
        if not lost:
            cleaned.append(group)
            continue
        kept = []
        for row in group:
            when = float(row.get("start") or 0.0)
            if any(start <= when <= end for start, end in lost):
                continue
            kept.append(row)
        cleaned.append(kept)
    return cleaned


def mix_track_wavs(tracks: list[tuple[Path, float]], dest: Path) -> None:
    """Mix mono WAVs with start offsets. One file — copy."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not tracks:
        raise ValueError("нет дорожек для склейки")
    if len(tracks) == 1:
        src, offset = tracks[0]
        if offset <= 0:
            if src.resolve() != dest.resolve():
                dest.write_bytes(src.read_bytes())
            return
    _run_amix(tracks, dest)


def _run_amix(tracks: list[tuple[Path, float]], dest: Path) -> None:
    ffmpeg = resolve_ffmpeg_tool("ffmpeg")
    cmd: list[str] = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    filters: list[str] = []
    labels: list[str] = []
    for index, (path, offset) in enumerate(tracks):
        cmd.extend(["-i", str(path)])
        delay_ms = max(0, int(round(float(offset) * MS_PER_SEC)))
        label = f"a{index}"
        filters.append(f"[{index}:a]adelay={delay_ms}|{delay_ms}[{label}]")
        labels.append(f"[{label}]")
    mix = "".join(labels) + f"amix=inputs={len(tracks)}:duration=longest:dropout_transition=0[out]"
    cmd.extend(["-filter_complex", ";".join(filters + [mix]), "-map", "[out]", "-ac", "1", str(dest)])
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        raise RuntimeError(err)
