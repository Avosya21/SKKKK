from __future__ import annotations

import sys
from pathlib import Path

from starlette.middleware.cors import CORSMiddleware

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
import asyncio  # noqa: E402
import subprocess  # noqa: E4021
from contextlib import asynccontextmanager  # noqa: E402

import json  # noqa: E402

from fastapi import Depends, FastAPI, Request, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.app.config import Settings, get_settings  # noqa: E402
from app.app.db import Base, get_db, get_engine  # noqa: E402
from app.app.logging_setup import log_event, setup_logging  # noqa: E402
from app.app.dependencies import get_current_user  # noqa: E402
from app.app.models.job import Job  # noqa: E402
from app.app.models.user import User  # noqa: E402
from app.app.routers import (  # noqa: E402
    acme,
    admin_map,
    auth,
    knowledge,
    meetings,
    pages,
    project_mcp,
    requirement_review,
    project_members,
    projects,
    speaker_profiles,
    upload,
)
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
    import app.app.models  # noqa: F401 — зарегистрировать все таблицы, включая project_members
    Base.metadata.create_all(bind=engine)
    from app.app.db import SessionLocal, apply_schema_migrations
    from app.app.services.project_tasks import abandon_running_api_tasks, cleanup_stale_tasks

    apply_schema_migrations()
    db = SessionLocal()
    try:
        abandoned = abandon_running_api_tasks(db)
        cleanup_stale_tasks(db)
        from app.app.services.user_activity import backfill_chat_activity
        from app.app.services.user_identity import ensure_login_name_unique_index, merge_duplicate_logins

        merged = merge_duplicate_logins(db)
        ensure_login_name_unique_index()
        backfilled = backfill_chat_activity(db)
        from app.app.services.graph import purge_orphan_links

        orphan_links = purge_orphan_links(db)
        if orphan_links:
            db.commit()
        if abandoned:
            log_event(logger, "api.abandoned_tasks", count=abandoned)
        if merged:
            log_event(logger, "users.merged_duplicate_logins", count=merged)
        if backfilled:
            log_event(logger, "activity.chat_backfill", count=backfilled)
        if orphan_links:
            log_event(logger, "graph.orphan_links.startup_purged", count=orphan_links)
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
    from app.app.services.llm import announce_llm_and_selfcheck

    announce_llm_and_selfcheck(settings, logger)

    async def _stale_loop():
        from app.app.models.meeting import STATUS_LIVE, Meeting
        from app.app.services.meetings import mark_stale_participants
        while True:
            await asyncio.sleep(30)
            def _cleanup() -> None:
                # Синхронная работа с БД не должна блокировать event loop.
                db = SessionLocal()
                try:
                    for meeting in db.query(Meeting).filter(Meeting.status == STATUS_LIVE).all():
                        mark_stale_participants(db, meeting)
                finally:
                    db.close()

            try:
                await asyncio.to_thread(_cleanup)
            except Exception as exc:
                from app.app.db import describe_pool_holders

                logger.warning("stale participant cleanup failed: %s\n%s", exc, describe_pool_holders())

    async def _edge_watch_loop():
        from app.app.services.edge_runtime import check_served_certificate, watch_edge_services

        await asyncio.sleep(15)
        tick = 0
        while True:
            await asyncio.sleep(30)
            tick += 1
            if tick % 10 == 1:  # раз в ~5 минут: отдаваемый :777 сертификат совпадает с файлом
                try:
                    state = await asyncio.to_thread(check_served_certificate, get_settings())
                    if state in ("reloaded", "failed"):
                        log_event(logger, "edge.tls.watch", state=state)
                except Exception as exc:
                    logger.warning("tls certificate check failed: %s", exc)
            try:
                statuses = watch_edge_services()
                restarted = [name for name, state in statuses.items() if state == "started"]
                if restarted:
                    log_event(logger, "edge.watch.restarted", services=restarted)
            except Exception as exc:
                logger.warning("edge watchdog failed: %s", exc)

    from app.app.services.edge_runtime import edge_disabled

    stale_task = asyncio.create_task(_stale_loop())
    edge_watch_task = asyncio.create_task(_edge_watch_loop()) if not edge_disabled() else asyncio.create_task(asyncio.sleep(0))
    try:
        yield
    finally:
        stale_task.cancel()
        edge_watch_task.cancel()
        await asyncio.gather(stale_task, edge_watch_task, return_exceptions=True)
    log_event(logger, "api.stopped")




# =====================================================================


app = FastAPI(title="Primary Analytics", lifespan=lifespan)


from fastapi.exception_handlers import http_exception_handler  # noqa: E402
from starlette.exceptions import HTTPException as StarletteHTTPException  # noqa: E402

_PAGE_ERRORS = {
    403: ("Недостаточно прав", "У вас нет доступа к этой странице. Если он нужен, попросите администратора проекта выдать его."),
    404: ("Страница не найдена", "Такой страницы нет, либо у вас нет к ней доступа. Проверьте адрес или вернитесь к списку проектов."),
}


@app.exception_handler(StarletteHTTPException)
async def html_error_pages(request: Request, exc: StarletteHTTPException):
    """Браузеру, открывшему страницу, показываем понятную страницу вместо сырого JSON; запросы скриптов (fetch) получают JSON как раньше."""
    if (
        exc.status_code in _PAGE_ERRORS
        and "text/html" in (request.headers.get("accept") or "")
        and not request.url.path.startswith(("/api/", "/mcp/", "/static/"))
    ):
        from app.app.routers.pages import templates

        heading, message = _PAGE_ERRORS[exc.status_code]
        return templates.TemplateResponse(request, "error.html", {"title": heading, "code": exc.status_code, "heading": heading, "message": message}, status_code=exc.status_code)
    return await http_exception_handler(request, exc)


# Заголовки безопасности (пункт плана 4.7). Без HSTS (сертификат на IP живёт 6 дней — при сбое продления HSTS
# заблокировал бы пользователей) и без CSP (страницы используют встроенные скрипты).
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    # Гостевая ссылка /m/{token} содержит секрет: не отдаём её во Referer внешним сайтам.
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(self), microphone=(self), display-capture=(self), geolocation=()",
}


@app.middleware("http")
async def add_security_headers(request, call_next):
    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    return response


static_dir = Path(__file__).resolve().parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(acme.router)
app.include_router(pages.router)
app.include_router(auth.router)
app.include_router(projects.router)
app.include_router(upload.router)
app.include_router(knowledge.router)
app.include_router(project_mcp.router)
app.include_router(requirement_review.router)
app.include_router(admin_map.router)
app.include_router(project_members.router)
app.include_router(meetings.router)
app.include_router(speaker_profiles.router)


_CLIENT_LOG_LIMITER = None


@app.post("/client-log", status_code=204)
async def client_log(request: Request):
    """Приём JS-ошибок из браузеров (пункт плана 5.2). Без входа (гость тоже): размер и частота ограничены."""
    from app.app.services.rate_limit import FailureLimiter

    global _CLIENT_LOG_LIMITER
    if _CLIENT_LOG_LIMITER is None:
        _CLIENT_LOG_LIMITER = FailureLimiter(limit=20, window_sec=60)
    ip = "clog:" + (request.client.host if request.client else "?")
    if _CLIENT_LOG_LIMITER.retry_after(ip):
        return Response(status_code=204)
    _CLIENT_LOG_LIMITER.fail(ip)
    try:
        body = json.loads((await request.body())[:4000] or b"{}")
    except ValueError:
        return Response(status_code=204)
    clean = lambda v, n: " ".join(str(v or "").split())[:n]  # noqa: E731  (без переводов строк: защита от подделки записей лога)
    log_event(logger, "client.js_error", page=clean(body.get("page"), 120), message=clean(body.get("message"), 300),
              source=clean(body.get("source"), 160), ua=clean(request.headers.get("user-agent"), 80))
    return Response(status_code=204)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/health/ready")
def health_ready(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user: User = Depends(get_current_user),
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
        "build": _build_for(_user),
        "llm_no_think": _no_think_for(_user),
    }


def _no_think_for(user: User) -> dict:
    from app.app.services.llm import no_think_status

    data = no_think_status()
    return data if user.role == "admin" else {"enabled": data["enabled"]}


def _build_for(user: User) -> dict:
    from app.app.services.build_info import build_status

    data = build_status()
    if user.role != "admin":  # имена файлов — только админу
        data = {"stale": data["stale"]}
    return data


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


def _ensure_edge() -> None:
    """Сертификат :777 (перевыпуск если надо), Caddy, LiveKit. Ошибка комнаты не валит портал."""
    from app.app.services.edge_runtime import ensure_edge_services

    try:
        ensure_edge_services(get_settings())
    except Exception as exc:
        log_event(startup_logger, "edge.failed", level=logging.WARNING, error=str(exc))


def _run_all(host: str, port: int, *, skip_checks: bool, reload: bool) -> int:
    code = _preflight("all", skip=skip_checks, host=host, port=port)
    if code == 1:
        return 0
    if code:
        return code

    _print_credentials(_NEW_CREDENTIALS, host=host, port=port)
    _ensure_edge()
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
    _ensure_edge()
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
            "all — проверка, Caddy, LiveKit, очистка голоса, портал и GPU-воркер (по умолчанию); "
            "api — Caddy, LiveKit, очистка голоса и портал; worker — только воркер; "
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
