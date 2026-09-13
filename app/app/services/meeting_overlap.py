"""Same-room и one-mic: не угадывать имя без технических признаков."""

from __future__ import annotations

import os

LEAK_BY_RANK = "rank"
LEAK_BY_RMS = "rms"
LEAK_BY_DIAR = "diarization"
LEAK_BY_UNRESOLVED = "unresolved"
SPEAKER_UNKNOWN = "unknown"
STATUS_CONFLICT = "CONFLICT"
RMS_TIE_RATIO_ENV = "MEETING_RMS_TIE_RATIO"
RMS_TIE_RATIO_DEFAULT = 1.15
DROP_METHODS = {LEAK_BY_RANK, LEAK_BY_RMS, LEAK_BY_DIAR}


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def rms_tie_ratio() -> float:
    return _env_float(RMS_TIE_RATIO_ENV, RMS_TIE_RATIO_DEFAULT)


def _as_rms_map(rms, name_a: str, name_b: str, start: float, end: float) -> dict[str, float] | None:
    if callable(rms):
        out: dict[str, float] = {}
        for name in (name_a, name_b):
            try:
                out[name] = float(rms(name, start, end))
            except (TypeError, ValueError):
                out[name] = 0.0
        return out
    return rms


def rms_winner(name_a: str, name_b: str, rms: dict[str, float] | None) -> str:
    if not rms:
        return ""
    left = rms.get(name_a)
    right = rms.get(name_b)
    if left is None or right is None:
        return ""
    if left <= 0 and right <= 0:
        return ""
    high, low = max(left, right), min(left, right)
    if low <= 0 or high / low < rms_tie_ratio():
        return ""
    return name_a if left > right else name_b


def resolve_same_room(
    name_a: str,
    name_b: str,
    *,
    rank: str = "",
    rms: dict[str, float] | None = None,
    diar=None,
    start: float = 0.0,
    end: float = 0.0,
) -> tuple[str, str]:
    if rank in (name_a, name_b):
        return rank, LEAK_BY_RANK
    winner = rms_winner(name_a, name_b, _as_rms_map(rms, name_a, name_b, start, end))
    if winner:
        return winner, LEAK_BY_RMS
    if callable(diar):
        pick = diar(name_a, name_b, start, end)
        if pick in (name_a, name_b):
            return pick, LEAK_BY_DIAR
    return "", LEAK_BY_UNRESOLVED


def refine_leaks(
    groups: list[list[dict]],
    leaks: list[dict],
    events: list[dict] | None = None,
    rms: dict[str, float] | None = None,
    diar=None,
) -> list[dict]:
    del groups, events
    out = []
    for leak in leaks:
        if leak.get("by") in DROP_METHODS and leak.get("from_speaker"):
            out.append(dict(leak))
            continue
        name_a = str(leak.get("left") or leak.get("from_speaker") or "")
        name_b = str(leak.get("right") or leak.get("speaker") or "")
        winner, method = resolve_same_room(
            name_a,
            name_b,
            rank=str(leak.get("rank") or ""),
            rms=rms,
            diar=diar,
            start=float(leak.get("start") or 0.0),
            end=float(leak.get("end") or 0.0),
        )
        row = dict(leak)
        if winner:
            row["from_speaker"] = winner
            row["speaker"] = name_b if winner != name_b else name_a
            row["by"] = method
            row.pop("status", None)
        else:
            row["by"] = LEAK_BY_UNRESOLVED
            row["status"] = STATUS_CONFLICT
        out.append(row)
    return out


def assign_one_mic(
    words: list[dict],
    segments: list[dict],
    profiles: dict[str, str] | None = None,
) -> list[dict]:
    names = profiles or {}
    out = []
    for word in words:
        when = (float(word.get("start") or 0.0) + float(word.get("end") or word.get("start") or 0.0)) / 2
        cluster = None
        for segment in segments:
            if float(segment.get("start") or 0.0) <= when <= float(segment.get("end") or 0.0):
                cluster = str(segment.get("cluster") or "")
        name = names.get(cluster or "") if cluster else ""
        item = dict(word)
        if name:
            item["speaker"] = name
        else:
            item["speaker"] = SPEAKER_UNKNOWN
            item["status"] = STATUS_CONFLICT
        out.append(item)
    return out
