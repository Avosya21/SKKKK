"""Дедлайны проекта: разбор сроков из текста («до пятницы», «на следующей неделе», даты) относительно даты артефакта и сведение задач в единый список (list_project_deadlines)."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.models.artifact import Artifact
from app.app.models.entity import Entity
from app.app.services.recorded_at import filename_date, recorded_local_date

_RU_MONTHS = {
    "январ": 1,
    "феврал": 2,
    "март": 3,
    "апрел": 4,
    "ма": 5,
    "июн": 6,
    "июл": 7,
    "август": 8,
    "сентябр": 9,
    "октябр": 10,
    "ноябр": 11,
    "декабр": 12,
}

_WEEKDAY_INDEX = {
    "понедельник": 0,
    "понедельника": 0,
    "понедельнику": 0,
    "вторник": 1,
    "вторника": 1,
    "вторнику": 1,
    "среда": 2,
    "среды": 2,
    "среду": 2,
    "четверг": 3,
    "четверга": 3,
    "четвергу": 3,
    "пятница": 4,
    "пятницы": 4,
    "пятницу": 4,
    "суббота": 5,
    "субботы": 5,
    "субботу": 5,
    "воскресенье": 6,
    "воскресенья": 6,
    "воскресенью": 6,
}

_NO_DEADLINE_MARKERS = (
    "срок не указан",
    "не указан",
    "неизвест",
    "unknown",
    "—",
    "-",
    "n/a",
    "нет",
)


def _normalize_key(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().casefold())


def _deadline_is_empty(raw: str | None) -> bool:
    if raw is None:
        return True
    text = raw.strip()
    if not text:
        return True
    return _normalize_key(text) in _NO_DEADLINE_MARKERS


def artifact_reference_date(artifact: Artifact) -> date | None:
    """Дата материала: ``recorded_at`` (встреча, имя файла, метаданные), иначе имя файла (19.08) или дата загрузки."""
    recorded = recorded_local_date(artifact)
    if recorded is not None:
        return recorded
    by_name = filename_date(artifact.original_filename, artifact.upload_date)
    if by_name is not None:
        return by_name
    if artifact.upload_date:
        return artifact.upload_date.date()
    return None


def reference_is_explicit(artifact: Artifact) -> bool:
    """True, если опорная дата — дата записи (встреча, имя файла, метаданные); False — только дата загрузки."""
    if recorded_local_date(artifact) is not None:
        return True
    return filename_date(artifact.original_filename, artifact.upload_date) is not None


def _weekday_on_or_after(reference: date, weekday: int) -> date:
    delta = (weekday - reference.weekday()) % 7
    return reference + timedelta(days=delta)


def _next_week_monday(reference: date) -> date:
    delta = (7 - reference.weekday()) % 7
    if delta == 0:
        delta = 7
    return reference + timedelta(days=delta)


def _parse_absolute_date(text: str) -> date | None:
    iso = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", text)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            pass

    dotted = re.search(r"\b(\d{1,2})[./](\d{1,2})[./](20\d{2})\b", text)
    if dotted:
        try:
            return date(int(dotted.group(3)), int(dotted.group(2)), int(dotted.group(1)))
        except ValueError:
            pass

    ru = re.search(
        r"\b(\d{1,2})\s+([а-яё]+)(?:\s+(20\d{2}))?\b",
        text.casefold(),
    )
    if ru:
        day = int(ru.group(1))
        month_key = ru.group(2)
        year = int(ru.group(3)) if ru.group(3) else datetime.now().year
        month = next(
            (
                num
                for prefix, num in sorted(_RU_MONTHS.items(), key=lambda item: -len(item[0]))
                if month_key.startswith(prefix)
            ),
            None,
        )
        if month:
            try:
                return date(year, month, day)
            except ValueError:
                pass
    return None


def parse_deadline_range(
    raw: str | None,
    reference: date | None = None,
) -> tuple[date | None, date | None]:
    """Разбор срока: абсолютная дата или относительно даты созвона/материала."""
    if _deadline_is_empty(raw):
        return None, None
    text = (raw or "").strip()
    lower = text.casefold()

    absolute = _parse_absolute_date(text)
    if absolute:
        return absolute, None

    if reference is None:
        return None, None

    if re.search(r"до\s+конца\s+(?:этой\s+|текущей\s+)?недел", lower):
        days_until_sunday = (6 - reference.weekday()) % 7
        end = reference + timedelta(days=days_until_sunday)
        return end, None

    next_week = bool(re.search(r"следующ", lower) and re.search(r"недел", lower))
    anchor = _next_week_monday(reference) if next_week else reference

    has_thu = bool(re.search(r"четверг", lower))
    has_fri = bool(re.search(r"пятниц", lower))
    if has_thu and has_fri:
        thu = _weekday_on_or_after(anchor, 3)
        fri = thu + timedelta(days=1)
        return thu, fri

    for token, weekday in sorted(_WEEKDAY_INDEX.items(), key=lambda item: -len(item[0])):
        if token in lower:
            return _weekday_on_or_after(anchor, weekday), None

    return None, None


def parse_deadline_date(raw: str | None, reference: date | None = None) -> date | None:
    start, _ = parse_deadline_range(raw, reference=reference)
    return start


def normalize_deadline_at(value: Any) -> str | None:
    """Точный срок от модели: ГГГГ-ММ-ДД или ГГГГ-ММ-ДДTЧЧ:ММ; всё остальное (мусор, «null», год вне 2000–2100) — None."""
    if value is None:
        return None
    text = str(value).strip().replace(" ", "T", 1)
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2})(?::\d{2}(?:\.\d+)?)?)?(?:Z|[+-]\d{2}:?\d{2})?", text)
    if not m:
        return None
    try:
        day = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if not 2000 <= day.year <= 2100:
            return None
        if m.group(4) is None:
            return day.isoformat()
        hour, minute = int(m.group(4)), int(m.group(5))
        if hour > 23 or minute > 59:
            return day.isoformat()
    except ValueError:
        return None
    return f"{day.isoformat()}T{hour:02d}:{minute:02d}"


def resolve_due(
    deadline: str | None, deadline_at: str | None, reference: date | None
) -> tuple[date | None, date | None, str | None]:
    """Срок для календаря: точная дата (и время) от модели, иначе разбор текста срока. → (начало, конец, «ЧЧ:ММ»)."""
    exact = normalize_deadline_at(deadline_at)
    if exact:
        return date.fromisoformat(exact[:10]), None, (exact[11:16] if "T" in exact else None)
    start, end = parse_deadline_range(deadline, reference=reference)
    return start, end, None


def _task_sort_key(item: dict[str, Any]) -> tuple[int, str, str]:
    parsed = item.get("deadline_date")
    if parsed:
        return (0, parsed, item.get("description") or "")
    if not _deadline_is_empty(item.get("deadline")):
        return (1, item.get("deadline") or "", item.get("description") or "")
    return (2, "яяя", item.get("description") or "")


def _merge_deadline_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Склеить задачи одного материала с одним сроком (КП + условия в КП)."""
    from app.app.services.entities import TaskItem, merge_related_tasks

    by_artifact: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        key = item.get("artifact_id") or ""
        by_artifact.setdefault(key, []).append(item)

    merged: list[dict[str, Any]] = []
    for group in by_artifact.values():
        if len(group) < 2:
            merged.extend(group)
            continue
        tasks = [
            TaskItem(
                description=str(g.get("description") or ""),
                assignee=g.get("assignee"),
                deadline=g.get("deadline"),
                deadline_at=g.get("deadline_at"),
                status=str(g.get("status") or "unknown"),
            )
            for g in group
        ]
        merged_tasks = merge_related_tasks(tasks)
        if len(merged_tasks) == len(group):
            merged.extend(group)
            continue
        used: set[int] = set()
        for mt in merged_tasks:
            match_idx = None
            mt_desc = mt.description.casefold()
            for i, g in enumerate(group):
                if i in used:
                    continue
                g_desc = str(g.get("description") or "").casefold()
                if g_desc in mt_desc or mt_desc in g_desc or g_desc[:40] in mt_desc:
                    match_idx = i
                    break
            if match_idx is None:
                for i, g in enumerate(group):
                    if i not in used:
                        match_idx = i
                        break
            base = dict(group[match_idx]) if match_idx is not None else {}
            if match_idx is not None:
                used.add(match_idx)
            base["description"] = mt.description
            base["assignee"] = mt.assignee
            base["deadline"] = mt.deadline
            base["deadline_at"] = mt.deadline_at
            base["status"] = mt.status
            ref = base.get("reference_date")
            start, end, due_time = resolve_due(mt.deadline, mt.deadline_at, date.fromisoformat(ref) if ref else None)
            base["deadline_date"] = start.isoformat() if start else None
            base["deadline_end_date"] = end.isoformat() if end else None
            base["deadline_time"] = due_time
            merged.append(base)
    return merged


def _human_assignee(name: str | None) -> str | None:
    """Метка неназванного голоса (speaker_4) — «Спикер 4»; имя, если спикера уже назвали, приходит из привязки."""
    m = re.fullmatch(r"speaker_(\d+)", (name or "").strip(), re.I)
    return f"Спикер {int(m.group(1))}" if m else name


def list_project_deadlines(db: Session, project_id: int) -> dict[str, Any]:
    rows = db.execute(
        select(Entity, Artifact)
        .join(Artifact, Artifact.id == Entity.artifact_id)
        .where(
            Artifact.project_id == project_id,
            Entity.is_active.is_(True),
            Entity.entity_type == "task",
        )
        .order_by(Entity.id.asc())
    ).all()

    seen: set[tuple[str, str, str]] = set()
    items: list[dict[str, Any]] = []

    for ent, art in rows:
        try:
            value = json.loads(ent.value_json)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        from app.app.services.task_board import effective, hidden

        if hidden(value):  # заменённая или склеенная задача (вердикт актуальности) — не срок
            continue
        value = effective(value)  # правка менеджера (статус, ответственный, срок) поверх извлечённого

        description = str(value.get("description") or "").strip()
        if not description:
            continue

        assignee = value.get("assignee")
        assignee_text = str(assignee).strip() if assignee else None
        deadline_raw = value.get("deadline")
        deadline_at = normalize_deadline_at(value.get("deadline_at"))
        deadline_text = None if _deadline_is_empty(str(deadline_raw) if deadline_raw is not None else None) else str(deadline_raw).strip()
        status = str(value.get("status") or "unknown").strip() or "unknown"

        if not deadline_text:
            continue
        from app.app.services.entities import is_informational_schedule

        if is_informational_schedule(deadline_text):
            continue

        dedupe_key = (_normalize_key(description), _normalize_key(assignee_text or ""), _normalize_key(deadline_text))
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        ref = artifact_reference_date(art)
        start, end, due_time = resolve_due(deadline_text, deadline_at, ref)
        # Относительный срок («до пятницы»), посчитанный от даты загрузки, — оценка, а не факт.
        approximate = (
            bool(start)
            and deadline_at is None
            and _parse_absolute_date(deadline_text) is None
            and not reference_is_explicit(art)
        )
        items.append(
            {
                "id": ent.id,
                "description": description,
                "assignee": _human_assignee(assignee_text),
                "deadline": deadline_text,
                "deadline_at": deadline_at,
                "deadline_time": due_time,
                "deadline_date": start.isoformat() if start else None,
                "deadline_end_date": end.isoformat() if end else None,
                "reference_date": ref.isoformat() if ref else None,
                "date_approximate": approximate,
                "status": status,
                "artifact_id": art.artifact_id,
                "filename": art.original_filename,
                "transcript_html_url": art.transcript_html_url if art.status == "done" else None,
            }
        )

    items = _merge_deadline_items(items)
    from app.app.services.task_board import _manual_rows

    items += [row for row in _manual_rows(db, project_id) if row.get("deadline")]  # ручные задачи со сроком (Ш4)

    seen: set[tuple[str, str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for item in items:
        dedupe_key = (
            _normalize_key(item.get("description") or ""),
            _normalize_key(item.get("assignee") or ""),
            _normalize_key(item.get("deadline") or ""),
        )
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        deduped.append(item)
    items = deduped

    items.sort(key=_task_sort_key)

    with_date = sum(1 for i in items if i.get("deadline_date"))
    without_date = len(items) - with_date

    return {
        "project_id": project_id,
        "total": len(items),
        "with_parsed_date": with_date,
        "with_text_only_deadline": without_date,
        "without_deadline": 0,
        "items": items,
    }
