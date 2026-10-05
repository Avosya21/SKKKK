"""Ключевые слова файлов: TF-IDF по именам, которые файл определяет (функции, классы, константы).

Источник — только граф (метки символов), исходники не читаются. Имена режутся по `snake_case` и `CamelCase`; слово весомо,
если часто встречается в файле и редко — в остальных файлах проекта.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

# слова, которые есть почти у каждого файла и ничего не говорят о его теме
STOP_WORDS = frozenset({
    "get", "set", "self", "cls", "init", "main", "test", "tests", "the", "and", "for", "with", "from", "into", "run", "make", "build",
    "new", "old", "func", "function", "method", "class", "var", "tmp", "temp", "val", "value", "data", "item", "items", "list", "dict",
    "str", "int", "bool", "none", "true", "false", "args", "kwargs", "param", "params", "result", "results", "name", "names",
    "path", "paths", "file", "files", "key", "keys", "type", "types", "obj", "ret", "res", "out", "err", "error", "app",
    "root", "dir", "sec", "min", "max", "num", "idx", "cfg", "conf", "ctx", "opts", "opt", "arg", "info", "text", "line", "lines",
})
CAMEL = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")


def split_identifier(name: str) -> list[str]:
    """`get_user_Name` / `getUserName` -> `["get", "user", "name"]` (без служебных слов и слов короче трёх букв)."""
    words: list[str] = []
    for chunk in re.split(r"[^A-Za-z0-9]+", name):
        words += [w.lower() for w in CAMEL.findall(chunk)]
    return [w for w in words if len(w) >= 3 and not w.isdigit() and w not in STOP_WORDS]


def file_keywords(graph: dict[str, Any], *, top: int = 5) -> dict[str, list[str]]:
    """id файла -> до `top` ключевых слов; файлы без осмысленных имён в результат не попадают."""
    files = {n["id"] for n in graph.get("nodes", []) if n.get("kind") == "file"}
    labels = {n["id"]: n.get("label", "") for n in graph.get("nodes", []) if n.get("kind") == "symbol"}
    tokens: dict[str, Counter[str]] = {}
    for edge in graph.get("edges", []):
        if edge.get("rel") == "defines" and edge.get("from") in files and edge.get("to") in labels:
            tokens.setdefault(edge["from"], Counter()).update(split_identifier(labels[edge["to"]]))
    document_frequency: Counter[str] = Counter()
    for counter in tokens.values():
        document_frequency.update(set(counter))
    total = max(len(tokens), 1)
    result: dict[str, list[str]] = {}
    for file_id, counter in tokens.items():
        size = sum(counter.values())
        scored = sorted(((count / size) * (math.log((total + 1) / (document_frequency[word] + 1)) + 1.0), word)
                        for word, count in counter.items())
        best = [word for _, word in sorted(scored, key=lambda item: (-item[0], item[1]))[:top]]
        if best:
            result[file_id] = best
    return result
