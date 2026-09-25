"""Реестр текстов интерфейса (пункт плана 3.9): собирает русские подписи, подсказки, aria-label и строки JS.

    venv\Scripts\python scripts\collect_ui_copy.py        # пишет docs/UI-COPY.md

Реестр помогает находить дубли и расхождения формулировок одного и того же действия.
"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = [*sorted((ROOT / "app" / "app" / "templates").glob("*.html")),
       *sorted(p for p in (ROOT / "app" / "app" / "static").glob("*.js"))]
CYR = re.compile(r"[А-Яа-яЁё]")
ATTR = re.compile(r'(title|aria-label|placeholder|data-idle-label|alt)="([^"]*[А-Яа-яЁё][^"]*)"')
JS_STR = re.compile(r"'([^'\n]{4,200})'|\"([^\"\n]{4,200})\"|`([^`\n]{4,200})`")


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\$\{[^}]*\}", "…", text)).strip()


def collect() -> dict[str, list[tuple[str, str]]]:
    out: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for path in SRC:
        rel = path.relative_to(ROOT / "app" / "app").as_posix()
        text = path.read_text(encoding="utf-8")
        for kind, value in ATTR.findall(text):
            out[rel].append((kind, norm(value)))
        if path.suffix != ".js" and "<script" not in text:
            continue
        for line in text.splitlines():
            if not CYR.search(line):
                continue
            for groups in JS_STR.findall(line):
                value = next((g for g in groups if g), "")
                if value and CYR.search(value) and not value.startswith(("<", "/")):
                    out[rel].append(("js", norm(value)))
    return out


def main() -> None:
    data = collect()
    seen: dict[str, set[str]] = defaultdict(set)
    for rel, rows in data.items():
        for _kind, value in rows:
            seen[value].add(rel)
    dups = {v: sorted(files) for v, files in seen.items() if len(files) > 1 and len(v) > 12}
    lines = ["# Реестр текстов интерфейса", "",
             "Генерируется `scripts/collect_ui_copy.py` — не править руками. Показывает подписи, подсказки (`title`, `aria-label`, `placeholder`) и русские строки JS по файлам; "
             "ниже — фразы, повторяющиеся в нескольких файлах (кандидаты на единую формулировку).", "",
             f"Файлов: {len(data)}, строк: {sum(len(r) for r in data.values())}, повторов между файлами: {len(dups)}.", ""]
    lines += ["## Повторы между файлами", ""]
    for value, files in sorted(dups.items()):
        lines.append(f"- «{value}» — {', '.join(files)}")
    lines.append("")
    for rel in sorted(data):
        lines += [f"## {rel}", ""]
        for kind, value in dict.fromkeys(data[rel]):
            lines.append(f"- `{kind}`: {value}")
        lines.append("")
    (ROOT / "docs" / "UI-COPY.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"docs/UI-COPY.md: файлов {len(data)}, строк {sum(len(r) for r in data.values())}, повторов {len(dups)}")


if __name__ == "__main__":
    main()
