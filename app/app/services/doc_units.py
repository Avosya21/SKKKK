"""Единицы документа с адресами ``P<n>`` (docs/plans/DECISION-MEMORY-PLAN.md, этап Э1).

Любой источник должен открываться по ссылке на своё место: созвон — реплика ``T<n>``, переписка — сообщение ``M<n>``,
документ — абзац ``P<n>``. Единицы строятся из канонического текста документа (``storage.artifact_knowledge_text``) без
модели и всегда одинаково: номер зависит только от текста, поэтому пока текст не менялся, адрес стабилен.

Что считается единицей: заголовок (строка ``#``), абзац (блок до пустой строки), пункт маркированного или нумерованного
списка, строка таблицы (``Таблица N`` + строки ``| … |``; разделитель ``| --- |`` и подпись «Таблица N» — не единицы).
Нумерация сквозная, с единицы. Адрес-строка — ``P12``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

KIND_HEADING = "heading"
KIND_PARA = "para"
KIND_LIST = "list"
KIND_ROW = "row"
CONTEXT_BEFORE = 3  # единиц до источника в карточке (DECISION-MEMORY-PLAN Э1)
CONTEXT_AFTER = 1

_HEADING = re.compile(r"^#{1,6}\s+\S")
_LIST_ITEM = re.compile(r"^\s*(?:[*•\-–—]|\d{1,3}[.)])\s+\S")
_TABLE_CAPTION = re.compile(r"^Таблица\s+\d+\s*$")
_TABLE_SEPARATOR = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_SPACES = re.compile(r"\s+")


@dataclass(frozen=True)
class Unit:
    id: int  # номер P<n>, с единицы
    kind: str
    text: str  # без служебной разметки заголовка; строки внутри абзаца склеены через пробел
    start: int  # смещение начала в исходном тексте (для поиска цитаты)
    end: int

    @property
    def address(self) -> str:
        return f"P{self.id}"


def _flat(text: str) -> str:
    return _SPACES.sub(" ", text).strip()


def split_units(text: str) -> list[Unit]:
    """Разбить текст документа на единицы. Пустой текст — пустой список."""
    units: list[Unit] = []
    if not (text or "").strip():
        return units
    block: list[tuple[int, str]] = []  # (смещение строки, строка) текущего абзаца
    in_table = False
    offset = 0

    def add(kind: str, start: int, end: int, raw: str) -> None:
        flat = _flat(raw)
        if flat:
            units.append(Unit(len(units) + 1, kind, flat, start, end))

    def flush() -> None:
        nonlocal block
        if block:
            start, end = block[0][0], block[-1][0] + len(block[-1][1])
            add(KIND_PARA, start, end, " ".join(line for _o, line in block))
        block = []

    for raw_line in text.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        start, stripped = offset, line.strip()
        offset += len(raw_line)
        if not stripped:
            flush()
            in_table = False
            continue
        if _TABLE_CAPTION.match(stripped):
            flush()
            in_table = True
            continue
        if in_table and stripped.startswith("|"):
            if not _TABLE_SEPARATOR.match(stripped):
                add(KIND_ROW, start, start + len(line), stripped.strip("|"))
            continue
        in_table = False
        if _HEADING.match(stripped):
            flush()
            add(KIND_HEADING, start, start + len(line), stripped.lstrip("#"))
        elif _LIST_ITEM.match(line):
            flush()
            add(KIND_LIST, start, start + len(line), re.sub(r"^\s*(?:[*•\-–—]|\d{1,3}[.)])\s+", "", line))
        else:
            block.append((start, line))
    flush()
    return units


def unit_by_id(units: list[Unit], unit_id: int | None) -> Unit | None:
    if unit_id is None:
        return None
    return next((u for u in units if u.id == unit_id), None)


def numbered_lines(units: list[Unit]) -> list[str]:
    """Строки для модели: «P<n> текст»; единица занимает одну строку."""
    return [f"P{u.id} {u.text}" for u in units]


def context_window(units: list[Unit], unit_id: int, *, before: int = CONTEXT_BEFORE, after: int = CONTEXT_AFTER) -> list[Unit]:
    """Источник и соседние единицы: ``before`` до и ``after`` после (ход текста вокруг пункта)."""
    pos = next((i for i, u in enumerate(units) if u.id == unit_id), None)
    if pos is None:
        return []
    return units[max(0, pos - before): pos + after + 1]


_QUOTE_NORM = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _QUOTE_NORM.sub(" ", (text or "").lower().replace("ё", "е")).strip()


def find_unit_by_quote(units: list[Unit], quote: str, *, prefer: list[int] | None = None) -> Unit | None:
    """Единица, в тексте которой дословно (без учёта регистра, пробелов и ё) есть цитата. Среди подсказанных (``prefer``)
    — первыми. Цитата, растянувшаяся на соседние единицы, ищется по началу: берётся единица, с которой она начинается."""
    needle = _norm(quote)
    if len(needle) < 12:
        return None
    ordered = [u for u in units if prefer and u.id in prefer] + [u for u in units if not prefer or u.id not in prefer]
    for unit in ordered:
        if needle in _norm(unit.text):
            return unit
    for unit in ordered:  # цитата начинается в конце единицы и продолжается в следующей: хвост единицы = начало цитаты
        hay = _norm(unit.text)
        for k in range(min(len(hay), len(needle) - 1), 11, -1):
            if hay.endswith(needle[:k]):
                return unit
    return None
