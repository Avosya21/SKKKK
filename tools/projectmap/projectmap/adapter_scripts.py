"""Адаптер запусков: один файл кода или скрипта запускает другой по имени (`run_pipeline.py`, `deploy.ps1`, `build.sh`).

Подпроцессы, команды в скриптах и строки конфигурации не видны разбору языка как вызовы, но связывают файлы по-настоящему. Связь
`runs` создаётся, когда имя скрипта (не короче восьми знаков, с разделителем) встречается в другом файле кода или скрипта как
отдельное слово. Тесты не считаются запускающими.
"""

from __future__ import annotations

import re

from projectmap.adapters import Adapter, AdapterContext
from projectmap.graphbuf import file_id
from projectmap.paths import SCRIPT_SUFFIXES, is_test_path, unique_script_names


def _applies(ctx: AdapterContext) -> bool:
    return bool(ctx.paths(*SCRIPT_SUFFIXES))


def _run(ctx: AdapterContext) -> None:
    scripts = unique_script_names(ctx.paths())
    count = 0
    for path in ctx.paths(*SCRIPT_SUFFIXES):
        if is_test_path(path):
            continue
        text = ctx.text(path)
        if not text:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for name, target in scripts.items():
                if target != path and name in line and re.search(r"(?<![\w.\-])" + re.escape(name) + r"(?![\w\-])", line):
                    ctx.edges.add(file_id(path), file_id(target), "runs", number)
                    count += 1
    ctx.notes.append(f"запуски «скрипт → скрипт»: {count}")


ADAPTER = Adapter("scripts", "Запуски скриптов друг другом", _applies, _run)
