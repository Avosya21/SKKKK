"""Защита стенограммы от галлюцинаций ASR на тишине и от «простыни в 4 секунды».

Таймлайн аудио не сжимаем: плеер должен совпадать с исходником.
Вместо вырезания тишины из WAV — отсев ложного текста и лимит по бюджету речи.
"""

from __future__ import annotations

import difflib
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from speakers import Turn

# Нормальная русская речь ~12–15 символов/с; 20 — верх с запасом на быстрый темп.
MAX_CHARS_PER_SEC = 20.0
# ~2–2.5 слова/с обычно; выше 3.5 на длинном куске почти всегда артефакт.
MAX_WORDS_PER_SEC = 3.5
# Короткий «остров» диаризации с длинным текстом — классика бага со скрина.
MIN_TURN_SEC_FOR_LONG_TEXT = 0.15
MAX_WORDS_ON_TINY_TURN = 2
# Один движок на почти пустом окне — типичный Whisper-loop на паузе.
MIN_SPEECH_SEC_FOR_SINGLE_ENGINE = 8.0
MIN_SPEECH_SEC_ANY = 0.5
MIN_WINDOW_SPEECH_RATIO = 0.04

_WORD_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+(?:-[0-9A-Za-zА-Яа-яЁё]+)?")
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+|\n+")
_META_LINE_RE = re.compile(
    r"(?i)("
    r"варианты\s+не\s+совпадают|"
    r"разные\s+фрагменты|"
    r"вариант\s*[123]\s+(и\s+вариант|представляет)|"
    r"это\s+разные\s+куски|"
    r"не\s+стыкуются\s+по\s+смыслу|"
    r"автоматически\s+собранн|"
    r"как\s+корректор\s+стенограмм"
    r")"
)


from clock_guard import fallback_budget_sec


def strip_merge_meta(text: str) -> str:
    """Убирает служебные комментарии Gemma про расхождение вариантов ASR."""
    body = (text or "").strip()
    if not body:
        return ""
    kept: list[str] = []
    for chunk in re.split(r"\n+", body):
        piece = chunk.strip()
        if not piece:
            continue
        if _META_LINE_RE.search(piece):
            continue
        # иногда мета приклеена к первой фразе через точку
        sentences = [s.strip() for s in _SENT_SPLIT_RE.split(piece) if s.strip()]
        clean = [s for s in sentences if not _META_LINE_RE.search(s)]
        if clean:
            kept.append(" ".join(clean))
    return "\n\n".join(kept).strip()


def word_count(text: str) -> int:
    return len(_WORD_RE.findall(text or ""))


def speech_budget_chars(speech_sec: float) -> int:
    sec = max(0.0, float(speech_sec))
    return max(0, int(sec * MAX_CHARS_PER_SEC))


def looks_like_loop(text: str) -> bool:
    body = (text or "").strip()
    if len(body) < 48:
        return False
    probe = body[:40].strip()
    if probe and body.count(probe) > 2:
        return True
    sentences = [s.strip() for s in _SENT_SPLIT_RE.split(body) if s.strip()]
    if len(sentences) >= 3:
        # три одинаковых предложения подряд
        for i in range(len(sentences) - 2):
            a, b, c = sentences[i], sentences[i + 1], sentences[i + 2]
            if a == b == c and len(a) >= 12:
                return True
        # одна фраза занимает >55% предложений
        counts: dict[str, int] = {}
        for s in sentences:
            counts[s] = counts.get(s, 0) + 1
        top = max(counts.values())
        if top >= 3 and top / len(sentences) >= 0.55:
            return True
    # подряд идущий повтор длинного куска (без пунктуации)
    compact = re.sub(r"\s+", " ", body)
    half = len(compact) // 2
    if half >= 40 and compact[:half].strip() == compact[half : 2 * half].strip():
        return True
    return False


def collapse_adjacent_repeats(text: str) -> str:
    """Схлопывает «фраза. фраза.» и тройные петли Whisper (в т.ч. со сдвигом цикла)."""
    body = (text or "").strip()
    if not body:
        return ""
    sentences = [s.strip() for s in _SENT_SPLIT_RE.split(body) if s.strip()]
    if not sentences:
        return body
    out: list[str] = []
    for sent in sentences:
        if out and _same_sentence_family(out[-1], sent):
            if len(sent) > len(out[-1]):
                out[-1] = sent
            continue
        out.append(sent)
    joined = " ".join(out)
    if looks_like_loop(joined):
        return ""
    # одна фраза, внутри которой цикл слов («Еще, у нас… Еще, у нас…»)
    if _internal_word_cycle(joined):
        return _first_cycle_sentence(joined) or ""
    return joined


def _norm_for_family(text: str) -> str:
    body = (text or "").strip().lower().replace("ё", "е")
    body = re.sub(r"\s+", " ", body)
    body = re.sub(r"[«»\"']", "", body)
    body = re.sub(r"\s*-\s*", "-", body)
    return body.strip(" .,-—–")


def _same_sentence_family(a: str, b: str) -> bool:
    na, nb = _norm_for_family(a), _norm_for_family(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= 0.75


def _internal_word_cycle(text: str) -> bool:
    words = _norm_for_family(text).split()
    if len(words) < 10:
        return False
    # ищем период 4–8 слов, повторяющийся ≥3 раз
    for period in range(4, 9):
        if len(words) < period * 3:
            continue
        chunk = words[:period]
        hits = 0
        for i in range(0, len(words) - period + 1, period):
            if words[i : i + period] == chunk:
                hits += 1
            else:
                break
        if hits >= 3:
            return True
    return False


def _first_cycle_sentence(text: str) -> str:
    sentences = [s.strip() for s in _SENT_SPLIT_RE.split(text) if s.strip()]
    if sentences:
        return sentences[0]
    words = (text or "").split()
    return " ".join(words[:8]).strip()


def scrub_engine_text(text: str) -> str:
    body = collapse_adjacent_repeats(text)
    if looks_like_loop(body):
        return ""
    return body.strip()


def scrub_merged_text(text: str) -> str:
    body = strip_merge_meta(text)
    body = collapse_adjacent_repeats(body)
    if looks_like_loop(body):
        return ""
    return body.strip()


def exceeds_density(text: str, duration_sec: float) -> bool:
    body = (text or "").strip()
    if not body:
        return False
    dur = max(0.0, float(duration_sec))
    words = word_count(body)
    if dur < MIN_TURN_SEC_FOR_LONG_TEXT and words > MAX_WORDS_ON_TINY_TURN:
        return True
    safe_dur = max(dur, 0.25)
    if len(body) / safe_dur > MAX_CHARS_PER_SEC:
        return True
    if words / safe_dur > MAX_WORDS_PER_SEC:
        return True
    return False


def cap_text_to_speech_budget(text: str, speech_sec: float) -> str:
    """Обрезает текст окна/реплики до физически возможного объёма речи."""
    body = (text or "").strip()
    if not body:
        return ""
    budget = speech_budget_chars(speech_sec)
    if budget <= 0:
        return ""
    if len(body) <= budget:
        return body
    cut = body[:budget]
    # режем по границе слова/предложения, не посередине
    for sep in (". ", "! ", "? ", "; ", ", ", " "):
        pos = cut.rfind(sep)
        if pos >= max(20, budget // 3):
            cut = cut[: pos + (0 if sep == " " else 1)].strip()
            break
    else:
        cut = cut.rsplit(" ", 1)[0].strip() if " " in cut else ""
    if exceeds_density(cut, speech_sec):
        return ""
    return cut


def window_speech_sec(turns: list[Turn], start: float, end: float) -> float:
    total = 0.0
    for t in turns:
        ov = min(t.end, end) - max(t.start, start)
        if ov > 0:
            total += ov
    return total


def prepare_engine_texts(
    whisper_text: str,
    tone_text: str,
    gigaam_text: str,
    *,
    speech_sec: float,
    window_span: float,
) -> tuple[str, str, str, str | None]:
    """Чистит три черновика окна. reason != None → окно в Gemma не отправлять."""
    w = scrub_engine_text(whisper_text)
    t = scrub_engine_text(tone_text)
    g = scrub_engine_text(gigaam_text)
    alive = [x for x in (w, t, g) if x]
    if not alive:
        return "", "", "", "empty_or_loop"

    span = max(float(window_span), 1.0)
    speech = max(0.0, float(speech_sec))
    coverage = speech / span

    if speech < MIN_SPEECH_SEC_ANY and coverage < MIN_WINDOW_SPEECH_RATIO:
        return "", "", "", "no_speech_timeline"

    if len(alive) == 1 and speech < MIN_SPEECH_SEC_FOR_SINGLE_ENGINE:
        return "", "", "", "single_engine_low_speech"

    # даже при 2–3 движках: суммарный сырой текст не должен быть «романом на паузе»
    raw_chars = sum(len(x) for x in alive)
    budget = speech_budget_chars(speech)
    if speech > 0 and budget > 0 and raw_chars > budget * 3:
        # режем по бюджету, но при 2+ движках пол не ниже самого длинного черновика
        # (иначе Gemma не видит финал окна — та же обрезка, что была после LLM)
        cap_sec = speech
        if len(alive) >= 2:
            eng_max = max(len(x) for x in alive)
            cap_sec = max(cap_sec, eng_max / MAX_CHARS_PER_SEC)
        w2 = cap_text_to_speech_budget(w, cap_sec) if w else ""
        t2 = cap_text_to_speech_budget(t, cap_sec) if t else ""
        g2 = cap_text_to_speech_budget(g, cap_sec) if g else ""
        if any((w2, t2, g2)):
            return w2, t2, g2, None
        if len(alive) >= 2:
            # запасной бюджет по факту «двое слышат» — иначе soft-VAD съедает живую речь
            rescue_sec = max(cap_sec, MIN_SPEECH_SEC_FOR_SINGLE_ENGINE)
            w2 = cap_text_to_speech_budget(w, rescue_sec) if w else ""
            t2 = cap_text_to_speech_budget(t, rescue_sec) if t else ""
            g2 = cap_text_to_speech_budget(g, rescue_sec) if g else ""
            if any((w2, t2, g2)):
                return w2, t2, g2, None
        return "", "", "", "over_budget"

    return w, t, g, None


def pick_fallback_engine_text(
    whisper_text: str,
    tone_text: str,
    gigaam_text: str,
    *,
    speech_sec: float,
    window_span: float = 0.0,
) -> str:
    """Лучший черновик ASR, если Gemma отвергла окно или вход пуст после scrub."""
    candidates: list[tuple[int, str]] = []
    for priority, raw in enumerate((tone_text, gigaam_text, whisper_text)):
        body = scrub_engine_text(raw)
        if body and not looks_like_loop(body):
            candidates.append((priority, body))
    if not candidates:
        return ""
    # tone < gigaam < whisper по priority; при равном приоритете — длиннее
    best = max(candidates, key=lambda x: (len(x[1]), -x[0]))[1]
    budget_sec = fallback_budget_sec(speech_sec, window_span)
    if len(candidates) >= 2:
        budget_sec = max(budget_sec, len(best) / MAX_CHARS_PER_SEC)
    out = cap_text_to_speech_budget(best, budget_sec)
    if not out:
        budget = max(speech_budget_chars(budget_sec), 80)
        cut = best[:budget]
        if len(cut) < len(best) and " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        out = cut.strip()
    return scrub_merged_text(out) or out.strip()


def sanitize_transcript_turns(
    turns: list[Turn],
    *,
    drop_dense: bool = False,
) -> list[Turn]:
    """Scrub meta/петель. densе-cut только по явному флагу (legacy carve), не для канона Gemma."""
    from speakers import Turn as TurnCls

    out: list[Turn] = []
    for t in turns:
        body = scrub_merged_text(t.text or "")
        if not body:
            continue
        if looks_like_loop(body):
            continue
        if drop_dense and exceeds_density(body, t.duration):
            continue
        out.append(TurnCls(start=t.start, end=t.end, speaker=t.speaker, text=body))
    return out
