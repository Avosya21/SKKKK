"""Адаптер документов: ссылки Markdown-файлов на файлы проекта становятся связями `refers_to`.

Берутся пути в обратных кавычках (`app/service.py`) и относительные ссылки `[текст](путь)`. Ссылка на несуществующий файл
(битая) связью не становится, а попадает в счётчик примечаний графа: документация, ведущая в пустоту, — тоже находка карты.
"""

from __future__ import annotations

import posixpath
import re

from projectmap.adapters import Adapter, AdapterContext
from projectmap.graphbuf import file_id

DOC_SUFFIXES = (".md", ".mdc", ".markdown")
BACKTICK_PATH = re.compile(r"`([\w.\-]+(?:/[\w.\-]+)+/?)`")
LINK = re.compile(r"\]\((?!https?://|mailto:|#)([^)\s#]+)(?:#[^)]*)?\)")


def _applies(ctx: AdapterContext) -> bool:
    return bool(ctx.paths(*DOC_SUFFIXES))


def _run(ctx: AdapterContext) -> None:
    files = set(ctx.facts)
    top_dirs = {p.split("/")[0] for p in files if "/" in p}
    found = broken = 0
    for doc in ctx.paths(*DOC_SUFFIXES):
        text = ctx.text(doc)
        if not text:
            continue
        base = doc.rsplit("/", 1)[0] if "/" in doc else ""
        for number, line in enumerate(text.splitlines(), 1):
            candidates = [m.group(1) for m in BACKTICK_PATH.finditer(line) if m.group(1).split("/")[0] in top_dirs]
            candidates += [posixpath.normpath(posixpath.join(base, m.group(1))) for m in LINK.finditer(line)
                           if not m.group(1).startswith(("/", "#"))]
            for token in candidates:
                target = token.rstrip("/")
                if target in files:
                    ctx.edges.add(file_id(doc), file_id(target), "refers_to", number)
                    found += 1
                elif not any(p.startswith(target + "/") for p in files):  # не каталог и не файл: ссылка битая
                    broken += 1
    ctx.notes.append(f"документы: ссылок на файлы {found}, битых {broken}")


ADAPTER = Adapter("markdown", "Ссылки документов на файлы", _applies, _run)
