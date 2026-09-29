"""Выгрузка переписки Telegram Desktop (``messages.html``) → сообщения с номером, автором, временем и ответом
(docs/plans/SOURCE-ANCHORS-PLAN.md, этап 1).

Даты и авторы берутся из разметки выгрузки, а не от модели: у каждого сообщения ``id="message106"``, время
``title="25.08.2026 15:58:22 UTC+03:00"``, автор ``from_name`` (у продолжения ``joined`` — автор предыдущего), ответ
``GoToMessage(105)``. Служебные сообщения (создание группы, смена даты) пропускаются. Разбор — стандартным
``html.parser`` (без bs4). Результат: ``parsed/documents/<artifact_id>_messages.json`` и чистый текст для индекса в виде,
который понимает ``contract_text.parse_chat`` («**25 августа 2026**», «**15:58 Имя:** текст»).

Я1: одно сообщение пересекающихся выгрузок одной переписки (одно название группы) — источник только в самой ранней
загруженной выгрузке; в поздней оно только контекст (``owned_elsewhere``).

Копия чата Битрикс24 (текст из окна мессенджера: «[Имя Фамилия]» — начало сообщения, «{Цитата} Автор … {Конец цитаты}» —
ответ на сообщение) разбирается в тот же формат: номер — порядок в копии, «в ответ на» — по автору и началу цитаты.
Времени в копии нет: ``at`` — полночь дня материала, ``at_precision="day"`` — время не выдумывается (А9).
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

RU_MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря")
_TITLE_TIME = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}):(\d{2}):(\d{2})(?:\s+UTC([+-])(\d{2}):(\d{2}))?")
_MSG_ID = re.compile(r"^message(-?\d+)$")
_REPLY = re.compile(r"go_to_message(-?\d+)|GoToMessage\((-?\d+)\)")


def looks_like_telegram_export(html: str) -> bool:
    return 'class="message default' in html and "pull_right date details" in html


def _parse_time(title: str) -> datetime | None:
    m = _TITLE_TIME.search(title or "")
    if not m:
        return None
    day, month, year, hh, mm, ss = (int(m.group(i)) for i in range(1, 7))
    tz = timezone.utc
    if m.group(7):
        delta = timedelta(hours=int(m.group(8)), minutes=int(m.group(9)))
        tz = timezone(delta if m.group(7) == "+" else -delta)
    try:
        return datetime(year, month, day, hh, mm, ss, tzinfo=tz)
    except ValueError:
        return None


class _Parser(HTMLParser):
    """Состояние: стек div'ов с классами; текущее сообщение; поле, в которое сейчас пишется текст."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, set[str]]] = []
        self.title_parts: list[str] = []
        self.messages: list[dict[str, Any]] = []
        self.current: dict[str, Any] | None = None
        self.msg_depth = -1
        self.field: str | None = None
        self.field_depth = -1
        self.last_author: str | None = None

    def _classes(self, attrs: list[tuple[str, str | None]]) -> set[str]:
        return set((dict(attrs).get("class") or "").split())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        classes = self._classes(attrs)
        if tag == "br":
            self._write("\n")
            return
        if tag in ("img", "meta", "link", "input", "hr"):
            return
        self.stack.append((tag, classes))
        depth = len(self.stack)
        if tag == "div" and "message" in classes and "default" in classes:
            m = _MSG_ID.match(a.get("id") or "")
            self.current = {"id": int(m.group(1)) if m else None, "joined": "joined" in classes, "author": None, "at": None,
                            "text": [], "reply_to": None, "forwarded_from": None, "attachments": []}
            self.msg_depth = depth
            return
        if self.current is None:
            if tag == "div" and "text" in classes and "bold" in classes and self._inside("page_header"):
                self.field, self.field_depth = "title", depth
            return
        if tag == "div" and {"date", "details"} <= classes and self.current["at"] is None and not self._inside("forwarded"):
            self.current["at"] = _parse_time(a.get("title") or "")
        elif tag == "div" and "from_name" in classes:
            self.field, self.field_depth = ("forwarded_from" if self._inside("forwarded") else "author"), depth
        elif tag == "div" and "text" in classes and "bold" not in classes:
            self.field, self.field_depth = "text", depth
        elif tag == "div" and "title" in classes and "bold" in classes and self._inside("media_wrap"):
            self.field, self.field_depth = "attachment", depth
        elif tag == "a":
            href = a.get("href") or ""
            onclick = a.get("onclick") or ""
            reply = _REPLY.search(href + " " + onclick)
            if reply and self._inside("reply_to"):
                self.current["reply_to"] = int(reply.group(1) or reply.group(2))
            elif "photo_wrap" in classes:
                self.current["attachments"].append("[фото]")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("br", "img", "meta", "link", "input", "hr") or not self.stack:
            return
        depth = len(self.stack)
        if self.field is not None and depth == self.field_depth:
            self.field, self.field_depth = None, -1
        if self.current is not None and depth == self.msg_depth:
            self._finish()
        self.stack.pop()

    def handle_data(self, data: str) -> None:
        self._write(data)

    def _inside(self, cls: str) -> bool:
        return any(cls in classes for _tag, classes in self.stack)

    def _write(self, data: str) -> None:
        if self.field == "title":
            self.title_parts.append(data)
            return
        if self.current is None or self.field is None:
            return
        if self.field == "text":
            self.current["text"].append(data)
        elif self.field in ("author", "forwarded_from"):
            self.current[self.field] = ((self.current.get(self.field) or "") + data)
        elif self.field == "attachment":
            self.current.setdefault("_att", []).append(data)

    def _finish(self) -> None:
        msg = self.current or {}
        self.current, self.msg_depth = None, -1
        author = re.sub(r"\s+", " ", msg.get("author") or "").strip() or None
        if author is None and msg.get("joined"):
            author = self.last_author
        if author:
            self.last_author = author
        text = "".join(msg.get("text") or [])
        text = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")).strip()
        attachments = list(msg.get("attachments") or [])
        name = re.sub(r"\s+", " ", "".join(msg.get("_att") or [])).strip()
        if name:
            attachments.append(f"[файл: {name}]")
        body = "\n".join(part for part in (" ".join(attachments), text) if part)
        forwarded = re.sub(r"\s+", " ", msg.get("forwarded_from") or "").strip() or None
        if forwarded:
            body = f"[переслано от {forwarded}] {body}".strip()
        if msg.get("id") is None or msg.get("at") is None or not body:
            return
        self.messages.append({"id": msg["id"], "at": msg["at"].isoformat(), "author": author or "неизвестный автор",
                              "text": body, "reply_to": msg.get("reply_to")})


def parse_telegram_html(html: str) -> dict[str, Any]:
    """{title, messages: [{id, at (ISO с поясом), author, text, reply_to}]} в порядке выгрузки."""
    parser = _Parser()
    parser.feed(html)
    parser.close()
    title = re.sub(r"\s+", " ", "".join(parser.title_parts)).strip()
    return {"title": title, "messages": parser.messages}


def _local(at: str, tz: timezone | None) -> datetime:
    moment = datetime.fromisoformat(at)
    return moment.astimezone(tz) if tz is not None else moment  # None — время как в выгрузке (пояс автора выгрузки)


def render_chat_text(export: dict[str, Any], tz: timezone | None = None) -> str:
    """Чистый текст для индекса и контракта: заголовки дней и «**ЧЧ:ММ Автор:** текст» (понимает ``parse_chat``)."""
    lines: list[str] = []
    day = None
    for msg in export.get("messages") or []:
        at = _local(msg["at"], tz)
        if at.date() != day:
            day = at.date()
            lines += ["", f"**{at.day} {RU_MONTHS[at.month - 1]} {at.year}**", ""]
        lines.append(f"**{at:%H:%M} {msg['author']}:** {msg['text']}")
    return "\n".join(lines).strip() + "\n"


def messages_path(parsed: Path, artifact_id: str) -> Path:
    return parsed / f"{artifact_id}_messages.json"


def save_export(path: Path, export: dict[str, Any], *, artifact_id: str, uploaded_at: datetime | None) -> dict[str, Any]:
    data = {"title": export.get("title") or "", "artifact_id": artifact_id,
            "uploaded_at": uploaded_at.isoformat() if uploaded_at else None, "messages": export.get("messages") or []}
    if export.get("format"):
        data["format"] = export["format"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


def load_export(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("messages"), list) else None


def owned_elsewhere(parsed: Path, export: dict[str, Any]) -> set[int]:
    """Я1: номера сообщений этой переписки, которые уже есть в более ранней загруженной выгрузке того же проекта."""
    title = export.get("title") or ""
    mine = (export.get("uploaded_at") or "", export.get("artifact_id") or "")
    out: set[int] = set()
    if not title:
        return out
    for path in parsed.glob("*_messages.json"):
        other = load_export(path)
        if not other or other.get("artifact_id") == export.get("artifact_id") or other.get("title") != title:
            continue
        if (other.get("uploaded_at") or "", other.get("artifact_id") or "") < mine:
            out |= {int(m["id"]) for m in other["messages"] if m.get("id") is not None}
    return out


def artifact_messages_path(settings: Any, artifact: Any) -> Path:
    from app.app.services.storage import parsed_dir

    return messages_path(parsed_dir(settings, artifact.project_id, artifact.source_type), artifact.artifact_id)


def load_artifact_export(settings: Any, artifact: Any) -> dict[str, Any] | None:
    return load_export(artifact_messages_path(settings, artifact))


def ingest_telegram_export(settings: Any, artifact: Any, html: str) -> str | None:
    """Если HTML — выгрузка Telegram: сохранить сообщения, дату материала (первое сообщение) и вернуть чистый текст.
    Иначе None — документ разбирается как обычно."""
    if not looks_like_telegram_export(html):
        return None
    export = parse_telegram_html(html)
    if not export["messages"]:
        return None
    save_export(artifact_messages_path(settings, artifact), export, artifact_id=artifact.artifact_id, uploaded_at=artifact.upload_date)
    first, _last = period(export)
    if first is not None:
        from app.app.services.recorded_at import SOURCE_MESSAGES

        artifact.recorded_at = first.astimezone(timezone.utc).replace(tzinfo=None)
        artifact.recorded_at_source = SOURCE_MESSAGES
    return render_chat_text(export)


def period(export: dict[str, Any]) -> tuple[datetime | None, datetime | None]:
    stamps = [datetime.fromisoformat(m["at"]) for m in export.get("messages") or [] if m.get("at")]
    return (min(stamps), max(stamps)) if stamps else (None, None)


# ---------------------------------------------------------------- копия чата Битрикс24

AT_PRECISION_DAY = "day"  # у сообщения известен только день (копия чата без времени)
BITRIX_COPY = "bitrix_copy"
BX_QUOTE_START = "{Цитата}"
BX_QUOTE_END = "{Конец цитаты}"
_BX_AUTHOR = re.compile(r"^\[([A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё .'\-]{1,60})\]$")
BX_MIN_HEADERS = 3  # «[Имя]» не реже трёх раз и хотя бы два автора — иначе это обычная заметка со скобками


def day_only(msg: dict[str, Any]) -> bool:
    """У сообщения известен только день: время не показывать и не сравнивать."""
    return msg.get("at_precision") == AT_PRECISION_DAY


def looks_like_bitrix_copy(text: str) -> bool:
    """Копия чата Битрикс24 из окна мессенджера: текст начинается строкой «[Имя Фамилия]», авторов не меньше двух."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines or _BX_AUTHOR.match(lines[0]) is None:
        return False
    authors = [m.group(1).strip() for m in (_BX_AUTHOR.match(line) for line in lines) if m]
    return len(authors) >= BX_MIN_HEADERS and len(set(authors)) >= 2


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _quoted_message(messages: list[dict[str, Any]], quote: list[str]) -> int | None:
    """Номер цитируемого сообщения: первая строка цитаты — автор, дальше — начало его текста."""
    lines = [line for line in quote if line]
    if not lines:
        return None
    who, probe = _squash(lines[0]), _squash(" ".join(lines[1:]))[:60]
    for msg in reversed(messages):
        if _squash(msg["author"]) == who and (not probe or probe in _squash(msg["text"])):
            return msg["id"]
    if probe:  # автор в цитате подписан иначе — по началу текста
        for msg in reversed(messages):
            if probe[:30] in _squash(msg["text"]):
                return msg["id"]
    return None


def parse_bitrix_copy(text: str, day: date) -> dict[str, Any]:
    """{title, format, messages: [{id, at, at_precision, author, text, reply_to}]} в порядке копии, номера с 1.

    «{Цитата} … {Конец цитаты}» начинает новое сообщение того же автора — ответ на процитированное; текст цитаты в ответ
    не входит (он уже есть в исходном сообщении)."""
    at = datetime(day.year, day.month, day.day, tzinfo=_local_tz()).isoformat()
    messages: list[dict[str, Any]] = []
    state: dict[str, Any] = {"author": None, "body": [], "reply_to": None, "quote": None}

    def flush() -> None:
        body = re.sub(r"\n{3,}", "\n\n", "\n".join(state["body"])).strip()
        if state["author"] and body:
            messages.append({"id": len(messages) + 1, "at": at, "at_precision": AT_PRECISION_DAY, "author": state["author"],
                             "text": body, "reply_to": state["reply_to"]})
        state["body"], state["reply_to"] = [], None

    for raw in (text or "").splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if state["quote"] is not None:
            if stripped == BX_QUOTE_END:
                state["reply_to"], state["quote"] = _quoted_message(messages, state["quote"]), None
            else:
                state["quote"].append(stripped)
            continue
        head = _BX_AUTHOR.match(stripped)
        if head:
            flush()
            state["author"] = head.group(1).strip()
        elif stripped == BX_QUOTE_START:
            flush()
            state["quote"] = []
        else:
            state["body"].append(line)
    if state["quote"]:  # цитата без конца — её строки остаются текстом, а не теряются
        state["body"] += state["quote"]
    flush()
    return {"title": "", "format": BITRIX_COPY, "messages": messages}


def _local_tz() -> timezone:
    from app.app.services.recorded_at import LOCAL_TZ

    return LOCAL_TZ


def render_bitrix_text(export: dict[str, Any]) -> str:
    """Чистый текст копии для индекса: «[Автор]» или «[Автор] (в ответ на Автор2: «начало…»)», затем текст сообщения.
    Цитата целиком не повторяется — иначе фрагменты поиска забиты копиями чужих сообщений."""
    by_id = {m["id"]: m for m in export.get("messages") or []}
    blocks: list[str] = []
    for msg in export.get("messages") or []:
        head = f"[{msg['author']}]"
        target = by_id.get(msg.get("reply_to"))
        if target is not None:
            start = re.sub(r"\s+", " ", target["text"]).strip()
            head += f" (в ответ на {target['author']}: «{start[:80]}{'…' if len(start) > 80 else ''}»)"
        blocks.append(f"{head}\n{msg['text']}")
    return "\n\n".join(blocks).strip() + "\n"


def ingest_bitrix_copy(settings: Any, artifact: Any, text: str) -> str | None:
    """Если текст — копия чата Битрикс24: сохранить сообщения (день — дата материала) и вернуть чистый текст.
    Иначе None — текст разбирается как обычно."""
    if not looks_like_bitrix_copy(text):
        return None
    from app.app.services.recorded_at import material_date

    day = material_date(artifact)[0] or datetime.now(_local_tz()).date()
    export = parse_bitrix_copy(text, day)
    if len(export["messages"]) < 2:
        return None
    save_export(artifact_messages_path(settings, artifact), export, artifact_id=artifact.artifact_id, uploaded_at=artifact.upload_date)
    return render_bitrix_text(export)
