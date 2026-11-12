"""Задача и проект Jira как документы портала: всё, что есть в Jira, без сокращений (docs/plans/JIRA-INTEGRATION-PLAN.md, этап 4).

``issue_document`` — задача целиком: поля (включая пользовательские, по их названиям в Jira), описание, все комментарии,
трудозатраты, подзадачи, связи, внешние ссылки, вложения, история изменений. ``project_document`` — сводка проекта:
сведения о проекте и таблица всех задач — по ней помощник отвечает на обзорные вопросы («что в работе», «что у кого»).
Разметка Jira (``[^файл]``, ``!картинка!``, ``{code}``, ``[текст|ссылка]``) переводится в обычный текст. Функции чистые:
сети и базы нет. Изменился вид документа — увеличить ``FORMAT_VERSION``: при следующей проверке задачи перезагрузятся.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

from app.app.services.jira_client import normalize_issue

FORMAT_VERSION = 9

STATUS_LABELS = {"open": "не начата", "in_progress": "в работе", "done": "завершена"}
# Поля, которые выводятся в шапке и разделах сами; остальные непустые идут в «Дополнительные поля».
_HANDLED = {"summary", "issuetype", "status", "priority", "resolution", "resolutiondate", "assignee", "reporter", "creator", "created",
            "updated", "duedate", "labels", "components", "fixVersions", "versions", "parent", "subtasks", "issuelinks", "environment",
            "timetracking", "description", "comment", "worklog", "attachment", "project", "watches", "votes", "security"}
# Служебные поля Jira без смысла для человека: счётчики секунд (их заменяет «Учёт времени»), проценты, отметки просмотра.
_SKIPPED = {"timeoriginalestimate", "timeestimate", "timespent", "aggregatetimeoriginalestimate", "aggregatetimeestimate",
            "aggregatetimespent", "progress", "aggregateprogress", "workratio", "lastViewed", "thumbnail", "issuekey",
            "statuscategorychangedate", "archiveddate", "archivedby"}
# Счётчики секунд, значение которых в документе есть словами («Оценка/Осталось/Затрачено времени», в т.ч. с подзадачами).
_TIME_COUNTERS = {"timeoriginalestimate", "timeestimate", "timespent", "aggregatetimeoriginalestimate", "aggregatetimeestimate",
                  "aggregatetimespent"}
# Названия системных полей в истории изменений Jira отдаёт по-английски — в документе пишем по-русски (исходное имя в скобках).
_HISTORY_LABELS = {"status": "Статус", "assignee": "Исполнитель", "reporter": "Автор", "priority": "Приоритет", "resolution": "Решение",
                   "summary": "Название", "duedate": "Срок", "labels": "Метки", "attachment": "Вложение", "link": "Связь",
                   "issuetype": "Вид задачи", "fix version": "Версия исправления", "version": "Затронутая версия",
                   "component": "Компонент", "parent": "Родительская задача", "sprint": "Спринт", "project": "Проект", "key": "Ключ",
                   "timespent": "Затрачено времени", "timeestimate": "Осталось времени", "timeoriginalestimate": "Оценка времени",
                   "worklogtimespent": "Время в записи трудозатрат", "worklogid": "Запись трудозатрат", "description": "Описание",
                   "comment": "Комментарий", "environment": "Окружение", "security": "Уровень безопасности", "workflow": "Процесс"}
_COURSE_FIELDS = {"status": "статус", "assignee": "исполнитель", "resolution": "решение", "priority": "приоритет", "duedate": "срок",
                  "summary": "название", "sprint": "спринт", "fix version": "версия исправления", "issuetype": "вид задачи"}
_HISTORY_SECONDS = {"timespent", "timeestimate", "timeoriginalestimate", "worklogtimespent"}
_NOISE_NAMES = {"rank", "ранг", "development", "разработка"}
_LONG_TEXT_FIELDS = {"description", "comment", "environment", "описание", "комментарий", "окружение"}
_SPRINT = re.compile(r"Sprint@[0-9a-f]+\[(.*)\]\s*$", re.S)


def clean_text(text: Any) -> str:
    """Текст из Jira: единые переводы строк и разметка Jira, переведённая в обычный текст. Содержание не сокращается."""
    if text is None:
        return ""
    s = str(text).replace("\r\n", "\n").replace("\r", "\n")
    blocks: list[str] = []

    def keep(match: re.Match) -> str:
        blocks.append(match.group(2).strip("\n"))
        return f"\x00{len(blocks) - 1}\x00"

    s = re.sub(r"\{(code|noformat)(?::[^}]*)?\}(.*?)\{\1\}", keep, s, flags=re.S)
    s = re.sub(r"\[\^([^\]\n]+)\]", r"(вложение: \1)", s)
    s = re.sub(r"!([^!|\n\s][^!|\n]*)(?:\|[^!\n]*)?!", r"(изображение: \1)", s)
    s = re.sub(r"\[~([^\]\n]+)\]", r"@\1", s)
    s = re.sub(r"\[([^\]|\n]+)\|([^\]\n]+)\]", r"\1 (\2)", s)
    s = re.sub(r"\[(https?://[^\]\s]+)\]", r"\1", s)
    s = re.sub(r"\{color(?::[^}]*)?\}", "", s)
    s = re.sub(r"\{(?:panel|quote|anchor)(?::[^}]*)?\}", "", s)
    s = re.sub(r"(?m)^h([1-6])\.\s*(.+)$", r"**\2**", s)
    s = re.sub(r"(?m)^#+\s+", "1. ", s)
    s = s.replace("||", "|")
    s = "\n".join(line.rstrip() for line in s.split("\n"))
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    return re.sub(r"\x00(\d+)\x00", lambda m: "\n```\n" + blocks[int(m.group(1))] + "\n```\n", s)


def person(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("displayName") or value.get("name") or value.get("emailAddress") or "").strip() or "—"
    return "—"


def when(value: Any) -> str:
    return str(value or "")[:16].replace("T", " ") or "—"


def duration(seconds: Any) -> str:
    """Секунды в «2ч 30м» (как считает Jira: без дней и недель, чтобы не зависеть от её настроек)."""
    try:
        total = int(seconds or 0)
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    hours, minutes = divmod(total // 60, 60)
    return " ".join(part for part in (f"{hours}ч" if hours else "", f"{minutes}м" if minutes or not hours else "") if part)


def flat(value: Any) -> str:
    """Значение поля Jira одной строкой или текстом: строки, числа, справочные значения, списки, спринты."""
    if value is None or value == "" or value == [] or value == {}:
        return ""
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, (int, float)):
        return str(int(value)) if float(value).is_integer() else str(value)
    if isinstance(value, str):
        sprint = _SPRINT.search(value)
        if sprint:
            parts = dict(p.split("=", 1) for p in sprint.group(1).split(",") if "=" in p)
            return f"{parts.get('name', '').strip()} ({parts.get('state', '').strip().lower() or 'состояние неизвестно'})"
        return clean_text(value)
    if isinstance(value, list):
        return ", ".join(x for x in (flat(v) for v in value) if x)
    if isinstance(value, dict):
        for key in ("displayName", "value", "name", "key"):
            if value.get(key) not in (None, ""):
                text = flat(value[key])
                child = value.get("child")
                return f"{text} / {flat(child)}" if isinstance(child, dict) and flat(child) else text
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _names(items: Any) -> str:
    return ", ".join(str(x.get("name") or "").strip() for x in items or [] if isinstance(x, dict) and x.get("name"))


def _field(lines: list[str], label: str, value: str) -> None:
    if not value:
        return
    if "\n" in value:
        lines.append(f"- {label}:")
        lines.extend("  " + line for line in value.split("\n"))
    else:
        lines.append(f"- {label}: {value}")


def extra_fields(raw: dict[str, Any]) -> list[tuple[str, str]]:
    """Непустые поля задачи, которых нет в шапке: пользовательские и редкие системные — по названиям из Jira."""
    fields = raw.get("fields") or {}
    names = raw.get("names") if isinstance(raw.get("names"), dict) else {}
    out: list[tuple[str, str]] = []
    for field_id in sorted(fields, key=lambda f: str(names.get(f) or f).lower()):
        if field_id in _HANDLED or field_id in _SKIPPED:
            continue
        label = str(names.get(field_id) or field_id).strip()
        if label.lower() in _NOISE_NAMES or label.startswith("["):
            continue
        value = flat(fields[field_id])
        if not value or value in ("{}", "[]") or re.fullmatch(r"\d+\|[0-9a-z]+:[0-9a-z]*", value):
            continue
        out.append((label, value))
    return out


def is_rendered(text: str) -> bool:
    """Текст — документ, собранный этим модулем (задача или сводка проекта Jira). Такой текст уже чистый и точный:
    конвейер документов не отдаёт его модели на «очистку», чтобы не исказить числа, имена и даты."""
    head = (text or "").lstrip()[:3000]
    return head.startswith("# Jira: проект ") or (head.startswith("# [") and "\nИсточник: Jira, проект " in head)


def omitted_fields(raw: dict[str, Any]) -> list[str]:
    """Названия непустых полей, которые в документ намеренно не выводятся (служебные) — для прозрачности сверки."""
    fields = raw.get("fields") or {}
    names = raw.get("names") if isinstance(raw.get("names"), dict) else {}
    shown = {label for label, _value in extra_fields(raw)}
    out = []
    for field_id, value in fields.items():
        if field_id in _HANDLED or field_id in _TIME_COUNTERS or not flat(value):
            continue
        label = str(names.get(field_id) or field_id).strip()
        if label not in shown:
            out.append(label)
    return sorted(set(out))


def counts(raw: dict[str, Any]) -> dict[str, int]:
    """Сколько чего в задаче — для проверки полноты: комментарии, трудозатраты, вложения, записи истории, подзадачи, связи."""
    fields = raw.get("fields") or {}
    comment = fields.get("comment") if isinstance(fields.get("comment"), dict) else {}
    worklog = fields.get("worklog") if isinstance(fields.get("worklog"), dict) else {}
    log = raw.get("changelog") if isinstance(raw.get("changelog"), dict) else {}
    return {"comments": len(comment.get("comments") or []), "comments_total": int(comment.get("total") or len(comment.get("comments") or [])),
            "worklogs": len(worklog.get("worklogs") or []), "worklogs_total": int(worklog.get("total") or len(worklog.get("worklogs") or [])),
            "attachments": len(fields.get("attachment") or []), "history": len(log.get("histories") or []),
            "history_total": int(log.get("total") or len(log.get("histories") or [])), "subtasks": len(fields.get("subtasks") or []),
            "links": len(fields.get("issuelinks") or []), "extra_fields": len(extra_fields(raw))}


def _hours(seconds: Any, jira_text: Any) -> str:
    """Время часами и минутами; запись Jira («4d 1h 30m») — в скобках с пояснением: её «день» — рабочий, не 24 часа."""
    exact, shown = duration(seconds), str(jira_text or "").strip()
    if exact and shown and re.search(r"\d[dw]", shown):
        return f"{exact} (в Jira записано «{shown}»: d — рабочий день, w — рабочая неделя, не календарные)"
    return exact or shown


def issue_document(raw: dict[str, Any], base_url: str, project_name: str | None = None, parsable: set[str] | None = None) -> str:
    """Задача Jira как документ Markdown — полностью, без сокращений. ``parsable`` — расширения файлов, которые портал
    разбирает: у каждого вложения сказано, загружено ли его содержимое отдельным материалом."""
    row = normalize_issue(raw, base_url)
    fields = raw.get("fields") or {}
    project = fields.get("project") if isinstance(fields.get("project"), dict) else {}
    project_key = project.get("key") or row["key"].rsplit("-", 1)[0]
    project_title = project_name or project.get("name") or ""
    status = row["status"] or "—"
    state = STATUS_LABELS.get(row["portal_status"], "")
    resolution = flat(fields.get("resolution"))
    lines = [f"# [{row['key']}] {row['summary'] or 'Без названия'}", "",
             f"Задача Jira {row['key']} «{row['summary'] or 'без названия'}» проекта {project_key}"
             + (f" ({project_title})" if project_title else "") + f": статус «{status}»"
             + (f" ({state})" if state else "") + f", исполнитель — {row['assignee'] or 'не назначен'}, обновлена {when(row['updated'])}.",
             "", f"Источник: Jira, проект {project_key}" + (f" ({project_title})" if project_title else "") + ".",
             f"Ссылка: {row['url'] or '—'}", ""]
    _field(lines, "Вид", row["type"] or "—")
    _field(lines, "Статус", status + (f" ({state})" if state else ""))
    _field(lines, "Резолюция", resolution)
    _field(lines, "Приоритет", row["priority"] or "—")
    _field(lines, "Исполнитель", row["assignee"] or "не назначен")
    _field(lines, "Автор", row["reporter"] or "—")
    creator = person(fields.get("creator"))
    if creator != "—" and creator != (row["reporter"] or ""):
        _field(lines, "Создал", creator)
    _field(lines, "Создана", when(row["created"]))
    _field(lines, "Обновлена", when(row["updated"]))
    _field(lines, "Решена", when(fields.get("resolutiondate")) if fields.get("resolutiondate") else "")
    _field(lines, "Срок", row["deadline_at"] or "не задан")
    _field(lines, "Метки", ", ".join(row["labels"]))
    _field(lines, "Компоненты", _names(fields.get("components")))
    _field(lines, "Версии исправления", _names(fields.get("fixVersions")))
    _field(lines, "Затронутые версии", _names(fields.get("versions")))
    parent = fields.get("parent") if isinstance(fields.get("parent"), dict) else None
    if parent and parent.get("key"):
        title = (parent.get("fields") or {}).get("summary") or ""
        _field(lines, "Родительская задача", parent["key"] + (f" — {title}" if title else ""))
    tracking = fields.get("timetracking") if isinstance(fields.get("timetracking"), dict) else {}
    _field(lines, "Оценка времени", _hours(fields.get("timeoriginalestimate"), tracking.get("originalEstimate")))
    _field(lines, "Осталось времени", str(tracking.get("remainingEstimate") or "") or duration(fields.get("timeestimate")))
    _field(lines, "Затрачено времени", _hours(fields.get("timespent"), tracking.get("timeSpent")))
    for label, own, total in (("Оценка времени с подзадачами", "timeoriginalestimate", "aggregatetimeoriginalestimate"),
                              ("Осталось времени с подзадачами", "timeestimate", "aggregatetimeestimate"),
                              ("Затрачено времени с подзадачами", "timespent", "aggregatetimespent")):
        if fields.get(total) and fields.get(total) != fields.get(own):
            _field(lines, label, duration(fields.get(total)))
    _field(lines, "Уровень безопасности", flat(fields.get("security")))
    watches = fields.get("watches") if isinstance(fields.get("watches"), dict) else {}
    _field(lines, "Наблюдателей", flat(watches.get("watchCount")) if watches.get("watchCount") else "")
    _field(lines, "Наблюдатели", ", ".join(person(w) for w in raw.get("watchers") or [] if isinstance(w, dict)))
    votes = fields.get("votes") if isinstance(fields.get("votes"), dict) else {}
    _field(lines, "Голосов", flat(votes.get("votes")) if votes.get("votes") else "")
    _field(lines, "Окружение", clean_text(fields.get("environment")))

    extra = extra_fields(raw)
    if extra:
        lines += ["", "## Дополнительные поля", ""]
        for label, value in extra:
            _field(lines, label, value)

    lines += ["", "## Описание", "", clean_text(fields.get("description")) or "Описания нет."]

    subtasks = [s for s in fields.get("subtasks") or [] if isinstance(s, dict) and s.get("key")]
    if subtasks:
        lines += ["", f"## Подзадачи ({len(subtasks)})", ""]
        for sub in subtasks:
            sub_fields = sub.get("fields") or {}
            lines.append(f"- {sub['key']} — {sub_fields.get('summary') or ''} [{flat(sub_fields.get('status')) or '—'}]")

    related = []
    for link in fields.get("issuelinks") or []:
        kind = link.get("type") if isinstance(link.get("type"), dict) else {}
        for side, verb in (("outwardIssue", kind.get("outward")), ("inwardIssue", kind.get("inward"))):
            other = link.get(side)
            if isinstance(other, dict) and other.get("key"):
                other_fields = other.get("fields") or {}
                title = other_fields.get("summary") or ""
                state_text = flat(other_fields.get("status"))
                related.append(f"- {verb or 'связана с'} {other['key']}" + (f" — {title}" if title else "") + (f" [{state_text}]" if state_text else ""))
    if related:
        lines += ["", f"## Связанные задачи ({len(related)})", ""] + related

    # Ход задачи отдельно от полной истории: в ней смены статуса тонут среди сотен записей об учёте времени.
    course = []
    for h in (raw.get("changelog") or {}).get("histories") or [] if isinstance(raw.get("changelog"), dict) else []:
        for item in h.get("items") or [] if isinstance(h, dict) else []:
            label = _COURSE_FIELDS.get(str(item.get("field") or "").lower())
            if label:
                course.append(f"- {when(h.get('created'))}, {person(h.get('author'))}: {label} — было «{clean_text(item.get('fromString')) or 'пусто'}», "
                              f"стало «{clean_text(item.get('toString')) or 'пусто'}»")
    if course:
        lines += ["", f"## Ход задачи: кто и когда менял статус, исполнителя, срок ({len(course)})", ""] + course

    remote = [r for r in raw.get("remotelinks") or [] if isinstance(r, dict)]
    if remote:
        lines += ["", f"## Внешние ссылки ({len(remote)})", ""]
        for link in remote:
            obj = link.get("object") if isinstance(link.get("object"), dict) else {}
            lines.append(f"- {obj.get('title') or obj.get('url') or 'ссылка'}: {obj.get('url') or '—'}")

    block = fields.get("comment") if isinstance(fields.get("comment"), dict) else {}
    comments = [c for c in block.get("comments") or [] if isinstance(c, dict)]
    if comments:
        lines += ["", f"## Комментарии ({len(comments)})"]
        for comment in comments:
            edited = ""
            if comment.get("updated") and comment.get("updated") != comment.get("created"):
                editor = person(comment.get("updateAuthor"))
                by = f", {editor}" if editor not in ("—", person(comment.get("author"))) else ""
                edited = f" (изменён {when(comment.get('updated'))}{by})"
            limit = comment.get("visibility") if isinstance(comment.get("visibility"), dict) else {}
            if limit.get("value"):
                edited += f" (виден только: {limit.get('value')})"
            lines += ["", f"### {person(comment.get('author'))}, {when(comment.get('created'))}{edited}", "", clean_text(comment.get("body")) or "(пусто)"]

    work = fields.get("worklog") if isinstance(fields.get("worklog"), dict) else {}
    worklogs = [w for w in work.get("worklogs") or [] if isinstance(w, dict)]
    if worklogs:
        total = sum(int(w.get("timeSpentSeconds") or 0) for w in worklogs)
        lines += ["", f"## Трудозатраты ({len(worklogs)}, всего {duration(total) or '0м'})", ""]
        per_person: dict[str, list[int]] = {}
        for w in worklogs:
            bucket = per_person.setdefault(person(w.get("author")), [0, 0])
            bucket[0] += int(w.get("timeSpentSeconds") or 0)
            bucket[1] += 1
        # Итог по людям посчитан программой: модель складывает десятки записей с ошибками.
        lines.append("Итого по людям: " + "; ".join(f"{name} — {duration(sec) or '0м'} (записей: {count})"
                                                     for name, (sec, count) in sorted(per_person.items(), key=lambda kv: -kv[1][0])) + ".")
        lines.append("")
        for w in worklogs:
            note = clean_text(w.get("comment")).replace("\n", " ")
            lines.append(f"- {when(w.get('started'))}, {person(w.get('author'))}: {w.get('timeSpent') or duration(w.get('timeSpentSeconds')) or '—'}"
                         + (f" — {note}" if note else ""))

    attachments = [a for a in fields.get("attachment") or [] if isinstance(a, dict)]
    if attachments:
        lines += ["", f"## Вложения ({len(attachments)})", ""]
        for a in attachments:
            name = str(a.get("filename") or "файл")
            ext = ("." + name.rsplit(".", 1)[1].lower()) if "." in name else ""
            note = ""
            if parsable is not None:
                note = ("; содержимое загружено в портал отдельным материалом" if ext in parsable
                        else f"; содержимое в портал не загружено: вид файла {ext or 'без расширения'} портал не разбирает")
            lines.append(f"- {name} ({int(a.get('size') or 0) // 1024} КБ, загрузил(а) {person(a.get('author'))}, {when(a.get('created'))}{note})")

    log = raw.get("changelog") if isinstance(raw.get("changelog"), dict) else {}
    histories = [h for h in log.get("histories") or [] if isinstance(h, dict)]
    if histories:
        lines += ["", f"## История изменений ({len(histories)})", ""]
        for h in histories:
            changes = []
            for item in h.get("items") or []:
                raw_name = str(item.get("field") or "поле")
                label = _HISTORY_LABELS.get(raw_name.lower())
                name = f"{label} ({raw_name})" if label else raw_name
                if raw_name.lower() in _LONG_TEXT_FIELDS:
                    changes.append(f"{name}: текст изменён")
                    continue
                before, after = clean_text(item.get("fromString")).replace("\n", " "), clean_text(item.get("toString")).replace("\n", " ")
                if raw_name.lower() in _HISTORY_SECONDS:  # Jira пишет секунды — показываем часами и минутами
                    before, after = (duration(int(v)) or "0м" if v.isdigit() else v for v in (before, after))
                changes.append(f"{name}: «{before or 'пусто'}» → «{after or 'пусто'}»")
            lines.append(f"- {when(h.get('created'))}, {person(h.get('author'))}: " + "; ".join(changes or ["изменение без подробностей"]))
        if raw.get("changelog_complete") is False:
            lines.append(f"- (Jira отдала {len(histories)} записей из {int(log.get('total') or 0)}: более ранние в выгрузку не попали)")
    return "\n".join(lines).strip() + "\n"


def project_document(info: dict[str, Any], issues: list[dict[str, Any]], base_url: str, *, loaded: int | None = None) -> str:
    """Сводка проекта Jira: сведения о проекте и все задачи таблицей. ``issues`` — строки ``normalize_issue``."""
    key = str(info.get("key") or "")
    name = str(info.get("name") or key)
    lines = [f"# Jira: проект {key} — {name} (сводка)", "",
             f"Сводка проекта Jira {key} «{name}»: сведения о проекте и перечень всех его задач со статусами, исполнителями и сроками.",
             f"Ссылка: {base_url.rstrip('/')}/browse/{key}", ""]
    _field(lines, "Руководитель проекта", person(info.get("lead")) if isinstance(info.get("lead"), dict) else "")
    _field(lines, "Вид проекта", flat(info.get("projectTypeKey")))
    category = info.get("projectCategory") if isinstance(info.get("projectCategory"), dict) else {}
    _field(lines, "Категория", flat(category.get("name")))
    _field(lines, "Компоненты", _names(info.get("components")))
    versions = [v for v in info.get("versions") or [] if isinstance(v, dict) and v.get("name")]
    if versions:
        lines.append("- Версии: " + "; ".join(
            f"{v['name']}" + (f" (выпущена {v['releaseDate']})" if v.get("released") and v.get("releaseDate") else
                              " (выпущена)" if v.get("released") else f" (план {v['releaseDate']})" if v.get("releaseDate") else "")
            for v in versions))
    _field(lines, "Всего задач", str(len(issues)) + (f" (в портал загружено {loaded})" if loaded is not None and loaded != len(issues) else ""))
    description = clean_text(info.get("description"))
    if description:
        lines += ["", "## Описание проекта", "", description]
    if issues:
        by_state = Counter(row["portal_status"] for row in issues)
        by_status = Counter(row["status"] or "—" for row in issues)
        lines += ["", "## Задачи по состоянию", ""]
        lines += [f"- {STATUS_LABELS.get(state, state)}: {count}" for state, count in by_state.most_common()]
        lines += ["", "## Задачи по статусам Jira", ""] + [f"- {status}: {count}" for status, count in by_status.most_common()]
        people = Counter(row["assignee"] or "не назначен" for row in issues)
        open_people = Counter(row["assignee"] or "не назначен" for row in issues if row["portal_status"] != "done")
        lines += ["", "## Задачи по исполнителям", ""]
        lines += [f"- {who}: всего {count}, не завершено {open_people.get(who, 0)}" for who, count in people.most_common()]
        lines += ["", "## Все задачи", "", "| Ключ | Вид | Статус | Состояние | Исполнитель | Автор | Создана | Срок | Обновлена | Название |", "|---|---|---|---|---|---|---|---|---|---|"]
        for row in issues:
            cells = [row["key"], row["type"] or "—", row["status"] or "—", STATUS_LABELS.get(row["portal_status"], "—"), row["assignee"] or "не назначен",
                     row.get("reporter") or "—", when(row.get("created")), row["deadline_at"] or "—", when(row["updated"]), (row["summary"] or "").replace("|", "/")]
            lines.append("| " + " | ".join(str(c).replace("\n", " ") for c in cells) + " |")
    else:
        lines += ["", "В проекте нет задач."]
    return "\n".join(lines).strip() + "\n"
