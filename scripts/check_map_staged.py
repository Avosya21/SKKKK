"""Проверка карты проекта перед коммитом: сверяет карту с тем, что реально попадает в коммит.

Вызывается git-хуком `pre-commit` (ставит `scripts/install_hooks.ps1`). Проверяется не рабочая копия, а содержимое
индекса git: файлы коммита выгружаются во временный каталог, и там запускается `check_map_drift.py`. Поэтому чужие
незавершённые правки в рабочей копии (другая сессия, другой ИИ) коммит не блокируют, а недописанная карта в самом
коммите — блокирует. Проверяет то же, что тест `test_map_coverage`: файлы кода, роуты, таблицы БД, переменные
окружения, реестр модулей, манифест. Сначала — что каждый Python-файл коммита разбирается: частичный коммит однажды
унёс в git `chat.py` с куском кода посреди вызова функции, и портал с чистой выгрузки не стартовал.

  venv\\Scripts\\python.exe scripts\\check_map_staged.py          # проверить индекс git
  venv\\Scripts\\python.exe scripts\\check_map_staged.py --all    # то же, даже если в индексе нет файлов кода

Код выхода: 0 — карта соответствует, 1 — расхождения (коммит отклоняется). Обход `git commit --no-verify` запрещён
правилами проекта (AGENTS.md): он оставляет карту неактуальной для всех, кто работает после вас.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Что влияет на карту: изменения вне этих путей (данные, план, картинки) проверку не запускают.
RELEVANT = ("app/", "asr/", "scripts/", "tools/", "docs/project-map/", "docs/PROJECT-MAP.md", ".env.example")
# Каталоги снимка: всё, что нужно check_map_drift.py; тяжёлые вендорные файлы в снимок не берём.
SNAPSHOT = ("app/", "asr/", "scripts/", "tools/", "docs/project-map/", "docs/PROJECT-MAP.md", ".env.example")
SKIP_PREFIXES = ("app/app/static/vendor/",)

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


def _git(*args: str, input_: bytes | None = None) -> bytes:
    result = subprocess.run(["git", *args], cwd=ROOT, input=input_, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {result.stderr.decode('utf-8', 'replace').strip()}")
    return result.stdout


def staged_paths() -> list[str]:
    """Пути, которые коммит добавляет, меняет или удаляет."""
    raw = _git("diff", "--cached", "--name-only", "-z", "--diff-filter=ACDMRT")
    return [p for p in raw.decode("utf-8").split("\0") if p]


def syntax_errors(paths: list[str]) -> list[str]:
    """Python-файлы коммита (содержимое индекса git), которые не разбираются: `путь:строка: ошибка`."""
    out = []
    for path in paths:
        if not path.endswith(".py"):
            continue
        try:
            src = _git("show", f":{path}")
        except RuntimeError:
            continue  # файл удаляется коммитом
        try:
            ast.parse(src.decode("utf-8-sig"), filename=path)
        except SyntaxError as exc:
            out.append(f"{path}:{exc.lineno}: {exc.msg}")
        except UnicodeDecodeError as exc:
            out.append(f"{path}: не UTF-8 ({exc.reason})")
    return out


def is_relevant(path: str) -> bool:
    return path.startswith(RELEVANT) or path in RELEVANT


def snapshot_paths() -> list[str]:
    raw = _git("ls-files", "-z", "--", *SNAPSHOT)
    return [p for p in raw.decode("utf-8").split("\0") if p and not p.startswith(SKIP_PREFIXES)]


def export_index(target: Path) -> int:
    """Выгрузить файлы индекса git (то, что попадёт в коммит) в каталог; возвращает число файлов."""
    paths = snapshot_paths()
    prefix = str(target).replace("\\", "/").rstrip("/") + "/"
    _git("checkout-index", "-z", "--stdin", f"--prefix={prefix}", input_="\0".join(paths).encode("utf-8") + b"\0")
    return len(paths)


GUIDE = """
Карта проекта не соответствует этому коммиту. Исправьте и повторите коммит:

  1. Опишите новое в docs/PROJECT-MAP.md: роут — §5 (адрес целиком или «…/хвост» после /projects/{id}),
     таблицу БД — «Модели» в §7, переменную окружения — §12.2 (системные и внутренние — в ENV_IGNORE
     скрипта scripts/check_map_drift.py), новый модуль — строка в §7/§8/§10.
  2. venv\\Scripts\\python.exe scripts\\check_map_drift.py --fix
     (перегенерирует реестр модулей и добавит узлы манифеста для новых файлов).
  3. git add docs/PROJECT-MAP.md docs/project-map/manifest.yaml

Не обходите проверку через --no-verify: карта нужна всем, кто будет работать с проектом после вас.
""".rstrip()


# Запускается во временном каталоге со снимком индекса и печатает расхождения в json.
DRIVER = """
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("check_map_drift", "scripts/check_map_drift.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
report = mod.analyse()
keys = ("missing_in_map", "dead_nodes", "bad_edges", "registry_stale", "missing_routes", "missing_tables", "missing_env")
print(json.dumps({k: report[k] for k in keys}, ensure_ascii=False, default=str))
"""


def format_problems(report: dict) -> list[str]:
    """Расхождения отчёта в виде строк. «Мёртвые» узлы манифеста проверяются по реальному дереву: часть файлов
    (`Login_up.txt`, бинарники) лежит вне git и в снимок индекса не попадает: узел не считается мёртвым, если файл есть
    на диске или игнорируется git."""
    def lives_outside_git(path: str) -> bool:
        clean = path.split("#", 1)[0]
        ignored = subprocess.run(["git", "check-ignore", "-q", clean], cwd=ROOT, capture_output=True, check=False).returncode == 0
        return ignored or (ROOT / clean).exists()

    dead = [(node, path) for node, path in report["dead_nodes"] if not lives_outside_git(path)]
    sections = (
        ("файлы кода, которых нет ни в карте, ни в манифесте", report["missing_in_map"]),
        ("узлы манифеста с несуществующим path", [f"{n}: {p}" for n, p in dead]),
        ("рёбра манифеста на несуществующие узлы",
         [f"{e['from']} -> {e['to']} ({e.get('relation')})" for e in report["bad_edges"]]),
        ("роуты, которых нет в карте", report["missing_routes"]),
        ("таблицы БД, которых нет в карте", report["missing_tables"]),
        ("переменные окружения, которых нет в карте", report["missing_env"]),
    )
    lines: list[str] = []
    for title, items in sections:
        if items:
            lines.append(f"✗ {title}: {len(items)}")
            lines += [f"     {item}" for item in items[:25]]
            if len(items) > 25:
                lines.append(f"     … и ещё {len(items) - 25}")
    if report["registry_stale"]:
        lines.append("✗ реестр модулей в PROJECT-MAP.md отстаёт от кода (запустите check_map_drift.py --fix)")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description="Проверка карты проекта по индексу git (перед коммитом)")
    ap.add_argument("--all", action="store_true", help="проверять, даже если в коммите нет файлов, влияющих на карту")
    args = ap.parse_args()

    try:
        staged = staged_paths()
    except RuntimeError as exc:
        print(f"SKKKK: проверка карты пропущена ({exc})", file=sys.stderr)
        return 0
    broken = syntax_errors(staged)
    if broken:
        print("✗ Python-файлы коммита не разбираются (частичный коммит захватил не те строки?):", file=sys.stderr)
        print("\n".join(f"     {line}" for line in broken), file=sys.stderr)
        return 1
    if not args.all and not any(is_relevant(p) for p in staged):
        return 0

    with tempfile.TemporaryDirectory(prefix="skkkk-map-") as tmp:
        target = Path(tmp)
        try:
            export_index(target)
        except RuntimeError as exc:
            print(f"SKKKK: проверка карты пропущена: не удалось выгрузить индекс ({exc})", file=sys.stderr)
            return 0
        checker = target / "scripts" / "check_map_drift.py"
        if not checker.is_file():
            print("SKKKK: в коммите нет scripts/check_map_drift.py, проверка карты пропущена", file=sys.stderr)
            return 0
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        result = subprocess.run([sys.executable, "-c", DRIVER], cwd=target, env=env, capture_output=True, check=False)
    if result.returncode != 0:
        print(f"SKKKK: проверка карты пропущена: {result.stderr.decode('utf-8', 'replace').strip()[-400:]}", file=sys.stderr)
        return 0
    report = json.loads(result.stdout.decode("utf-8"))
    problems = format_problems(report)
    if not problems:
        return 0
    print("\n".join(problems), file=sys.stderr)
    print(GUIDE, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
