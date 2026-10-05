"""Git-хуки проверки карты для любого проекта: личные данные в коммите (pre-commit) и карта вместе с кодом (commit-msg).

`projectmap hooks install` кладёт в каталог хуков короткие скрипты, которые вызывают `python -m projectmap hook <имя>`.
Чужой хук не затирается без `--force`: тогда он сохраняется рядом (`<имя>.projectmap-backup`) и выполняется после нашего.

- **pre-commit** читает то, что попадает в коммит (снимок индекса, а не рабочую копию), и ищет слова из файла личных данных
  (`privacy.terms_file` конфига, по умолчанию `.projectmap/private-terms.txt`; сам файл в git не хранится). Находки — путь и строка.
- **commit-msg** при правке кода без файлов карты (`map.files`) отклоняет коммит, если в сообщении нет строки отказа
  (`map.waiver`, по умолчанию «Карта: не требуется — причина»). Без `map.files` в конфиге проверка выключена.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from projectmap.config import Config, glob_to_regex, load_config
from projectmap.languages import language_of
from projectmap.paths import is_test_path

MARKER = "# projectmap-hook"
HOOKS = ("pre-commit", "commit-msg")
MAX_SCAN_BYTES = 2_000_000


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False)


def hooks_dir(root: Path) -> Path:
    out = _git(root, "rev-parse", "--git-path", "hooks").stdout.decode("utf-8").strip()
    if not out:
        raise RuntimeError("это не git-репозиторий: хукам некуда встать (выполните git init)")
    path = Path(out)
    return path if path.is_absolute() else (root / path)


def _script(name: str, root: Path) -> str:
    python = Path(sys.executable).as_posix()
    package_parent = Path(__file__).resolve().parents[1]
    env = "" if "site-packages" in package_parent.parts else f'PYTHONPATH="{package_parent.as_posix()}" '
    return (f'#!/bin/sh\n{MARKER}\n{env}"{python}" -m projectmap hook {name} "$@" --root "{root.resolve().as_posix()}" || exit $?\n'
            f'if [ -f "$0.projectmap-backup" ]; then exec sh "$0.projectmap-backup" "$@"; fi\n')


def _ours(path: Path) -> bool:
    return path.is_file() and MARKER in path.read_text(encoding="utf-8", errors="ignore")


def status(root: Path) -> dict[str, str]:
    directory = hooks_dir(root)
    result = {}
    for name in HOOKS:
        path = directory / name
        result[name] = "absent" if not path.exists() else "ours" if _ours(path) else "foreign"
    return result


def install(root: Path, *, force: bool = False) -> dict[str, str]:
    directory = hooks_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    result = {}
    for name in HOOKS:
        path = directory / name
        if path.exists() and not _ours(path):
            if not force:
                result[name] = "skipped: чужой хук (повторите с --force, он сохранится как .projectmap-backup)"
                continue
            backup = path.with_name(name + ".projectmap-backup")
            if not backup.exists():
                path.replace(backup)
        path.write_text(_script(name, root), encoding="utf-8", newline="\n")
        path.chmod(0o755)
        result[name] = "installed"
    return result


def uninstall(root: Path) -> dict[str, str]:
    directory = hooks_dir(root)
    result = {}
    for name in HOOKS:
        path = directory / name
        backup = path.with_name(name + ".projectmap-backup")
        if _ours(path):
            path.unlink()
            result[name] = "removed"
            if backup.exists():
                backup.replace(path)
                result[name] = "removed, чужой хук возвращён"
        else:
            result[name] = "не наш: не тронут" if path.exists() else "не было"
    return result


# ---- проверки ----

def staged_paths(root: Path) -> list[str]:
    out = _git(root, "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").stdout.decode("utf-8")
    return [p for p in out.split("\0") if p]


def load_terms(root: Path, config: Config) -> list[str]:
    path = root / config.privacy_terms
    if not path.is_file():
        return []
    lines = (line.strip() for line in path.read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def scan_staged(root: Path, config: Config) -> list[tuple[str, int, str]]:
    """Находки в снимке индекса: (путь, строка, слово)."""
    terms = load_terms(root, config)
    if not terms:
        return []
    lowered = [(t, t.lower()) for t in terms]
    found: list[tuple[str, int, str]] = []
    for path in staged_paths(root):
        blob = _git(root, "show", f":{path}").stdout
        if len(blob) > MAX_SCAN_BYTES or b"\0" in blob[:8000]:
            continue
        for number, line in enumerate(blob.decode("utf-8", errors="ignore").splitlines(), 1):
            low = line.lower()
            found += [(path, number, term) for term, needle in lowered if needle in low]
    return found


def run_pre_commit(root: Path) -> int:
    config = load_config(root)
    found = scan_staged(root, config)
    for path, number, term in found[:30]:
        print(f"  {path}:{number}: слово из файла личных данных ({term[0]}…, {len(term)} зн.)", file=sys.stderr)
    if found:
        print(f"projectmap: в коммите личные данные ({len(found)} находок). Замените их вымышленными значениями. "
              f"Список слов — {config.privacy_terms}.", file=sys.stderr)
        return 1
    return 0


def _is_code(path: str, config: Config) -> bool:
    if not language_of(path) or is_test_path(path) or path in config.map_files:
        return False
    return not config.map_code or any(glob_to_regex(mask).match(path) for mask in config.map_code)


def waiver_reason(message: str, waiver: str) -> str | None:
    """Причина из строки отказа вида «<waiver> — причина»; None, если строки нет или причины нет."""
    for line in message.splitlines():
        if line.startswith(waiver):
            reason = line[len(waiver):].lstrip(" \t:-—–").strip()
            return reason or None
    return None


def run_commit_msg(root: Path, message_file: Path) -> int:
    config = load_config(root)
    if not config.map_files or (_git(root, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0):
        return 0
    staged = staged_paths(root)
    if not any(_is_code(p, config) for p in staged) or any(p in config.map_files for p in staged):
        return 0
    message = "\n".join(line for line in message_file.read_text(encoding="utf-8", errors="ignore").splitlines() if not line.startswith("#"))
    if waiver_reason(message, config.map_waiver):
        return 0
    print(f"projectmap: изменён код, а карты в коммите нет ({', '.join(config.map_files)}).\n"
          f"  Опишите изменение в карте и добавьте её в коммит, либо добавьте в сообщение строку «{config.map_waiver} — причина».", file=sys.stderr)
    return 1


def run_hook(name: str, root: Path, args: list[str]) -> int:
    if name == "pre-commit":
        return run_pre_commit(root)
    if name == "commit-msg":
        return run_commit_msg(root, Path(args[0])) if args else 0
    print(f"projectmap hook: неизвестный хук {name}", file=sys.stderr)
    return 2
