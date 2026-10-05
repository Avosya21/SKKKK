"""Схема графа projectmap: слои карты, виды узлов и связей, проверка целостности.

Граф — словарь `{"nodes": [...], "edges": [...]}`: узел имеет `id` и `kind`, ребро — `from`, `to` и `rel`.
Каждый вид узла и связи относится к одному из слоёв карты; по слоям считается охват (модуль coverage).
Связи со смысловым префиксом `m:` (из ручного описания проекта) всегда относятся к слою «Смысл».
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class Layer:
    id: str
    title: str
    about: str


LAYERS: tuple[Layer, ...] = (
    Layer("structure", "Структура", "файлы, функции, классы, вызовы, импорты, наследование"),
    Layer("interfaces", "Интерфейсы", "роуты, кнопки и экраны, их связь с API"),
    Layer("data", "Данные", "таблицы, столбцы, кто пишет и читает, миграции"),
    Layer("settings", "Настройки", "переменные окружения, конфиги, ключи"),
    Layer("tests", "Тесты", "какие тесты покрывают какой код"),
    Layer("history", "История", "коммиты, частота правок, пары файлов, меняющихся вместе"),
    Layer("meaning", "Смысл", "описания, манифест, глоссарий, ссылки документ → код"),
    Layer("metrics", "Метрики", "степени связности, группы, «хабы», ключевые слова"),
    Layer("environment", "Среда", "сервисы, процессы, скрипты запуска, внешние инструменты"),
)
LAYER_IDS = tuple(layer.id for layer in LAYERS)

# вид узла -> (слой, подпись)
NODE_KINDS: dict[str, tuple[str, str]] = {
    "file": ("structure", "файл"),
    "symbol": ("structure", "символ"),
    "route": ("interfaces", "роут"),
    "ui": ("interfaces", "элемент интерфейса"),
    "table": ("data", "таблица БД"),
    "column": ("data", "столбец БД"),
    "env": ("settings", "переменная окружения"),
    "commit": ("history", "коммит"),
    "manifest": ("meaning", "узел карты"),
    "service": ("environment", "сервис"),
}

# связь -> (слой, подпись)
RELATIONS: dict[str, tuple[str, str]] = {
    "defines": ("structure", "определяет"),
    "calls": ("structure", "вызывает"),
    "calls_ref": ("structure", "передаёт как значение"),
    "calls_ext": ("structure", "вызывает внешнее"),
    "imports": ("structure", "импортирует"),
    "inherits": ("structure", "наследует"),
    "imports_ext": ("structure", "импортирует внешнее"),
    "handles": ("interfaces", "обрабатывает"),
    "handled_by": ("interfaces", "обрабатывается"),
    "calls_api": ("interfaces", "обращается к API"),
    "guarded_by": ("interfaces", "защищён проверкой прав"),
    "reads": ("data", "читает таблицу"),
    "writes": ("data", "пишет в таблицу"),
    "declares": ("data", "объявляет"),
    "has_column": ("data", "имеет столбец"),
    "reads_env": ("settings", "читает переменную"),
    "covers": ("tests", "покрывает тестом"),
    "changed": ("history", "изменил"),
    "co_changes": ("history", "меняется вместе с"),
    "depends_on": ("meaning", "зависит от"),
    "refers_to": ("meaning", "ссылается на"),
    "runs": ("environment", "запускает"),
}

# метрики, которые инструмент дописывает в узлы файлов и символов (модуль metrics)
NODE_METRICS = ("community", "fan_in", "fan_out", "instability", "churn", "keywords")
REQUIRED_NODE_FIELDS = ("id", "kind")
REQUIRED_EDGE_FIELDS = ("from", "to", "rel")


def layer_of_kind(kind: str) -> str | None:
    entry = NODE_KINDS.get(kind)
    return entry[0] if entry else None


def layer_of_relation(rel: str) -> str | None:
    if rel.startswith("m:"):
        return "meaning"
    entry = RELATIONS.get(rel)
    return entry[0] if entry else None


@dataclass(frozen=True)
class Problem:
    """Находка проверки графа: `error` ломает граф, `warning` — отклонение от схемы."""

    severity: str
    message: str


def validate_graph(graph: dict[str, Any]) -> list[Problem]:
    """Проверить целостность графа: поля, повторы id, висячие рёбра, неизвестные виды узлов и связей."""
    problems: list[Problem] = []
    nodes = graph.get("nodes")
    edges = graph.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        return [Problem("error", "в графе должны быть списки `nodes` и `edges`")]
    ids: set[str] = set()
    unknown_kinds: set[str] = set()
    for index, node in enumerate(nodes):
        missing = [f for f in REQUIRED_NODE_FIELDS if f not in node]
        if missing:
            problems.append(Problem("error", f"узел №{index}: нет полей {', '.join(missing)}"))
            continue
        if node["id"] in ids:
            problems.append(Problem("error", f"повторяется id узла: {node['id']}"))
        ids.add(node["id"])
        if node["kind"] not in NODE_KINDS:
            unknown_kinds.add(node["kind"])
    unknown_rels: set[str] = set()
    dangling = 0
    for index, edge in enumerate(edges):
        missing = [f for f in REQUIRED_EDGE_FIELDS if f not in edge]
        if missing:
            problems.append(Problem("error", f"ребро №{index}: нет полей {', '.join(missing)}"))
            continue
        if edge["from"] not in ids or edge["to"] not in ids:
            dangling += 1
        if layer_of_relation(edge["rel"]) is None:
            unknown_rels.add(edge["rel"])
    if dangling:
        problems.append(Problem("error", f"рёбер на несуществующие узлы: {dangling}"))
    for kind in sorted(unknown_kinds):
        problems.append(Problem("warning", f"вид узла вне схемы: {kind}"))
    for rel in sorted(unknown_rels):
        problems.append(Problem("warning", f"вид связи вне схемы: {rel}"))
    return problems


def count_by_layer(graph: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Сколько узлов и связей в каждом слое: `{слой: {"nodes": n, "edges": m}}`."""
    result = {layer: {"nodes": 0, "edges": 0} for layer in LAYER_IDS}
    for node in graph.get("nodes", []):
        layer = layer_of_kind(node.get("kind", ""))
        if layer:
            result[layer]["nodes"] += 1
    for edge in graph.get("edges", []):
        layer = layer_of_relation(edge.get("rel", ""))
        if layer:
            result[layer]["edges"] += 1
    return result


def layers_by_id() -> dict[str, Layer]:
    return {layer.id: layer for layer in LAYERS}


def known_kinds() -> Iterable[str]:
    return NODE_KINDS.keys()
