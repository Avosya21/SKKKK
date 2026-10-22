"""Проверка гигиены репозитория: в git не попадают БД, проекты и рабочие документы.

    venv\\Scripts\\python.exe scripts\\check_no_data_files.py
    venv\\Scripts\\python.exe scripts\\check_no_data_files.py --staged

Запрещены пути из data/, private/ и onec/, файлы баз (*.db, *.sqlite*), офисные
документы, аудио/видео и веса моделей. Исключения — синтетические фикстуры
тестов и примеры настроек. Хук pre-commit (блок SKKKK-DATA) вызывает --staged.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_DIRS = ("data/", "private/", "onec/")
FORBIDDEN_NAMES = {"companies.txt", "login_up.txt", "panel_users.txt"}
FORBIDDEN_SUFFIXES = {
    ".db", ".db-wal", ".db-shm", ".db-journal",
    ".sqlite", ".sqlite3",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".m4a", ".mp3", ".wav", ".flac", ".ogg", ".mp4", ".mkv", ".mov", ".avi", ".webm",
    ".safetensors", ".gguf", ".onnx", ".pt", ".pth", ".ckpt",
}
ALLOWED_PREFIXES = ("app/app/tests/fixtures/",)
ALLOWED_FILES = {
    ".env.example",
    "asr/whisper.env",
    "scripts/qa_data_speech_a.wav",
    "scripts/qa_data_speech_b.wav",
}


def reason(path: str) -> str | None:
    """Вернуть причину запрета или None, если файл можно коммитить."""
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    p = p.lstrip("/")
    name = p.rsplit("/", 1)[-1].lower()
    if p.startswith(ALLOWED_PREFIXES) or p in ALLOWED_FILES or name.endswith(".env.example"):
        return None
    for directory in FORBIDDEN_DIRS:
        if p == directory.rstrip("/") or p.startswith(directory):
            return f"рабочий каталог {directory}"
    if name in FORBIDDEN_NAMES:
        return "список людей или логинов"
    if name == ".env":
        return "секреты окружения"
    if Path(p).suffix.lower() in FORBIDDEN_SUFFIXES:
        return "данные проекта или рабочий документ"
    return None


def git_files(staged: bool) -> list[str]:
    """Файлы из индекса (--staged) или все отслеживаемые файлы репозитория."""
    cmd = ["git", "diff", "--cached", "--name-only", "--diff-filter=AM"] if staged else ["git", "ls-files"]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        raise SystemExit(proc.stderr.strip() or "git завершился с ошибкой")
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def violations(staged: bool = False) -> list[tuple[str, str]]:
    """Пары (путь, причина) для файлов, которые нельзя коммитить."""
    return [(path, why) for path in git_files(staged) if (why := reason(path))]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Не пускает в git БД и данные проектов")
    parser.add_argument("--staged", action="store_true", help="проверить только индекс git")
    args = parser.parse_args(argv)
    found = violations(args.staged)
    if not found:
        print("OK: БД и данные проектов в git не попадают")
        return 0
    print("Запрещено коммитить рабочие данные:", file=sys.stderr)
    for path, why in found:
        print(f"  {path} — {why}", file=sys.stderr)
    print("Уберите файлы из индекса: git restore --staged <файл>", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
