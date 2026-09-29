"""Разбор текста для контракта обмена с агентом анализа: сообщения переписки, дословные цитаты, оценки часов, якоря кода.

Только чистые функции без БД. Главное правило контракта: цитата — ДОСЛОВНЫЙ фрагмент источника (срез строки),
никогда не пересказ; если подходящего фрагмента нет, доказательство не выдумывается.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
MSG_RE = re.compile(r"^\*\*(\d{1,2}):(\d{2})\s+(.+?):\*\*\s*(.*)$")
DATE_RE = re.compile(r"^\*\*(\d{1,2})\s+([а-яё]+)\s+(\d{4})\*\*\s*$", re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s<>()\"'«»]+")
QUOTE_MAX = 500


@dataclass
class Message:
    idx: int
    day: date | None
    hh: int
    mm: int
    speaker: str
    text: str  # дословное тело сообщения (без заголовка)
    msg_id: int | None = None  # номер сообщения в выгрузке Telegram (SOURCE-ANCHORS-PLAN); у разбора текста — нет
    day_only: bool = False  # копия чата без времени: известен только день, момента нет

    def at(self) -> datetime | None:
        if self.day is None or self.day_only:
            return None
        return datetime(self.day.year, self.day.month, self.day.day, self.hh, self.mm)


def messages_from_export(export: dict, tz) -> list[Message]:
    """Сообщения из разобранной выгрузки (``chat_export``): номер, автор, время в поясе проекта — без угадывания по тексту.
    У копии чата без времени (``at_precision="day"``) — только день, без перевода пояса."""
    out: list[Message] = []
    for msg in export.get("messages") or []:
        if msg.get("at_precision") == "day":
            day = date.fromisoformat(str(msg["at"])[:10])
            out.append(Message(len(out), day, 0, 0, msg["author"], msg["text"], msg.get("id"), day_only=True))
            continue
        at = datetime.fromisoformat(msg["at"]).astimezone(tz)
        out.append(Message(len(out), at.date(), at.hour, at.minute, msg["author"], msg["text"], msg.get("id")))
    return out


def parse_chat(text: str) -> list[Message]:
    """Сообщения из нормализованной переписки: «**ДД месяц ГГГГ**» — дата, «**ЧЧ:ММ Имя:**» — начало сообщения."""
    messages: list[Message] = []
    day: date | None = None
    current: dict | None = None
    body: list[str] = []

    def flush() -> None:
        if current is not None:
            messages.append(Message(len(messages), current["day"], current["hh"], current["mm"], current["speaker"], "\n".join(body).strip()))

    for line in text.splitlines():
        stripped = line.strip()
        dm = DATE_RE.match(stripped)
        if dm and dm.group(2).lower() in MONTHS:
            flush()
            current, body = None, []
            day = date(int(dm.group(3)), MONTHS[dm.group(2).lower()], int(dm.group(1)))
            continue
        mm = MSG_RE.match(stripped)
        if mm:
            flush()
            current = {"day": day, "hh": int(mm.group(1)), "mm": int(mm.group(2)), "speaker": mm.group(3).strip()}
            body = [mm.group(4)] if mm.group(4) else []
            continue
        if current is not None:
            body.append(line.rstrip())
    flush()
    return messages


def looks_like_chat(text: str) -> bool:
    return sum(1 for line in text.splitlines() if MSG_RE.match(line.strip())) >= 5


_WORD = re.compile(r"[а-яёa-z0-9]{4,}", re.IGNORECASE)


def stems(text: str) -> set[str]:
    """Грубые основы слов (первые 5 символов): достаточно, чтобы связать формулировку сущности с репликой."""
    return {w.lower()[:5] for w in _WORD.findall(text or "")}


def coverage(needle: set[str], hay: set[str]) -> float:
    """Доля основ needle, найденных в hay."""
    return len(needle & hay) / len(needle) if needle else 0.0


def split_sentences(text: str) -> list[tuple[int, int]]:
    """Границы предложений как срезы исходной строки (для дословной цитаты)."""
    spans: list[tuple[int, int]] = []
    start = 0
    for m in re.finditer(r"(?<=[.!?…])\s+|\n+", text):
        if m.start() > start:
            spans.append((start, m.start()))
        start = m.end()
    if start < len(text):
        spans.append((start, len(text)))
    return [(a, b) for a, b in spans if text[a:b].strip()]


def best_quote(statement: str, text: str, *, limit: int = QUOTE_MAX) -> tuple[str, float] | None:
    """Дословный фрагмент text (1–3 соседних предложения, до `limit` символов), лучше всего покрывающий statement.
    Возвращает (цитата, покрытие) или None. Цитата — срез text без правок."""
    need = stems(statement)
    spans = split_sentences(text)
    if not need or not spans:
        return None
    best: tuple[float, int, int] | None = None
    for i in range(len(spans)):
        for j in range(i, min(i + 3, len(spans))):
            a, b = spans[i][0], spans[j][1]
            if b - a > limit:
                break
            score = coverage(need, stems(text[a:b])) - 0.02 * (j - i)  # при равенстве — короче
            if best is None or score > best[0]:
                best = (score, a, b)
    if best is None:
        return None
    _score, a, b = best
    quote = text[a:b].strip()
    return quote, round(coverage(need, stems(quote)), 3)


HOURS_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:[-–—]\s*(\d+(?:[.,]\d+)?)\s*)?(?:часов|часа|час|ч\b)", re.IGNORECASE)


def find_hours(text: str) -> tuple[float, float] | None:
    m = HOURS_RE.search(text or "")
    if not m:
        return None
    lo = float(m.group(1).replace(",", "."))
    hi = float((m.group(2) or m.group(1)).replace(",", "."))
    return (lo, hi) if lo <= hi else (hi, lo)


# Условные формулировки: согласие «с оговоркой» согласием не считаем (нужна отдельная оценка/решение)
CONDITIONAL_RE = re.compile(r"по дооценке|не входит|вне\s+(?:текущего\s+)?тз|предварительно|подумаю|нужно подумать|возможно|если все устроит|если всё устроит|под вопросом", re.IGNORECASE)


def requirement_status(entity_type: str, task_status: str, evidence: list[dict]) -> tuple[str, str]:
    """Статус требования и его основание. agreed — только если в источнике есть реплика ЗАКАЗЧИКА без оговорок;
    «решение», найденное моделью в репликах исполнителя или без известной стороны, — лишь proposed (иначе agreed значило бы «модель назвала это решением»)."""
    if task_status == "cancelled":
        return "rejected", "task_cancelled"
    if entity_type != "decision":
        return "proposed", "task_not_confirmed"
    customer = [e for e in evidence if e.get("side") == "customer"]
    if not customer:
        return "proposed", "decision_side_unknown" if not evidence or all(e.get("side") == "unknown" for e in evidence) else "decision_by_vendor_only"
    if any(CONDITIONAL_RE.search(e.get("quote", "")) for e in customer):
        return "proposed", "customer_conditional"
    return "agreed", "customer_evidence"


SCOPE_RE = re.compile(
    r"в\s+текущ\w+\s+ТЗ|вне\s+(?:текущ\w+\s+|нашего\s+)?ТЗ|не\s+входит\s+в\s+(?:наше\s+|текущ\w+\s+|данн\w+\s+)?ТЗ|"
    r"по\s+дооценк|дополнительн\w+\s+оценк|доп\.?\s*оценк",
    re.IGNORECASE,
)


def find_urls(text: str) -> list[str]:
    seen: list[str] = []
    for u in URL_RE.findall(text or ""):
        u = u.rstrip(".,;:!?)")
        if u not in seen:
            seen.append(u)
    return seen


# Якоря кода: слова заказчика → гипотезы объектов 1С / Битрикс24. Это правила, а не знание о конфигурации:
# агент анализа проверяет их по коду и возвращает уточнения (`anchor_corrections`).
ANCHOR_RULES: list[tuple[re.Pattern, str, str, str]] = [
    (re.compile(r"номенклатур", re.I), "1c", "Справочник.Номенклатура", "номенклатура"),
    (re.compile(r"характеристик", re.I), "1c", "Справочник.ХарактеристикиНоменклатуры", "характеристики номенклатуры"),
    (re.compile(r"заказ\w*\s+клиент|заказ\w*\s+покупател", re.I), "1c", "Документ.ЗаказКлиента", "заказ клиента"),
    (re.compile(r"реализаци", re.I), "1c", "Документ.РеализацияТоваровУслуг", "реализация"),
    (re.compile(r"печатн\w+\s+форм|печатк", re.I), "1c", "печатные формы документов", "печатные формы"),
    (re.compile(r"контрагент", re.I), "1c", "Справочник.Контрагенты", "контрагенты"),
    (re.compile(r"\bсчет|\bсчёт|счета\s+на\s+оплат", re.I), "1c", "Документ.СчетНаОплатуПокупателю", "счёт на оплату"),
    (re.compile(r"спецификаци", re.I), "1c", "печатная форма «Спецификация»", "спецификация"),
    (re.compile(r"паспорт\w*\s+качеств", re.I), "1c", "печатная форма «Паспорт качества»", "паспорт качества"),
    (re.compile(r"прайс|цен[аыу]\b", re.I), "1c", "Документ.УстановкаЦенНоменклатуры", "цены"),
    (re.compile(r"коннектор|обмен", re.I), "1c", "Обработка/расширение «Коннектор к 1С»", "обмен 1С — Битрикс24"),
    (re.compile(r"б24|bitrix|битрикс", re.I), "b24", "catalog.product / crm.deal", "Битрикс24"),
    (re.compile(r"сделк", re.I), "b24", "crm.deal", "сделка"),
    (re.compile(r"каталог", re.I), "b24", "catalog.product", "каталог товаров"),
]


def anchors_for(text: str, rules: list | None = None) -> list[dict]:
    """rules — правила выбранного пакета якорей проекта (agent_settings.load_pack_rules); None — пакет 1С + Битрикс24."""
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for rx, system, obj, term in (ANCHOR_RULES if rules is None else rules):
        if rx.search(text or "") and (system, obj) not in seen:
            seen.add((system, obj))
            out.append({"system": system, "object": obj, "hint": term, "basis": "keyword_rule"})
    return out


REQ_VERB_RE = re.compile(
    r"добав|созда|реализ|доработ|вывест|вывод|настро|обеспеч|запрет|передав|передач|выгруж|загруж|формир|использ|"
    r"нужн|необходим|должн|будет|переносим|сократ|нормализ|заполн|отображ|подтягив|блокир|привяз|синхронизир",
    re.IGNORECASE,
)
COMMERCIAL_RE = re.compile(r"руб|ставк|скидк|оплат|ндс|смет|договор|акт\b|счёт|счет\b|стоимост|предоплат", re.IGNORECASE)
SIDE_CUSTOMER_RE = re.compile(r"клиент|заказчик|руковод|директор|бухгалтер", re.IGNORECASE)
SIDE_VENDOR_RE = re.compile(r"консультант|интегратор|разработчик|аналитик|подрядчик|исполнител|менеджер проекта", re.IGNORECASE)


def side_from_role(role: str | None, hints: dict[str, list[str]] | None = None) -> str | None:
    """Сторона по роли. hints — слова из настроек проекта (названия компаний): {"customer": [...], "vendor": [...]}."""
    if not role:
        return None
    low = role.lower()
    hints = hints or {}
    if SIDE_VENDOR_RE.search(role) or any(w in low for w in hints.get("vendor", [])):
        return "vendor"
    if SIDE_CUSTOMER_RE.search(role) or any(w in low for w in hints.get("customer", [])):
        return "customer"
    return None


def norm_key(text: str) -> str:
    return re.sub(r"[^0-9a-zа-яё]+", " ", (text or "").lower()).strip()
