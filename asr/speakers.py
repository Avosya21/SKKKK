"""Диаризация отдельно от ASR: реплики, сглаживание границ, overlay текста, имена.

Идеи навигации — из OpenTranscribe (не копируем их код). Кто говорит берётся
из PyAnnote, слова — из ASR. Gemma спикеров не выдумывает.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

LINE_RE = re.compile(r"^\[([^\]]+)\]\s*(.*)$")
RANGE_RE = re.compile(r"^(.+?)\s*-\s*(.+)$")
SPEAKER_PREFIX_RE = re.compile(r"^(Спикер\s*№?\s*\d+|SPEAKER[_\s]*\d+)\s*:\s*(.*)$", re.IGNORECASE)
SPEAKER_NUM_RE = re.compile(r"(?:Спикер\s*№?\s*|SPEAKER[_\s]*)(\d+)", re.IGNORECASE)

MIN_ISLAND_SEC = 0.8
MIN_TEXT_TURN_SEC = 0.6
MIN_PART_CHARS = 8
SAME_SPEAKER_GAP_SEC = 2.0
MAX_CONTAINED_SEC = 4.0
STICKY_ISLAND_SEC = 2.5
_KEEP_SHORT = {"да", "нет", "ну", "а", "и", "я", "ок", "угу", "эм", "ага", "мм", "хм", "ой"}


@dataclass
class Turn:
    start: float
    end: float
    speaker: str
    text: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def parse_clock(token: str) -> float:
    parts = token.strip().split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    return float(parts[0])


def format_clock(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def canonical_speaker(label: str) -> str:
    raw = (label or "").strip()
    if not raw:
        return "Спикер №?"
    match = SPEAKER_NUM_RE.search(raw)
    if match:
        return f"Спикер №{int(match.group(1))}"
    return raw


def parse_timecode_lines(text: str) -> list[Turn]:
    turns: list[Turn] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        wrapped = LINE_RE.match(line)
        if not wrapped:
            continue
        span = RANGE_RE.match(wrapped.group(1).strip())
        if not span:
            continue
        start = parse_clock(span.group(1))
        end = parse_clock(span.group(2))
        rest = wrapped.group(2).strip()
        speaker = ""
        body = rest
        labeled = SPEAKER_PREFIX_RE.match(rest)
        if labeled:
            speaker = canonical_speaker(labeled.group(1))
            body = (labeled.group(2) or "").strip()
        turns.append(Turn(start=start, end=max(end, start), speaker=speaker, text=body))
    return turns


def parse_whisper_segments(payload: dict) -> list[Turn]:
    turns: list[Turn] = []
    for seg in payload.get("segments") or []:
        if not isinstance(seg, dict):
            continue
        start = float(seg.get("start") or 0.0)
        end = float(seg.get("end") or start)
        body = (seg.get("text") or "").strip()
        if not body:
            continue
        turns.append(Turn(start=start, end=max(end, start), speaker="", text=body))
    return turns


def merge_adjacent(turns: list[Turn], gap_sec: float = 0.35) -> list[Turn]:
    if not turns:
        return []
    ordered = sorted(turns, key=lambda t: (t.start, t.end))
    out = [
        Turn(
            start=ordered[0].start,
            end=ordered[0].end,
            speaker=ordered[0].speaker,
            text=ordered[0].text,
        )
    ]
    for turn in ordered[1:]:
        prev = out[-1]
        same = turn.speaker == prev.speaker and turn.speaker != ""
        close = turn.start <= prev.end + gap_sec
        if same and close:
            prev.end = max(prev.end, turn.end)
            prev.text = " ".join(p for p in (prev.text, turn.text) if p).strip()
        else:
            out.append(Turn(start=turn.start, end=turn.end, speaker=turn.speaker, text=turn.text))
    return out


def smooth_islands(turns: list[Turn], min_duration: float = MIN_ISLAND_SEC) -> list[Turn]:
    """Схлопывает короткие чужие островки на стыке реплик одного человека."""
    merged = merge_adjacent(turns)
    if len(merged) < 3:
        return merged
    changed = True
    while changed:
        changed = False
        i = 1
        while i < len(merged) - 1:
            prev, cur, nxt = merged[i - 1], merged[i], merged[i + 1]
            island = (
                cur.duration < min_duration
                and prev.speaker
                and prev.speaker == nxt.speaker
                and cur.speaker != prev.speaker
            )
            if island:
                prev.end = max(prev.end, cur.end, nxt.end)
                prev.text = " ".join(p for p in (prev.text, cur.text, nxt.text) if p).strip()
                del merged[i : i + 2]
                changed = True
                if i > 1:
                    i -= 1
                continue
            i += 1
        merged = merge_adjacent(merged)
    return merged


def _overlap(a: Turn, b: Turn) -> float:
    return max(0.0, min(a.end, b.end) - max(a.start, b.start))


def overlay_text(
    text_turns: list[Turn],
    speaker_turns: list[Turn],
    join_gap_sec: float | None = 0.35,
) -> list[Turn]:
    """Вешает слова ASR на интервалы PyAnnote по пересечению времени.

    Целый чанк остаётся целым: спикер — у кого больше пересечения. Для чанков
    18–20 с это прячет смену голоса внутри чанка; тогда нужен carve_text_by_speakers.

    join_gap_sec=None — не склеивать соседние фразы (нужно для Whisper-фрагментов,
    иначе max_phrase_sec снова схлопнется в минуту монолога).
    """
    if not text_turns:
        return [Turn(start=t.start, end=t.end, speaker=t.speaker, text=t.text) for t in speaker_turns]
    if not speaker_turns:
        return [Turn(start=t.start, end=t.end, speaker=t.speaker or "Спикер №?", text=t.text) for t in text_turns]
    speakers = merge_adjacent(
        [Turn(start=t.start, end=t.end, speaker=t.speaker, text="") for t in speaker_turns if t.speaker]
    )
    assigned: list[Turn] = []
    for piece in text_turns:
        best = None
        best_ov = 0.0
        for spk in speakers:
            ov = _overlap(piece, spk)
            if ov > best_ov:
                best_ov = ov
                best = spk
        if best is None:
            nearest = min(speakers, key=lambda s: min(abs(s.start - piece.start), abs(s.end - piece.end)))
            speaker = nearest.speaker
        else:
            speaker = best.speaker
        assigned.append(Turn(start=piece.start, end=piece.end, speaker=speaker, text=piece.text))
    if join_gap_sec is None:
        return assigned
    return merge_adjacent(assigned, gap_sec=join_gap_sec)


def apply_map(
    turns: list[Turn],
    mapping: dict[str, str],
    join_gap_sec: float | None = 0.35,
) -> list[Turn]:
    table = {canonical_speaker(k): v.strip() for k, v in mapping.items() if v and str(v).strip()}
    out: list[Turn] = []
    for turn in turns:
        name = table.get(turn.speaker, turn.speaker)
        out.append(Turn(start=turn.start, end=turn.end, speaker=name, text=turn.text))
    if join_gap_sec is None:
        return out
    return merge_adjacent(out, gap_sec=join_gap_sec)


def merge_speakers(turns: list[Turn], source: str, target: str) -> list[Turn]:
    src = canonical_speaker(source)
    dst = canonical_speaker(target)
    remapped = [
        Turn(
            start=t.start,
            end=t.end,
            speaker=dst if canonical_speaker(t.speaker) == src else t.speaker,
            text=t.text,
        )
        for t in turns
    ]
    return merge_adjacent(remapped)


def drop_minor_speakers(turns: list[Turn], min_seconds: float = 12.0) -> list[Turn]:
    """Микрокластер целиком → один основной голос, не «к ближайшему» на каждый кусок.

    Поштучная привязка склеивала двух женщин в одном месте и разделяла их в другом.
    """
    stats = {row["speaker"]: row["seconds"] for row in talk_time(turns)}
    keep = {name for name, sec in stats.items() if sec >= min_seconds}
    if not keep:
        return turns
    majors = [t for t in turns if t.speaker in keep]
    if not majors:
        return turns
    votes: dict[str, dict[str, float]] = {}
    for turn in turns:
        if turn.speaker in keep:
            continue
        nearest = min(
            majors,
            key=lambda s: min(abs(s.start - turn.start), abs(s.end - turn.end), abs(s.start - turn.end)),
        )
        bucket = votes.setdefault(turn.speaker, {})
        bucket[nearest.speaker] = bucket.get(nearest.speaker, 0.0) + turn.duration
    alias = {
        minor: max(targets.items(), key=lambda kv: kv[1])[0]
        for minor, targets in votes.items()
        if targets
    }
    remapped = [
        Turn(start=t.start, end=t.end, speaker=alias.get(t.speaker, t.speaker), text=t.text)
        for t in turns
    ]
    return merge_adjacent(remapped)


def renumber_speakers(turns: list[Turn]) -> list[Turn]:
    order = list(dict.fromkeys(t.speaker for t in turns))
    names = {old: f"Спикер №{i + 1}" for i, old in enumerate(order)}
    return [
        Turn(start=t.start, end=t.end, speaker=names.get(t.speaker, t.speaker), text=t.text)
        for t in turns
    ]


def talk_time(turns: list[Turn]) -> list[dict]:
    totals: dict[str, float] = {}
    for turn in turns:
        totals[turn.speaker] = totals.get(turn.speaker, 0.0) + turn.duration
    overall = sum(totals.values()) or 1.0
    rows = [
        {
            "speaker": name,
            "seconds": round(sec, 1),
            "share": round(sec / overall, 3),
        }
        for name, sec in sorted(totals.items(), key=lambda kv: -kv[1])
    ]
    return rows


def format_turns_txt(turns: list[Turn]) -> str:
    blocks: list[str] = []
    for turn in turns:
        blocks.append(f"[{turn.speaker}]\n{turn.text}".rstrip())
    return "\n\n".join(blocks).strip() + ("\n" if turns else "")


def format_turns_timecodes(turns: list[Turn]) -> str:
    lines = [
        f"[{format_clock(t.start)} - {format_clock(t.end)}] {t.speaker}: {t.text}".rstrip()
        for t in turns
    ]
    return "\n".join(lines) + ("\n" if lines else "")


def turns_to_json(turns: list[Turn], mapping: dict[str, str] | None = None) -> dict:
    return {
        "min_speakers": 2,
        "max_speakers": 15,
        "speakers": sorted({t.speaker for t in turns}),
        "talk_time": talk_time(turns),
        "mapping": mapping or {},
        "turns": [asdict(t) for t in turns],
    }


def load_mapping(path: Path | None) -> dict[str, str]:
    if path is None or not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


_CUT_CHARS = ".!?…;:—\n"


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch in "ёЁ-"


def _valid_cut(body: str, i: int) -> bool:
    if i <= 0 or i >= len(body):
        return False
    return not (_is_word_char(body[i - 1]) and _is_word_char(body[i]))


def _best_cut(body: str, target: int, lo: int, hi: int) -> int | None:
    best: int | None = None
    best_score = None
    for i in range(lo, hi + 1):
        if not _valid_cut(body, i):
            continue
        ch = body[i - 1]
        score = 0
        if ch in _CUT_CHARS:
            score = 3
        elif ch.isspace():
            score = 1
        elif not _is_word_char(ch):
            score = 2
        ranked = score * 1000 - abs(i - target)
        if best_score is None or ranked > best_score:
            best_score = ranked
            best = i
    return best


def looks_like_cut_letter(text: str) -> bool:
    token = (text or "").strip()
    if not token or token.lower() in _KEEP_SHORT:
        return False
    return len(token) <= 2 and token.isalpha()


def _should_glue_to_next(cur_text: str, next_text: str) -> bool:
    token = (cur_text or "").strip()
    nxt = (next_text or "").lstrip()
    if not token or not nxt or not nxt[0].islower():
        return False
    if looks_like_cut_letter(token):
        return True
    return token.isalpha() and len(token) <= 2


def drop_contained_islands(turns: list[Turn], max_inner: float = MAX_CONTAINED_SEC) -> list[Turn]:
    """Короткий чужой интервал внутри длинного — ложный детект, не смена голоса."""
    if len(turns) < 2:
        return turns
    drop: set[int] = set()
    for j, inner in enumerate(turns):
        if inner.duration >= max_inner:
            continue
        for outer in turns:
            if outer is inner or outer.speaker == inner.speaker:
                continue
            if outer.duration <= inner.duration:
                continue
            ov = _overlap(inner, outer)
            if inner.duration > 0 and ov / inner.duration >= 0.85:
                drop.add(j)
                break
    return [t for i, t in enumerate(turns) if i not in drop]


def flatten_overlaps(turns: list[Turn], min_keep: float = 0.7) -> list[Turn]:
    """Пока текущий голос ещё в кадре — не переключаем метку.

    Иначе пересечение PyAnnote (один голос, два кластера) каждые 2–3 с меняет спикера.
    """
    if not turns:
        return []
    points = sorted({round(t.start, 4) for t in turns} | {round(t.end, 4) for t in turns})
    pieces: list[Turn] = []
    current: str | None = None
    for start, end in zip(points, points[1:]):
        if end - start < 1e-3:
            continue
        covering = [t for t in turns if t.start < end - 1e-4 and t.end > start + 1e-4]
        covering = [t for t in covering if t.duration >= min_keep] or covering
        if not covering:
            current = None
            continue
        names = {t.speaker for t in covering}
        if current is not None and current in names:
            winner = current
        else:
            winner = max(covering, key=lambda t: (t.duration, -t.start)).speaker
            current = winner
        pieces.append(Turn(start=start, end=end, speaker=winner, text=""))
    return merge_adjacent(pieces, gap_sec=0.05)


def absorb_brief_turns(turns: list[Turn], min_duration: float = MIN_TEXT_TURN_SEC) -> list[Turn]:
    """Нулевые и полусекундные вспышки PyAnnote не получают свой кусок текста."""
    if not turns:
        return []
    ordered = sorted(turns, key=lambda t: (t.start, t.end))
    out: list[Turn] = []
    pending: list[Turn] = []
    for turn in ordered:
        if turn.duration < min_duration:
            if out:
                out[-1].end = max(out[-1].end, turn.end)
            else:
                pending.append(turn)
            continue
        start = turn.start
        if pending:
            start = min(pending[0].start, start)
            pending = []
        out.append(Turn(start=start, end=turn.end, speaker=turn.speaker, text=turn.text))
    if pending and out:
        out[-1].end = max(out[-1].end, pending[-1].end)
    elif pending:
        out.extend(Turn(t.start, t.end, t.speaker, t.text) for t in pending)
    return merge_adjacent(out, gap_sec=SAME_SPEAKER_GAP_SEC)


def prepare_speaker_timeline(turns: list[Turn], min_seconds: float = 20.0) -> list[Turn]:
    """Сырой PyAnnote → интервалы, на которые уже можно вешать склейку Gemma."""
    prepared = drop_minor_speakers(turns, min_seconds=min_seconds)
    prepared = drop_contained_islands(prepared)
    prepared = flatten_overlaps(prepared, min_keep=0.7)
    prepared = absorb_brief_turns(prepared)
    prepared = smooth_islands(prepared, min_duration=STICKY_ISLAND_SEC)
    return merge_adjacent(prepared, gap_sec=SAME_SPEAKER_GAP_SEC)


def reattach_cut_letters(turns: list[Turn], join_gap_sec: float | None = 0.35) -> list[Turn]:
    """Страховка: «Э» + «то в первую очередь» снова одно слово."""
    items = [Turn(t.start, t.end, t.speaker, t.text) for t in turns]
    i = 0
    while i < len(items):
        cur = items[i]
        nxt = items[i + 1] if i + 1 < len(items) else None
        prev = items[i - 1] if i else None
        if nxt is not None and _should_glue_to_next(cur.text, nxt.text):
            token = cur.text.strip()
            rest = nxt.text.lstrip()
            glue = "" if looks_like_cut_letter(token) and token.upper() not in "ИА" else " "
            nxt.text = token + glue + rest
            nxt.start = min(nxt.start, cur.start)
            del items[i]
            continue
        if prev is not None and looks_like_cut_letter(cur.text):
            if prev.text and _is_word_char(prev.text[-1]) and _is_word_char(cur.text[0]):
                prev.text = prev.text + cur.text
            else:
                prev.text = " ".join(p for p in (prev.text, cur.text) if p).strip()
            prev.end = max(prev.end, cur.end)
            del items[i]
            continue
        i += 1
    if join_gap_sec is None:
        return items
    return merge_adjacent(items, gap_sec=join_gap_sec)


def split_text_by_weights(text: str, weights: list[float], min_part_chars: int = MIN_PART_CHARS) -> list[str]:
    """Режет склейку Gemma на куски реплик только по границе слова, не по букве."""
    body = (text or "").strip()
    n = len(weights)
    if n == 0:
        return []
    if n == 1:
        return [body]
    if not body:
        return [""] * n
    safe = [max(0.0, float(w)) for w in weights]
    total = sum(safe) or 1.0
    keep = [i for i, w in enumerate(safe) if (w / total) * len(body) >= 2]
    if not keep:
        keep = [max(range(n), key=lambda i: safe[i])]
    kept_weights = [safe[i] for i in keep]
    kept_total = sum(kept_weights) or 1.0
    cuts: list[int] = []
    acc = 0.0
    for weight in kept_weights[:-1]:
        acc += weight
        target = int(round(len(body) * acc / kept_total))
        target = max(1, min(len(body) - 1, target))
        floor = (cuts[-1] + 2) if cuts else 1
        window = max(48, min_part_chars * 2)
        lo = max(floor, target - window)
        hi = min(len(body) - 1, target + window)
        if lo > hi:
            continue
        best = _best_cut(body, target, lo, hi)
        if best is None:
            best = _best_cut(body, target, floor, len(body) - 1)
        if best is None:
            continue
        if len(body) - best < 2:
            continue
        cuts.append(best)
    kept_parts: list[str] = []
    prev = 0
    for cut in cuts:
        kept_parts.append(body[prev:cut].strip())
        prev = cut
    kept_parts.append(body[prev:].strip())
    while len(kept_parts) < len(keep):
        kept_parts.append("")
    out = [""] * n
    for idx, part in zip(keep, kept_parts[: len(keep)]):
        out[idx] = part
    return out


def apply_merged_windows(turns: list[Turn], windows: list[tuple[float, float, str]]) -> list[Turn]:
    """Вешает текст склейки на интервалы PyAnnote: в каждом окне — по длительности реплик."""
    from hallucination_guard import cap_text_to_speech_budget, sanitize_transcript_turns

    out: list[Turn] = []
    for start, end, merged in windows:
        chunk = [t for t in turns if t.end > start + 0.05 and t.start < end - 0.05]
        if not chunk:
            continue
        chunk = merge_adjacent(
            [Turn(start=t.start, end=t.end, speaker=t.speaker, text="") for t in chunk],
            gap_sec=SAME_SPEAKER_GAP_SEC,
        )
        text_chunk = [t for t in chunk if t.duration >= MIN_TEXT_TURN_SEC]
        if not text_chunk:
            text_chunk = chunk
        weights = [t.duration for t in text_chunk]
        speech_sec = sum(weights)
        body = cap_text_to_speech_budget(merged, speech_sec)
        if not body:
            continue
        pieces = split_text_by_weights(body, weights)
        for turn, piece in zip(text_chunk, pieces):
            capped = cap_text_to_speech_budget(piece, turn.duration)
            if not capped:
                continue
            out.append(Turn(start=turn.start, end=turn.end, speaker=turn.speaker, text=capped))
    return sanitize_transcript_turns(reattach_cut_letters(merge_adjacent(out)))


def carve_text_by_speakers(text_turns: list[Turn], speaker_turns: list[Turn], min_overlap: float = 0.2) -> list[Turn]:
    """Режет длинный чанк ASR по границам PyAnnote.

    GigaAM без word-timestamps вешает одного спикера на 18 с по середине чанка.
    Тогда мужской голос внутри женской реплики не переключает метку. Здесь чанк
    режется, текст делится пропорционально пересечению.
    """
    from hallucination_guard import cap_text_to_speech_budget, sanitize_transcript_turns

    speakers = merge_adjacent(
        [Turn(start=t.start, end=t.end, speaker=t.speaker, text="") for t in speaker_turns if t.speaker]
    )
    if not speakers:
        raw = [Turn(start=t.start, end=t.end, speaker=t.speaker or "Спикер №?", text=t.text) for t in text_turns]
        return sanitize_transcript_turns(raw)
    out: list[Turn] = []
    for piece in text_turns:
        hits = [(spk, _overlap(piece, spk)) for spk in speakers]
        hits = [(spk, ov) for spk, ov in hits if ov >= min_overlap]
        hits.sort(key=lambda item: max(piece.start, item[0].start))
        speech_sec = sum(ov for _, ov in hits) if hits else piece.duration
        body = cap_text_to_speech_budget(piece.text, speech_sec)
        if not body:
            continue
        if not hits:
            nearest = min(speakers, key=lambda s: min(abs(s.start - piece.start), abs(s.end - piece.end)))
            out.append(Turn(start=piece.start, end=piece.end, speaker=nearest.speaker, text=body))
            continue
        parts = split_text_by_weights(body, [ov for _, ov in hits])
        for (spk, ov), part in zip(hits, parts):
            start = max(piece.start, spk.start)
            end = min(piece.end, spk.end)
            if end <= start:
                continue
            capped = cap_text_to_speech_budget(part, ov)
            if not capped:
                continue
            out.append(Turn(start=start, end=end, speaker=spk.speaker, text=capped))
    return sanitize_transcript_turns(reattach_cut_letters(merge_adjacent(out)))


def load_pyannote_segments(path: Path) -> list[Turn]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("segments") if isinstance(data, dict) else data
    turns: list[Turn] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        start = float(row.get("start") or 0.0)
        end = float(row.get("end") or start)
        speaker = canonical_speaker(str(row.get("speaker") or ""))
        turns.append(Turn(start=start, end=max(end, start), speaker=speaker, text=""))
    return turns
