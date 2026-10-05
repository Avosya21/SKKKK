"""Заготовка карты проекта (Markdown) по графу: подсистемы, роуты, таблицы БД, переменные окружения, сервисы, экраны, модули.

Факты (что есть, где определено, кто читает и пишет) берутся из графа и обновляются командой `projectmap map`; смысл (зачем это нужно)
вписывает человек или ИИ в последний столбец «Описание». Новая строка получает черновик из докстринга (`⟨черновик⟩ …`) или метку `⟨описать⟩`.
Пока метка стоит, описание считается невписанным: пробы `map_described` и `map_mentions` показывают, сколько осталось.

Устройство документа: таблицы лежат между маркерами `<!-- projectmap:begin <раздел> -->` и `<!-- projectmap:end -->`. При обновлении
перезаписывается только содержимое между маркерами, причём вписанные описания переносятся по ключу строки (первый столбец в обратных
кавычках). Всё вне маркеров (введение, заголовки, свои абзацы) остаётся как есть. Строки удалённых из кода сущностей пропадают.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from projectmap.config import Config
from projectmap.languages import language_of
from projectmap.paths import is_test_path

TODO = "⟨описать⟩"
DRAFT = "⟨черновик⟩"
MARK_BEGIN = re.compile(r"<!-- projectmap:begin (\w+) -->")
MARK_END = "<!-- projectmap:end -->"
ROW_KEY = re.compile(r"^\|\s*`([^`]+)`\s*\|")
TOP_ITEMS = 4


def is_unfinished(description: str) -> bool:
    """Описание не вписано: пусто, метка или черновик из докстринга."""
    text = description.strip()
    return not text or TODO in text or DRAFT in text


def _cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def _names(items: list[str], limit: int = TOP_ITEMS) -> str:
    items = sorted(dict.fromkeys(items))
    shown = ", ".join(f"`{_cell(i)}`" for i in items[:limit])
    return shown + (f" и ещё {len(items) - limit}" if len(items) > limit else "") if items else "—"


@dataclass
class Section:
    id: str
    title: str
    intro: str
    header: list[str]  # столбцы; последний — «Описание»
    rows: list[tuple[str, list[str], str]] = field(default_factory=list)  # (ключ, ячейки без ключа и описания, черновик описания)
    groups: dict[str, list[tuple[str, list[str], str]]] | None = None  # для модулей: подсистема -> строки


class Index:
    """Индексы графа для запросов раздела."""

    def __init__(self, graph: dict[str, Any]) -> None:
        self.nodes = {n["id"]: n for n in graph.get("nodes", [])}
        self.edges = graph.get("edges", [])
        self.out: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.inc: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.owner = {i: i for i, n in self.nodes.items() if n["kind"] == "file"}
        for e in self.edges:
            self.out[e["from"]].append(e)
            self.inc[e["to"]].append(e)
            if e["rel"] == "defines":
                self.owner[e["to"]] = e["from"]
        for i, n in self.nodes.items():
            if i not in self.owner and n.get("path") and f"file:{n['path']}" in self.nodes:
                self.owner[i] = f"file:{n['path']}"

    def path_of(self, node_id: str) -> str:
        owner = self.owner.get(node_id)
        return self.nodes[owner]["path"] if owner else self.nodes.get(node_id, {}).get("path", "")

    def reachable_routes(self, start: str, depth: int = 2) -> list[str]:
        """Роуты, которые функция вызывает по адресу (`calls_api`) сама и через вызовы глубиной до `depth`."""
        found: list[str] = []
        frontier, seen = {start}, {start}
        for _ in range(depth + 1):
            nxt: set[str] = set()
            for node in frontier:
                for e in self.out.get(node, []):
                    if e["rel"] == "calls_api" and e["to"] in self.nodes:
                        found.append(self.nodes[e["to"]]["label"])
                    elif e["rel"] in ("calls", "calls_ref") and e["to"] not in seen:
                        seen.add(e["to"])
                        nxt.add(e["to"])
            frontier = nxt
        return sorted(dict.fromkeys(found))

    def reachable_tables(self, start: str, depth: int = 2) -> tuple[set[str], set[str]]:
        """Таблицы, которые читает и пишет функция сама и через вызовы глубиной до `depth`."""
        reads: set[str] = set()
        writes: set[str] = set()
        frontier, seen = {start}, {start}
        for _ in range(depth + 1):
            nxt: set[str] = set()
            for node in frontier:
                for e in self.out.get(node, []):
                    target = self.nodes.get(e["to"], {})
                    if e["rel"] == "reads" and target.get("kind") == "table":
                        reads.add(target["label"])
                    elif e["rel"] == "writes" and target.get("kind") == "table":
                        writes.add(target["label"])
                    elif e["rel"] in ("calls", "calls_ref") and e["to"] not in seen:
                        seen.add(e["to"])
                        nxt.add(e["to"])
            frontier = nxt
        return reads, writes


def _draft(doc: str | None) -> str:
    return f"{DRAFT} {doc.strip()}" if doc and doc.strip() else TODO


def build_sections(graph: dict[str, Any], config: Config) -> list[Section]:
    ix = Index(graph)
    files = [n for n in ix.nodes.values() if n["kind"] == "file" and not config.is_ignored(n["path"])]
    sections: list[Section] = []

    # подсистемы
    by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for n in files:
        by_group[n.get("group") or "—"].append(n)
    sub = Section("subsystems", "Подсистемы", "Каталоги верхнего уровня (или правила `groups` конфига) и что в них лежит.",
                  ["Подсистема", "Файлов", "Из них код", "Главные темы"])
    for group, items in sorted(by_group.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        words = Counter(w for n in items for w in n.get("keywords", []))
        code = sum(1 for n in items if language_of(n["path"]) and not is_test_path(n["path"]))
        sub.rows.append((group, [str(len(items)), str(code), ", ".join(w for w, _ in words.most_common(5)) or "—"], TODO))
    sections.append(sub)

    # роуты
    routes = Section("routes", "Роуты", "Адрес, функция-обработчик, проверки прав и таблицы, которых функция касается (напрямую и через вызовы глубиной до двух).",
                     ["Роут", "Обработчик", "Права", "Читает / пишет таблицы"])
    for n in sorted((n for n in ix.nodes.values() if n["kind"] == "route"), key=lambda n: (n["label"].split(" ", 1)[-1], n["label"])):
        handler = next((e["to"] for e in ix.out.get(n["id"], []) if e["rel"] == "handles"), None)
        reads, writes = ix.reachable_tables(handler) if handler else (set(), set())
        guards = n.get("guards") or "—"
        tables = (f"читает {_names(sorted(reads))}" if reads else "") + ("; " if reads and writes else "") + (f"пишет {_names(sorted(writes))}" if writes else "")
        where = f"`{n.get('path')}:{n.get('line')}` `{n.get('handler')}`" if n.get("handler") else f"`{n.get('path')}`"
        routes.rows.append((n["label"], [where, _cell(guards), tables or "—"], _draft(n.get("doc"))))
    sections.append(routes)

    # таблицы
    tables_sec = Section("tables", "Таблицы БД", "Столбцы и кто пишет и читает таблицу.", ["Таблица", "Столбцы", "Пишут", "Читают"])
    for n in sorted((n for n in ix.nodes.values() if n["kind"] == "table"), key=lambda n: n["label"]):
        writers = sorted({ix.path_of(e["from"]) for e in ix.inc.get(n["id"], []) if e["rel"] == "writes"} - {""})
        readers = sorted({ix.path_of(e["from"]) for e in ix.inc.get(n["id"], []) if e["rel"] == "reads"} - {""})
        columns = [c.split(":")[0].strip() for c in (n.get("columns") or "").split(",") if c.strip()]
        tables_sec.rows.append((n["label"], [_names(columns, 8), _names(writers), _names(readers)], TODO))
    sections.append(tables_sec)

    # переменные окружения
    env = Section("env", "Переменные окружения", "Кто читает переменную и описана ли она в образце настроек.", ["Переменная", "Читают", "В образце"])
    for n in sorted((n for n in ix.nodes.values() if n["kind"] == "env"), key=lambda n: n["label"]):
        if config.is_env_ignored(n["label"]):
            continue
        readers = sorted({ix.path_of(e["from"]) for e in ix.inc.get(n["id"], []) if e["rel"] == "reads_env"} - {""})
        declared = n.get("declared") or any(e["rel"] == "declares" for e in ix.inc.get(n["id"], []))
        env.rows.append((n["label"], [_names(readers, 3), "да" if declared else "нет"], TODO))
    sections.append(env)

    # сервисы
    services = Section("services", "Сервисы", "Сервисы compose-файлов: образ, порты, откуда описан.", ["Сервис", "Образ", "Порты", "Файл"])
    for n in sorted((n for n in ix.nodes.values() if n["kind"] == "service"), key=lambda n: n["id"]):
        services.rows.append((n["label"], [_cell(n.get("image") or "—"), _cell(n.get("ports") or "—"), f"`{n.get('path')}`"], TODO))
    sections.append(services)

    # экраны: интерактивные элементы, которые вызывают API
    screen = Section("interface", "Экраны и кнопки", "Элементы интерфейса, которые вызывают API или обрабатываются кодом.", ["Элемент", "Подпись", "Вызывает / обработчик"])
    for n in sorted((n for n in ix.nodes.values() if n["kind"] == "ui"), key=lambda n: n["id"]):
        targets = [ix.nodes[e["to"]] for e in ix.out.get(n["id"], []) if e["rel"] in ("handled_by", "calls_api") and e["to"] in ix.nodes]
        if n.get("sub") not in ("button", "form", "a") or not targets:
            continue
        shown = []
        for target in targets:
            if target["kind"] == "route":
                shown.append(target["label"])
            elif target["kind"] == "file":  # элемент найден в общем скрипте страницы: что именно он вызывает, граф не различает
                shown.append(f"скрипт страницы `{target['label']}`")
            else:  # обработчик-функция: показываем, какие роуты он вызывает (сам или через вызовы глубиной до двух)
                reached = ix.reachable_routes(target["id"])
                shown += reached or [f"{target['label']}()"]
        screen.rows.append((f"{n['path'].rsplit('/', 1)[-1]}#{n['label']}", [_cell(n.get("text") or "—"), _names(shown, 3)], TODO))
    sections.append(screen)

    # модули по подсистемам
    modules = Section("modules", "Модули", "Файлы кода (без тестов) по подсистемам: что определяют и какова роль.", ["Файл", "Главные определения"], groups={})
    for n in sorted(files, key=lambda n: n["path"]):
        if not language_of(n["path"]) or is_test_path(n["path"]):
            continue
        defined = [ix.nodes[e["to"]] for e in ix.out.get(n["id"], []) if e["rel"] == "defines"]
        top = [s["label"] for s in sorted(defined, key=lambda s: (-s.get("fan_in", 0), s.get("line") or 0))[:TOP_ITEMS]]
        modules.groups.setdefault(n.get("group") or "—", []).append((n["path"], [_names(top, TOP_ITEMS)], _draft(n.get("doc"))))
    sections.append(modules)
    return [s for s in sections if s.rows or s.groups]


# ---- вывод ----

def _table(header: list[str], rows: list[tuple[str, list[str], str]], descriptions: dict[str, str]) -> list[str]:
    cols = [*header, "Описание"]
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for key, cells, draft in rows:
        old = descriptions.get(key)
        description = old if old is not None and not is_unfinished(old) else draft  # вписанное человеком сохраняется, остальное — свежий черновик
        lines.append(f"| `{_cell(key)}` | " + " | ".join(cells) + f" | {_cell(description) or TODO} |")
    return lines


def render_block(section: Section, descriptions: dict[str, str]) -> str:
    lines = [f"<!-- projectmap:begin {section.id} -->"]
    if section.groups is not None:
        for group, rows in sorted(section.groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            lines += ["", f"#### {group} ({len(rows)})", ""] + _table(section.header, rows, descriptions)
    else:
        lines += _table(section.header, section.rows, descriptions)
    return "\n".join(lines + [MARK_END])


def _blocks(text: str) -> dict[str, tuple[int, int]]:
    """Раздел -> (начало, конец) блока в тексте: от маркера begin до конца маркера end."""
    found = {}
    for m in MARK_BEGIN.finditer(text):
        end = text.find(MARK_END, m.end())
        if end >= 0:
            found[m.group(1)] = (m.start(), end + len(MARK_END))
    return found


def parse_descriptions(text: str) -> dict[str, dict[str, str]]:
    """Вписанные описания по разделам: {раздел: {ключ строки: описание}} (последняя ячейка строки таблицы)."""
    result: dict[str, dict[str, str]] = {}
    for section, (start, end) in _blocks(text).items():
        items: dict[str, str] = {}
        for line in text[start:end].splitlines():
            match = ROW_KEY.match(line)
            if match:
                cells = re.split(r"(?<!\\)\|", line.strip().strip("|"))
                items[match.group(1).replace("\\|", "|")] = cells[-1].strip().replace("\\|", "|")
        result[section] = items
    return result


def intro(title: str) -> str:
    return (f"# {title}\n\n"
            "Карта собрана инструментом projectmap по графу проекта. Таблицы между маркерами `<!-- projectmap:begin … -->` обновляет команда "
            "`projectmap map` (факты: где определено, кто читает и пишет); вручную в них правят только последний столбец «Описание». "
            f"Метка `{TODO}` — описания ещё нет, `{DRAFT}` — взято из докстринга и не проверено человеком: уберите метку, когда описание верно. "
            "Всё вне маркеров — ваш текст, команда его не трогает.\n")


def render_document(graph: dict[str, Any], config: Config, existing: str | None = None, title: str = "") -> tuple[str, dict[str, Any]]:
    """Документ карты: новый или обновлённый поверх существующего. Возвращает (текст, отчёт: добавлено, удалено, без описаний)."""
    sections = build_sections(graph, config)
    kept = parse_descriptions(existing) if existing else {}
    text = existing if existing else intro(title or "Карта проекта")
    report: dict[str, Any] = {"sections": {}, "added": 0, "removed": 0, "unfinished": 0, "rows": 0}
    for section in sections:
        keys = [k for k, _, _ in section.rows] + [k for rows in (section.groups or {}).values() for k, _, _ in rows]
        old = kept.get(section.id, {})
        report["added"] += len([k for k in keys if k not in old]) if old else 0
        report["removed"] += len([k for k in old if k not in set(keys)])
        block = render_block(section, old)
        report["rows"] += len(keys)
        report["unfinished"] += sum(1 for line in block.splitlines() if ROW_KEY.match(line) and is_unfinished(re.split(r"(?<!\\)\|", line.strip().strip("|"))[-1]))
        report["sections"][section.id] = len(keys)
        spans = _blocks(text)
        if section.id in spans:
            start, end = spans[section.id]
            text = text[:start] + block + text[end:]
        else:
            text = text.rstrip("\n") + f"\n\n## {section.title}\n\n{section.intro}\n\n{block}\n"
    return text.rstrip("\n") + "\n", report
