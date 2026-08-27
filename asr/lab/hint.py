"""Подписи имён с боевого проекта — не основа лаборатории.

Боевая нарезка GigaAM/Docker сюда не копируется. Берём только имена с
``docker_gigaam_speakers.json`` и голосуем ими по уже принятым лабораторным
словам (assigned + оверлей этапа 2). Реплики, текст и ``speaker_index``
не меняются.

Пишет только ``hint/`` внутри прогона.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

MIN_SHARE = 0.85
HINT_DIR = "hint"


def word_mid(word: dict[str, Any]) -> float:
    return (float(word["start"]) + float(word["end"])) / 2


def prod_names_at(mid: float, turns: list[dict[str, Any]]) -> list[str]:
    hits = [
        str(turn.get("speaker") or "")
        for turn in turns
        if float(turn["start"]) <= mid < float(turn["end"]) and turn.get("speaker")
    ]
    return hits or ["(нет реплики на проде)"]


def countable_for_names(word: dict[str, Any]) -> bool:
    """Голос только у слов, которые лаборатория уже кому-то отдала."""
    if word.get("speaker_index") is None:
        return False
    return word.get("verdict") == "assigned" or bool(word.get("sense"))


def leftover_words(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        word
        for word in words
        if word.get("verdict") in {"contested", "unassigned"} and not word.get("sense")
    ]


def map_lab_speakers_to_prod_names(
    words: list[dict[str, Any]],
    turns: list[dict[str, Any]],
    *,
    min_share: float = MIN_SHARE,
) -> dict[str, Any]:
    """Мажоритарные имена. Слабый голос не подписываем и не сливаем спикеров."""
    votes: dict[int, Counter[str]] = defaultdict(Counter)
    for word in words:
        if not countable_for_names(word):
            continue
        for name in prod_names_at(word_mid(word), turns):
            votes[int(word["speaker_index"])][name] += 1

    speakers: dict[str, Any] = {}
    for index in sorted(votes):
        counts = votes[index]
        total = sum(counts.values())
        top, n = counts.most_common(1)[0]
        share = n / total if total else 0.0
        accepted = share >= min_share and not top.startswith("(")
        speakers[str(index)] = {
            "speaker": f"speaker_{index}",
            "display_name": top if accepted else None,
            "guess": top,
            "share": round(share, 3),
            "votes": n,
            "total": total,
            "counts": counts.most_common(),
            "accepted": accepted,
        }

    leftover = []
    for word in leftover_words(words):
        mid = word_mid(word)
        nearby = [
            {
                "start": turn["start"],
                "end": turn["end"],
                "speaker": turn.get("speaker"),
                "text": (turn.get("text") or "")[:180],
            }
            for turn in turns
            if abs(((float(turn["start"]) + float(turn["end"])) / 2) - mid) <= 2
            or (float(turn["start"]) - 0.5 <= mid <= float(turn["end"]) + 0.5)
        ]
        leftover.append(
            {
                "i": word.get("i"),
                "word": word.get("word"),
                "start": word.get("start"),
                "end": word.get("end"),
                "closed": False,
                "reason": (
                    "нет пересечения с боевой стенограммой"
                    if not nearby
                    else "боевая реплика рядом есть, но это не основание отдать слово"
                ),
                "prod_nearby": nearby,
            }
        )

    return {
        "stage": "prod_hint",
        "overlay": True,
        "does_not_replace": [
            "02_asr/words.json",
            "04_paint/paint.json",
            "06_transcript/transcript.json",
            "sense/sense.json",
            "sense/transcript.json",
        ],
        "source": "asr/docker_gigaam_speakers.json",
        "rule": (
            "имена с боя голосуют по серединам уже принятых лабораторных слов; "
            "нарезка реплик и speaker_index не копируются"
        ),
        "min_share": min_share,
        "speakers": speakers,
        "leftover": leftover,
    }
