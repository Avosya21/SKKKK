"""Проверка всех систем перед запуском.

Каждая проверка пишется тем же форматом событий, что и остальной проект, и
возвращает одну из трёх оценок: `ok`, `warn` (работать можно, но часть функций
отвалится) и `fail` (запускаться бессмысленно).
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from app.app.config import Settings
from app.app.logging_setup import log_event

logger = logging.getLogger("skkkk.preflight")

OK = "ok"
WARN = "warn"
FAIL = "fail"

_LEVELS = {OK: logging.INFO, WARN: logging.WARNING, FAIL: logging.ERROR}


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str = ""
    hint: str = ""
    extra: dict = field(default_factory=dict)


def _http_get(url: str, timeout: float = 5):
    import httpx

    with httpx.Client(timeout=timeout) as client:
        response = client.get(url)
        response.raise_for_status()
        return response


def check_python() -> CheckResult:
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info < (3, 10):
        return CheckResult("python", FAIL, f"версия {version}", "нужен Python 3.10+")
    return CheckResult("python", OK, f"версия {version}", extra={"executable": sys.executable})


def check_env(settings: Settings) -> CheckResult:
    from app.app.bootstrap import ENV_PATH, ROLES, role_hashes

    if not ENV_PATH.is_file():
        return CheckResult("env", FAIL, f"нет файла {ENV_PATH}", "запустите main — он создаст .env")
    if len(settings.secret_key) < 8 or settings.secret_key == "change-me":
        return CheckResult("env", FAIL, "SECRET_KEY не задан", "перезапустите main для генерации")

    hashes = role_hashes()
    missing = [role for role in ROLES if not hashes.get(role)]
    if missing:
        return CheckResult(
            "env",
            FAIL,
            f"нет паролей ролей: {', '.join(missing)}",
            "python -m app.app.main credentials --reset",
        )
    return CheckResult("env", OK, f"{ENV_PATH.name}, пароли всех ролей заданы")


def check_data_dir(settings: Settings) -> CheckResult:
    path = settings.data_dir
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return CheckResult("data_dir", FAIL, f"нет записи в {path}: {exc}")

    free_gb = shutil.disk_usage(path).free / 1024**3
    if free_gb < 5:
        return CheckResult("data_dir", WARN, f"{path}, свободно {free_gb:.1f} ГБ", "мало места под аудио")
    return CheckResult("data_dir", OK, f"{path}, свободно {free_gb:.1f} ГБ")


def check_database(settings: Settings) -> CheckResult:
    from sqlalchemy import inspect

    from app.app.db import Base, get_engine
    import app.app.models  # noqa: F401

    try:
        engine = get_engine()
        Base.metadata.create_all(bind=engine)
        tables = inspect(engine).get_table_names()
    except Exception as exc:
        return CheckResult("database", FAIL, f"недоступна: {exc}")
    return CheckResult("database", OK, f"таблиц {len(tables)}", extra={"url": settings.database_url})


def check_personal_passwords(settings: Settings) -> CheckResult:
    """Личные пароли: при optional/required нужна argon2 и база credentials.db (создаётся здесь)."""
    mode = settings.personal_password
    if mode == "off":
        return CheckResult("personal_passwords", OK, "выключены (PERSONAL_PASSWORD=off)")
    try:
        import argon2  # noqa: F401

        from app.app.services.personal_password import all_info

        count = sum(1 for info in all_info().values() if info.has_password)
    except Exception as exc:
        return CheckResult(
            "personal_passwords",
            FAIL,
            f"режим {mode}, база {settings.credentials_db_file} недоступна: {exc}",
            "pip install -r app/requirements.txt; проверьте CREDENTIALS_DB_PATH",
        )
    return CheckResult("personal_passwords", OK, f"режим {mode}, личных паролей задано: {count}")


def check_companies(settings: Settings) -> CheckResult:
    """Без компаний анкету при первом входе не заполнить — вход новых людей встанет."""
    from app.app.services.companies import load_companies_file

    path = settings.companies_path
    lines = load_companies_file(path)
    if not lines:
        return CheckResult(
            "companies",
            FAIL,
            f"нет компаний в {path}",
            "создайте файл по образцу Companies.example.txt: «ACME | Исполнитель»",
        )
    no_side = [c.name for c in lines if not c.side]
    detail = f"{len(lines)}: " + ", ".join(c.name for c in lines[:5])
    if no_side:
        return CheckResult("companies", WARN, detail, "без стороны (Исполнитель/Заказчик): " + ", ".join(no_side[:5]))
    return CheckResult("companies", OK, detail)


def check_vector_store() -> CheckResult:
    from app.app.db import get_engine
    from app.app.services.vector_index import init_vector_store

    try:
        init_vector_store(get_engine())
    except Exception as exc:
        return CheckResult(
            "vector_store",
            FAIL,
            f"sqlite-vec недоступен: {exc}",
            "поиск и чат работать не будут",
        )
    return CheckResult("vector_store", OK, "sqlite-vec подключён, chunk_vectors и entity_vectors готовы")


def check_llm(settings: Settings) -> CheckResult:
    from app.app.services.llm import check_llm_provider

    return check_llm_provider(settings)


def check_lmstudio(settings: Settings) -> CheckResult:
    """Обратная совместимость для старых тестов."""
    return check_llm(settings)


def check_ollama(settings: Settings) -> CheckResult:
    from app.app.services.ollama_client import ollama_tags

    try:
        tags = ollama_tags(settings)
    except Exception as exc:
        return CheckResult(
            "ollama",
            FAIL,
            f"{settings.ollama_base_url} недоступен: {exc}",
            "запустите Ollama",
        )
    wanted = settings.ollama_embed_model
    # Ollama отдаёт имена как есть, регистр может отличаться: `BGE-M3:latest`.
    installed = {tag.split(":")[0].casefold() for tag in tags}
    if wanted.split(":")[0].casefold() not in installed:
        return CheckResult(
            "ollama",
            FAIL,
            f"нет модели {wanted} (установлены: {', '.join(tags) or 'ничего'})",
            f"ollama pull {wanted}",
        )
    return CheckResult("ollama", OK, f"модель {wanted} доступна, num_gpu={settings.ollama_num_gpu}")


def check_asr(settings: Settings) -> CheckResult:
    engines = {
        "whisper": settings.whisper_url,
        "t-one": settings.tone_url,
        "gigaam": settings.gigaam_url,
    }
    up: list[str] = []
    down: list[str] = []
    for name, url in engines.items():
        try:
            path = "/health" if name == "whisper" else ""
            _http_get(f"{url.rstrip('/')}{path or ''}", timeout=8)
            up.append(name)
        except Exception:
            down.append(name)

    if not down:
        return CheckResult("asr", OK, "все три движка в строю: " + ", ".join(up))
    if not up:
        return CheckResult(
            "asr",
            WARN,
            "ни один движок не отвечает",
            "docker compose -f asr/docker-compose.yml up -d; обработка аудио будет падать",
        )
    return CheckResult(
        "asr",
        WARN,
        f"в строю: {', '.join(up)}; не отвечают: {', '.join(down)}",
        "ансамбль требует все три движка, аудио обрабатывать нельзя",
    )


def check_ffmpeg() -> CheckResult:
    from app.app.services.ffmpeg_tools import resolve_ffmpeg_tool

    try:
        ffmpeg = resolve_ffmpeg_tool("ffmpeg")
        ffprobe = resolve_ffmpeg_tool("ffprobe")
    except FileNotFoundError as exc:
        return CheckResult("ffmpeg", WARN, str(exc), "нужен для аудио")
    return CheckResult("ffmpeg", OK, f"ffmpeg={ffmpeg}, ffprobe={ffprobe}")


def check_gpu() -> CheckResult:
    try:
        raw = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name,memory.used,memory.total", "--format=csv,noheader,nounits"],
            text=True,
            timeout=15,
        ).strip()
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        return CheckResult("gpu", WARN, f"nvidia-smi недоступна: {exc}", "ASR пойдёт на CPU или упадёт")

    first = raw.splitlines()[0]
    parts = [p.strip() for p in first.split(",")]
    name, used, total = parts[0], int(parts[1]), int(parts[2])
    free = total - used
    if free < 4000:
        return CheckResult("gpu", WARN, f"{name}: свободно {free} МиБ из {total}", "выгрузите модели из VRAM")
    return CheckResult("gpu", OK, f"{name}: свободно {free} МиБ из {total}")


def check_port(settings: Settings) -> CheckResult:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        occupied = sock.connect_ex((settings.web_host, settings.web_port)) == 0
    if occupied:
        return CheckResult(
            "port",
            FAIL,
            f"{settings.web_host}:{settings.web_port} уже занят",
            "остановите прежний экземпляр или укажите --port",
        )
    return CheckResult("port", OK, f"{settings.web_host}:{settings.web_port} свободен")


def check_worker_slot(settings: Settings) -> CheckResult:
    from app.app.services.single_instance import AlreadyRunning, SingleInstance

    try:
        with SingleInstance("gpu_worker", settings.data_dir / "locks"):
            pass
    except AlreadyRunning as exc:
        return CheckResult("worker_slot", FAIL, str(exc), "воркер должен быть ровно один")
    return CheckResult("worker_slot", OK, "очередь свободна, другой воркер не запущен")


def check_ports_reserved() -> CheckResult:
    """Порт 8000 занят MCP 1С и не должен использоваться проектом."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        busy = sock.connect_ex(("127.0.0.1", 8000)) == 0
    detail = "8000 занят MCP 1С, как и ожидается" if busy else "8000 свободен"
    return CheckResult("mcp_port", OK, detail)


def _plan(settings: Settings, mode: str) -> list[tuple[str, object]]:
    plan: list[tuple[str, object]] = [
        ("python", check_python),
        ("env", lambda: check_env(settings)),
        ("data_dir", lambda: check_data_dir(settings)),
        ("database", lambda: check_database(settings)),
        ("personal_passwords", lambda: check_personal_passwords(settings)),
        ("companies", lambda: check_companies(settings)),
        ("vector_store", check_vector_store),
    ]
    if mode in {"all", "api"}:
        plan.append(("port", lambda: check_port(settings)))
        plan.append(("meeting_voice", lambda: check_meeting_voice(settings)))
    if mode in {"all", "worker"}:
        plan.append(("worker_slot", lambda: check_worker_slot(settings)))

    if settings.dry_run:
        plan.append(("dry_run", lambda: CheckResult("dry_run", WARN, "DRY_RUN=1: ASR и LLM не вызываются")))
    else:
        plan.extend(
            [
                ("llm", lambda: check_llm(settings)),
                ("ollama", lambda: check_ollama(settings)),
                ("asr", lambda: check_asr(settings)),
                ("ffmpeg", check_ffmpeg),
                ("gpu", check_gpu),
            ]
        )
    plan.append(("mcp_port", check_ports_reserved))
    return plan


def _log(item: CheckResult) -> None:
    log_event(
        logger,
        f"check.{item.status}",
        level=_LEVELS[item.status],
        name=item.name,
        detail=item.detail,
        **({"hint": item.hint} if item.hint else {}),
        **item.extra,
    )


def check_meeting_voice(settings: Settings) -> CheckResult:
    if not settings.meeting_voice_server:
        return CheckResult("meeting_voice", OK, "чистка комнаты выключена")
    from app.app.services.edge_runtime import VOICE_CLEANER, VOICE_PY, voice_venv_ready

    if not VOICE_PY.is_file() or not VOICE_CLEANER.is_file():
        return CheckResult(
            "meeting_voice",
            WARN,
            "нет venvs/voice, комната будет без серверной очистки",
            hint="venv\\Scripts\\python.exe scripts\\install_meeting_voice.py",
        )
    if not voice_venv_ready():
        return CheckResult(
            "meeting_voice",
            WARN,
            "venvs/voice без torchaudio/DeepFilterNet, чистильщик не стартует",
            hint="venv\\Scripts\\python.exe scripts\\install_meeting_voice.py",
        )
    return CheckResult("meeting_voice", OK, "venvs/voice готов, main поднимет чистильщик")


def run_preflight(settings: Settings, *, mode: str = "all") -> list[CheckResult]:
    """Выполнить проверки, логируя каждую сразу по завершении."""
    plan = _plan(settings, mode)
    log_event(logger, "preflight.start", mode=mode, checks=len(plan))

    results: list[CheckResult] = []
    for name, func in plan:
        try:
            item = func()
        except Exception as exc:  # проверка не должна ронять запуск сама по себе
            item = CheckResult(name, FAIL, f"проверка не выполнилась: {exc}")
        _log(item)
        results.append(item)
    return results


def summarize(results: list[CheckResult]) -> tuple[int, int]:
    failed = sum(1 for item in results if item.status == FAIL)
    warned = sum(1 for item in results if item.status == WARN)
    log_event(
        logger,
        "preflight.done",
        level=logging.ERROR if failed else logging.INFO,
        ok=sum(1 for item in results if item.status == OK),
        warn=warned,
        fail=failed,
    )
    return failed, warned
