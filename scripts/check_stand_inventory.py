"""Проверка полноты карты: всё, что лежит на стенде, должно быть в инвентаре `docs/project-map/stand.yaml`.

Что делает:
  1. обходит верхний уровень корня проекта и каталогов из `roots` (пропускает мусор: `__pycache__`, `.git`, `.idea`,
     `.pytest_cache`, `tmp`, `*.pyc`, `*-shm/-wal`, `*.lock`, `*.log` и файлы `*.py` — код проверяет `check_map_drift.py`);
  2. сверяет найденное с инвентарём: `unknown` — есть на диске, но не описано; `absent` — описано и обязательно, но пропало;
  3. помечает каждую сущность состоянием git: `tracked`, `ignored`, `untracked` или «нет на диске»;
  4. `--docs` переписывает раздел §17 карты между маркерами `generated:begin stand` / `generated:end stand`.

Запуск:
  venv\\Scripts\\python.exe scripts\\check_stand_inventory.py            # расхождения и цифры
  venv\\Scripts\\python.exe scripts\\check_stand_inventory.py --docs      # обновить §17 карты
  venv\\Scripts\\python.exe scripts\\check_stand_inventory.py --json      # машиночитаемо
Код выхода: 0 — карта полна, 1 — есть расхождения. Страж в тестах — `app/app/tests/test_stand_inventory.py`.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAND = ROOT / "docs" / "project-map" / "stand.yaml"
MAP = ROOT / "docs" / "PROJECT-MAP.md"
BEGIN = "<!-- generated:begin stand -->"
END = "<!-- generated:end stand -->"
DEFAULT_SKIP_DIRS = {"__pycache__", ".git", ".idea", ".pytest_cache", "tmp"}
DEFAULT_SKIP_SUFFIX = (".pyc", ".py", "-shm", "-wal", "-journal", ".lock", ".log")

GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Корень проекта", ()),
    ("Документы (`docs/`)", ("docs/",)),
    ("Инструменты (`tools/`)", ("tools/",)),
    ("Окружения (`venvs/`)", ("venvs/",)),
    ("Данные (`data/`)", ("data/",)),
    ("ASR (`asr/`)", ("asr/",)),
    ("Вендоры фронтенда (`app/app/static/vendor/`)", ("app/app/static/vendor/",)),
)

_STATE_LABEL = {"tracked": "да", "ignored": "нет", "untracked": "нет (не в git)", "missing": "—", "unknown": "?"}


def load_stand(path: Path | None = None) -> dict:
    import yaml

    return yaml.safe_load((path or STAND).read_text(encoding="utf-8")) or {}


def _skip_rules(spec: dict) -> tuple[set[str], tuple[str, ...]]:
    dirs = set(spec.get("skip_dirs") or []) | DEFAULT_SKIP_DIRS
    suffix = tuple(spec.get("skip_suffix") or []) or DEFAULT_SKIP_SUFFIX
    return dirs, tuple(set(suffix) | set(DEFAULT_SKIP_SUFFIX))


def _children(rel: str, skip_dirs: set[str], skip_suffix: tuple[str, ...]) -> list[str]:
    """Прямые потомки каталога, кроме пропускаемых (относительные пути через `/`)."""
    base = ROOT if rel in ("", ".") else ROOT / rel
    if not base.is_dir():
        return []
    out: list[str] = []
    for child in sorted(base.iterdir()):
        if child.name in skip_dirs or child.name.endswith(skip_suffix):
            continue
        out.append(child.name if rel in ("", ".") else f"{rel}/{child.name}")
    return out


def _git_state(rel: str) -> str:
    """`tracked`, `ignored`, `untracked` или `missing` — состояние пути в git."""
    if not (ROOT / rel).exists():
        return "missing"
    try:
        ignored = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", rel],
                                 capture_output=True, timeout=20).returncode == 0
        if ignored:
            return "ignored"
        tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--error-unmatch", rel],
                                 capture_output=True, timeout=20).returncode == 0
        return "tracked" if tracked else "untracked"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _doc_title(path: Path) -> str:
    """Заголовок документа для указателя: первый `# …` или первая непустая строка."""
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            text = line.strip()
            if not text:
                continue
            return text.lstrip("#").strip()[:110] or path.name
    except OSError:
        pass
    return path.name


def _doc_index(spec: dict) -> list[tuple[str, str]]:
    """Указатель документов каталогов карты: (путь, заголовок) — чтобы ни один документ не остался невидимым."""
    out: list[tuple[str, str]] = []
    for directory in spec.get("doc_dirs") or []:
        base = ROOT / directory
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if not child.is_file() or child.name.endswith((".pyc", ".db")):
                continue
            rel = f"{directory}/{child.name}"
            if _git_state(rel) == "ignored":
                continue
            out.append((rel, _doc_title(child)))
    return out


def _doc_orphans(spec: dict) -> list[str]:
    """Файлы каталогов документов, не упомянутые ни в одном документе карты (и не игнорируемые git)."""
    sources = []
    for rel in spec.get("doc_sources") or []:
        path = ROOT / rel
        if path.is_file():
            sources.append(path.read_text(encoding="utf-8", errors="ignore"))
    text = "\n".join(sources)
    orphans: list[str] = []
    for directory in spec.get("doc_dirs") or []:
        base = ROOT / directory
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if not child.is_file() or child.name == "README.md" or child.name.endswith((".pyc", ".db")):
                continue
            rel = f"{directory}/{child.name}"
            if _git_state(rel) == "ignored":  # локальные выгрузки и эксперименты вне git не требуются в карте
                continue
            if child.name not in text:
                orphans.append(rel)
    return orphans


def check(spec: dict | None = None) -> dict:
    """Сверка инвентаря с диском."""
    spec = spec if spec is not None else load_stand()
    items = spec.get("items") or []
    known = {str(item["path"]) for item in items}
    skip_dirs, skip_suffix = _skip_rules(spec)
    found: list[str] = []
    for root in spec.get("roots") or ["."]:
        found.extend(_children(str(root), skip_dirs, skip_suffix))
    unknown = sorted({path for path in found if path not in known})
    absent, absent_optional = [], []
    for item in items:
        path = str(item["path"])
        if (ROOT / path).exists():
            continue
        (absent_optional if item.get("optional") else absent).append(path)
    rows = [{"path": str(i["path"]), "kind": str(i.get("kind") or ""), "purpose": str(i.get("purpose") or ""),
             "optional": bool(i.get("optional")), "state": _git_state(str(i["path"]))} for i in items]
    return {"unknown": unknown, "absent": sorted(absent), "absent_optional": sorted(absent_optional),
            "orphan_docs": _doc_orphans(spec), "doc_index": _doc_index(spec), "doc_dirs": spec.get("doc_dirs") or [],
            "rows": rows,
            "machine": spec.get("machine") or [], "total": len(items),
            "checked_at": time.strftime("%Y-%m-%d %H:%M")}


def render_docs(report: dict) -> str:
    """Раздел §17 карты: инвентарь стенда и машинные факты."""
    rows = report["rows"]
    lines = [BEGIN, "",
             f"_Проверено {report['checked_at']}: сущностей {report['total']}, "
             f"не описано {len(report['unknown'])}, пропало {len(report['absent'])}. "
             f"Список — `docs/project-map/stand.yaml`, проверка — `scripts/check_stand_inventory.py`._", ""]
    if report["unknown"] or report["absent"] or report.get("orphan_docs"):
        lines += ["**Расхождения:**", ""]
        lines += [f"- есть на диске, но не описано: `{path}`" for path in report["unknown"]]
        lines += [f"- описано, но не найдено: `{path}`" for path in report["absent"]]
        lines += [f"- документ не упомянут ни в одном документе карты: `{path}`" for path in report.get("orphan_docs") or []]
        lines.append("")
    used: set[str] = set()
    number = 0
    for title, prefixes in GROUPS:
        subset = [row for row in rows
                  if (not prefixes and "/" not in row["path"]) or any(row["path"].startswith(p) for p in prefixes)]
        if not subset:
            continue
        number += 1
        used.update(row["path"] for row in subset)
        lines += [f"### 17.{number}. {title}", "", "| Путь | Вид | Назначение | В git |", "|---|---|---|---|"]
        for row in subset:
            note = " _(может отсутствовать)_" if row["optional"] else ""
            lines.append(f'| `{row["path"]}` | {row["kind"]} | {row["purpose"]}{note} | '
                         f'{_STATE_LABEL.get(row["state"], row["state"])} |')
        lines.append("")
    rest = [row for row in rows if row["path"] not in used]
    if rest:
        number += 1
        lines += [f"### 17.{number}. Прочее", "", "| Путь | Вид | Назначение | В git |", "|---|---|---|---|"]
        for row in rest:
            lines.append(f'| `{row["path"]}` | {row["kind"]} | {row["purpose"]} | '
                         f'{_STATE_LABEL.get(row["state"], row["state"])} |')
        lines.append("")
    if report.get("doc_index"):
        number += 1
        dirs = ", ".join(f"`{d}`" for d in report.get("doc_dirs") or [])
        lines += [f"### 17.{number}. Документы каталогов (генерируется)", "",
                  f"Все файлы каталогов {dirs} с их заголовками — чтобы ни один документ не остался невидимым.", "",
                  "| Документ | О чём |", "|---|---|"]
        for rel, title in report["doc_index"]:
            lines.append(f"| `{rel}` | {title} |")
        lines.append("")
    if report["machine"]:
        number += 1
        lines += [f"### 17.{number}. Машина и внешние приложения", "",
                  "| Что | Вид | Назначение |", "|---|---|---|"]
        for item in report["machine"]:
            lines.append(f'| {item.get("name", "")} | {item.get("kind", "")} | {item.get("purpose", "")} |')
        lines.append("")
    if report["absent_optional"]:
        lines += ["_Нет на диске (необязательные, машинно-зависимые): " +
                  ", ".join(f"`{p}`" for p in report["absent_optional"]) + "._", ""]
    lines += [END, ""]
    return "\n".join(lines)


def write_docs(text: str, path: Path | None = None) -> Path:
    """Вставить раздел в карту между маркерами, сохранив остальной текст."""
    target = path or MAP
    current = target.read_text(encoding="utf-8")
    if BEGIN in current and END in current:
        head, rest = current.split(BEGIN, 1)
        _, tail = rest.split(END, 1)
        target.write_text(head + text + tail.lstrip("\n"), encoding="utf-8", newline="")
    else:
        marker = "\n---\n\n## Архив разработки"
        section = "\n---\n\n## 17. Инвентарь стенда (генерируется)\n\n" + text + "\n"
        target.write_text(current.replace(marker, section + marker, 1) if marker in current else current + section,
                          encoding="utf-8", newline="")
    return target


def main() -> int:
    ap = argparse.ArgumentParser(description="Полнота карты: инвентарь стенда")
    ap.add_argument("--docs", action="store_true", help="обновить раздел §17 в docs/PROJECT-MAP.md")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    report = check()
    if args.json:
        print(json.dumps({k: report[k] for k in ("unknown", "absent", "absent_optional", "orphan_docs", "total", "checked_at")},
                         ensure_ascii=False, indent=1))
    if args.docs:
        # Проверка «документ упомянут» читает саму карту, поэтому после записи раздела пересчитываем
        # и при изменении списка расхождений пишем ещё раз — генерация сходится за один проход.
        first = check()
        write_docs(render_docs(first))
        again = check()
        if (again["orphan_docs"], again["unknown"], again["absent"]) != (first["orphan_docs"], first["unknown"], first["absent"]):
            write_docs(render_docs(again))
        print("обновлено:", MAP.relative_to(ROOT))
        return 0
    print(f"сущностей в инвентаре: {report['total']}; проверено {report['checked_at']}")
    if report["unknown"]:
        print(f"✗ не описано на диске ({len(report['unknown'])}):")
        for path in report["unknown"]:
            print("   ", path)
    else:
        print("✓ на диске нет сущностей вне инвентаря")
    if report["absent"]:
        print(f"✗ описано, но не найдено ({len(report['absent'])}): " + ", ".join(report["absent"]))
    else:
        print("✓ все обязательные сущности на месте")
    if report["absent_optional"]:
        print(f"· нет на диске (необязательные, {len(report['absent_optional'])}): " + ", ".join(report["absent_optional"]))
    if report.get("orphan_docs"):
        print(f"✗ документы вне карты ({len(report['orphan_docs'])}):")
        for path in report["orphan_docs"]:
            print("   ", path)
    else:
        print("✓ все документы упомянуты в карте")
    return 1 if (report["unknown"] or report["absent"] or report.get("orphan_docs")) else 0


if __name__ == "__main__":
    raise SystemExit(main())
