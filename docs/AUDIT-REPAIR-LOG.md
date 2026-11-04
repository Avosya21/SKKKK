# Журнал ремонта по AUDIT_REPORT.md

Все правки кода и документов по пакетному плану из `AUDIT_REPORT.md` фиксируются здесь. Не git-история: что изменили, зачем, что сознательно не трогали.

**Старт:** 2026-08-27  
**Правило:** следующий пакет только после зелёного контура и pytest затронутых тестов.

---

## Пакет 0 — опора тестов (2026-08-27)

**Цель:** явные фикстуры anonymous / cookie-сессия, без смены прод-поведения.

### Изменения

*   `app/app/tests/conftest.py`
    *   Константа `SESSION_COOKIE_NAME` = `primary_analitics_session` (совпадает с сервером).
    *   `anon_client` — TestClient без логина.
    *   `cookie_auth_client` — тот же клиент после `_login` (в cookie jar сессия).
    *   Комментарий: Bearer по-прежнему `auth_headers` / `admin_token`.

### Не трогали

*   Существующие фикстуры `client`, `admin_token`, `auth_headers` — поведение прежнее плюс cookie на клиенте после `_login` (пакет 1).

---

## Пакет 1 — двойная сессия, clip, Bootstrap с диска (2026-08-27)

**Цель:** ничего не отнять у уже залогиненных; убрать утечку `asr/work` по `?clip=`; не грузить Bootstrap с jsDelivr.

### Изменения

*   `app/app/dependencies.py`
    *   `SESSION_COOKIE_NAME = "primary_analitics_session"`.
    *   `set_session_cookie` / `clear_session_cookie`.
    *   `Secure` только если `request.url.scheme == https` или `X-Forwarded-Proto: https`. Не смотреть `WEB_PUBLIC_URL` (сейчас http).
    *   `get_current_user`: если есть `Authorization: Bearer` — только он (битый Bearer не падает на cookie). Иначе cookie. Иначе 401.
    *   TTL cookie = `jwt_expire_minutes` (720), не укорачивали.
*   `app/app/routers/auth.py`
    *   `POST /auth/login` ставит HttpOnly cookie + JSON `access_token` как раньше.
    *   `POST /auth/logout` сбрасывает cookie (можно без сессии).
*   `app/app/static/app.js`
    *   `clearSession()` шлёт `POST /auth/logout` (fire-and-forget), localStorage как раньше.
*   `app/app/routers/pages.py`
    *   Ветка `clip` удалена. Query `?clip=` игнорируется FastAPI (лишний query не 422). Всегда WAV/AAC **этого** артефакта.
*   `app/app/services/artifact_audio.py`
    *   two-voice HTML: `audio_url` без `?clip=240`.
*   `app/app/static/vendor/bootstrap/`
    *   Bootstrap 5.3.3: `bootstrap.min.css`, `bootstrap.bundle.min.js` (копия npm/jsDelivr, без SRI).
*   `app/app/templates/base.html`
    *   CSS и JS Bootstrap с `/static/vendor/bootstrap/`.
*   `app/app/templates/index.html`, `project.html`
    *   Убран bootstrap bundle с jsDelivr. FullCalendar на project по-прежнему CDN (не этот пакет).
*   Тесты: `test_auth.py`, `test_audio_playback.py`, `test_session_cookie.py`.
*   `docs/PROJECT-MAP.md` §5.2 / §5.8: cookie+Bearer, logout, clip не читает `asr/work`.
*   `docs/TESTING.md`: фикстуры cookie/anon.
*   `docs/project-map/manifest.yaml`: `updated`.

### Не трогали (сознательно)

*   Auth на `/report`, `/audio`, speaker API — пакет 2.
*   CSP, короткий JWT, `Secure` на http, PyJWT, autoprovisioning.
*   `app.js` `Authorization` из localStorage — оставлен.
*   FullCalendar CDN.

### Проверка

```
pytest app/app/tests/test_auth.py app/app/tests/test_session_cookie.py app/app/tests/test_audio_playback.py app/app/tests/test_artifact_download.py app/app/tests/test_projects.py app/app/tests/test_bootstrap.py app/app/tests/test_lab_report.py app/app/tests/test_lab_enroll.py -q
```

Результат 2026-08-27: 22 + 36 passed (перечисленные файлы). `scripts/index_project_map.py` — 295 чанков в `data/project_map.db`.

---

## Пакет 2 — периметр HTML и API (2026-08-27)

**Цель:** отчёты, `/audio`, speaker API и HTML-оболочки требуют сессию. Анонимный HTML — 302 на `/login`, API — 401. Не ломать cookie+Bearer.

### Изменения

*   `require_html_user` в `dependencies.py`: те же credentials, что `get_current_user`, но 401 → 302 `Location: /login`.
*   `pages.py`: `/login` публичный; `/`, проект, граф/ТЗ/чат, `/report*`, lab HTML — `require_html_user`. `/admin/conflicts` после сессии проверяет `role == admin` (403, не 401). `/audio` и lab `file/` — `get_current_user` (401).
*   `speaker_profiles.py`: `dependencies=[Depends(get_current_user)]`. 500 без путей; `FileNotFoundError` → `detail="Not found"`.
*   Тесты: `test_perimeter.py`; audio/lab HTTP берут сессию.

### Не трогали

*   Cookie `Secure` на http, CSP, укорочение JWT.

---

## Пакет 3 — воркер и Telegram (2026-08-27)

*   `notify_artifact_user`: шлёт на `users.telegram_id`, не SQLite PK; пустой/нечисловой id — skip.
*   `gpu_worker` except: `rollback`, затем если есть стенограмма и voice-gate не пройден → `speakers_pending`, иначе `transcript_ready` / `error`.

---

## Пакет 4 / 7 — Docker и lab-логи, без Whisper fallback (2026-08-27)

*   `ASR_CONTAINERS = ("primaryanalitics-gigaam",)`; `docker compose up -d gigaam`.
*   `asr/docker-compose.yml`: whisper и tone — `profiles: [legacy]`.
*   `asr/run_pipeline.py`: без `ALLOW_LEGACY_ASR=1` поднимает только GigaAM.
*   `pipeline.py` else: нет lab run → `error` «нужен повтор лабораторной расшифровки», **не** `process_audio_artifact`.
*   `_run_cmd`: stdout/stderr в `data/logs/lab_steps/`, не `capture_output` в RAM.
*   Список артефактов больше не вызывает `ensure_audio_report` на каждый poll.

---

## Пакет 5 — upload complete (2026-08-27)

*   `copy2` в dest → регистрация в БД → `cleanup_session`. При ошибке регистрации dest удаляется, part остаётся. Unique `(project_id, sha256)` **не** вводили (тесты и данные с `"0"*64`).

---

## Пакет 6 — health (2026-08-27)

*   `GET /health` → `{"status":"ok"}` без очереди/VRAM/LLM.
*   `GET /health/ready` — те же детали, что раньше был `/health`, только с сессией.
*   Бейдж LLM в `app.js` читает `/health/ready` (cookie + Bearer).

---

## Пакет 8 / 9 / 10 — очередь, FK, knowledge, MCP (2026-08-27)

*   `claim_next_job`: `BEGIN IMMEDIATE` после commit; если SQLite уже в транзакции — продолжаем с `UPDATE … WHERE status=queued`. Orphan `attempts >= max` → `error`.
*   `PRAGMA foreign_keys=ON` в `db.py`.
*   Search `limit` 1..50; notes `NoteRefIn` + файл `user_note_{ts}_{user.id}.txt` внутри notes dir; `create_link` проверяет проект и принадлежность сущностей; resolve winner ∈ {source, target}; `value_json` ≤ 32768; чат при local Gemma и running job → 409 «GPU занят расшифровкой».
*   `GET …/mcp/info` — admin/manager. Карточка MCP скрыта для роли `user`.
*   Логи: `RotatingFileHandler` 20 MiB × 5.

### Сознательно не делали

*   Unique sha256, Alembic, PyJWT вместо python-jose, выключение auto-provisioning, CSP, короткий JWT, TTL MCP, split `speaker_profiles.py`, cosine name-boost.
*   `/health/ready` по-прежнему дергает LLM/Ollama (бейдж ~10 с) — не ускоряли, только закрыли утечку очереди/VRAM с `/health`.

---

## Пользовательские сценарии

Файл `app/app/tests/test_user_scenarios.py` — 30 API-сценариев (вход, права, загрузка, очередь dry-run, периметр audio/report/download, поиск, заметки, граф, ТЗ, дедлайны, чат, 409 GPU, health, MCP, конфликты, архив).

### Ошибки, найденные при прогоне suite (исправлены сразу)

*   `GET /health/ready` падал с `NameError: Job` — импорт `Job` потерялся при выносе ready. Вернули.
*   `should_continue_asr` смотрел `projects/.../tmp/{id}` вместо `artifacts/{id}` — прерванный ASR не находился. Перевели на `artifact_dir`.
*   `POST rebuild-summary/reindex` без сессии стали 401 (периметр). Тесты ждут 404 только с JWT; добавлены 401 для анонима.

Полный `pytest app -m "not gpu and not slow"`: сначала 3 красных (выше), после правок — догон этих файлов. Живой процесс на :777 **не подхватывает** этот код, пока портал не перезапустят.

---

## Пакет CSRF / Host (2026-11-03)

**Цель:** закрыть остатки приоритета 2 из внешнего ТЗ (DeepSeek) без ломки DECISIONS (TTL 30 дней, без `session_version`).

### Изменения

*   `app/app/middleware/hosts.py`, `csrf.py`, `trusted_host.py` — allowlist Host; CSRF Origin/Referer на мутациях с cookie.
*   `main.py` — оба middleware; `config.allowed_hosts` / `ALLOWED_HOSTS`.
*   Тесты: `test_csrf_host.py`; SameSite в `test_session_cookie.py`; MCP A↛B в `test_project_mcp.py`.
*   DECISIONS: политика аудита (INSERT + purge-исключение) и список отложенного hardening.

### Не трогали

*   Отказ от Bearer в localStorage, CSP на report HTML.

---

## Пакет session_version / аудит / TTL 8ч (2026-11-03)

*   `JWT_EXPIRE_MINUTES=480`; JWT `sv` + `portal_settings.session_version`; bump при `credentials --reset` и `POST /admin/users/sessions/revoke-all`.
*   `user_activity_log`: поля `login`/`profile_id`; SQLite-триггеры append-only; purge не удаляет журнал.
*   Тесты: `test_security_remaining.py`.

