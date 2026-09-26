"""Построчная сверка снимка 1+2 с независимым мнением GigaAM (этап 3b).

Что здесь есть и чего нет:

* **есть** сопоставление спикеров GigaAM ↔ лаборатории по уже уверенным словам;
* **есть** приговор по каждому спорному куску: закрыт, подтверждён, конфликт, открыт;
* **есть** сравнение текста по окнам реплик GigaAM и выбор лучшего варианта;
* **нет** молчаливой перезаписи ``04_paint`` / ``sense/``: приговоры живут отдельно;
* **нет** сочинения текста — берётся либо вариант лаборатории, либо GigaAM.

Таймкоды GigaAM округлены до секунды, поэтому «уверенно внутри реплики» — это
не просто попадание, а попадание с запасом от границ (``EDGE_MARGIN``).
"""

from __future__ import annotations

import difflib
import re
from collections import Counter, defaultdict
from typing import Any

from .stages.s04_paint import ASSIGNED, CONTESTED, UNASSIGNED

DISPUTED = {CONTESTED, UNASSIGNED}

EDGE_MARGIN = 0.5
MIN_MAP_SHARE = 0.75
MIN_MAP_VOTES = 5
MIN_TURN_PURITY = 0.9
MIN_TURN_VOTES = 5
TEXT_SIMILAR = 0.93

CLOSED = "closed"
CONFIRMED = "confirmed"
CONFLICT = "conflict"
UNVERIFIED = "unverified"
OPEN = "open"

WORD_RE = re.compile(r"[\w\-]+", re.UNICODE)


def norm_words(text: str) -> list[str]:
    """Слова без пунктуации и регистра — сравниваем услышанное, не оформление."""
    return WORD_RE.findall((text or "").lower().replace("ё", "е"))


def similarity(left: str, right: str) -> float:
    a, b = norm_words(left), norm_words(right)
    if not a and not b:
        return 1.0
    return round(difflib.SequenceMatcher(None, a, b).ratio(), 4)


def word_mid(word: dict[str, Any]) -> float:
    return (float(word["start"]) + float(word["end"])) / 2


def turns_touching(
    turns: list[dict[str, Any]],
    start: float,
    end: float,
) -> list[dict[str, Any]]:
    return [
        turn
        for turn in turns
        if float(turn["end"]) > start and float(turn["start"]) < end
    ]


def turn_at(turns: list[dict[str, Any]], moment: float) -> dict[str, Any] | None:
    for turn in turns:
        if float(turn["start"]) <= moment < float(turn["end"]):
            return turn
    return None


def map_speakers(
    words: list[dict[str, Any]],
    turns: list[dict[str, Any]],
    *,
    min_share: float = MIN_MAP_SHARE,
    min_votes: int = MIN_MAP_VOTES,
) -> dict[str, Any]:
    """GigaAM → лаборатория по словам, в которых лаборатория уже уверена.

    Спикеров GigaAM обычно больше: pyannote дробит один голос. Несколько
    ``giga_*`` могут указывать на одного лабораторного — это не ошибка.
    """
    votes: dict[str, Counter[int]] = defaultdict(Counter)
    for word in words:
        if word.get("speaker_index") is None:
            continue
        if word.get("verdict") != ASSIGNED and not word.get("sense"):
            continue
        turn = turn_at(turns, word_mid(word))
        if turn is None:
            continue
        votes[turn["speaker"]][int(word["speaker_index"])] += 1

    mapping: dict[str, Any] = {}
    for giga_speaker in sorted(votes):
        counts = votes[giga_speaker]
        total = sum(counts.values())
        top, n = counts.most_common(1)[0]
        share = n / total if total else 0.0
        accepted = share >= min_share and n >= min_votes
        mapping[giga_speaker] = {
            "speaker_index": top if accepted else None,
            "guess_index": top,
            "share": round(share, 3),
            "votes": n,
            "total": total,
            "counts": counts.most_common(),
            "accepted": accepted,
        }
    return mapping


def turn_purity(
    turns: list[dict[str, Any]],
    words: list[dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    """Насколько реплика GigaAM однородна по нашим уверенным словам.

    У pyannote в одну реплику нередко попадает целый диалог — в тексте GigaAM это
    видно по тире. Проверять это текстом ненадёжно, поэтому смотрим на данные:
    если внутри реплики уверенные слова принадлежат разным лабораторным
    спикерам, реплика смешанная и закрывать по ней спорный кусок нельзя.

    Голосуют только слова, уверенные ещё на этапе 1. Ответы этапа 2 сюда не
    берём: именно их сверка и проверяет, иначе спорный кусок оценивал бы сам себя.
    """
    inside: dict[int, Counter[int]] = defaultdict(Counter)
    for word in words:
        if word.get("speaker_index") is None or word.get("verdict") != ASSIGNED:
            continue
        turn = turn_at(turns, word_mid(word))
        if turn is not None:
            inside[int(turn["i"])][int(word["speaker_index"])] += 1

    rows: dict[int, dict[str, Any]] = {}
    for turn in turns:
        counts_ = inside.get(int(turn["i"]), Counter())
        total = sum(counts_.values())
        if total:
            top, n = counts_.most_common(1)[0]
            purity = n / total
        else:
            top, n, purity = None, 0, 0.0
        rows[int(turn["i"])] = {
            "speaker_index": top,
            "votes": n,
            "total": total,
            "purity": round(purity, 3),
            "counts": counts_.most_common(),
            "pure": total >= MIN_TURN_VOTES and purity >= MIN_TURN_PURITY,
        }
    return rows


def disputed_spans(
    words: list[dict[str, Any]],
    *,
    max_internal_gap: float = 1.5,
) -> list[list[dict[str, Any]]]:
    """Куски, которые были спорными в снимке этапа 1 — даже если этап 2 их закрыл."""
    spans: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for word in words:
        if word.get("verdict") in DISPUTED:
            if current and float(word["start"]) - float(current[-1]["end"]) > max_internal_gap:
                spans.append(current)
                current = []
            current.append(word)
        elif current:
            spans.append(current)
            current = []
    if current:
        spans.append(current)
    return spans


def giga_opinion(
    span: list[dict[str, Any]],
    turns: list[dict[str, Any]],
    mapping: dict[str, Any],
    purity: dict[int, dict[str, Any]],
    *,
    edge_margin: float = EDGE_MARGIN,
) -> dict[str, Any]:
    """Мнение GigaAM о куске: одна однородная реплика с запасом — или ничего."""
    start = float(span[0]["start"])
    end = float(span[-1]["end"])
    touched = turns_touching(turns, start, end)
    labels = sorted({turn["speaker"] for turn in touched})
    if len(labels) != 1:
        return {
            "giga_speaker": None,
            "speaker_index": None,
            "confident": False,
            "reason": "GigaAM тоже делит этот кусок" if labels else "GigaAM здесь молчит",
            "turns": [turn["i"] for turn in touched],
        }
    turn = touched[0]
    margin = round(min(start - float(turn["start"]), float(turn["end"]) - end), 3)
    row = mapping.get(turn["speaker"]) or {}
    index = row.get("speaker_index")
    turn_row = purity.get(int(turn["i"])) or {}
    pure = bool(turn_row.get("pure"))
    if index is None:
        reason = f"{turn['speaker']} не сопоставлен с лабораторным спикером"
    elif not pure:
        reason = (
            "реплика GigaAM смешанная: внутри неё наши уверенные слова "
            f"разных спикеров ({turn_row.get('counts')})"
        )
    elif margin < edge_margin:
        reason = f"кусок у границы реплики GigaAM (запас {margin} с)"
    else:
        reason = "кусок целиком внутри однородной реплики GigaAM"
    # Внутри однородной реплики верим её собственному большинству, а не карте:
    # карта усредняет по всей записи, чистая реплика говорит про это место.
    local_index = turn_row.get("speaker_index") if pure else None
    confident = pure and index is not None and margin >= edge_margin
    return {
        "giga_speaker": turn["speaker"],
        "speaker_index": local_index if confident else None,
        "confident": confident,
        "margin": margin,
        "purity": turn_row.get("purity"),
        "purity_counts": turn_row.get("counts"),
        "mapped_index": index,
        "reason": reason,
        "turns": [turn["i"]],
        "giga_text": turn["text"],
    }


def judge_spans(
    words: list[dict[str, Any]],
    turns: list[dict[str, Any]],
    mapping: dict[str, Any],
    *,
    edge_margin: float = EDGE_MARGIN,
) -> list[dict[str, Any]]:
    """Приговор по каждому спорному куску снимка. Ничего не переписывает."""
    purity = turn_purity(turns, words)
    rows: list[dict[str, Any]] = []
    for span in disputed_spans(words):
        opinion = giga_opinion(span, turns, mapping, purity, edge_margin=edge_margin)
        ours = [w.get("speaker_index") for w in span]
        by_sense = any(w.get("sense") for w in span)
        our_index = ours[0] if len(set(ours)) == 1 else None
        giga_index = opinion["speaker_index"] if opinion["confident"] else None

        if giga_index is None:
            verdict = UNVERIFIED if our_index is not None else OPEN
        elif our_index is None:
            verdict = CLOSED
        elif our_index == giga_index:
            verdict = CONFIRMED
        else:
            verdict = CONFLICT

        rows.append(
            {
                "word_from": int(span[0]["i"]),
                "word_to": int(span[-1]["i"]),
                "start": float(span[0]["start"]),
                "end": float(span[-1]["end"]),
                "text": " ".join(w["word"] for w in span),
                "verdicts": sorted({w.get("verdict") for w in span}),
                "our_speaker_index": our_index,
                "from_sense": by_sense,
                "giga": opinion,
                "verdict": verdict,
            }
        )
    return rows


def text_windows(
    words: list[dict[str, Any]],
    turns: list[dict[str, Any]],
    *,
    similar: float = TEXT_SIMILAR,
) -> list[dict[str, Any]]:
    """Окно = реплика GigaAM. Внутри — наши слова того же времени и оба текста."""
    rows: list[dict[str, Any]] = []
    for turn in turns:
        start, end = float(turn["start"]), float(turn["end"])
        inside = [word for word in words if start <= word_mid(word) < end]
        our_text = " ".join(word["word"] for word in inside)
        score = similarity(our_text, turn["text"])
        rows.append(
            {
                "turn": int(turn["i"]),
                "start": start,
                "end": end,
                "giga_speaker": turn["speaker"],
                "word_from": int(inside[0]["i"]) if inside else None,
                "word_to": int(inside[-1]["i"]) if inside else None,
                "our_text": our_text,
                "giga_text": turn["text"],
                "similarity": score,
                "agree": score >= similar,
                "our_words": len(inside),
            }
        )
    return rows


def counts(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    return dict(Counter(str(row[key]) for row in rows))
