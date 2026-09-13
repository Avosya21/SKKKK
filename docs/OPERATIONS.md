# Эксплуатация SKKKK

Корень: `C:\1C_NEW_SKKKK`.

## Запуск

Весь проект поднимается одной командой. Она готовит `.env`, прогоняет проверку всех
систем и запускает веб-портал вместе с GPU-воркером:

```powershell
cd C:\1C_NEW_SKKKK
.\venv\Scripts\python.exe -m app.app.main
```

Режимы `main`:

| Команда | Что делает |
|---|---|
| `main` или `main all` | проверка систем, Caddy, LiveKit, очистка голоса (`skkkk-dfn`), портал и воркер |
| `main check` | только проверка систем, код возврата 2 при провале |
| `main api` | Caddy + LiveKit + очистка голоса + веб-портал |
| `main worker` | только GPU-воркер |
| `main credentials --reset` | перевыпуск паролей ролей |

Флаги: `--host`, `--port`, `--reload` (автоперезапуск при правках), `--skip-checks`
(пропустить проверку систем), `--use-saved-llm` (без вопроса, взять `LLM_PROVIDER` из `.env`).

В консоли PyCharm и в обычном терминале перед стартом спрашивается режим: **1** — LM Studio (`LMSTUDIO_MODEL`), **2** — Cursor API, Enter — как в `.env`. Службы NSSM запускают с `--use-saved-llm`.

## Проверка систем перед стартом

Перед запуском выполняется 13 проверок, каждая пишется в тот же лог, что и остальная
работа. Оценка `fail` останавливает запуск с кодом 2, `warn` только предупреждает.

```
15:22:36 INFO    skkkk.preflight preflight.start  mode=all checks=13
15:22:36 INFO    skkkk.preflight check.ok         name=database detail="таблиц 17"
15:22:37 INFO    skkkk.preflight check.ok         name=lmstudio detail="модель qwen/qwen3.8-27b загружена"
15:22:41 WARNING skkkk.preflight check.warn       name=asr detail="отвечает t-one; не отвечают whisper, gigaam"
15:22:42 INFO    skkkk.preflight preflight.done   ok=10 warn=3 fail=0
```

Что проверяется: версия Python, `.env` и пароли ролей, права на запись и место в
`data`, база и схема, расширение sqlite-vec, свободный порт портала, отсутствие
второго воркера, LM Studio с нужной моделью, Ollama с `bge-m3`, три ASR-движка,
`ffmpeg`/`ffprobe`, свободная VRAM, порт 8000 под MCP 1С.

При `DRY_RUN=1` внешние сервисы не проверяются — вместо них одно предупреждение.

## Службы

| Служба | Команда | Назначение |
|---|---|---|
| SKKKK-API | `python -m app.app.main api --host 127.0.0.1 --port 1111` | Веб-портал |
| SKKKK-GPU | `python -m app.app.main worker` | Очередь jobs, ASR/LLM |
| SKKKK-CADDY | `caddy run --config tools\caddy\Caddyfile` | HTTPS наружу, прокси на портал |

Установка через NSSM: `scripts\install_services.ps1` (от администратора).

## HTTPS наружу

Портал слушает loopback, наружу смотрит Caddy: он же обратный прокси, он же ACME-клиент,
он же автопродление. Сертификат Let's Encrypt выдаётся **на IP-адрес**, домен не нужен —
но такие сертификаты обязаны быть короткоживущими (профиль `shortlived`, 160 часов),
поэтому машина должна быть онлайн. Снаружи открыты `:80` (проверка сертификата → Caddy `:8088`)
и `:777` (HTTPS). Порт 443 не используем. Гостевая встреча: `https://<хост>:777/m/{token}`
(страница без входа в портал; сырой токен в БД не хранится).
LiveKit — обычный процесс `tools/livekit/livekit-server.exe`, не Docker.
Очистка голоса в комнате — **на сервере**: `venv\Scripts\python.exe scripts\install_meeting_voice.py`, затем перезапуск портала. Процесс `voice_cleaner` (CPU, `skkkk-dfn`) чистит каждую дорожку для всех. Нет `venvs/voice` — обычный звук. ASR / `01_audio` не трогает. Сила DFN: `MEETING_VOICE_ATTEN_LIM` (дБ, по умолчанию 20; `0` = полная модель). `MEETING_VOICE_POST_FILTER=1` — жёстче. Клиентский DFN3 не включать (`MEETING_VOICE_DFN3=0`) — иначе двойной денойз.

Тот же процесс пишет **сырые** дорожки спикеров в `data/meetings/{room_name}/tracks/*.wav` и `manifest.json` (все файлы одной комнаты = один созвон). Чистый звук `skkkk-dfn` в архив не кладётся. `MEETING_RECORD=0` — без записи. После смены кода перезапустить портал (или только `voice_cleaner`).
Docker Desktop на Windows ломает WebRTC (STUN отвечает адресом `172.24.0.1`).
Сигналинг через `:777`. LiveKit рекламирует `SKKKK_PUBLIC_HOST` как ICE `node_ip` (не STUN: исходящий IP у машины другой). Медиа: TCP 7881, UDP 50000, TURN 3478 / relay 50001–50011.

Порядок установки (от администратора):

```powershell
cd C:\1C_NEW_SKKKK
# 1. В .env: SKKKK_PUBLIC_HOST, SKKKK_ACME_EMAIL и staging-адрес в SKKKK_ACME_CA
.\scripts\install_caddy.ps1
nssm start SKKKK-CADDY
# 2. Убедиться, что сертификат выдан, затем вернуть боевой SKKKK_ACME_CA и перезапустить
```

После успешного staging сразу верните боевой `SKKKK_ACME_CA`
(`https://acme-v02.api.letsencrypt.org/directory`) и перезапустите Caddy.
Staging Chrome не доверяет — страница будет «Не защищено».

Проверка: `https://<SKKKK_PUBLIC_HOST>:777/health`. Журнал — `data\logs\caddy.log`,
сертификаты и состояние ACME — `data\caddy\`.
`main all` / `main api` при старте смотрит файл `SKKKK_TLS_CERT` (или каталог Caddy для текущего CA):
нет файла, просрочен, staging или осталось меньше `SKKKK_TLS_RENEW_HOURS` часов — перевыпуск на `:8443`.
Журнал: `edge.tls.ok` / `edge.tls.reissue` / `edge.tls.renewed` / `edge.tls.failed`.

Роутер:

- внешний `:80` → `127.0.0.1:8088` (Caddy HTTP-01 + редирект на https://…:777)
- внешний `:777` → `127.0.0.1:777` (Caddy HTTPS → портал `:1777`)

Если оставить проброс `80 → 777`, проверка сертификата сломается: на 777 уже HTTPS.
На этой машине Apache2.4 слушает локальный `:80` и проксирует только
`/.well-known/acme-challenge/` в Caddy `:8088` (`tools/caddy/apache-acme.conf`).
Сайт на `:777` отдаёт готовый файл сертификата (Chrome к IP часто без SNI).
Боевой сертификат Caddy выпускает и продлевает на `:8443`. Staging Chrome не доверяет.

Ручной запуск:

```powershell
cd C:\1C_NEW_SKKKK
.\scripts\run_all.ps1      # проверка систем + портал + воркер
.\scripts\check_systems.ps1  # только проверка
.\scripts\run_api.ps1      # только портал
.\scripts\run_worker.ps1   # только воркер
```

Службы NSSM запускают `api` и `worker` по отдельности, потому что перезапускаются
независимо. Для ручной работы удобнее `run_all.ps1`.

## Пароли ролей

`.env` готовится автоматически при первом запуске `python -m app.app.main`: генерируются
`SECRET_KEY` и пароли ролей `admin`/`manager`/`user`, пароли один раз печатаются в консоль.
В `.env` хранятся только bcrypt-хеши, поэтому показать пароль повторно нельзя.

```powershell
python -m app.app.main credentials --reset
```

Команда перевыпускает все три пароля, печатает их и приводит `password_hash` уже созданных
пользователей к новым значениям — иначе старые аккаунты продолжат проверяться по прежнему хешу.

На форме `/login` **имя — логин**: он должен быть в `Login_up.txt` в корне проекта (одна строка — один логин, `#` и пустые строки пропускаются). Регистр не важен: `DEMO_USER` = `demo_user`. Файл читается при каждом входе — правки подхватываются без перезапуска. Telegram ID просто сохраняется и на вход не влияет. Пароль роли с учётом регистра: какой пароль ввели — такая роль, в том числе при повторном входе (роль на учётке перезаписывается). Путь к списку можно сменить через `LOGIN_ALLOWLIST_PATH`.

## Зависимости

Перед GPU-воркером должны быть доступны:

- LM Studio (`http://127.0.0.1:1234/v1/models`), модель из `LMSTUDIO_MODEL` (`qwen/qwen3.8-27b`), JIT выключен
- Ollama (`http://127.0.0.1:11434/api/tags`), модель `bge-m3`, `num_gpu=0`
- ASR-контейнеры (`docker compose -f asr/docker-compose.yml up -d`)

## Логи

API и воркер пишут одновременно в консоль и в файл, поэтому весь ход работы виден
прямо в окне запуска (в том числе в консоли PyCharm).

- `data\logs\api.log` — вход в систему, загрузка файлов, постановка в очередь
- `data\logs\worker.log` — обработка: статусы артефакта, ASR, сущности, индекс, граф, ТЗ
- ASR/LLM — см. `asr/out`, LM Studio

События пишутся одной строкой вида `событие ключ=значение`, что удобно грепать:

```
15:03:26 INFO skkkk.worker    job.claimed        job_id=3 artifact=art_... file=03-change-request.txt
15:04:13 INFO skkkk.documents document.normalized artifact=art_... mode=llm chars=1000
15:05:36 INFO skkkk.knowledge index.stored       artifact=art_... chunks=1 model=bge-m3
15:05:38 INFO skkkk.graph     graph.seeded       artifact=art_... entities=12 related=3 conflicts=1
15:07:14 INFO skkkk.tz        tz.rebuild.done    project_id=1 version=2026-08-19_1507 chars=2879
```

Пароли в логи не попадают: при входе пишется роль и итог проверки, а `log_event`
вырезает поля `password`, `token`, `secret` и `authorization`.

## Один воркер

GPU-воркер держит файловую блокировку `data\locks\gpu_worker.lock`. Второй запуск
завершается с кодом 1 и сообщением о том, какой pid уже занял очередь. Это защищает
от параллельной записи в SQLite: два воркера ломают обработку ошибкой
`database is locked`.

Проверить полный цикл на тестовых документах:

```powershell
$env:SMOKE_PASSWORD = '<пароль роли>'
python scripts\smoke_cycle.py --base-url http://127.0.0.1:1111 <файлы>
```

## Проверка

```powershell
.\scripts\check_env.ps1
curl http://127.0.0.1:1111/health
pytest app -m "not gpu" -q
```

## Бэкап БД

```powershell
.\scripts\backup_db.ps1
```

Копии: `data\backups\app-YYYY-MM-DD_HHMM.db` (хранится 7 последних).

## Обновление приложения

1. Остановить `SKKKK-API` и `SKKKK-GPU`
2. Обновить код (`git pull`)
3. `venv\Scripts\pip install -r app\requirements.txt`
4. Запустить службы снова

Очередь `jobs` сохраняется в SQLite — необработанные задачи продолжатся после старта воркера.

## Типичные проблемы

| Симптом | Что проверить |
|---|---|
| Портал не открывается | Служба SKKKK-API, порт 1111 |
| `/login` отвечает 401 | Имя уже есть и хранит другой хеш роли: верный пароль этой учётки или `credentials --reset` |
| Файлы висят в uploaded | SKKKK-GPU, `worker.log`, LM Studio/Ollama |
| `database is locked` | Запущено два воркера: проверить `data\locks\gpu_worker.pid` и остановить лишний |
| Поиск и чат ничего не находят | `SEARCH_MIN_SCORE` (по умолчанию 0.35); `SIMILARITY_THRESHOLD` относится к графу, не к поиску |
| ASR падает по VRAM | Gemma выгружена (`lms unload --all`), JIT выключен |
| Чат «идёт транскрипция» | Дождаться завершения ASR job |
| После перезагрузки GPU недоступен | Служба под учёткой с доступом к GPU и активной сессией для LM Studio |
