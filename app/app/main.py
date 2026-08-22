from __future__ import annotations

import sys
from pathlib import Path

# Запуск файлом (`python app/app/main.py`) не кладёт корень проекта в sys.path.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Settings кешируется при первом импорте app.app.db, поэтому `.env` должен быть
# готов раньше любого импорта конфигурации.
_NEW_CREDENTIALS: dict[str, str] = {}
if __name__ == "__main__":
    from app.app.bootstrap import ensure_env

    _NEW_CREDENTIALS = ensure_env()

import logging  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402

from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.app.config import Settings, get_settings  # noqa: E402
from app.app.db import Base, get_db, get_engine  # noqa: E402
from app.app.logging_setup import log_event, setup_logging  # noqa: E402
from app.app.models.job import Job  # noqa: E402
from app.app.routers import auth, knowledge, pages, project_mcp, projects, speaker_profiles, upload  # noqa: E402
from app.app.services.gpu_phase import nvidia_memory_used_mib  # noqa: E402
from app.app.services.llm import check_llm_provider, provider_uses_lmstudio  # noqa: E402
from app.app.services.ollama_client import ollama_tags  # noqa: E402
from app.app.services.vector_index import init_vector_store  # noqa: E402

logger = logging.getLogger("skkkk.api")
startup_logger = logging.getLogger("skkkk.startup")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    setup_logging("api", settings.data_dir / "logs")
    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    from app.app.db import SessionLocal, apply_schema_migrations
    from app.app.services.project_tasks import cleanup_stale_tasks

    apply_schema_migrations()
    db = SessionLocal()
    try:
        cleanup_stale_tasks(db)
    finally:
        db.close()
    try:
        init_vector_store(engine)
        vector_store = "ready"
    except Exception as exc:
        logger.warning("vector store init skipped: %s", exc)
        vector_store = f"unavailable ({exc})"
    log_event(
        logger,
        "api.started",
        data_dir=settings.data_dir,
        database=settings.database_url,
        dry_run=settings.dry_run,
        vector_store=vector_store,
    )
    yield
    log_event(logger, "api.stopped")


app = FastAPI(title="Primary Analytics", lifespan=lifespan)
static_dir = Path(__file__).resolve().parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(pages.router)
app.include_router(auth.router)
app.include_router(projects.router)
app.include_router(upload.router)
app.include_router(knowledge.router)
app.include_router(project_mcp.router)
app.include_router(speaker_profiles.router)


@app.get("/health")
def health(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    queued = db.scalar(select(func.count()).select_from(Job).where(Job.status == "queued")) or 0
    running = db.scalar(select(func.count()).select_from(Job).where(Job.status == "running")) or 0
    lm_ok = False
    ollama_ok = False
    llm_detail = settings.llm_provider
    try:
        llm_check = check_llm_provider(settings)
        lm_ok = llm_check.status == "ok"
        llm_detail = llm_check.detail or settings.llm_provider
    except Exception:
        pass
    try:
        ollama_ok = bool(ollama_tags(settings))
    except Exception:
        pass
    return {
        "status": "ok",
        "database": "ok",
        "llm_provider": settings.llm_provider,
        "llm": lm_ok,
        "llm_detail": llm_detail,
        "lmstudio": lm_ok if provider_uses_lmstudio(settings) else None,
        "ollama": ollama_ok,
        "vram_mib": nvidia_memory_used_mib(),
        "queue": {"queued": queued, "running": running},
    }


def _print_credentials(passwords: dict[str, str], *, host: str, port: int) -> None:
    if not passwords:
        return
    print()
    print("=" * 52)
    print("  Пароли ролей Primary Analytics (показываются один раз)")
    print("=" * 52)
    for role, password in passwords.items():
        print(f"  {role:<8} {password}")
    print("=" * 52)
    print(f"  Вход: http://{host}:{port}/login")
    print("  Имя и Telegram ID — любые, пароль берётся из списка выше.")
    print("=" * 52)
    print()


def _sync_existing_users(passwords: dict[str, str]) -> int:
    """После перевыпуска паролей обновить уже созданных пользователей.

    Иначе аккаунт, заведённый под старым паролем роли, продолжит проверяться по
    собственному устаревшему хешу и вход не пройдёт.
    """
    from app.app.bootstrap import role_hashes
    from app.app.db import SessionLocal
    from app.app.models.user import User

    hashes = role_hashes()
    db = SessionLocal()
    try:
        Base.metadata.create_all(bind=get_engine())
        updated = 0
        for user in db.scalars(select(User)).all():
            new_hash = hashes.get(user.role)
            if new_hash and user.password_hash != new_hash:
                user.password_hash = new_hash
                updated += 1
        db.commit()
        return updated
    finally:
        db.close()


def _preflight(mode: str, *, skip: bool, host: str, port: int) -> int:
    """Прогнать проверку систем. Возвращает код выхода, 0 — можно стартовать."""
    settings = get_settings()
    setup_logging("worker" if mode == "worker" else "api", settings.data_dir / "logs")
    if skip:
        log_event(startup_logger, "preflight.skipped", mode=mode, reason="--skip-checks")
        return 0

    from app.app.preflight import run_preflight, summarize
    from urllib.request import urlopen
    from urllib.error import URLError
    import ssl

    settings = settings.model_copy(update={"web_host": host, "web_port": port})
    results = run_preflight(settings, mode=mode)
    failed, _warned = summarize(results)
    if not failed:
        return 0

    # Если preflight упёрся только в "уже запущено" (порт занят + воркер-лок),
    # то запуска второй раз не требуется: текущий экземпляр уже обслуживает.
    failed_names = {r.name for r in results if r.status == "fail"}
    allowed = {"port", "worker_slot"}
    if failed_names and failed_names.issubset(allowed):
        try:
            ctx = ssl.create_default_context()
            with urlopen(f"http://{host}:{port}/health", timeout=2, context=ctx) as resp:
                body = resp.read().decode("utf-8", "replace")
            if "status" in body and "ok" in body:
                log_event(
                    startup_logger,
                    "startup.already_running",
                    level=logging.INFO,
                    failed_checks=list(failed_names),
                    hint="уже запущен API/воркер, повторный старт не нужен",
                )
                return 1
        except URLError:
            pass

    log_event(
        startup_logger,
        "startup.aborted",
        level=logging.ERROR,
        failed=failed,
        hint="исправьте отмеченные пункты или запустите с --skip-checks",
    )
    return 2


def _spawn_worker() -> subprocess.Popen:
    """Воркер поднимается отдельным процессом: у него свой цикл GPU-фаз."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(_PROJECT_ROOT), env.get("PYTHONPATH", "")]))
    process = subprocess.Popen(
        [sys.executable, "-m", "app.app.main", "worker", "--skip-checks", "--use-saved-llm"],
        cwd=str(_PROJECT_ROOT),
        env=env,
    )
    log_event(startup_logger, "worker.spawned", pid=process.pid)
    return process


def _stop_worker(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    log_event(startup_logger, "worker.stopping", pid=process.pid)
    if os.name == "nt":
        # venv\Scripts\python.exe — обёртка, реальный интерпретатор её потомок.
        # terminate() убил бы только обёртку, а воркер продолжил бы разбирать
        # очередь и держать блокировку.
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(process.pid)],
            capture_output=True,
            check=False,
        )
    else:
        process.terminate()
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        log_event(startup_logger, "worker.killed", level=logging.WARNING, pid=process.pid)
        process.kill()


def _serve(host: str, port: int, *, reload: bool) -> None:
    import uvicorn

    # При reload uvicorn перезапускает воркер по строке импорта; `.env` к этому
    # моменту уже подготовлен родительским процессом.
    target = "app.app.main:app" if reload else app
    uvicorn.run(target, host=host, port=port, log_level="info", reload=reload)


def _run_all(host: str, port: int, *, skip_checks: bool, reload: bool) -> int:
    code = _preflight("all", skip=skip_checks, host=host, port=port)
    if code == 1:
        return 0
    if code:
        return code

    _print_credentials(_NEW_CREDENTIALS, host=host, port=port)
    worker = _spawn_worker()
    try:
        _serve(host, port, reload=reload)
    except KeyboardInterrupt:
        log_event(startup_logger, "shutdown.requested")
    finally:
        _stop_worker(worker)
    return 0


def _run_api(host: str, port: int, *, skip_checks: bool, reload: bool) -> int:
    code = _preflight("api", skip=skip_checks, host=host, port=port)
    if code == 1:
        return 0
    if code:
        return code
    _print_credentials(_NEW_CREDENTIALS, host=host, port=port)
    _serve(host, port, reload=reload)
    return 0


def _run_worker(*, skip_checks: bool, host: str, port: int) -> int:
    code = _preflight("worker", skip=skip_checks, host=host, port=port)
    if code == 1:
        return 0
    if code:
        return code
    from app.app.workers.gpu_worker import main as worker_main

    return worker_main() or 0


def _run_check(host: str, port: int) -> int:
    settings = get_settings()
    setup_logging("api", settings.data_dir / "logs")

    from app.app.preflight import run_preflight, summarize

    settings = settings.model_copy(update={"web_host": host, "web_port": port})
    failed, _warned = summarize(run_preflight(settings, mode="all"))
    return 2 if failed else 0


def _run_credentials(*, reset: bool, host: str, port: int) -> None:
    from app.app.bootstrap import reset_role_passwords

    if reset:
        passwords = reset_role_passwords()
        get_settings.cache_clear()
        updated = _sync_existing_users(passwords)
        _print_credentials(passwords, host=host, port=port)
        if updated:
            print(f"Обновлено существующих пользователей: {updated}")
        return

    if _NEW_CREDENTIALS:
        _print_credentials(_NEW_CREDENTIALS, host=host, port=port)
        return

    print(
        "Пароли ролей уже заданы в .env, восстановить их из хеша нельзя.\n"
        "Перевыпустить: python -m app.app.main credentials --reset"
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="skkkk", description="Единая точка входа Primary Analytics")
    parser.add_argument(
        "mode",
        nargs="?",
        choices=("all", "api", "worker", "check", "credentials"),
        default="all",
        help=(
            "all — проверка систем, веб-портал и GPU-воркер (по умолчанию); "
            "api — только портал; worker — только воркер; "
            "check — только проверка систем; credentials — пароли ролей"
        ),
    )
    settings = get_settings()
    parser.add_argument("--host", default=settings.web_host)
    parser.add_argument("--port", type=int, default=settings.web_port)
    parser.add_argument("--reset", action="store_true", help="перевыпустить пароли ролей")
    parser.add_argument("--reload", action="store_true", help="автоперезапуск при правках кода")
    parser.add_argument("--skip-checks", action="store_true", help="пропустить проверку систем")
    parser.add_argument(
        "--llm",
        choices=("cursor", "gemma"),
        help="быстрый выбор LLM: cursor (API) или gemma (LM Studio); сохраняет в .env",
    )
    parser.add_argument(
        "--llm-provider",
        choices=("lmstudio", "cursor"),
        help="LLM: Gemma локально или Cursor API (устар.: используйте --llm gemma|cursor)",
    )
    parser.add_argument(
        "--use-saved-llm",
        action="store_true",
        help="не показывать выбор LLM, взять LLM_PROVIDER из .env",
    )
    args = parser.parse_args(argv)

    if args.mode in {"all", "api", "worker"}:
        from app.app.startup_llm import resolve_llm_provider_at_startup

        cli_choice = args.llm_provider
        if args.llm:
            cli_choice = "lmstudio" if args.llm == "gemma" else args.llm
        use_saved = args.use_saved_llm or (args.skip_checks and cli_choice is None)
        interactive = cli_choice is None and not use_saved

        provider = resolve_llm_provider_at_startup(
            cli_choice=cli_choice,
            use_saved=use_saved,
            interactive=interactive,
        )
        get_settings.cache_clear()
        log_event(
            startup_logger,
            "llm.provider",
            provider=provider,
            source=(
                "cli"
                if cli_choice
                else ("env" if use_saved else "prompt")
            ),
        )

    if args.mode == "all":
        return _run_all(args.host, args.port, skip_checks=args.skip_checks, reload=args.reload)
    if args.mode == "api":
        return _run_api(args.host, args.port, skip_checks=args.skip_checks, reload=args.reload)
    if args.mode == "worker":
        return _run_worker(skip_checks=args.skip_checks, host=args.host, port=args.port)
    if args.mode == "check":
        return _run_check(args.host, args.port)
    _run_credentials(reset=args.reset, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
