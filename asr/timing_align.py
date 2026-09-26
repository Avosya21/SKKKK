"""Привязка склейки Gemma к часам ASR и спикерам.

Канон доверия:
  текст  = только Gemma (окна склейки)
  часы   = Whisper words (на петле — Tone / интервалы спикеров)
  кто    = Sortformer/PyAnnote по пересечению

Сырой Whisper/Tone в UI не подставляем «кусками» — иначе непонятно, чья это расшифровка.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from hallucination_guard import MAX_CHARS_PER_SEC, scrub_merged_text
from speakers import Turn, overlay_text

MAX_WORD_GAP_SEC = 0.75
MAX_PHRASE_SEC = 18.0
MIN_WORD_DUR = 0.04
MIN_TURN_GAP_SEC = 0.05
MIN_TURN_DUR_SEC = 0.25
GEMMA_MATCH_RATIO = 0.52
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+|\n+")


def _turn_from_sentence(sent: str, start: float, end: float) -> Turn:
    s = float(start)
    e = float(end)
    if e <= s:
        e = s + MIN_TURN_DUR_SEC
    return Turn(start=s, end=e, speaker="", text=sent)


def parse_gigaam_timecoded(text: str) -> list[Turn]:
    """Сегменты GigaAM вида `[MM:SS - MM:SS] текст` — основной якорь часов, если Whisper петлит."""
    out: list[Turn] = []
    for line in (text or "").splitlines():
        body = line.strip()
        if not body.startswith("["):
            continue
        m = re.match(r"\[(\d+):(\d+)\s*-\s*(\d+):(\d+)\]\s*(.*)$", body)
        if not m:
            continue
        sh, sm, eh, em, txt = m.groups()
        start = int(sh) * 60 + int(sm)
        end = int(eh) * 60 + int(em)
        if end <= start:
            end = start + 0.35
        if txt.strip():
            out.append(Turn(start=float(start), end=float(end), speaker="", text=txt.strip()))
    return out


def parse_gigaam_timecoded_payload(payload: dict) -> list[Turn]:
    if not isinstance(payload, dict):
        return []
    raw = payload.get("transcription_with_timecodes") or payload.get("transcription_timecoded") or ""
    if isinstance(raw, list):
        turns: list[Turn] = []
        for row in raw:
            if not isinstance(row, dict):
                continue
            txt = str(row.get("text") or "").strip()
            if not txt:
                continue
            start = float(row.get("start") or 0.0)
            end = float(row.get("end") or start)
            if end <= start:
                end = start + 0.35
            turns.append(Turn(start=start, end=end, speaker="", text=txt))
        return turns
    return parse_gigaam_timecoded(str(raw))


@dataclass
class WordSpan:
    word: str
    start: float
    end: float


def parse_whisper_words(payload: dict) -> list[WordSpan]:
    rows = payload.get("words") if isinstance(payload, dict) else None
    out: list[WordSpan] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        token = str(row.get("word") or "").strip()
        if not token:
            continue
        start = float(row.get("start") or 0.0)
        end = float(row.get("end") or start)
        if end < start:
            end = start
        if end - start < MIN_WORD_DUR:
            end = start + MIN_WORD_DUR
        out.append(WordSpan(word=token, start=start, end=end))
    out.sort(key=lambda w: (w.start, w.end))
    return out


def turns_from_whisper_words(
    words: list[WordSpan],
    *,
    max_gap_sec: float = MAX_WORD_GAP_SEC,
    max_phrase_sec: float = MAX_PHRASE_SEC,
    collapse_cycles: bool = True,
) -> tuple[list[Turn], list[tuple[float, float]]]:
    """Собирает фразы из word-timestamps. Вторым значением — дыры после среза петли Whisper."""
    if not words:
        return [], []
    gaps: list[tuple[float, float]] = []
    if collapse_cycles:
        words, gaps = collapse_whisper_word_cycles(words)
    if not words:
        return [], gaps
    turns: list[Turn] = []
    buf = [words[0]]
    for w in words[1:]:
        prev = buf[-1]
        gap = w.start - prev.end
        span = w.end - buf[0].start
        if gap > max_gap_sec or span > max_phrase_sec:
            turns.append(_flush_words(buf))
            buf = [w]
        else:
            buf.append(w)
    if buf:
        turns.append(_flush_words(buf))
    return turns, gaps


def collapse_whisper_word_cycles(
    words: list[WordSpan],
    *,
    min_repeats: int = 3,
) -> tuple[list[WordSpan], list[tuple[float, float]]]:
    """Режет циклы токенов Whisper и возвращает временные дыры, куда подставить Tone/Gemma."""
    if len(words) < 12:
        return words, []
    norms = [_norm_token(w.word) for w in words]
    out: list[WordSpan] = []
    gaps: list[tuple[float, float]] = []
    i = 0
    n = len(words)
    while i < n:
        matched = False
        for period in range(4, 9):
            if i + period * min_repeats > n:
                continue
            chunk = norms[i : i + period]
            joined = " ".join(chunk)
            if len(joined) < 8:
                continue
            repeats = 1
            k = i + period
            while k + period <= n and norms[k : k + period] == chunk:
                repeats += 1
                k += period
            if repeats >= min_repeats:
                out.extend(words[i : i + period])
                if k > i + period:
                    gaps.append((words[i + period].start, words[k - 1].end))
                i = k
                matched = True
                break
        if not matched:
            out.append(words[i])
            i += 1
    if not gaps:
        return out, []
    gaps.sort()
    merged = [gaps[0]]
    for start, end in gaps[1:]:
        prev_s, prev_e = merged[-1]
        if start <= prev_e + 1.0:
            merged[-1] = (prev_s, max(prev_e, end))
        else:
            merged.append((start, end))
    return out, merged


def _norm_token(text: str) -> str:
    body = (text or "").strip().lower().replace("ё", "е")
    body = re.sub(r"\s+", "", body)
    return body


def _flush_words(buf: list[WordSpan]) -> Turn:
    text = " ".join(w.word for w in buf).strip()
    for ch in ",.!?;:":
        text = text.replace(f" {ch}", ch)
    return Turn(start=buf[0].start, end=max(b.end for b in buf), speaker="", text=text)


def timed_asr_turns(
    payload: dict, segments: list[Turn] | None = None
) -> tuple[list[Turn], list[tuple[float, float]]]:
    """Предпочитает words; иначе сегменты. Второе значение — дыры после среза петли Whisper."""
    words = parse_whisper_words(payload)
    if words:
        return turns_from_whisper_words(words)
    from hallucination_guard import exceeds_density
    from speakers import parse_whisper_segments

    segs = segments if segments is not None else parse_whisper_segments(payload)
    fixed: list[Turn] = []
    for seg in segs:
        if not seg.text.strip():
            continue
        if exceeds_density(seg.text, seg.duration):
            need = max(len(seg.text) / 20.0, 0.4)
            fixed.append(Turn(start=seg.start, end=seg.start + need, speaker="", text=seg.text))
        else:
            fixed.append(seg)
    return fixed, []


def _gemma_sentences(body: str) -> list[str]:
    text = scrub_merged_text(body)
    if not text:
        return []
    parts = [s.strip() for s in _SENT_SPLIT_RE.split(text) if s.strip()]
    if parts:
        return parts
    return [text]


def pick_gemma_correction(whisper_text: str, gemma_sentences: list[str]) -> str | None:
    """Подмена Whisper→Gemma только при похожей фразе (та же реплика, чище слова).

    Не принимаем кандидата сильно короче оригинала — иначе середина реплики вырезается.
    """
    probe = (whisper_text or "").strip()
    if len(probe) < 8 or not gemma_sentences:
        return None
    best = None
    best_ratio = 0.0
    for cand in gemma_sentences:
        if len(cand) < 6:
            continue
        # запрет вырезать содержание: Gemma короче на >30% — не подмена, а потеря фразы
        if len(cand) < len(probe) * 0.7:
            continue
        ratio = difflib.SequenceMatcher(None, probe.lower(), cand.lower()).ratio()
        len_ratio = min(len(probe), len(cand)) / max(len(probe), len(cand))
        score = ratio * (0.55 + 0.45 * len_ratio)
        if score > best_ratio:
            best_ratio = score
            best = cand
    if best is None or best_ratio < GEMMA_MATCH_RATIO:
        return None
    return best


def apply_gemma_to_phrase(whisper_text: str, gemma_sentences: list[str]) -> str:
    """Правит Whisper посерединно: каждое предложение отдельно, ничего не выбрасывая.

    Раньше одна «лучшая» фраза Gemma заменяла весь кусок Whisper из 2–3 предложений —
    отсюда пропажа «И остальные три — это как раз перенос…» между багфиксом и «Вот, соответственно».
    """
    body = (whisper_text or "").strip()
    if not body:
        return ""
    if not gemma_sentences:
        return body
    parts = [s.strip() for s in _SENT_SPLIT_RE.split(body) if s.strip()]
    if len(parts) <= 1:
        return pick_gemma_correction(body, gemma_sentences) or body

    used: set[int] = set()
    out: list[str] = []
    for part in parts:
        available = [s for i, s in enumerate(gemma_sentences) if i not in used]
        fixed = pick_gemma_correction(part, available)
        if fixed:
            for i, s in enumerate(gemma_sentences):
                if i not in used and s == fixed:
                    used.add(i)
                    break
            out.append(fixed)
        else:
            out.append(part)
    return " ".join(out)


LOOP_RUN_MIN = 3  # одинаковая фраза ≥3 раза подряд у Whisper = петля, не речь


def _norm_phrase(text: str) -> str:
    body = (text or "").strip().lower()
    body = body.replace("ё", "е")
    body = re.sub(r"\s+", " ", body)
    body = re.sub(r"[«»\"']", "", body)
    body = re.sub(r"\s*-\s*", "-", body)
    return body.strip(" .,-—–")


def _same_loop_family(a: str, b: str) -> bool:
    """Варианты одной петли Whisper: сдвиг цикла, склейка 2–3 копий."""
    na, nb = _norm_phrase(a), _norm_phrase(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if len(na) >= 12 and len(nb) >= 12 and (na in nb or nb in na):
        return True
    if difflib.SequenceMatcher(None, na, nb).ratio() >= 0.72:
        return True
    # общий «стержень» из 4+ слов
    wa, wb = na.split(), nb.split()
    if len(wa) >= 4 and len(wb) >= 4:
        grams_a = {" ".join(wa[i : i + 4]) for i in range(len(wa) - 3)}
        grams_b = {" ".join(wb[i : i + 4]) for i in range(len(wb) - 3)}
        if grams_a & grams_b:
            return True
    return False


def strip_identical_phrase_runs(
    turns: list[Turn],
    *,
    min_run: int = LOOP_RUN_MIN,
) -> tuple[list[Turn], list[tuple[float, float]]]:
    """Убирает петлю Whisper: одна и та же фраза на десятках подряд идущих кусков.

    Возвращает очищенные реплики и интервалы времени, которые нужно заполнить
    из Tone/Gemma (там обычно нормальный текст, пока Whisper крутит одно предложение).
    """
    if not turns:
        return [], []
    ordered = sorted(turns, key=lambda t: (t.start, t.end))
    kept: list[Turn] = []
    gaps: list[tuple[float, float]] = []
    i = 0
    while i < len(ordered):
        j = i + 1
        while j < len(ordered) and _same_loop_family(ordered[i].text, ordered[j].text):
            j += 1
        run = ordered[i:j]
        norm = _norm_phrase(ordered[i].text)
        if len(run) >= min_run and len(norm) >= 10:
            kept.append(run[0])
            gap_start = run[1].start
            gap_end = run[-1].end
            if gap_end > gap_start:
                gaps.append((gap_start, gap_end))
        else:
            kept.extend(run)
        i = j
    return kept, _merge_spans(gaps)


def _merge_spans(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not spans:
        return []
    ordered = sorted(spans)
    out = [ordered[0]]
    for start, end in ordered[1:]:
        prev_s, prev_e = out[-1]
        if start <= prev_e + 1.0:
            out[-1] = (prev_s, max(prev_e, end))
        else:
            out.append((start, end))
    return out


def _window_text_for_span(
    windows: list[tuple[float, float, str]],
    start: float,
    end: float,
) -> str:
    parts: list[str] = []
    for w_start, w_end, text in windows:
        if w_end <= start or w_start >= end:
            continue
        body = scrub_merged_text(text)
        if body:
            parts.append(body)
    return "\n\n".join(parts).strip()


def _clock_anchors(
    timed: list[Turn],
    fallback_timed: list[Turn] | None,
    speakers: list[Turn],
    extra_gaps: list[tuple[float, float]] | None,
) -> list[Turn]:
    """Сетка времени без текста ASR: Whisper-фразы, на петлях — Tone или интервалы спикеров."""
    ordered = sorted(timed, key=lambda t: (t.start, t.end)) if timed else []
    clean, gaps = strip_identical_phrase_runs(ordered) if ordered else ([], [])
    if extra_gaps:
        gaps = _merge_spans(list(gaps) + list(extra_gaps))

    clocks = [Turn(start=t.start, end=t.end, speaker="", text="") for t in clean if t.duration > 0.05]

    for gap_start, gap_end in gaps:
        added = False
        if fallback_timed:
            for t in fallback_timed:
                if t.end <= gap_start + 0.05 or t.start >= gap_end - 0.05:
                    continue
                start = max(t.start, gap_start)
                end = min(t.end, gap_end)
                if end - start > 0.15:
                    clocks.append(Turn(start=start, end=end, speaker="", text=""))
                    added = True
        if not added and speakers:
            for spk in speakers:
                if spk.end <= gap_start + 0.05 or spk.start >= gap_end - 0.05:
                    continue
                start = max(spk.start, gap_start)
                end = min(spk.end, gap_end)
                if end - start > 0.25:
                    clocks.append(Turn(start=start, end=end, speaker="", text=""))
                    added = True
        if not added and gap_end - gap_start > 0.5:
            # хоть одна метка, чтобы текст Gemma окна не пропал
            clocks.append(Turn(start=gap_start, end=gap_end, speaker="", text=""))

    clocks.sort(key=lambda t: (t.start, t.end))
    return clocks


def drop_cross_turn_phrase_loops(
    turns: list[Turn],
    *,
    min_run: int = LOOP_RUN_MIN,
    max_keep: int = 2,
) -> list[Turn]:
    """Страховка от расползания одной фразы подряд (не глобальный запрет повторов)."""
    if not turns:
        return []
    ordered = sorted(turns, key=lambda t: (t.start, t.end))
    out: list[Turn] = []
    i = 0
    while i < len(ordered):
        j = i + 1
        while j < len(ordered) and _same_loop_family(ordered[i].text, ordered[j].text):
            j += 1
        run = ordered[i:j]
        norm = _norm_phrase(ordered[i].text)
        if len(run) >= min_run and len(norm) >= 10:
            out.extend(run[: max(1, max_keep)])
        else:
            out.extend(run)
        i = j
    return out


def align_merged_to_timed(
    timed: list[Turn],
    windows: list[tuple[float, float, str]],
    speakers: list[Turn],
    fallback_timed: list[Turn] | None = None,
    extra_gaps: list[tuple[float, float]] | None = None,
    words: list[WordSpan] | None = None,
    *,
    whisper_trusted: bool = True,
    audio_end: float = 0.0,
) -> list[Turn]:
    """Канон: текст = только Gemma; часы = alignment с Whisper words; кто = Sortformer.

    Без пропорциональной нарезки и без урезания текста по density на финале.
    Петля Whisper → дыры в часах; текст окна Gemma сохраняется целиком.
    """
    if not windows:
        return []

    gaps = _collect_whisper_gaps(timed, extra_gaps)
    clock_words = _words_for_alignment(words, timed, gaps)
    pieces: list[Turn] = []

    for w_start, w_end, merged in windows:
        body = scrub_merged_text(merged)
        if not body:
            continue
        sentences = _gemma_sentences(body)
        if not sentences:
            continue
        win_words = [
            w
            for w in clock_words
            if w.end > w_start + 0.02 and w.start < w_end - 0.02 and not _in_gaps(w.start, w.end, gaps)
        ]
        # Tone-токены в окне: после удлинения склейки ближе к Gemma, чем петля Whisper
        tone_words: list[WordSpan] = []
        if fallback_timed:
            tone_in = [
                t
                for t in fallback_timed
                if t.end > w_start + 0.05 and t.start < w_end - 0.05 and (t.text or "").strip()
            ]
            tone_words = _pseudo_words_from_turns(tone_in)
        gemma_norms = [_norm_token(tok) for sent in sentences for tok in _tokenize(sent)]
        prefer_fallback_words = (
            fallback_timed
            and tone_words
            and (
                not whisper_trusted
                or not win_words
                or _match_ratio(gemma_norms, tone_words)
                >= _match_ratio(gemma_norms, win_words) - 0.02
            )
        )
        if prefer_fallback_words:
            win_words = tone_words

        win_clocks = [
            t
            for t in timed
            if t.end > w_start + 0.05
            and t.start < w_end - 0.05
            and not _in_gaps(t.start, t.end, gaps)
        ]
        # для текстового match всегда тащим Tone-сегменты с текстом в phrase_clocks
        if fallback_timed:
            for t in fallback_timed:
                if t.end <= w_start + 0.05 or t.start >= w_end - 0.05:
                    continue
                if not (t.text or "").strip():
                    continue
                win_clocks.append(
                    Turn(
                        start=max(t.start, w_start),
                        end=min(t.end, w_end),
                        speaker="",
                        text=t.text,
                    )
                )
        covered = sum(
            max(0.0, min(t.end, w_end) - max(t.start, w_start)) for t in win_clocks
        )
        need_fallback = covered < 0.25 * max(w_end - w_start, 1.0)
        if need_fallback and fallback_timed:
            for t in fallback_timed:
                if t.end <= w_start + 0.05 or t.start >= w_end - 0.05:
                    continue
                win_clocks.append(
                    Turn(
                        start=max(t.start, w_start),
                        end=min(t.end, w_end),
                        speaker="",
                        text=t.text or "",
                    )
                )
        if need_fallback and not any(t.end - t.start > 0.5 for t in win_clocks) and speakers:
            for sp in speakers:
                if sp.end <= w_start + 0.05 or sp.start >= w_end - 0.05:
                    continue
                ov0, ov1 = max(sp.start, w_start), min(sp.end, w_end)
                if ov1 - ov0 > 0.2:
                    win_clocks.append(Turn(start=ov0, end=ov1, speaker="", text=""))
        timed_sents = _align_sentences_to_words(
            sentences,
            win_words,
            w_start=w_start,
            w_end=w_end,
            gaps=gaps,
            fallback_timed=fallback_timed,
            speakers=speakers,
            phrase_clocks=win_clocks,
        )
        supplements = _engine_supplements_for_window(
            body,
            fallback_timed or [],
            w_start,
            w_end,
            timed_sents,
        )
        pieces.extend(timed_sents)
        pieces.extend(supplements)

    if not pieces:
        return []

    # Порядок Gemma сохранён; только поджимаем start при наложении — end/якоря Whisper не сдвигаем.
    pieces = _nudge_monotonic_keep_order(pieces)
    if speakers:
        labeled = overlay_text(pieces, speakers, join_gap_sec=None)
    else:
        labeled = [
            Turn(start=t.start, end=t.end, speaker=t.speaker or "Спикер №?", text=t.text)
            for t in pieces
        ]
    labeled = _extend_timeline_to_audio_end(labeled, audio_end, windows)
    return _sanitize_keep_content(drop_cross_turn_phrase_loops(labeled, max_keep=2))


def _extend_timeline_to_audio_end(
    turns: list[Turn],
    audio_end: float,
    windows: list[tuple[float, float, str]],
) -> list[Turn]:
    """Если последнее окно Gemma короче аудио — растягиваем хвост до конца записи."""
    if not turns or audio_end <= 0:
        return turns
    target = audio_end
    if windows:
        target = min(audio_end, max(w[1] for w in windows))
    if turns[-1].end >= target - 1.5:
        return turns
    out = list(turns)
    slack = target - out[-1].end
    if slack <= 0:
        return out
    tail_n = min(4, len(out))
    weights = [max(len(out[i].text or ""), 12) for i in range(-tail_n, 0)]
    total_w = sum(weights) or 1.0
    cursor = out[-tail_n].start
    for i, w in enumerate(weights):
        idx = len(out) - tail_n + i
        share = slack * (w / total_w)
        new_end = min(target, cursor + max(out[idx].end - out[idx].start, 0.35) + share)
        out[idx] = Turn(
            start=out[idx].start if i else cursor,
            end=new_end,
            speaker=out[idx].speaker,
            text=out[idx].text,
        )
        cursor = new_end
    out[-1] = Turn(
        start=out[-1].start,
        end=target,
        speaker=out[-1].speaker,
        text=out[-1].text,
    )
    return out


def _collect_whisper_gaps(
    timed: list[Turn],
    extra_gaps: list[tuple[float, float]] | None,
) -> list[tuple[float, float]]:
    ordered = sorted(timed, key=lambda t: (t.start, t.end)) if timed else []
    _, gaps = strip_identical_phrase_runs(ordered) if ordered else ([], [])
    if extra_gaps:
        gaps = _merge_spans(list(gaps) + list(extra_gaps))
    return gaps


def _in_gaps(start: float, end: float, gaps: list[tuple[float, float]]) -> bool:
    mid = (start + end) / 2.0
    for gs, ge in gaps:
        if gs <= mid <= ge:
            return True
    return False


def _words_for_alignment(
    words: list[WordSpan] | None,
    timed: list[Turn],
    gaps: list[tuple[float, float]],
) -> list[WordSpan]:
    if words:
        cleaned, cycle_gaps = collapse_whisper_word_cycles(list(words))
        all_gaps = _merge_spans(list(gaps) + list(cycle_gaps))
        return [w for w in cleaned if not _in_gaps(w.start, w.end, all_gaps)]
    # нет words — раскладываем текст фраз Whisper как псевдо-токены по длительности фразы
    out: list[WordSpan] = []
    for t in sorted(timed, key=lambda x: (x.start, x.end)):
        if _in_gaps(t.start, t.end, gaps):
            continue
        toks = _tokenize(t.text or "")
        if not toks:
            continue
        dur = max(t.end - t.start, 0.05)
        step = dur / len(toks)
        for i, tok in enumerate(toks):
            start = t.start + i * step
            end = start + max(step, MIN_WORD_DUR)
            out.append(WordSpan(word=tok, start=start, end=min(end, t.end)))
    return out


_TOKEN_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+(?:-[0-9A-Za-zА-Яа-яЁё]+)?", re.UNICODE)


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text or "")


def _pseudo_words_from_turns(turns: list[Turn]) -> list[WordSpan]:
    """Токены с временами из сегментов Tone/фраз (равномерно внутри сегмента)."""
    out: list[WordSpan] = []
    for t in sorted(turns, key=lambda x: (x.start, x.end)):
        toks = _tokenize(t.text or "")
        if not toks:
            continue
        dur = max(t.end - t.start, 0.05)
        step = dur / len(toks)
        for i, tok in enumerate(toks):
            start = t.start + i * step
            end = start + max(step, MIN_WORD_DUR)
            out.append(WordSpan(word=tok, start=start, end=min(end, t.end)))
    return out


def _match_ratio(gemma_norms: list[str], words: list[WordSpan]) -> float:
    if not gemma_norms or not words:
        return 0.0
    other = [_norm_token(w.word) for w in words]
    return difflib.SequenceMatcher(a=gemma_norms, b=other, autojunk=False).ratio()


def _window_time_holes(
    w_start: float,
    w_end: float,
    occupied: list[Turn],
    *,
    min_hole_sec: float = 8.0,
) -> list[tuple[float, float]]:
    spans = sorted(
        (
            max(t.start, w_start),
            min(t.end, w_end),
        )
        for t in occupied
        if t.end > w_start + 0.05 and t.start < w_end - 0.05
    )
    cursor = w_start
    holes: list[tuple[float, float]] = []
    for a, b in spans:
        if a > cursor + min_hole_sec:
            holes.append((cursor, a))
        cursor = max(cursor, b)
    if w_end > cursor + min_hole_sec:
        holes.append((cursor, w_end))
    return holes


def _text_covered_in_body(segment_text: str, body_norm: str, *, threshold: float = 0.45) -> bool:
    sn = _norm_phrase(segment_text)
    if len(sn) < 12:
        return True
    words = [w for w in sn.split() if len(w) >= 4]
    if not words:
        return True
    hit = sum(1 for w in words if w in body_norm)
    return hit / len(words) >= threshold


def _engine_supplements_for_window(
    gemma_body: str,
    clock_timed: list[Turn],
    w_start: float,
    w_end: float,
    aligned: list[Turn],
) -> list[Turn]:
    """GigaAM/Tone сегменты в «дырах» окна, если Gemma их не сохранила при склейке."""
    if not clock_timed:
        return []
    body_norm = _norm_phrase(gemma_body)
    out: list[Turn] = []
    seen_norm: set[str] = set()
    for hs, he in _window_time_holes(w_start, w_end, aligned):
        for seg in sorted(clock_timed, key=lambda t: (t.start, t.end)):
            if seg.end <= hs + 0.5 or seg.start >= he - 0.5:
                continue
            txt = (seg.text or "").strip()
            if len(txt) < 15 or _text_covered_in_body(txt, body_norm):
                continue
            sn = _norm_phrase(txt)
            if sn in seen_norm:
                continue
            seen_norm.add(sn)
            out.append(
                Turn(
                    start=max(float(seg.start), hs),
                    end=min(float(seg.end), he),
                    speaker="",
                    text=txt,
                )
            )
    return out


def _align_sentences_by_segment_text(
    sentences: list[str],
    segments: list[Turn],
    *,
    w_start: float,
    w_end: float,
) -> list[Turn] | None:
    """Монотонная привязка предложений Gemma к сегментам Tone/ASR по тексту.

    После удлинения склейки равномерная сетка часов даёт сдвиг клика — якоримся
    по похожести текста сегмента, а не по порядковому номеру.
    """
    segs = [
        t
        for t in sorted(segments, key=lambda x: (x.start, x.end))
        if (t.text or "").strip()
        and t.end > w_start + 0.05
        and t.start < w_end - 0.05
    ]
    if not sentences or len(segs) < 1:
        return None

    starts: list[float | None] = [None] * len(sentences)
    ends: list[float | None] = [None] * len(sentences)
    seg_norms = [_norm_phrase(t.text) for t in segs]
    j = 0
    matched = 0
    for i, sent in enumerate(sentences):
        probe = _norm_phrase(sent)
        if len(probe) < 6:
            continue
        best_j = None
        best_score = 0.0
        # смотрим вперёд по сегментам, без отката (монотонность)
        limit = min(len(segs), j + max(6, len(segs) // max(len(sentences), 1) + 3))
        for k in range(j, limit):
            cand = seg_norms[k]
            if not cand:
                continue
            score = difflib.SequenceMatcher(None, probe, cand).ratio()
            # бонус за общие длинные токены
            pw = set(probe.split())
            cw = set(cand.split())
            if pw and cw:
                score = 0.65 * score + 0.35 * (len(pw & cw) / max(len(pw), 1))
            if score > best_score:
                best_score = score
                best_j = k
        if best_j is not None and best_score >= 0.42:
            seg = segs[best_j]
            starts[i] = max(seg.start, w_start)
            ends[i] = min(seg.end, w_end)
            if ends[i] <= starts[i]:
                ends[i] = starts[i] + 0.35
            j = best_j + 1
            matched += 1

    if matched < max(2, (len(sentences) + 1) // 2):
        return None

    _fill_orphan_times(
        starts,
        ends,
        w_start,
        w_end,
        gaps=[],
        fallback_timed=segs,
        speakers=[],
        phrase_clocks=segs,
    )
    _split_duplicate_slots(starts, ends, 0, len(sentences) - 1, sentences)
    out: list[Turn] = []
    for i, sent in enumerate(sentences):
        s = float(starts[i] if starts[i] is not None else w_start)
        e = float(ends[i] if ends[i] is not None else max(s + 0.35, w_end))
        out.append(_turn_from_sentence(sent, s, e))
    return out


def _align_sentences_to_words(
    sentences: list[str],
    words: list[WordSpan],
    *,
    w_start: float,
    w_end: float,
    gaps: list[tuple[float, float]],
    fallback_timed: list[Turn] | None,
    speakers: list[Turn],
    phrase_clocks: list[Turn] | None = None,
) -> list[Turn]:
    """Каждое предложение Gemma → интервал; сироты интерполируются, не удаляются."""
    if not sentences:
        return []

    # 1) текст→текст к Tone только если якорей достаточно и совпадения уверенные
    text_segs: list[Turn] = []
    if fallback_timed:
        text_segs.extend(
            t for t in fallback_timed if (t.text or "").strip() and t.end > w_start and t.start < w_end
        )
    if phrase_clocks:
        text_segs.extend(
            t for t in phrase_clocks if (t.text or "").strip() and t.end > w_start and t.start < w_end
        )
    by_text = _align_sentences_by_segment_text(sentences, text_segs, w_start=w_start, w_end=w_end)

    gemma_toks: list[tuple[int, str]] = []
    for si, sent in enumerate(sentences):
        for tok in _tokenize(sent):
            gemma_toks.append((si, _norm_token(tok)))

    if not words or not gemma_toks:
        if by_text is not None:
            return by_text
        return _distribute_even(
            sentences,
            w_start,
            w_end,
            gaps,
            fallback_timed,
            speakers,
            phrase_clocks=phrase_clocks,
        )

    whisper_norms = [_norm_token(w.word) for w in words]
    gemma_norms = [n for _, n in gemma_toks]
    g2w: list[int | None] = [None] * len(gemma_toks)
    matcher = difflib.SequenceMatcher(a=gemma_norms, b=whisper_norms, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                g2w[i1 + k] = j1 + k
        elif tag == "replace":
            span_g = i2 - i1
            span_w = j2 - j1
            for k in range(min(span_g, span_w)):
                if gemma_norms[i1 + k] == whisper_norms[j1 + k] or (
                    len(gemma_norms[i1 + k]) >= 4
                    and difflib.SequenceMatcher(
                        None, gemma_norms[i1 + k], whisper_norms[j1 + k]
                    ).ratio()
                    >= 0.8
                ):
                    g2w[i1 + k] = j1 + k

    g2w = _monotonize_word_map(g2w)

    starts: list[float | None] = [None] * len(sentences)
    ends: list[float | None] = [None] * len(sentences)
    for gi, (si, _) in enumerate(gemma_toks):
        wi = g2w[gi]
        if wi is None:
            continue
        w = words[wi]
        if starts[si] is None or w.start < starts[si]:
            starts[si] = w.start
        if ends[si] is None or w.end > ends[si]:
            ends[si] = w.end

    matched = sum(1 for s in starts if s is not None)
    # токены слабо легли — берём уверенный text-match к Tone, иначе равномерные clocks
    if matched < max(1, len(sentences) // 3):
        if by_text is not None:
            return by_text
        if phrase_clocks:
            return _map_sentences_to_phrase_clocks(sentences, phrase_clocks, w_start, w_end)

    _fill_orphan_times(
        starts,
        ends,
        w_start,
        w_end,
        gaps,
        fallback_timed,
        speakers,
        phrase_clocks=phrase_clocks,
    )
    _split_duplicate_slots(starts, ends, 0, len(sentences) - 1, sentences)

    out: list[Turn] = []
    for i, sent in enumerate(sentences):
        s = starts[i] if starts[i] is not None else w_start
        e = ends[i] if ends[i] is not None else max(float(s) + 0.4, w_end)
        out.append(_turn_from_sentence(sent, float(s), float(e)))
    return out


def _map_sentences_to_phrase_clocks(
    sentences: list[str],
    phrase_clocks: list[Turn],
    w_start: float,
    w_end: float,
) -> list[Turn]:
    """Раскладка предложений по часам ASR/Tone равномерно по всему интервалу.

    Раньше брали clocks[0..n) — на петле Whisper все окна садились на первые
    Tone-сегменты (~начало дыры), хвост аудио оставался без расшифровки в UI.
    """
    clocks = sorted(phrase_clocks, key=lambda t: (t.start, t.end))
    n_s = len(sentences)
    if n_s == 0:
        return []
    if not clocks:
        step = max((w_end - w_start) / n_s, 0.35)
        return [
            Turn(start=w_start + i * step, end=w_start + (i + 1) * step, speaker="", text=s)
            for i, s in enumerate(sentences)
        ]
    slots = _slots_from_spans([(c.start, c.end) for c in clocks], n_s)
    return [
        Turn(start=a, end=max(a + 0.25, b), speaker="", text=sent)
        for (a, b), sent in zip(slots, sentences)
    ]


def _slots_from_spans(spans: list[tuple[float, float]], count: int) -> list[tuple[float, float]]:
    """count слотов, равномерно растянутых по spans (не только первые count штук)."""
    spans = [(a, b) for a, b in sorted(spans) if b > a]
    if count <= 0:
        return []
    if not spans:
        return [(0.0, 0.35)] * count
    n = len(spans)
    if count == 1:
        return [(spans[0][0], spans[-1][1])]
    if count <= n:
        out: list[tuple[float, float]] = []
        used: set[int] = set()
        for k in range(count):
            idx = min(n - 1, int((k + 0.5) * n / count))
            # монотонно вперёд, без отката на уже занятый индекс
            while idx in used and idx < n - 1:
                idx += 1
            if idx in used:
                idx = max(0, min(n - 1, k * n // count))
            used.add(idx)
            out.append(spans[idx])
        out.sort(key=lambda x: x[0])
        return out
    # больше предложений, чем сегментов — делим сегменты на группы
    out = []
    for k in range(count):
        i0 = k * n // count
        i1 = max(i0, (k + 1) * n // count - 1)
        a, b = spans[i0][0], spans[i1][1]
        # если несколько предложений попали в один сегмент — дробим внутри
        group_start = k * n // count
        group_keys = [j for j in range(count) if j * n // count == group_start]
        if len(group_keys) > 1:
            pos = group_keys.index(k)
            step = max(b - a, 0.35 * len(group_keys)) / len(group_keys)
            a2 = a + pos * step
            b2 = a2 + step
            out.append((a2, b2))
        else:
            out.append((a, b))
    return out


def _gap_clock_segments(
    gap_start: float,
    gap_end: float,
    fallback_timed: list[Turn] | None,
    speakers: list[Turn],
) -> list[tuple[float, float]]:
    segs: list[tuple[float, float]] = []
    if fallback_timed:
        for t in fallback_timed:
            if t.end <= gap_start + 0.05 or t.start >= gap_end - 0.05:
                continue
            segs.append((max(t.start, gap_start), min(t.end, gap_end)))
    if not segs and speakers:
        for spk in speakers:
            if spk.end <= gap_start + 0.05 or spk.start >= gap_end - 0.05:
                continue
            segs.append((max(spk.start, gap_start), min(spk.end, gap_end)))
    if not segs and gap_end - gap_start > 0.2:
        segs.append((gap_start, gap_end))
    return [(a, b) for a, b in segs if b - a > 0.1]


def _fill_orphan_times(
    starts: list[float | None],
    ends: list[float | None],
    w_start: float,
    w_end: float,
    gaps: list[tuple[float, float]],
    fallback_timed: list[Turn] | None,
    speakers: list[Turn],
    phrase_clocks: list[Turn] | None = None,
) -> None:
    n = len(starts)
    anchored = [i for i in range(n) if starts[i] is not None and ends[i] is not None]
    if not anchored:
        if phrase_clocks:
            mapped = _map_sentences_to_phrase_clocks(["x"] * n, phrase_clocks, w_start, w_end)
            for i, t in enumerate(mapped):
                starts[i] = t.start
                ends[i] = t.end
            return
        even = _distribute_even(
            ["x"] * n, w_start, w_end, gaps, fallback_timed, speakers, phrase_clocks=None
        )
        for i, t in enumerate(even):
            starts[i] = t.start
            ends[i] = t.end
        return

    first = anchored[0]
    if first > 0:
        left = w_start
        right = float(starts[first])  # type: ignore[arg-type]
        _place_range(starts, ends, 0, first - 1, left, right, gaps, fallback_timed, speakers)

    for a, b in zip(anchored, anchored[1:]):
        if b == a + 1:
            continue
        left = float(ends[a])  # type: ignore[arg-type]
        right = float(starts[b])  # type: ignore[arg-type]
        if right < left:
            right = left + 0.2 * (b - a)
        _place_range(starts, ends, a + 1, b - 1, left, right, gaps, fallback_timed, speakers)

    last = anchored[-1]
    if last < n - 1:
        left = float(ends[last])  # type: ignore[arg-type]
        right = w_end
        _place_range(starts, ends, last + 1, n - 1, left, right, gaps, fallback_timed, speakers)


def _place_range(
    starts: list[float | None],
    ends: list[float | None],
    i0: int,
    i1: int,
    left: float,
    right: float,
    gaps: list[tuple[float, float]],
    fallback_timed: list[Turn] | None,
    speakers: list[Turn],
) -> None:
    count = i1 - i0 + 1
    if count <= 0:
        return
    mid = (left + right) / 2.0
    gap_hit = next((g for g in gaps if g[0] <= mid <= g[1]), None)
    if gap_hit:
        # только сегменты внутри [left, right], не вся 8-минутная дыра Whisper
        clip_a = max(left, gap_hit[0])
        clip_b = min(right, gap_hit[1])
        segs = _gap_clock_segments(clip_a, clip_b, fallback_timed, speakers)
        if not segs:
            segs = [(clip_a, clip_b)] if clip_b > clip_a else [(left, right)]
        slots = _slots_from_spans(segs, count)
        for k, idx in enumerate(range(i0, i1 + 1)):
            a, b = slots[k]
            starts[idx] = a
            ends[idx] = max(a + 0.35, b)
        return

    span = max(right - left, 0.35 * count)
    step = span / count
    for k, idx in enumerate(range(i0, i1 + 1)):
        starts[idx] = left + k * step
        ends[idx] = left + (k + 1) * step


def _split_duplicate_slots(
    starts: list[float | None],
    ends: list[float | None],
    i0: int,
    i1: int,
    sentences: list[str] | None = None,
) -> None:
    i = i0
    while i <= i1:
        j = i + 1
        while (
            j <= i1
            and starts[j] == starts[i]
            and ends[j] == ends[i]
        ):
            j += 1
        if j > i + 1:
            a = float(starts[i])  # type: ignore[arg-type]
            b = float(ends[i])  # type: ignore[arg-type]
            n = j - i
            weights = [
                max(len((sentences[idx] if sentences else "") or ""), 12)
                for idx in range(i, j)
            ]
            total_w = max(sum(weights), 1)
            min_span = sum(max(0.35, w / MAX_CHARS_PER_SEC) for w in weights)
            next_a = float(starts[j]) if j <= i1 and starts[j] is not None else None
            span = max(b - a, min_span, 0.35 * n)
            if next_a is not None and next_a > a + MIN_TURN_GAP_SEC:
                span = min(max(span, min_span), next_a - a - MIN_TURN_GAP_SEC)
            pos = a
            for k, idx in enumerate(range(i, j)):
                slot = max(span * (weights[k] / total_w), 0.35)
                starts[idx] = pos
                ends[idx] = pos + slot
                pos = ends[idx]
        i = j


def _distribute_even(
    sentences: list[str],
    w_start: float,
    w_end: float,
    gaps: list[tuple[float, float]],
    fallback_timed: list[Turn] | None,
    speakers: list[Turn],
    phrase_clocks: list[Turn] | None = None,
) -> list[Turn]:
    n = len(sentences)
    if n == 0:
        return []
    if phrase_clocks:
        return _map_sentences_to_phrase_clocks(sentences, phrase_clocks, w_start, w_end)
    starts: list[float | None] = [None] * n
    ends: list[float | None] = [None] * n
    _place_range(
        starts,
        ends,
        0,
        n - 1,
        w_start,
        w_end,
        gaps or [(w_start, w_end)],
        fallback_timed,
        speakers,
    )
    out: list[Turn] = []
    for i, sent in enumerate(sentences):
        s = float(starts[i] if starts[i] is not None else w_start)
        e = float(ends[i] if ends[i] is not None else w_end)
        out.append(_turn_from_sentence(sent, s, e))
    return out


def _monotonize_word_map(g2w: list[int | None]) -> list[int | None]:
    """Whisper-индексы только вперёд — иначе часы «перемешивают» предложения Gemma."""
    out: list[int | None] = [None] * len(g2w)
    prev = -1
    for i, wi in enumerate(g2w):
        if wi is None:
            continue
        if wi >= prev:
            out[i] = wi
            prev = wi
    return out


def _nudge_monotonic_keep_order(turns: list[Turn]) -> list[Turn]:
    """Монотонные start без сдвига якорей Whisper: end сохраняем, если возможно."""
    if not turns:
        return []
    out: list[Turn] = []
    for t in turns:
        orig_start = float(t.start)
        orig_end = float(t.end)
        orig_dur = max(orig_end - orig_start, MIN_TURN_DUR_SEC)
        start = orig_start
        end = orig_end
        if end <= start:
            end = start + MIN_TURN_DUR_SEC
        if out and start < out[-1].end + MIN_TURN_GAP_SEC:
            start = out[-1].end + MIN_TURN_GAP_SEC
            end = max(orig_end, start + orig_dur)
        if end <= start:
            end = start + MIN_TURN_DUR_SEC
        out.append(Turn(start=start, end=end, speaker=t.speaker, text=t.text))
    return out


def _reconcile_timestamps_keep_order(
    turns: list[Turn],
    *,
    max_end: float | None = None,
) -> list[Turn]:
    """Deprecated: используйте _nudge_monotonic_keep_order."""
    return _nudge_monotonic_keep_order(turns)


def _enforce_monotonic_keep_order(turns: list[Turn]) -> list[Turn]:
    return _nudge_monotonic_keep_order(turns)


def _enforce_monotonic(turns: list[Turn]) -> list[Turn]:
    """Лёгкая монотонность без сжатия всего хвоста в начало.

    Не раздуваем end до prev_end+… — иначе после удлинения склейки клики уезжают.
    """
    if not turns:
        return []
    out: list[Turn] = []
    prev_start = -1.0
    for t in sorted(turns, key=lambda x: (x.start, x.end)):
        start = t.start
        end = t.end
        if start < prev_start:
            start = prev_start
        if end <= start:
            end = start + 0.25
        out.append(Turn(start=start, end=end, speaker=t.speaker, text=t.text))
        prev_start = start
    return out


def _sanitize_keep_content(turns: list[Turn]) -> list[Turn]:
    """Meta/loop scrub only — текст Gemma не режем по density."""
    from hallucination_guard import looks_like_loop, scrub_merged_text
    from speakers import Turn as TurnCls

    out: list[Turn] = []
    for t in turns:
        body = scrub_merged_text(t.text or "")
        if not body:
            continue
        if looks_like_loop(body):
            continue
        out.append(TurnCls(start=t.start, end=t.end, speaker=t.speaker, text=body))
    return out
