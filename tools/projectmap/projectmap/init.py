"""`projectmap init`: определить стек проекта и создать `.projectmap/config.yaml`, правила `.gitignore` и (по просьбе) git-хуки."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from projectmap.config import CONFIG_PATH
from projectmap.detect import detect_stack
from projectmap.languages import BY_ID

GITIGNORE_BLOCK = (".projectmap/out/", ".projectmap/grammars/", ".projectmap/private-terms.txt")


def _q(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def render_config(root: Path, stack: dict[str, Any]) -> str:
    lines = [
        "# Конфиг projectmap. Документация: tools/projectmap/README.md (или `projectmap --help`).",
        f"title: {_q(root.name)}",
        "graph: .projectmap/out/graph.json  # граф строит `projectmap index`; в git не хранится",
        "",
        "# Осознанные исключения: то, что карте не нужно. У каждого — причина; всё остальное, чего нет в графе, считается пробелом.",
        "ignore:",
    ]
    lines += [f"  - {_q(mask)}  # {why}" for mask, why in stack["ignore"]] or ["  []"]
    lines += ["", "# Цели охвата по слоям (доля 0..1). Ниже цели проба видна как пробел, `projectmap check --strict` даёт код выхода 1.", "targets:",
              "  files: 0.9", "  doc_refs_valid: 0.95", "  metrics: 0.9", ""]
    lines += ["# Файлы карты, которые правят вместе с кодом (хук commit-msg). Пусто — проверка выключена.",
              "map:", f"  files: [{', '.join(_q(p) for p in stack['map_files'][:1])}]", "  waiver: 'Карта: не требуется'", ""]
    lines += ["# Слова, которых не должно быть в коммитах (имена людей, заказчиков), — в файле, который в git не попадает.",
              "privacy:", "  terms_file: .projectmap/private-terms.txt", ""]
    lines += ["# Подсистемы по умолчанию — первый каталог пути. Свои правила: {префикс пути: название}.", "groups: {}", "",
              "# Адаптеры стека: auto — все, чей стек есть в проекте (fastapi, sqlalchemy, env, docker, markdown, scripts, web).",
              "adapters: auto", ""]
    return "\n".join(lines)


def describe(stack: dict[str, Any]) -> list[str]:
    names = ", ".join(f"{BY_ID[k].title if k in BY_ID else k} {n}" for k, n in list(stack["languages"].items())[:8]) or "кода не найдено"
    out = [f"файлов: {stack['files']}; языки: {names}"]
    if stack["frameworks"]:
        out.append("фреймворки: " + ", ".join(stack["frameworks"]))
    out.append(f"документов: {stack['docs']}; тестов: {stack['tests']}; Docker: {'да' if stack['docker'] else 'нет'}")
    out.append(f"исключений в конфиге: {len(stack['ignore'])} (проверьте список и причины)")
    return out


def ensure_gitignore(root: Path) -> list[str]:
    path = root / ".gitignore"
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    present = {line.strip() for line in text.splitlines()}
    missing = [line for line in GITIGNORE_BLOCK if line not in present]
    if missing:
        prefix = "" if not text or text.endswith("\n") else "\n"
        path.write_text(text + prefix + "\n# projectmap: построенные данные и личные слова\n" + "\n".join(missing) + "\n", encoding="utf-8", newline="")
    return missing


def run_init(root: Path, *, force: bool = False) -> tuple[int, list[str]]:
    config_path = root / CONFIG_PATH
    messages: list[str] = []
    if config_path.exists() and not force:
        return 1, [f"{CONFIG_PATH} уже есть (перезаписать — --force)"]
    stack = detect_stack(root)
    messages += describe(stack)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(render_config(root, stack), encoding="utf-8", newline="")
    messages.append(f"создан {CONFIG_PATH}")
    added = ensure_gitignore(root)
    if added:
        messages.append("в .gitignore добавлено: " + ", ".join(added))
    terms = root / ".projectmap" / "private-terms.txt"
    if not terms.exists():
        terms.write_text("# Слова, которых не должно быть в коммитах, по одному на строку (файл в git не попадает).\n", encoding="utf-8", newline="")
    return 0, messages
