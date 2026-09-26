"""Нормализация текста документов: разбиение на части, промпт LLM и лёгкая нормализация без LLM.

Таблицы в LLM не отправляются: на их месте метка ``[[ТАБЛИЦА_N]]``, после ответа таблица возвращается дословно
(цифры не переписываются и не теряются). Ответ LLM не берётся (часть идёт лёгкой нормализацией), если он заметно
«ужат» по значимым символам или в нём пропало больше 10 % чисел входа (время, таймкоды, суммы, номера пунктов).
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from pathlib import Path

from app.app.config import Settings
from app.app.logging_setup import log_event
from app.app.services.llm import chat_completion_no_think
from app.app.services.prompts import load_prompt_block
from app.app.services.table_md import split_blocks

logger = logging.getLogger("skkkk.normalize")
ROOT = Path(__file__).resolve().parents[3]
PROMPT_PATH = ROOT / "docs" / "prompts" / "normalize_document.md"
SECTION_MAX_CHARS = 8000
SHRINK_MIN_RATIO = 0.6  # ответ LLM короче 60% входа — считаем обрезанным
SHRINK_CHECK_MIN_CHARS = 400
NUMBERS_MIN_KEPT = 0.9  # ответ LLM потерял больше 10 % чисел входа (время, таймкоды, суммы, номера пунктов) — не берём
NUMBERS_MIN_LOST = 3
_NUMBER_RE = re.compile(r"\d+(?:[.,:]\d+)*")
_DIGIT_GROUP_RE = re.compile(r"(?<=\d)[ \u00a0](?=\d{3}(?!\d))")
TABLE_TOKEN = "[[ТАБЛИЦА_{n}]]"
TABLE_TOKEN_RE = re.compile(r"\[\[ТАБЛИЦА_(\d+)\]\]")


def _load_prompt() -> str:
    """Первый ограждённый блок промпта (не заголовок файла) — см. services/prompts.py."""
    return load_prompt_block(PROMPT_PATH)


def protect_tables(raw: str) -> tuple[str, dict[int, str]]:
    """Таблицы → метки ``[[ТАБЛИЦА_N]]``; возвращает текст с метками и словарь N → исходный блок таблицы."""
    blocks = split_blocks(raw)
    if not any(b.kind == "table" for b in blocks):
        return raw, {}
    tables: dict[int, str] = {}
    parts: list[str] = []
    for block in blocks:
        if block.kind == "table":
            n = len(tables) + 1
            tables[n] = block.text
            parts.append(TABLE_TOKEN.format(n=n))
        else:
            parts.append(block.text)
    return "\n\n".join(parts), tables


def restore_tables(text: str, tables: dict[int, str]) -> str:
    """Метки → таблицы дословно; пропавшие у LLM метки дописываются после ближайшей предыдущей таблицы/в конец."""
    used: set[int] = set()

    def sub(m: re.Match[str]) -> str:
        n = int(m.group(1))
        if n not in tables or n in used:
            return ""
        used.add(n)
        return tables[n]

    out = TABLE_TOKEN_RE.sub(sub, text)
    missing = [n for n in sorted(tables) if n not in used]
    if missing:
        log_event(logger, "normalize.table_token_lost", tables=",".join(map(str, missing)))
        out = out.rstrip() + "\n\n" + "\n\n".join(tables[n] for n in missing)
    return out


def _split_long(part: str, max_chars: int) -> list[str]:
    """Часть без заголовков длиннее лимита → по абзацам, абзац длиннее лимита → по строкам."""
    if len(part) <= max_chars:
        return [part]
    units: list[str] = []
    for para in re.split(r"\n{2,}", part):
        if len(para) <= max_chars:
            units.append(para)
        else:
            units += para.splitlines()
    out: list[str] = []
    buf = ""
    for unit in units:
        if buf and len(buf) + len(unit) + 2 > max_chars:
            out.append(buf)
            buf = unit
        else:
            buf = f"{buf}\n\n{unit}" if buf else unit
    if buf:
        out.append(buf)
    return out


def _split_sections(text: str, max_chars: int = SECTION_MAX_CHARS) -> list[str]:
    parts: list[str] = []
    for part in re.split(r"(?=^#{1,3}\s)", text, flags=re.M):
        parts += _split_long(part, max_chars)
    chunks: list[str] = []
    buf = ""
    for part in parts:
        if len(buf) + len(part) > max_chars and buf:
            chunks.append(buf.strip())
            buf = part
        else:
            buf += part if not buf or buf.endswith("\n") else "\n\n" + part
    if buf.strip():
        chunks.append(buf.strip())
    return chunks or [text]


def _prose_len(text: str) -> int:
    """Значимые символы прозы: без меток таблиц и пробелов (отступы HTML и пустые строки законно убираются)."""
    return len(re.sub(r"\s+", "", TABLE_TOKEN_RE.sub("", text)))


def _numbers(text: str) -> Counter[str]:
    """Числа прозы (без меток таблиц): «240 000» и «240000» — одно число."""
    return Counter(_NUMBER_RE.findall(_DIGIT_GROUP_RE.sub("", TABLE_TOKEN_RE.sub("", text))))


def _numbers_lost(src: str, out: str) -> tuple[int, int]:
    """(сколько чисел входа пропало в ответе, сколько их было)."""
    before, after = _numbers(src), _numbers(out)
    total = sum(before.values())
    lost = sum(max(0, k - after[n]) for n, k in before.items())
    return lost, total


def normalize_document(settings: Settings, raw: str, *, source: str | None = None) -> str:
    system = _load_prompt()
    protected, tables = protect_tables(raw)
    cleaned_parts: list[str] = []
    for section in _split_sections(protected):
        if _prose_len(section) == 0:  # только таблицы — нечего чистить
            cleaned_parts.append(section)
            continue
        user = f"Нормализуй текст, не теряя числа, даты и суммы:\n\n{section}"
        part = chat_completion_no_think(settings, system=system, user=user, max_tokens=8192).strip()
        src_len, out_len = _prose_len(section), _prose_len(part)
        lost, total = _numbers_lost(section, part)
        if src_len >= SHRINK_CHECK_MIN_CHARS and out_len < SHRINK_MIN_RATIO * src_len:
            log_event(logger, "normalize.shrunk", artifact=source, src_chars=src_len, out_chars=out_len)
            part = light_normalize(section).strip()
        elif lost >= NUMBERS_MIN_LOST and lost > (1 - NUMBERS_MIN_KEPT) * total:
            log_event(logger, "normalize.numbers_lost", artifact=source, numbers=total, lost=lost)
            part = light_normalize(section).strip()
        cleaned_parts.append(part)
    return restore_tables("\n\n".join(cleaned_parts).strip(), tables).strip() + "\n"


def light_normalize(raw: str) -> str:
    """Без LLM: пробелы и пустые строки."""
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in raw.splitlines()]
    out: list[str] = []
    blank = False
    for ln in lines:
        if not ln:
            if not blank:
                out.append("")
            blank = True
            continue
        blank = False
        out.append(ln)
    return "\n".join(out).strip() + "\n"
