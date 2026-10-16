"""Проверка цитат в ответе чата: каждая цитата «…» от четырёх слов ищется в материалах, которые видела модель.

Урок 16.10 (пр. 28): под нажимом пользователя чат сослался на выжимку созвона, где этих слов не было. Модель
просят подкреплять факты дословными цитатами (``docs/prompts/rag_chat.md``), а код без вызова модели сверяет
их с контекстом ответа. Предыдущий диалог и слова пользователя доказательством не считаются: цитата из них не
помечается, но и подтверждённой не идёт. Ненайденная цитата помечается прямо в тексте ответа.
"""

from __future__ import annotations

import re
from typing import Any

QUOTE_MIN_WORDS = 4  # короче — обычно термин или название («Заказ клиента»), а не цитата
SHINGLE = 3  # слов в куске для нечёткой сверки: расшифровка и модель расходятся в отдельных словах
SHINGLE_SHARE = 0.7  # доля кусков цитаты, найденных в материалах, чтобы цитата считалась найденной
UNVERIFIED_MARK = " (⚠ дословно в материалах не найдено)"

_QUOTE_RE = re.compile(r"«([^«»]{3,800})»|“([^“”]{3,800})”")
_ELLIPSIS_RE = re.compile(r"\.{3}|…|\[\.\.\.\]|\(\.\.\.\)")
_NON_WORD = re.compile(r"[^\w]+", re.U)
_DIGIT_GROUP = re.compile(r"(?<=\d)[\s  ](?=\d{3}(?!\d))")


def normalize(text: str) -> str:
    """Сравнимый вид: строчные, ё → е, разряды чисел склеены, знаки препинания — пробел."""
    value = _DIGIT_GROUP.sub("", (text or "").lower().replace("ё", "е"))
    return " " + " ".join(_NON_WORD.sub(" ", value).split()) + " "


def _part_found(words: list[str], evidence: str) -> bool:
    if " " + " ".join(words) + " " in evidence:
        return True
    if "".join(words) in evidence.replace(" ", ""):  # «штрих-кодирование» и «штрихкодирование», «1С: УТ» и «1С:УТ»
        return True
    if len(words) < SHINGLE + 1:
        return False
    shingles = [" " + " ".join(words[i:i + SHINGLE]) + " " for i in range(len(words) - SHINGLE + 1)]
    found = sum(1 for s in shingles if s in evidence)
    return found / len(shingles) >= SHINGLE_SHARE


def quote_found(quote: str, evidence: str) -> bool:
    """Цитата есть в нормализованных материалах ``evidence``; части, разделённые «…», проверяются по отдельности."""
    parts = [normalize(p).split() for p in _ELLIPSIS_RE.split(quote)]
    parts = [p for p in parts if len(p) >= 2]
    return bool(parts) and all(_part_found(p, evidence) for p in parts)


def extract_quotes(answer: str) -> list[str]:
    """Цитаты из ответа в кавычках «…» или “…” от ``QUOTE_MIN_WORDS`` слов, без повторов, в порядке появления."""
    seen: list[str] = []
    for m in _QUOTE_RE.finditer(answer or ""):
        quote = (m.group(1) or m.group(2) or "").strip()
        if len(normalize(quote).split()) >= QUOTE_MIN_WORDS and quote not in seen:
            seen.append(quote)
    return seen


def verify_answer_quotes(answer: str, evidence: str, *, user_texts: list[str] | tuple[str, ...] = ()) -> tuple[str, dict[str, Any]]:
    """Сверяет цитаты ответа с материалами ``evidence``. Возвращает ответ с пометками у ненайденных цитат и отчёт
    ``{checked, verified, unverified: [...], from_user}``. Цитаты из ``user_texts`` (вопрос, указания) не помечаются."""
    evidence_norm = normalize(evidence)
    user_norm = normalize(" \n ".join(user_texts))
    report: dict[str, Any] = {"checked": 0, "verified": 0, "unverified": [], "from_user": 0}
    marked = answer
    for quote in extract_quotes(answer):
        report["checked"] += 1
        if quote_found(quote, evidence_norm):
            report["verified"] += 1
            continue
        if user_norm.strip() and quote_found(quote, user_norm):
            report["from_user"] += 1
            continue
        report["unverified"].append(quote)
        for opening, closing in (("«", "»"), ("“", "”")):
            wrapped = f"{opening}{quote}{closing}"
            if wrapped in marked:
                marked = marked.replace(wrapped, wrapped + UNVERIFIED_MARK)
                break
    return marked, report
