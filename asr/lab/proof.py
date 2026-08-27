"""Вычитка текста: пословные расхождения лаборатории и GigaAM (этап 3в).

Целыми окнами выбирать нельзя: в одном и том же куске GigaAM бывает прав в
слове «Битриксе», но добавляет своё «э-э», а Parakeet наоборот. Поэтому окно
режется на **расхождения** — минимальные участки, где два движка услышали
по-разному, и решение принимается по каждому отдельно.

Модуль ничего не сочиняет: итоговый текст состоит только из слов, которые
произнёс один из двух движков. Времена и спикеры берутся из снимка 1+2.
"""

from __future__ import annotations

import difflib
import re
from typing import Any

CONTEXT_TOKENS = 8
BATCH_DIFFS = 24

PICK_LAB = "lab"
PICK_GIGA = "giga"
PICKS = {PICK_LAB, PICK_GIGA}

_NORM_RE = re.compile(r"[^\w\-]+", re.UNICODE)

# «1С» кириллицей и «1C» латиницей звучат одинаково и различаются только
# начертанием. Без свёртки такие пары попадали бы в расхождения как настоящие.
_HOMOGLYPHS = str.maketrans("aceopxykmt", "асеорхукмт")


def norm_token(token: str) -> str:
    plain = (token or "").lower().replace("ё", "е")
    return _NORM_RE.sub("", plain.translate(_HOMOGLYPHS))


def giga_tokens(text: str) -> list[str]:
    """Слова GigaAM как есть. Тире-разделители диалога словами не считаем."""
    return [token for token in (text or "").split() if norm_token(token)]


def flat_giga(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Реплики GigaAM в один поток слов: у каждого слова помним, из какой реплики."""
    flat: list[dict[str, Any]] = []
    for turn in sorted(turns, key=lambda row: float(row["start"])):
        for token in giga_tokens(turn.get("text") or ""):
            flat.append({"token": token, "turn": int(turn["i"]), "speaker": turn["speaker"]})
    return flat


def collect_diffs(
    words: list[dict[str, Any]],
    turns: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Расхождения по всей записи сразу.

    Резать на окна по репликам GigaAM нельзя: границы реплик у двух движков не
    совпадают, и на стыке в пару попадают куски из разных мест записи. Одно
    выравнивание на всю стенограмму такие пары не создаёт.
    """
    ours = [dict(word) for word in words]
    theirs = flat_giga(turns)
    a = [norm_token(word["word"]) for word in ours]
    b = [norm_token(row["token"]) for row in theirs]

    rows: list[dict[str, Any]] = []
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        piece = ours[i1:i2]
        if piece:
            anchor, anchor_mode = None, None
        elif i1 > 0:
            anchor, anchor_mode = int(ours[i1 - 1]["i"]), "after"
        elif ours:
            anchor, anchor_mode = int(ours[0]["i"]), "before"
        else:
            continue
        neighbour = piece[0] if piece else ours[max(0, i1 - 1)]
        # Иногда в пару попадают куски, отличающиеся только регистром, «ё» или
        # разбивкой на токены: Parakeet выдаёт «в 1С» одним словом, GigaAM —
        # двумя. Это не про слух, спрашивать о таком нечего.
        cosmetic = "".join(a[i1:i2]) == "".join(b[j1:j2])
        rows.append(
            {
                "id": f"d{i1}:{j1}",
                "op": tag,
                "cosmetic": cosmetic,
                "turn": theirs[j1]["turn"] if j1 < len(theirs) else None,
                "giga_speaker": theirs[j1]["speaker"] if j1 < len(theirs) else None,
                "word_from": int(piece[0]["i"]) if piece else None,
                "word_to": int(piece[-1]["i"]) if piece else None,
                "anchor": anchor,
                "anchor_mode": anchor_mode,
                "start": float(neighbour["start"]),
                "end": float(piece[-1]["end"]) if piece else float(neighbour["end"]),
                "lab": " ".join(word["word"] for word in piece),
                "giga": " ".join(row["token"] for row in theirs[j1:j2]),
                "left": " ".join(
                    word["word"] for word in ours[max(0, i1 - CONTEXT_TOKENS) : i1]
                ),
                "right": " ".join(word["word"] for word in ours[i2 : i2 + CONTEXT_TOKENS]),
                "pick": None,
            }
        )
    return rows


def parse_picks(raw: Any, allowed_ids: set[str]) -> dict[str, str]:
    """Только ``lab`` или ``giga`` по известным id. Свой текст модели не принимаем."""
    if isinstance(raw, dict):
        raw = raw.get("decisions") or raw.get("picks") or []
    if not isinstance(raw, list):
        return {}
    out: dict[str, str] = {}
    for row in raw:
        if not isinstance(row, dict):
            continue
        key = str(row.get("id") or "")
        pick = str(row.get("pick") or "").strip().lower()
        if key in allowed_ids and pick in PICKS:
            out[key] = pick
    return out


def batched(items: list[Any], size: int = BATCH_DIFFS) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def apply_picks(
    words: list[dict[str, Any]],
    diffs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Итоговые слова: у каждого слова появляется ``final`` (может быть пустым).

    Вставки GigaAM цепляются к предыдущему слову — так сохраняются времена и
    спикеры снимка, а новый текст не получает выдуманного таймкода.
    """
    result = [dict(word) for word in words]
    by_index = {int(word["i"]): word for word in result}
    for word in result:
        word.setdefault("final", word["word"])

    for row in diffs:
        if row.get("pick") != PICK_GIGA:
            continue
        giga_text = (row.get("giga") or "").strip()
        first, last = row.get("word_from"), row.get("word_to")
        if first is None or last is None:
            anchor = row.get("anchor")
            if anchor is None or anchor not in by_index or not giga_text:
                continue
            target = by_index[anchor]
            if row.get("anchor_mode") == "before":
                target["final"] = f"{giga_text} {target['final']}".strip()
            else:
                target["final"] = f"{target['final']} {giga_text}".strip()
            target["proof"] = "giga_insert"
            continue
        indexes = [i for i in range(int(first), int(last) + 1) if i in by_index]
        if not indexes:
            continue
        head = by_index[indexes[0]]
        head["final"] = giga_text
        head["proof"] = "giga" if giga_text else "giga_delete"
        for i in indexes[1:]:
            by_index[i]["final"] = ""
            by_index[i]["proof"] = "giga_merged"
    return result


def final_replicas(
    words: list[dict[str, Any]],
    *,
    max_gap: float = 1.2,
) -> list[dict[str, Any]]:
    """Реплики из итоговых слов. Границы — те же, что у снимка: спикер и пауза."""
    replicas: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for word in words:
        speaker = word.get("speaker") or "unknown"
        start, end = float(word["start"]), float(word["end"])
        if (
            current is None
            or current["speaker"] != speaker
            or start - current["end"] > max_gap
        ):
            current = {
                "speaker": speaker,
                "speaker_index": word.get("speaker_index"),
                "start": start,
                "end": end,
                "words": [word],
            }
            replicas.append(current)
        else:
            current["end"] = end
            current["words"].append(word)

    rows = []
    for replica in replicas:
        pieces = [str(word.get("final") or "") for word in replica["words"]]
        text = " ".join(piece for piece in pieces if piece)
        rows.append(
            {
                "speaker": replica["speaker"],
                "speaker_index": replica["speaker_index"],
                "start": round(replica["start"], 3),
                "end": round(replica["end"], 3),
                "text": re.sub(r"\s+", " ", text).strip(),
                "from_giga": sum(1 for word in replica["words"] if word.get("proof")),
                "words": [
                    {
                        "i": word["i"],
                        "word": word["word"],
                        "final": word.get("final"),
                        "start": word["start"],
                        "end": word["end"],
                        "verdict": word.get("verdict"),
                        "sense": word.get("sense"),
                        "proof": word.get("proof"),
                    }
                    for word in replica["words"]
                ],
            }
        )
    return rows
