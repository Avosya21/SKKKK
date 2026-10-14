"""Нарезка текста на фрагменты для индексации: документы (проза + таблицы), реплики стенограммы, сообщения переписки, оценка токенов."""

from __future__ import annotations

import re
from dataclasses import dataclass

CHUNK_TOKENS = 512
OVERLAP_TOKENS = 128
CHARS_PER_TOKEN = 4
WIDE_TABLE_COLS = 8  # шире — строки таблицы идут записями «Столбец: значение»


@dataclass
class TextChunk:
    index: int
    text: str
    speaker: str | None
    token_estimate: int
    start_sec: float | None = None
    end_sec: float | None = None
    unit_first: str | None = None  # адрес первой единицы источника во фрагменте: «M101» — сообщение переписки
    unit_last: str | None = None


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?…])\s+", text.strip())
    return [p for p in parts if p.strip()]


def chunk_plain_text(text: str, *, max_tokens: int = CHUNK_TOKENS, overlap_tokens: int = OVERLAP_TOKENS) -> list[TextChunk]:
    sentences = _split_sentences(text)
    if not sentences:
        return []
    max_chars = max_tokens * CHARS_PER_TOKEN
    overlap_chars = overlap_tokens * CHARS_PER_TOKEN
    chunks: list[TextChunk] = []
    buf: list[str] = []
    size = 0
    idx = 0
    for sent in sentences:
        sent_len = len(sent) + 1
        if buf and size + sent_len > max_chars:
            body = " ".join(buf).strip()
            chunks.append(TextChunk(idx, body, None, estimate_tokens(body)))
            idx += 1
            carry = body[-overlap_chars:] if overlap_chars else ""
            buf = [carry, sent] if carry else [sent]
            size = sum(len(x) + 1 for x in buf)
        else:
            buf.append(sent)
            size += sent_len
    if buf:
        body = " ".join(buf).strip()
        chunks.append(TextChunk(idx, body, None, estimate_tokens(body)))
    return chunks


def _hard_split(text: str, max_chars: int) -> list[str]:
    return [text[i : i + max_chars] for i in range(0, len(text), max_chars)] or [text]


def _prose_units(text: str, max_chars: int) -> list[str]:
    """Абзацы; длинный абзац — по предложениям; сверхдлинное предложение — жёстко по длине."""
    units: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= max_chars:
            units.append(para)
            continue
        for sent in _split_sentences(para):
            units += _hard_split(sent, max_chars) if len(sent) > max_chars else [sent]
    return units


def _context_line(headings: list[str]) -> str:
    return "Раздел: " + " › ".join(headings) if headings else ""


def _table_chunk_texts(block, max_chars: int) -> list[str]:
    """Таблица → фрагменты: целиком, если влезает; иначе окна целых строк, в каждом — раздел, подпись и шапка.
    Широкая таблица — записями «Столбец: значение; …», чтобы строка была понятна без шапки."""
    from app.app.services.table_md import CAPTION_PREFIX, render_header, render_record

    table = block.table
    first = block.text.splitlines()[0] if block.text else ""
    caption = first if first.startswith(CAPTION_PREFIX) else ""
    head = "\n".join(x for x in (_context_line(block.headings), caption) if x)
    header_md = render_header(table)
    rows_md = ["| " + " | ".join(r) + " |" for r in table.rows]
    wide = len(table.header) > WIDE_TABLE_COLS or len(header_md) > max_chars // 3
    if not wide:
        whole = "\n".join(x for x in (head, header_md, *rows_md) if x)
        if len(whole) <= max_chars or not rows_md:
            return [whole]
        lines = rows_md
    else:
        lines = [f"Строка {i}: {render_record(table, r)}" for i, r in enumerate(table.rows, start=1)]
        if not lines:
            return ["\n".join(x for x in (head, header_md) if x)]
    prefix_len = len(head) + (0 if wide else len(header_md)) + 40
    budget = max(max_chars - prefix_len, max_chars // 3)
    windows: list[tuple[int, int]] = []
    start, size = 0, 0
    for i, line in enumerate(lines):
        if i > start and size + len(line) + 1 > budget:
            windows.append((start, i))
            start, size = i, 0
        size += len(line) + 1
    windows.append((start, len(lines)))
    total = len(lines)
    out: list[str] = []
    for a, b in windows:
        span = f"(строки {a + 1}–{b} из {total})"
        parts = [x for x in (head, span) if x]
        if not wide:
            parts.append(header_md)
        parts += lines[a:b]
        out.append("\n".join(parts))
    return out


def chunk_document(text: str, *, max_tokens: int = CHUNK_TOKENS, overlap_tokens: int = OVERLAP_TOKENS) -> list[TextChunk]:
    """Фрагменты документа: проза — абзацами (переносы строк сохраняются, у фрагмента — его раздел), таблицы —
    отдельными фрагментами целыми строками с шапкой (см. ``_table_chunk_texts``). Перекрытие — только у прозы."""
    from app.app.services.table_md import split_blocks

    max_chars = max_tokens * CHARS_PER_TOKEN
    overlap_chars = overlap_tokens * CHARS_PER_TOKEN
    bodies: list[str] = []
    buf: list[tuple[str, list[str]]] = []  # (абзац, раздел)
    size = 0
    carried = 0  # сколько абзацев в начале buf — перекрытие с прошлым фрагментом

    def emit_prose() -> None:
        nonlocal buf, size, carried
        if len(buf) <= carried:  # только перекрытие, нового текста нет
            buf, size, carried = [], 0, 0
            return
        body = "\n".join(u for u, _h in buf)
        first, headings = buf[0]
        ctx = _context_line(headings)
        if ctx and not first.startswith("#"):
            body = f"{ctx}\n{body}"
        bodies.append(body)
        last = buf[-1]
        carry = [last] if len(buf) > 1 and len(last[0]) <= overlap_chars and not last[0].startswith("#") else []
        buf = carry
        size = sum(len(u) + 1 for u, _h in buf)
        carried = len(buf)

    for block in split_blocks(text):
        if block.kind == "table" and block.table is not None:
            # Голый заголовок раздела таблицы уже в её строке «Раздел: …». Абзац «### Заголовок\nтекст» — не голый:
            # раньше он выкидывался вместе с текстом (и все такие разделы подряд до таблицы).
            while len(buf) > carried and buf[-1][0].startswith("#") and "\n" not in buf[-1][0].strip():
                buf.pop()
            emit_prose()
            buf, size, carried = [], 0, 0  # перекрытие через таблицу не переносим
            bodies.extend(_table_chunk_texts(block, max_chars))
            continue
        for unit in _prose_units(block.text, max_chars):
            if buf and size + len(unit) + 1 > max_chars:
                emit_prose()
            buf.append((unit, block.headings))
            size += len(unit) + 1
    emit_prose()
    return [TextChunk(i, b, None, estimate_tokens(b)) for i, b in enumerate(bodies) if b.strip()]


def _turn_pieces(turn, max_chars: int) -> list[str]:
    """Строка канона дословно; слишком длинная реплика режется по предложениям с тем же префиксом «[таймкод] Имя: »."""
    line = turn.line.replace("\n", " ")
    if len(line) <= max_chars:
        return [line]
    prefix = line[: len(line) - len(turn.text)] if turn.text and line.endswith(turn.text) else ""
    pieces: list[str] = []
    buf = ""
    for sent in _split_sentences(turn.text) or [turn.text]:
        if buf and len(prefix) + len(buf) + len(sent) + 1 > max_chars:
            pieces.append(prefix + buf)
            buf = sent
        else:
            buf = f"{buf} {sent}".strip()
    if buf:
        pieces.append(prefix + buf)
    return pieces


def chunk_transcript(text: str, *, max_tokens: int = CHUNK_TOKENS) -> list[TextChunk]:
    """Чанки-окна из подряд идущих реплик канона (``[таймкод] Имя: текст``) с перекрытием в одну реплику.

    Строки чанка — дословно строки ``_final.txt``: таймкод и имя видны LLM, вопрос и ответ «да» попадают
    в одно окно. Экранные блоки (``Экран …``, ``Речь на экране``) идут отдельными окнами, не смешиваясь с речью.
    ``speaker`` — имя, если в окне говорит один человек, иначе ``None``.
    """
    from app.app.services.transcript_turns import parse_final_turns

    turns = parse_final_turns(text)
    if not turns:
        return chunk_plain_text(text, max_tokens=max_tokens, overlap_tokens=0)
    max_chars = max_tokens * CHARS_PER_TOKEN
    chunks: list[TextChunk] = []

    def emit(window: list) -> None:
        body = "\n".join(line for _turn, line in window)
        names = {t.known_speaker for t, _line in window if t.is_speech}
        speakers = {n for n in names if n}
        starts = [t.start_sec for t, _line in window if t.start_sec is not None]
        ends = [t.end_sec for t, _line in window if t.end_sec is not None]
        chunks.append(
            TextChunk(
                len(chunks),
                body,
                next(iter(speakers)) if len(speakers) == 1 and None not in names else None,
                estimate_tokens(body),
                min(starts) if starts else None,
                max(ends) if ends else None,
            )
        )

    for group in (
        [t for t in turns if t.is_speech],
        [t for t in turns if not t.is_speech],
    ):
        window: list = []
        size = 0
        for turn in group:
            for line in _turn_pieces(turn, max_chars):
                if window and size + len(line) + 1 > max_chars:
                    emit(window)
                    last_turn, last_line = window[-1]
                    # Перекрытие: последняя реплика окна повторяется в начале следующего, если она не огромная.
                    fits = len(last_line) + len(line) + 2 <= max_chars
                    carry = [(last_turn, last_line)] if len(window) > 1 and len(last_line) < max_chars // 3 and fits else []
                    window = carry
                    size = sum(len(x) + 1 for _t, x in window)
                window.append((turn, line))
                size += len(line) + 1
        if window:
            emit(window)
    return chunks


MESSAGE_GAP_SEC = 30 * 60  # пауза дольше — новый фрагмент: другой разговор
REPLY_PREVIEW_CHARS = 80


def _message_lines(msg: dict, by_id: dict, max_chars: int) -> list[str]:
    """Строка сообщения в том же виде, что текст переписки для контракта («**ЧЧ:ММ Автор:** текст»); у копии чата без
    времени — только автор. Ответ — номер и начало сообщения, на которое отвечают. Длинное сообщение режется по
    предложениям, префикс повторяется у каждой части."""
    from datetime import datetime

    from app.app.services.chat_export import day_only

    at = datetime.fromisoformat(msg["at"])
    prefix = f"**{msg['author']}:** " if day_only(msg) else f"**{at:%H:%M} {msg['author']}:** "
    target = by_id.get(msg.get("reply_to"))
    if target is not None:
        start = re.sub(r"\s+", " ", target.get("text") or "").strip()
        cut = start[:REPLY_PREVIEW_CHARS] + ("…" if len(start) > REPLY_PREVIEW_CHARS else "")
        prefix += f"(в ответ на M{target['id']}, {target['author']}: «{cut}») "
    text = re.sub(r"\s*\n\s*", " ", msg.get("text") or "").strip()
    if len(prefix) + len(text) <= max_chars:
        return [prefix + text]
    pieces: list[str] = []
    buf = ""
    for sent in _split_sentences(text) or [text]:
        for part in _hard_split(sent, max(1, max_chars - len(prefix))):
            if buf and len(prefix) + len(buf) + len(part) + 1 > max_chars:
                pieces.append(prefix + buf)
                buf = part
            else:
                buf = f"{buf} {part}".strip()
    if buf:
        pieces.append(prefix + buf)
    return pieces


def chunk_messages(export: dict, *, skip_ids: set[int] | None = None, max_tokens: int = CHUNK_TOKENS) -> list[TextChunk]:
    """Фрагменты переписки из разметки сообщений (``chat_export``, ``*_messages.json``): соседние сообщения одного дня,
    пока фрагмент не больше ``max_tokens``; пауза дольше ``MESSAGE_GAP_SEC`` начинает новый фрагмент, если текущий уже
    наполовину полон.

    Первая строка — «Переписка «название» · день»: дата видна у каждого фрагмента. ``unit_first`` / ``unit_last`` —
    номера сообщений «M…» (ссылка открывает сообщение). ``speaker`` — автор, если он во фрагменте один. Сообщения из
    ``skip_ids`` (их источник — более ранняя выгрузка той же переписки, правило Я1) в индекс не идут. Если фрагмент
    обрезан по размеру, последнее сообщение повторяется в начале следующего — вопрос и ответ не теряют друг друга."""
    from datetime import datetime

    from app.app.services.chat_export import RU_MONTHS, day_only

    skip = skip_ids or set()
    all_msgs = export.get("messages") or []
    by_id = {m.get("id"): m for m in all_msgs if m.get("id") is not None}
    msgs = [m for m in all_msgs if m.get("id") is not None and m.get("at") and (m.get("text") or "").strip() and int(m["id"]) not in skip]
    title = re.sub(r"\s+", " ", str(export.get("title") or "")).strip()
    max_chars = max_tokens * CHARS_PER_TOKEN
    chunks: list[TextChunk] = []

    def header(at: datetime) -> str:
        day = f"{at.day} {RU_MONTHS[at.month - 1]} {at.year}"
        return f"Переписка «{title}» · {day}" if title else f"Переписка · {day}"

    def emit(window: list[tuple[dict, str]]) -> None:
        first, last = window[0][0], window[-1][0]
        body = header(datetime.fromisoformat(first["at"])) + "\n" + "\n".join(line for _m, line in window)
        authors = {m["author"] for m, _line in window}
        chunks.append(TextChunk(len(chunks), body, next(iter(authors)) if len(authors) == 1 else None, estimate_tokens(body),
                                unit_first=f"M{first['id']}", unit_last=f"M{last['id']}"))

    head_room = len(header(datetime(2000, 12, 30))) + len(title) // 2 + 2
    room = max(200, max_chars - head_room)
    window: list[tuple[dict, str]] = []
    size = 0
    prev_at: datetime | None = None
    for msg in msgs:
        at = datetime.fromisoformat(msg["at"])
        # Новый день — всегда новый фрагмент (в шапке одна дата). Пауза режет, только если фрагмент уже наполовину полон:
        # иначе редкая переписка распадается на одиночные сообщения без контекста.
        new_talk = prev_at is not None and (
            at.date() != prev_at.date()
            or (not day_only(msg) and (at - prev_at).total_seconds() > MESSAGE_GAP_SEC and size >= room // 2))
        if window and new_talk:
            emit(window)
            window, size = [], 0
        for line in _message_lines(msg, by_id, room):
            if window and size + len(line) + 1 > room:
                emit(window)
                last_msg, last_line = window[-1]
                fits = len(last_line) + len(line) + 2 <= room
                window = [(last_msg, last_line)] if len(window) > 1 and len(last_line) < room // 3 and fits else []
                size = sum(len(x) + 1 for _m, x in window)
            window.append((msg, line))
            size += len(line) + 1
        prev_at = at
    if window:
        emit(window)
    return chunks

