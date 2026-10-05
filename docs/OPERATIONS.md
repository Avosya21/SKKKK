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
| `main` или `main all` | проверка систем, Caddy, LiveKit, запись голоса (`skkkk-dfn`), портал и воркер |
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
Шумодав голоса в комнате — **в браузере говорящего** (DFN3, `static/vendor/dfn3`, ставит `scripts/fetch_meeting_dfn3.py`): процессор livekit-client на микрофоне, слушатели получают одну очищенную дорожку, кнопка «Шумодав» включает и выключает фильтр у себя. `MEETING_VOICE_DFN3=0` — везде шумоподавление браузера. На телефоне и без WASM — тоже шумоподавление браузера. Рядом браузер публикует сырую дорожку `skkkk-raw` (захват до DFN3) — её слушает только рекордер. ASR / `01_audio` не трогает.

Рекордер голоса `voice_cleaner` (участник `skkkk-dfn`, имя историческое; ставит `venv\Scripts\python.exe scripts\install_meeting_voice.py`, нужен только livekit SDK) пишет **сырые** дорожки спикеров (`skkkk-raw`, без неё — основную) в `data/meetings/{room_name}/tracks/*.wav` и `manifest.json` (все файлы одной комнаты = один созвон). Очищенный звук в архив не кладётся. `MEETING_RECORD=0` — без записи. `events.jsonl` появляется после реального входа в LiveKit, не после API `join`. Видео шаринга пишет отдельный `skkkk-rec` (`tools/livekit/screen_recorder.py`) в ту же `tracks/` (`{identity}-screen_video-{sid}.mp4`), `MEETING_SCREEN_RECORD=0` выключает файлы.

**Потолок:** перезапуск `voice_cleaner` посреди живой встречи открывает WAV заново (`wave.open(..., "wb")`) и обрезает уже записанное. То же для `screen_recorder` (`skkkk-rec`): mid-call рестарт бросает текущий ffmpeg и начинает новый mp4. Код рекордеров менять только между встречами; в комнате — дождаться «Завершить». После смены кода перезапустить портал (или только `voice_cleaner` / `screen_recorder`).

Пороги проверок шкалы и протечек — группа Meeting в `.env.example`: `MEETING_CHECK_OK_SEC`, `MEETING_CHECK_WARN_SEC`, `MEETING_HEAD_LOSS_WARN_SEC`, `MEETING_GAP_MUTE_WINDOW_SEC`, `MEETING_RMS_TIE_RATIO`, `MEETING_SPEECH_MIN_SEC`, `MEETING_SPEECH_RMS_MIN`.
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

На форме `/login` **имя — логин**: он должен быть в `Login_up.txt` в корне проекта (одна строка — один логин, `#` и пустые строки пропускаются). Регистр не важен: `DEMO_USER` = `demo_user`. Файл читается при каждом входе — правки подхватываются без перезапуска. Поле Telegram ID на форме пока скрыто (решение владельца 2026-09-28: уведомлений в Telegram нет — бот не настроен); если Telegram ID передать, он сохраняется у учётки и на вход не влияет. Пароль роли с учётом регистра: какой пароль ввели — такая роль, в том числе при повторном входе (роль на учётке перезаписывается). Путь к списку можно сменить через `LOGIN_ALLOWLIST_PATH`.

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

### Перезапуск и признак устаревшего кода

- `scripts\restart.ps1` — безопасный перезапуск: проверяет Ollama, модель в LM Studio и порт, запускает через `start.ps1`, ждёт нового воркера и `/health`; при отказе пишет причину. Провайдер берётся из сохранённого выбора (`--llm gemma|cursor` его меняет и пишет в `.env`).
- Серверы работают без `--reload`: правки `.py`, шаблонов и `.env` не действуют до перезапуска. Админу в шапке показывается жёлтый значок «Нужен перезапуск» (`/health/ready` → `build`, отдельно для API и воркера).
- Обход зависшего reasoning (`LMSTUDIO_NO_THINK`, по умолчанию `1`) проверяется при старте: если модель/шаблон другие и обход даёт мусор, вызовы сами идут через обычный chat-эндпоинт с увеличенным бюджетом токенов. Статус — `/health/ready` → `llm_no_think`.
- Оценка качества: `scripts\eval_tz.py` и `scripts\eval_chat.py` (см. `docs/eval/`); на клиентских проектах — только чтение.

### Проверки качества и здоровья

- `scripts\qa_all.py` — одна команда: весь pytest, смоук на живом сервере (нужны `SMOKE_ADMIN_PW`/`SMOKE_MANAGER_PW`), аудит целостности данных, отчёт здоровья. Флаги: `--per-file` (каждый тест-файл отдельно), `--evals` (eval ТЗ и чата на ПИЛОТЕ, только чтение). Отчёт — `data/eval/qa_*.json`.
- `scripts\health_report.py` — ошибки и предупреждения логов с последнего запуска, отказы входа, GPU-задания, метрики LLM, очередь, JS-ошибки клиентов. Код выхода 1 при красных признаках.
- JS-ошибки браузеров приходят на `POST /client-log` (не более 5 на страницу, 20/мин на IP) и пишутся событием `client.js_error`.
- `scriptsudit_integrity.py` — «сироты» в БД и на диске (только чтение).
- Бэкап: `scriptsackup_db.ps1` (копия БД, хранится 7) → проверка `scriptserify_backup.py` (integrity_check, набор таблиц, восстановление во временную папку). Папки `data\projects` копируются вручную (около 3 ГБ): `robocopy data\projects dataackups\projects_ГГГГ-ММ-ДД /E`. Раз в сутки достаточно; расписание задаёт оператор (в SKKKK планировщика нет).
- Зависимость `livekit-api` обязательна для API: без неё завершение встречи не закрывает комнату LiveKit (`close_livekit_room`).

## Перезапуск и идущие встречи

`scripts\restart.ps1` отказывается перезапускать сервер, если идёт встреча (статус live и есть участник на связи за последние 10 минут): перезапуск обрывает процессы записи голоса. Дождитесь конца встречи или запустите с `-Force` (запись оборвётся; рекордер продолжит дорожки, а не затрёт их, но пропущенные секунды будут тишиной). Проверка вручную: `venv\Scripts\python.exe scripts\live_meetings.py` (код 3 = идёт встреча).

## Индекс проекта

Поиск по коду, документам, связям, БД, окружению и истории (для людей и ИИ-агентов). План: `docs/plans/PROJECT-INDEX-PLAN.md`. Хранилище — `data/project_map.db` (восстановимо из кода, в резервные копии не входит), лог — `data/logs/index.log`.

| Действие | Команда |
|---|---|
| Обновить индекс (только изменённые файлы) | `venv\Scripts\python.exe scripts\index_project_map.py` |
| Пересобрать всё (кэш эмбеддингов сохраняется) | `venv\Scripts\python.exe scripts\index_project_map.py --full` |
| Только структура и точный поиск, без Ollama | `... index_project_map.py --no-embed` |
| Поиск | `venv\Scripts\python.exe scripts\search_project_map.py "вопрос"` (режимы: `--help`) |
| Диагностика | `venv\Scripts\python.exe scripts\index_doctor.py` |
| Расхождения карты и кода | `venv\Scripts\python.exe scripts\check_map_drift.py` (`--fix` — реестр модулей и узлы манифеста) |
| Граф зависимостей: полный граф, свод по подсистемам, атлас | `venv\Scripts\python.exe scripts\export_project_graph.py` (см. §16 карты; `--stats` — только цифры) |
| Качество поиска | `venv\Scripts\python.exe scripts\eval_index.py --show-misses` (набор: `docs/eval/index_questions.json`) |
| Git-хуки: индекс после коммита, проверка карты и личных данных перед коммитом | `powershell -ExecutionPolicy Bypass -File scripts\install_hooks.ps1` (снять: `-Remove`; ставятся сами из `INSTALL.bat` и `restart.ps1`; `scripts\index_doctor.py` показывает, стоят ли) |
| Проверка карты по индексу git (то, что запускает `pre-commit`) | `venv\Scripts\python.exe scripts\check_map_staged.py` (`--all` — даже без файлов кода в индексе) |

- Эмбеддинги считает Ollama `bge-m3` на CPU (GPU не занимается); первая полная сборка — около получаса, дальше секунды: неизменённые фрагменты берутся из кэша. Если Ollama не запущена, точный поиск работает, смысловой — после следующего запуска индексатора.
- Индексатор один: повторный запуск во время работы ждёт и сообщает. Лок-файл `data/project_map.lock`, устаревший снимается сам.
- Если поиск пишет «индекс отстаёт» — запустите индексатор (или `search_project_map.py --auto`). `index_doctor.py` показывает возраст индекса, число устаревших файлов и фрагментов без вектора.
- В индекс не попадают `.env`, `Login_up.txt`, `*.db`, ключи; значения похожих на секреты строк маскируются. Список — `scripts/index_config.yaml`.
- Страница `/admin/map` (только admin) и MCP-сервер `skkkk-index` (`.mcp.json`, только чтение, локально по stdio) — те же режимы.
- Граф зависимостей (§16 карты): полный граф — `data/project_map_graph.json` (рабочий артефакт, в git не хранится), свод — `docs/project-map/graph-modules.json`, атлас — `docs/project-map/DEPENDENCIES.md`, страница — `/admin/map/graph` (только admin). Пересобирать после правок кода: `scripts\export_project_graph.py`. Вопросы к графу из терминала — `search_project_map.py --graph ИМЯ --depth 2`, `--deps ФАЙЛ`, `--orphans`, `--import-cycles`.

## Перенос проекта на другой ПК

1. Скопировать папку целиком (`tools\libreoffice`, `tools\tesseract`, `tools\ffmpeg` — портативные, едут вместе с ней).
2. Поставить Python 3.10 (`winget install Python.Python.3.10`), драйвер NVIDIA, Docker, Ollama/LM Studio.
3. `powershell -ExecutionPolicy Bypass -File scripts\setup_env.ps1` — пересоздаёт `venv\` (по `app\requirements.txt`) и `venvs\*` (по `scripts\requirements_lock\*.txt`), проверяет `tools\`, правит пути в `.env`.
   Окружения `venv`/`venvs` копировать нельзя: в них зашит путь к Python.
