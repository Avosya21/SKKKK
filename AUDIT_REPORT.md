# PrimaryAnalitics — глубокий технический аудит кодовой базы

**Дата съёмки:** 2026-08-27  
**Контур:** монолит FastAPI + GPU-воркер + SQLite WAL + lab-ASR (Parakeet/Sortformer/GigaAM) + LLM (Cursor/LM Studio) + Ollama bge-m3  
**Метод:** чтение исходников по цепочкам вызовов (роутер → сервис → ORM/FS → subprocess/HTTP), сверка с `docs/PROJECT-MAP.md` / `docs/DECISIONS.md`, без предположений о «задуманном поведении», которого нет в коде.  
**Вне скоупа:** планы спринтов, готовые diff. По каждому шагу ниже — **план** и **как выполнить** (файлы, инварианты, проверки).  
**Совместимость:** шаги сверены с текущим UX/API (2026-08-27). Пакетная реализация «как написано» **сломает портал**. Условия безболезненного ремонта — раздел в конце файла.

Стек фактически: Python 3.10 (lab venv) / 3.11 (заявлено в PLAN), FastAPI 0.141, SQLAlchemy 2, SQLite `data/app.db`, Jinja2 + Bootstrap с CDN, один процесс API и один GPU-воркер. Бизнес: внутренний портал памяти проекта (~20 сотрудников) с аудиосозвонами, сущностями, ТЗ и RAG-чатом.

**Инвариант угрозы:** в карте проекта фигурирует `WEB_PUBLIC_URL=http://203.0.113.10:777`. Если процесс слушает не loopback, все Blocker/High ниже — эксплуатационные, не лабораторные.

---

## Модуль A. HTTP-периметр и аутентификация

### 🚨 [Критичность: Blocker] | [Категория: Безопасность]
*   **Локация:** `app/app/routers/pages.py` — `artifact_report_page` (стр. ~233–257), `artifact_sortformer_report_page`, `artifact_raw_diar_report_page`, `artifact_speaker_match_page`, `artifact_two_voice_diar_page`, `artifact_lab_report_page`, `artifact_lab_file`, `artifact_audio_file` (стр. ~124–155). Отсутствует `Depends(get_current_user)`.
*   **Суть дефекта:** канонические HTML-стенограммы, match-отчёты, lab-снимки, произвольные файлы прогона и поток аудио отдаются анонимным GET.
*   **Чем вызвана (Первопричина / Root Cause):** портальные страницы (`/`, `/projects/{id}`) закрыты **только клиентским** `PrimaryAnalitics.requireAuth()` в шаблонах (`app/app/templates/index.html:28`, `project.html:754`). Сгенерированный `transcript.html` — не Jinja-шелл, а `HTMLResponse` сырого файла с диска (`report_path.read_text`). В нём нет `requireAuth`. Исторически плеер `<audio src="/artifacts/{id}/audio">` не умеет слать `Authorization`; вместо cookie/media-token авторы оставили GET открытым. `artifact_id` вида `art_YYYY-MM-DD_HHMMSS_{6 hex}` попадает в ТЗ, чат, MCP `list_artifacts` и URL. Скачивание MP3/txt/docx в тех же файлах **защищено** JWT (`artifact_download_file` и соседние) — асимметрия внутри одного роутера доказывает, что забыли не «весь модуль», а именно просмотр.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* любой TCP-клиент, знающий id, получает полный текст созвона и PCM/AAC в теле ответа. Браузер не участвует. Lab `file/{relative}` ограничен `LabRun.resolve` (path escape закрыт тестом), но **всё внутри прогона** (WAV 16 kHz, json proof, npy теплокарты) читается без сессии.
    - *При нагрузке:* нет auth → нет квоты на пользователя. `ensure_audio_report` / `ensure_lab_report` на GET могут пересобрать HTML (lab — `importlib.reload` + полный rebuild). 10k анонимных хитов по разным id — диск + CPU uvicorn worker (по умолчанию 1 поток обработки sync-handler), event loop блокируется чтением больших HTML.
    - *Для бизнеса:* утечка переговоров, ПДн, коммерческой тайны. При публичном IP — состав правонарушения по персональным данным без факта взлома пароля. Доверие заказчика к «закрытому порталу» обнуляется одним URL.
*   **Шаги решения:**
    1. Ввести серверную сессию (httpOnly cookie с JWT или отдельный signed cookie) — Bearer из localStorage не попадает в `<audio src>` и в навигационный GET.
        - **План:** Сессия должна жить в cookie, которую браузер шлёт на GET HTML и на <audio src>, без Authorization.
        - **Как выполнить:** В `auth.py` login: `Set-Cookie` (HttpOnly, Secure на HTTPS, SameSite=Lax, path=/). В `dependencies.py` `get_current_user`: сначала cookie, иначе Bearer. В `app.js` убрать запись JWT в localStorage (оставить только на переходный период). Logout: `Set-Cookie` с Max-Age=0.
    2. На все GET в `pages.py` для `/artifacts/{id}/report*`, `/audio`, `/lab/.../file` повесить `Depends(get_current_user)` (или проверку cookie).
        - **План:** Анонимный GET канонических байтов созвона должен давать 401/302, авторизованный — 200.
        - **Как выполнить:** В `pages.py` на `artifact_report_page`, sortformer/raw/match/two-voice/lab report, `artifact_lab_file`, `artifact_audio_file` добавить `Depends(get_current_user)` (или общий `_require_page_user`, читающий cookie). Не трогать статику `/static`.
    3. Для плеера: либо тот же cookie (SameSite=Lax), либо одноразовый media-token в query с TTL 5–15 мин, выписываемый только авторизованным.
        - **План:** Плеер не умеет Bearer; cookie закроет обычный src. Media-token — запасной путь для сторонних клиентов.
        - **Как выполнить:** Предпочтительно cookie. Если нужен query-token: эндпоинт `POST /artifacts/{id}/audio-token` (auth) пишет одноразовый HMAC в redis/sqlite TTL 5–15 мин; `artifact_audio_file` принимает `?t=` XOR cookie. Не класть JWT в query.
    4. В сгенерированный `transcript.html` не класть секреты; audio URL строить относительным — cookie уйдёт сам.
        - **План:** Сгенерированный HTML не должен содержать токен или абсолютный URL с секретом.
        - **Как выполнить:** В `asr/transcript_view.py` / lab report builder: `src="/artifacts/{id}/audio"` относительный. Убрать любые inline token. Cookie уйдёт сама на same-origin.
    5. Инвертировать тесты: без сессии 401, с сессией 200. Обновить `test_lab_report.py` / `test_audio_playback.py`.
        - **План:** CI не должен закреплять анонимный 200 на report/audio.
        - **Как выполнить:** В `test_lab_report.py` и `test_audio_playback.py`: клиент без cookie → 401. Фикстура логина (как в `test_artifact_download.py`) → 200. Прогнать `pytest app/app/tests/test_lab_report.py test_audio_playback.py test_artifact_download.py`.
    6. Проверить, что архивный проект (`is_active=False`) тоже 404 на report.
        - **План:** Архив проекта не должен отдавать report по старому URL.
        - **Как выполнить:** В тех же GET: JOIN/load `Project`, если `is_active is False` — 404 (не 401), чтобы не палить существование. Тест: archive_project → GET report 404 даже с сессией.

### 🚨 [Критичность: Blocker] | [Категория: Безопасность]
*   **Локация:** `app/app/routers/pages.py` `artifact_audio_file` стр. ~135–142, ветка `clip`.
*   **Суть дефекта:** query `?clip=1` (любое `clip > 0` и `<= 240`) отдаёт **общий** файл `asr/work/input_16k_4min.wav`, не клип запрошенного артефакта.
*   **Чем вызвана (Первопричина / Root Cause):** docker-stage кладёт текущий WAV в mount `asr/work` для контейнеров. Legacy-ветка «4 минуты превью» читает этот staging, не `artifact_dir/.../audio`. Идентификатор артефакта в URL игнорируется после проверки, что запись вообще audio.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* клиент A запрашивает audio артефакта B с `clip=` и получает кусок **последнего** прогона, который воркер скопировал в `asr/work`. Межпроектная утечка на уровне inode.
    - *При нагрузке:* файл один на хост; параллельный ASR перезаписывает его — гонка чтения/записи, битый WAV или чужой фрагмент.
    - *Для бизнеса:* несанкционированный доступ к чужому созвону даже без знания «правильного» id содержимого.
*   **Шаги решения:**
    1. Удалить ветку `clip` целиком **или** резать только WAV **этого** артефакта через `ensure_audio_wav` + ffmpeg `-t 240` во временный файл в `artifact_dir`, не `asr/work`.
        - **План:** Клип не должен читать чужой staging `asr/work`; либо фичи нет.
        - **Как выполнить:** В `pages.py` `artifact_audio_file` удалить ветку `clip` **или** после `ensure_audio_wav(artifact)` резать ffmpeg `-t 240` в `artifact_dir/audio/preview_4min.wav` и отдавать только его. Не открывать `asr/work`.
    2. Запретить чтение `asr/work/*` из HTTP-роутеров.
        - **План:** HTTP-слой не имеет права читать docker-mount ASR.
        - **Как выполнить:** Grep `asr/work` по `app/app/routers/`. Оставить чтение только воркеру (`pipeline_lab` / docker_runtime). Тест: FileNotFound на отсутствие work-файла не должен менять ответ audio.
    3. Тест: два артефакта, `clip=` на первом не отдаёт байты второго.
        - **План:** Регрессия межпроектной утечки должна ломаться в CI, не на проде.
        - **Как выполнить:** Два fixture-артефакта с разным WAV; GET `.../audio?clip=1` первого, сравнить sha256 тела со вторым — не равны. Если clip удалён — тест 422/игнор query.

### 🚨 [Критичность: Blocker] | [Категория: Безопасность]
*   **Локация:** `app/app/routers/speaker_profiles.py` — весь роутер (`prefix="/api"`). Функции: `artifact_active_tasks`, `api_list_profiles`, `api_enroll_speaker`, `api_merge_speakers`, `api_lab_enroll_speaker`, `api_lab_merge_speakers`, `api_recalculate_speakers`, `api_speakers_status`, `api_speakers_confirm`, `api_rebuild_summary`, `api_reindex_artifact`. Импорт `get_current_user` **отсутствует** (grep по файлу пуст).
*   **Суть дефекта:** мутации голосов, подтверждение спикеров (старт knowledge-job), пересборка LLM-выжимки и переиндексация Ollama доступны без JWT.
*   **Чем вызвана (Первопричина / Root Cause):** отчёты `/report` и `/report-speaker-match` — автономный HTML. Fetch из них к `/api/artifacts/...` не проходил бы CORS/Authorization, если бы сервер требовал Bearer, а в HTML нет `PrimaryAnalitics.api`. Вместо cookie-сессии или вшитого токена слой auth сняли целиком. UI (`project.html` `bindSpeakerConfirmButtons`, `app.js` `PrimaryAnalitics.api`) токен всё же шлёт — мёртвый заголовок, сервер его не читает. Тесты закрепляют дыру: `app/app/tests/test_lab_enroll.py`, `test_lab_report.py`, `test_audio_playback.py` ходят без `Authorization`.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* POST enroll пишет `speaker_profiles` (BLOB эмбеддинга), переписывает `docker_gigaam_speakers.json` и HTML. POST confirm при `speakers_pending` вставляет `jobs(phase=knowledge)`. POST rebuild-summary вызывает LLM минуты. Исключения отдаются клиенту `detail=str(exc)` (стр. ~100–102, 177–179).
    - *При нагрузке:* нет rate limit. Параллельные enroll/reindex бьют в тот же SQLite writer, что и GPU-воркер → `database is locked`, срыв очереди ASR. LLM/Cursor — прямой финансовый и GPU-расход с анонимного IP.
    - *Для бизнеса:* порча реестра голосов (подмена имён в протоколах), запуск тяжёлых job, счета Cursor API, искажение юридически значимой стенограммы.
*   **Шаги решения:**
    1. На каждый handler в `speaker_profiles.py` добавить `Depends(get_current_user)`.
        - **План:** Мутации спикеров и knowledge-кнопки — только с сессией.
        - **Как выполнить:** В `speaker_profiles.py` (роутер) импорт `get_current_user`; на каждый handler `user: Annotated[User, Depends(get_current_user)]`. Не менять сигнатуры сервиса, только HTTP.
    2. Чтобы fetch из `/report` работал: cookie-сессия (предпочтительно) или прокинуть токен в HTML только как non-httpOnly нельзя — лучше cookie + SameSite.
        - **План:** Fetch из автономного `/report` должен нести cookie, не токен в HTML.
        - **Как выполнить:** После cookie (A1) `fetch('/api/...')` same-origin отправит cookie сам. Не вшивать JWT в transcript.html. SameSite=Lax достаточно для GET/POST с портала.
    3. `except Exception`: в клиент только generic 500, детали в `logger.exception`.
        - **План:** Клиент не должен видеть traceback и пути диска.
        - **Как выполнить:** Во всех `except Exception` роутера: `logger.exception(...)`; `raise HTTPException(500, detail="Internal Server Error")`. ValueError с безопасным текстом — 400.
    4. Тесты: 401 без токена/cookie на enroll, merge, confirm, reindex, rebuild-summary; 200 с user JWT.
        - **План:** Тесты больше не кодируют дыру.
        - **Как выполнить:** Параметризовать enroll/merge/confirm/reindex/rebuild-summary: без cookie 401, с user JWT/cookie 200/409. Обновить `test_lab_enroll.py`, `test_lab_report.py`.
    5. После auth — rate limit на LLM-кнопки (summary/reindex).
        - **План:** Даже авторизованный не должен долбить Cursor/Ollama без лимита.
        - **Как выполнить:** Общий лимитер (slowapi или счётчик в `project_tasks`) на `rebuild-summary` и `reindex`: N/час на user. 429 с Retry-After. Тест: третий вызов 429.

### 🚨 [Критичность: High] | [Категория: Безопасность]
*   **Локация:** `app/app/routers/pages.py` — `index_page`, `project_page`, `graph_page`, `tz_page`, `chat_page`, `conflicts_page` (стр. ~51–106). Ни один HTML-шелл не зависит от `get_current_user`.
*   **Суть дефекта:** сервер отдаёт каркас портала, включая `/admin/conflicts`, любому. «Пароль на главной» — иллюзия JS.
*   **Чем вызвана (Первопричина / Root Cause):** SPA-привычка: HTML статичен, API с JWT. `HTTPBearer` не читает cookie; сессия только `localStorage` (`app/app/static/app.js` `setSession`). Серверный шаблон не может проверить Bearer, его нет в первом навигационном GET. Решение «проверим в JS» принято и не доведено до reports (см. Blocker выше).
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* HTML главной без данных проектов безвреден (API `/projects` с JWT). `/admin/conflicts` без JS всё равно шелл; данные конфликтов — отдельный API с `require_role("admin")`. Главный вред — ложное чувство периметра у оператора.
    - *При нагрузке:* пренебрежимо.
    - *Для бизнеса:* при аудите ИБ фиксируется «auth только на клиенте» — типовой провал PCI/ISO-чеклистов, даже если API части закрыты.
*   **Шаги решения:**
    1. Middleware или dependency на HTML-шеллы: нет валидной cookie/сессии → 302 `/login` (не JS).
        - **План:** Каркас портала не отдаётся анониму.
        - **Как выполнить:** Dependency `require_html_session` на `index_page`, `project_page`, `graph_page`, `tz_page`, `chat_page`, `conflicts_page`: нет cookie → 302 `/login` (или 401 JSON если Accept). Страница логина без этой зависимости.
    2. `/admin/conflicts` дополнительно `require_role("admin")` на сервере.
        - **План:** Админ-шелл не для роли user.
        - **Как выполнить:** На `conflicts_page` дополнительно `Depends(require_role("admin"))` — 403 с сессией user. Данные API conflicts уже admin; выровнять HTML.
    3. Оставить `requireAuth()` в JS как UX, не как периметр.
        - **План:** JS-проверка остаётся подсказкой, не периметром.
        - **Как выполнить:** Оставить `PrimaryAnalitics.requireAuth()` в шаблонах для UX редиректа, не убирать до cookie. Не считать это закрытием дыры.

### 🚨 [Критичность: High] | [Категория: Безопасность]
*   **Локация:** `app/app/routers/auth.py` `login`; `app/app/dependencies.py` `authenticate_new_user`; `app/app/models/user.py`.
*   **Суть дефекта:** три bcrypt-хеша ролей в `.env`. Несуществующий `telegram_id` + пароль роли → INSERT пользователя с этой ролью. Нет lockout, нет rate limit, нет капчи.
*   **Чем вызвана (Первопричина / Root Cause):** `docs/PLAN.md` §2 явно описывает эту модель для ~20 человек без IdP. `lru_cache` на Settings заставил читать хеши с диска при логине (комментарий в `dependencies.py`) — усложнение вокруг той же трёхпарольной схемы, не замена её. Существующий пользователь **не** эскалируется чужим паролем роли (`test_login_existing_user_role_not_escalated`) — защита только на повторный вход, не на провижининг.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* bcrypt ~100ms на попытку; без лимита CPU на логине. Успех — JWT на 720 минут (`jwt_expire_minutes` в `config.py`).
    - *При нагрузке:* 10k login/s упрётся в bcrypt и SQLite writes при регистрации. DoS CPU, не « fort Knox ».
    - *Для бизнеса:* один утекший admin-пароль роли = неограниченные учётки. На белом IP это удалённый захват портала.
*   **Шаги решения:**
    1. Выключить автопровижининг: новый `telegram_id` создаёт только admin (отдельный API) или первый логин только если таблица users пуста.
        - **План:** Несуществующий telegram_id не должен сам создавать User.
        - **Как выполнить:** В `authenticate_new_user` / `login`: если `select(User).where(telegram_id==...)` пуст — 401, кроме bootstrap: `count(users)==0` создаёт первого admin. Отдельный `POST /admin/users` (admin) для остальных.
    2. Rate limit `/auth/login` (IP + telegram_id), lockout после N неудач.
        - **План:** Брутфорс трёх ролевых паролей должен упираться в лимит, не в bcrypt.
        - **Как выполнить:** Счётчик неудач в sqlite (`login_failures`: ip, telegram_id, ts). После N=5 за 15 мин — 429. Очистка по TTL. Тест: 6-й login 429.
    3. Если портал на белом IP — reverse-proxy allowlist / VPN, не оставлять три пароля как единственный фактор.
        - **План:** Белый IP не компенсирует shared password, но режет анонимный интернет.
        - **Как выполнить:** Nginx/Caddy allowlist или WireGuard; в `preflight.py` предупреждение если bind не loopback и нет `TRUST_PROXY_ALLOWLIST`. Не код-пароль как VPN.
    4. Длинный срок: нормальный IdP (хотя бы per-user bcrypt, не shared role hash).
        - **План:** Долгосрочно — персональный секрет, не три хеша на фирму.
        - **Как выполнить:** Миграция: колонка `password_hash` на User; login сверяет её, fallback на role hash только если NULL (переход). Документ в DECISIONS. Не делать в том же PR, что выключение провижининга, если не нужно.

### 🚨 [Критичность: High] | [Категория: Безопасность]
*   **Локация:** `app/app/static/app.js` `setSession` / `token`; `app/app/dependencies.py` `create_access_token`; `app/app/config.py` `jwt_expire_minutes = 720`.
*   **Суть дефекта:** JWT в `localStorage`, TTL 12 часов, нет `jti`, нет revoke, нет logout на сервере. XSS на любой странице с `innerHTML` = кража сессии.
*   **Чем вызвана (Первопричина / Root Cause):** FastAPI `HTTPBearer` заточен под SPA. Cookie httpOnly не использовали, чтобы не решать CSRF. XSS на портале частично закрыт `escapeHtml` в `project.html`, но `/report` — чужой HTML-генератор (`asr/transcript_view.py`) без CSP.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* любой скрипт в origin читает `primary_analitics_token`. `role` в токене дублируется; `get_current_user` берёт роль из БД — подделка role в JWT сама по себе не эскалирует, но `sub` = user id.
    - *При нагрузке:* n/a.
    - *Для бизнеса:* украденный токен 12 часов = полный доступ API (проекты, чат, загрузка, MCP-выпуск для manager+).
*   **Шаги решения:**
    1. Перенести access token в httpOnly Secure cookie; CSRF: SameSite=Lax + проверка Origin на мутациях.
        - **План:** Access token недоступен JS.
        - **Как выполнить:** Cookie `primary_analitics_session` = JWT (или opaque id). `app.js` `PrimaryAnalitics.api`: `credentials: 'include'`, удалить `Authorization` header. CSRF: SameSite=Lax; на POST проверять `Origin`/`Referer` совпадает с host портала (`dependencies.py`).
    2. Укоротить TTL (15–60 мин) + refresh cookie или re-login.
        - **План:** 12 часов — слишком длинный stolen-token window.
        - **Как выполнить:** `jwt_expire_minutes` в config: 30–60. Refresh: вторая cookie `primary_analitics_refresh` (дольше, path=/auth/refresh) или повторный логин. Обновить `.env.example`.
    3. `jti` + таблица revoked при logout / смене SECRET_KEY.
        - **План:** Logout и смена SECRET_KEY должны инвалидировать живые JWT.
        - **Как выполнить:** В payload `jti=uuid4()`. Таблица `revoked_jti(jti PK, exp)`. Logout INSERT. `get_current_user` SELECT; промах — 401. Смена SECRET_KEY сама инвалидирует все HS256 без таблицы.
    4. CSP на портале и на report HTML, чтобы XSS не читал даже не-httpOnly остатки.
        - **План:** XSS не должен читать сессию; CSP снижает XSS.
        - **Как выполнить:** `base.html`: `Content-Security-Policy: default-src 'self'; img-src 'self' data:; media-src 'self'`. Report HTML — отдельный минимальный CSP. После vendor Bootstrap (A9) без CDN.

### 🚨 [Критичность: Medium] | [Категория: Безопасность]
*   **Локация:** `app/requirements.txt` `python-jose==3.5.0`, `ecdsa`, `rsa`, `passlib==1.7.4` (passlib нигде не импортируется); `app/app/dependencies.py` `from jose import jwt`.
*   **Суть дефекта:** JOSE-стек с историей CVE (алгоритм confusion, JWT bomb). Текущий код фиксирует `algorithms=["HS256"]` при decode — прямой algorithm confusion на вход закрыт. Библиотека слабо сопровождается; JWE RSA1_5 (issue #408) не используется, но зависимость живая. `passlib` — мёртвый груз.
*   **Чем вызвана (Первопричина / Root Cause):** шаблон FastAPI 2023 «python-jose + passlib». Хеши перенесли на raw bcrypt (`app/app/services/auth.py`), jose оставили.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* нативный ecdsa/rsa backend ставится всегда; cryptography тоже в дереве. Лишняя атакуемая поверхность в процессе API.
    - *При нагрузке:* пренебрежимо vs bcrypt.
    - *Для бизнеса:* при следующем advisory по jose патч может не выйти; придётся экстренно менять стек под живым JWT.
*   **Шаги решения:**
    1. Заменить `python-jose` на PyJWT, оставить только HS256.
        - **План:** Уйти на сопровождаемый JWT-стек с тем же HS256.
        - **Как выполнить:** `pip uninstall python-jose`; `PyJWT` в `app/requirements.txt`. В `dependencies.py` заменить `from jose import jwt` на `import jwt` (PyJWT). Алгоритм только HS256 encode/decode.
    2. Удалить неиспользуемые `passlib`, транзитивные `ecdsa`/`rsa` если уйдут сами.
        - **План:** Мёртвые crypto-зависимости не держать.
        - **Как выполнить:** Удалить `passlib` из requirements. После смены jose `pip check`; если `ecdsa`/`rsa` не импортируются в app — убрать явные pin, оставить транзитивные от того, что реально нужно.
    3. Прогнать `test_auth.py` (expiry, tamper).
        - **План:** Контракт логина не должен сломаться.
        - **Как выполнить:** `pytest app/app/tests/test_auth.py` — expiry, битый токен, роль из БД. Ручной login → cookie/JWT принимается новым decode.

### 🚨 [Критичность: Medium] | [Категория: Безопасность]
*   **Локация:** `app/app/main.py` `health` (стр. ~93–123); `app/app/static/app.js` `updateLlmBadge` бьёт в `/health` без токена.
*   **Суть дефекта:** анонимный endpoint отдаёт `llm_provider`, доступность LLM/Ollama, `vram_mib`, глубину очереди jobs.
*   **Чем вызвана (Первопричина / Root Cause):** нужен liveness для preflight и бейджа в навбаре. Не разделили `/health` (процесс жив) и `/health/ready` (секреты инфраструктуры).
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* `check_llm_provider` и `ollama_tags` на каждый poll бейджа + внешние мониторы. Синхронные HTTP из request thread.
    - *При нагрузке:* health как усилитель нагрузки на LM Studio/Ollama.
    - *Для бизнеса:* разведка стека перед атакой (VRAM, очередь, Cursor vs Gemma).
*   **Шаги решения:**
    1. `/health` — только `{"status":"ok"}` без LLM/VRAM/очереди.
        - **План:** Публичный liveness не раскрывает стек.
        - **Как выполнить:** В `main.py` `health`: вернуть `{"status":"ok"}` без `check_llm_provider`, nvidia-smi, очереди. Не звать Ollama.
    2. `/health/ready` за auth или только с loopback: провайдер, очередь, VRAM.
        - **План:** Диагностика — отдельный контур.
        - **Как выполнить:** Новый `/health/ready`: те же поля что сейчас в health. `Depends(get_current_user)` **или** reject если `request.client.host` не 127.0.0.1. Preflight бьёт в ready с loopback.
    3. Бейдж LLM в навбаре бить в ready с cookie, не в публичный health.
        - **План:** Бейдж навбара не должен ходить анонимно в старый health.
        - **Как выполнить:** В `app.js` `updateLlmBadge`: GET `/health/ready` с credentials. 401 — бейдж «нет сессии», не парсить публичный JSON.

### 🚨 [Критичность: Medium] | [Категория: Безопасность]
*   **Локация:** `app/app/templates/base.html` стр. 6–7 — Bootstrap CSS/JS с `cdn.jsdelivr.net` без SRI; `index.html` дополнительно грузит bundle с CDN.
*   **Суть дефекта:** supply-chain на каждый логин: компрометация CDN = XSS в origin портала = кража JWT из localStorage.
*   **Чем вызвана (Первопричина / Root Cause):** MVP на CDN, локальный вендор не положили.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* лишний DNS/TLS; при недоступности CDN ломается UI (модалка проекта).
    - *При нагрузке:* n/a.
    - *Для бизнеса:* классический вектор «внутренний портал с секретами созвонов, скрипты с общественного CDN».
*   **Шаги решения:**
    1. Скопировать Bootstrap CSS/JS в `app/app/static/vendor/`.
        - **План:** UI не зависит от jsDelivr.
        - **Как выполнить:** Скачать bootstrap.min.css/js той же мажорной версии, что в шаблонах, в `app/app/static/vendor/bootstrap/`. Зафиксировать версию в комментарии.
    2. Подключать только локально; убрать jsDelivr из `base.html` и `index.html`.
        - **План:** Портал грузит только /static.
        - **Как выполнить:** `base.html` и `index.html`: `url_for('static', path='vendor/...')`. Grep `jsdelivr` / `cdn.` по templates — пусто.
    3. При желании — SRI на локальные файлы не обязателен.
        - **План:** SRI на локальные файлы не нужен для закрытия CDN-вектора.
        - **Как выполнить:** Не добавлять integrity на свои же файлы, пока нет CDN. После CSP (A6) достаточно.

### 🚨 [Критичность: Medium] | [Категория: Безопасность]
*   **Локация:** `app/app/services/auth.py` `hash_password` / `verify_password` — `bcrypt.hashpw(password.encode())` без усечения; Pydantic `LoginRequest.password` только `min_length=1`.
*   **Суть дефекта:** bcrypt использует первые 72 байта. Длинные пароли, отличающиеся после 72-го байта, коллизируют. Нет max_length на пароле роли.
*   **Чем вызвана (Первопричина / Root Cause):** прямой bcrypt вместо passlib, который хотя бы документирует 72. Ролевые пароли генерируются 14 символов (`bootstrap.py`) — для них не бьёт; ручной длинный пароль в `.env` — бьёт.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* ложное «сильный пароль».
    - *При нагрузке:* огромное тело login — память request; bcrypt всё равно режет.
    - *Для бизнеса:* слабость политики паролей при формальном «длинном» секрете.
*   **Шаги решения:**
    1. На login отклонять пароль > 72 байт UTF-8 (400) или явно хешировать SHA-256→bcrypt (документировать).
        - **План:** Коллизия после 72 байт не должна быть «успешным логином».
        - **Как выполнить:** В `verify_password`/`login`: если `len(password.encode()) > 72` — 400 `password too long` **или** `hashpw(sha256(password), ...)`. Выбрать одно и написать в DECISIONS. Предпочтительно reject (ролевые пароли 14 символов).
    2. `Field(max_length=72)` на `LoginRequest.password` как минимум.
        - **План:** Схема запроса режет до bcrypt.
        - **Как выполнить:** `LoginRequest.password`: `Field(min_length=1, max_length=72)` (символы; дополнительно проверка байт в сервисе). OpenAPI отразит лимит.
    3. Не менять уже выпущенные 14-символьные пароли ролей.
        - **План:** Не инвалидировать текущие роли.
        - **Как выполнить:** Не трогать `bootstrap.py` генерацию 14 символов и существующие хеши в `.env`. Тест: пароль 80 байт → 400, 14 символов роли → 200.

### 🚨 [Критичность: Low] | [Категория: Безопасность]
*   **Локация:** `app/app/config.py` `secret_key: str = Field(min_length=8)`; `app/app/preflight.py` ловит `change-me`; `app/app/bootstrap.py` `ensure_env` переписывает короткий ключ.
*   **Суть дефекта:** нижняя граница 8 символов для HMAC-ключа JWT формально слабая, если bootstrap обойти (`--skip-checks` в `main.py`).
*   **Чем вызвана (Первопричина / Root Cause):** pydantic-валидатор слабее, чем preflight; два контура проверки.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* короткий SECRET_KEY → offline brute JWT.
    - *При нагрузке:* n/a.
    - *Для бизнеса:* компрометация всех сессий сразу.
*   **Шаги решения:**
    1. `Field(min_length=32)` на `secret_key`.
        - **План:** HMAC-ключ JWT формально не слабее 32 символов.
        - **Как выполнить:** В `config.py` `secret_key: str = Field(min_length=32)`. Упадёт pydantic на старте при коротком ключе.
    2. Preflight fail без `--skip-checks`, если ключ короткий или `change-me`.
        - **План:** Preflight ловит слабый ключ до bind порта.
        - **Как выполнить:** В `preflight.py`: fail если len<32 или `change-me` in key, если нет `--skip-checks`. Сообщение — сменить через bootstrap.
    3. Запретить старт API при слабом ключе даже в skip для prod-флага.
        - **План:** Prod-флаг не обходится skip.
        - **Как выполнить:** Если `WEB_PUBLIC_URL` не localhost или `PRIMARY_ANALITICS_ENV=prod`: игнорировать skip для secret_key, `sys.exit`. Тест preflight с коротким ключом.

---

## Модуль B. Модель данных и SQLite

### 🚨 [Критичность: High] | [Категория: Логика]
*   **Локация:** `app/app/db.py` `_set_sqlite_pragma` (стр. 28–40): WAL + `busy_timeout=15000`, **нет** `PRAGMA foreign_keys=ON`. Модели: `app/app/models/*.py` объявляют `ForeignKey` без `ondelete`.
*   **Суть дефекта:** SQLite по умолчанию не применяет FK. `create_all` создаёт ссылки как документацию, не как ограничение. Каскадов нет.
*   **Чем вызвана (Первопричина / Root Cause):** типичный недосмотр SQLAlchemy+SQLite. `load_sqlite_vec` на connect важнее казался авторам, чем FK. Alembic в requirements есть, каталога миграций нет — схема живёт `create_all` + `_MIGRATIONS` ADD COLUMN.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* DELETE/архив проекта (`projects.py` `archive_project` только `is_active=False`) оставляет artifacts/jobs/entities/chunks. Сирота `jobs.artifact_id` при ручном SQL. `note_refs.target_id` вообще без FK (`entity.py` `NoteRef`).
    - *При нагрузке:* рост `chunks`+`chunk_vectors` без сборки мусора. SQLite один writer; распухший файл → длинные lock.
    - *Для бизнеса:* «удалили проект» в UI — файлы в `data/projects/{id}/` и строки БД остаются. Утечка при копировании диска, ложные сущности в графе, если кто-то снимет is_active обратно несогласованно.
*   **Шаги решения:**
    1. В `_set_sqlite_pragma` добавить `PRAGMA foreign_keys=ON`.
        - **План:** SQLite должен реально проверять ForeignKey из моделей.
        - **Как выполнить:** В `_set_sqlite_pragma` после WAL: `cursor.execute("PRAGMA foreign_keys=ON")`. Проверка: вставить job с несуществующим artifact_id — IntegrityError.
    2. Прогнать существующую БД: найти сирот (jobs без artifacts и т.д.), почистить.
        - **План:** Включить FK на грязной БД нельзя без уборки сирот.
        - **Как выполнить:** Скрипт `scripts/db_orphans.py`: jobs без artifacts, chunks без artifact, links без entity. DELETE/карантин. Прогон на копии `data/app.db`.
    3. Проставить `ondelete` осознанно: jobs/entities/chunks CASCADE с artifact; projects — RESTRICT или явная процедура архива.
        - **План:** ondelete должен совпадать с продуктовым «архив ≠ purge».
        - **Как выполнить:** В моделях: Job/Entity/Chunk `ondelete="CASCADE"` от Artifact. Project: `RESTRICT` на artifacts (архив — soft). Alembic revision после autogenerate. Не CASCADE проект→артефакты пока нет процедуры purge.
    4. `NoteRef.target_id` — либо FK, либо проверка существования в сервисе.
        - **План:** NoteRef не должен ссылаться в никуда.
        - **Как выполнить:** Либо `ForeignKey` на entities.id, либо в `create_note` SELECT entity/chunk exists else 400. Предпочтительно проверка в сервисе + индекс target_id.

### 🚨 [Критичность: High] | [Категория: Архитектура]
*   **Локация:** `app/app/db.py` `Base.metadata.create_all` в `main.py` lifespan; `_MIGRATIONS` три колонки; `alembic==1.19.1` в requirements, **нет** `alembic.ini`.
*   **Суть дефекта:** нет воспроизводимой истории схемы. Два стенда разойдутся. Новые колонки, забытые в `_MIGRATIONS`, появятся только на пустой БД.
*   **Чем вызвана (Первопричина / Root Cause):** скорость MVP: «таблица появится сама». Alembic добавили в pin «на будущее» и не включили.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* `OperationalError` на старой БД после деплоя кода с новым mapped_column.
    - *При нагрузке:* n/a.
    - *Для бизнеса:* простой портала, ручной ALTER на проде, риск потери данных при «пересоздать db».
*   **Шаги решения:**
    1. `alembic init`, revision `--autogenerate` от текущих моделей как baseline.
        - **План:** Схема версионируется, а не «создастся сама».
        - **Как выполнить:** `alembic init alembic` в `app/`. env.py: URL из Settings. Revision `--autogenerate` от текущего metadata = baseline stamp на проде (`alembic stamp`).
    2. Убрать `create_all` из прод-lifespan (оставить в тестах) или вызывать upgrade.
        - **План:** create_all в прод-lifespan маскирует забытые миграции.
        - **Как выполнить:** В `main.py` lifespan: `alembic upgrade head` (или subprocess), `create_all` только если `testing`/пустая CI sqlite. Тесты могут оставить create_all.
    3. Перенести `_MIGRATIONS` в одну alembic revision и удалить самодельный ALTER.
        - **План:** Самодельный ALTER дублирует alembic.
        - **Как выполнить:** Перенести `_MIGRATIONS` ADD COLUMN в одну revision с `IF NOT EXISTS` семантикой уже применённых колонок; удалить цикл `_MIGRATIONS` из `db.py`.

### 🚨 [Критичность: Medium] | [Категория: Логика]
*   **Локация:** `app/app/models/artifact.py` `sha256` — index, не UniqueConstraint. `app/app/services/upload.py` `find_duplicate` — SELECT по `(project_id, sha256)` на уровне приложения.
*   **Суть дефекта:** дедуп не сериализован уникальным индексом. Два concurrent complete одного байтового содержимого проходят оба `find_duplicate is None`.
*   **Чем вызвана (Первопричина / Root Cause):** дубликат обрабатывается как «вернуть существующий ArtifactOut», не как DB constraint. Гонка TOCTOU.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* два `artifacts`, два `jobs` ASR на один файл, удвоенный GPU.
    - *При нагрузке:* 10k не реалистичны; 2 параллельных complete с телефона + десктопа — да.
    - *Для бизнеса:* двойная расшифровка, двойные сущности, расхождение «какой id канон».
*   **Шаги решения:**
    1. UniqueConstraint `(project_id, sha256)`.
        - **План:** Дедуп — инвариант БД, не SELECT.
        - **Как выполнить:** На `artifacts`: `UniqueConstraint("project_id", "sha256", name="uq_artifact_sha")`. Индекс sha256 оставить или заменить unique.
    2. `create_artifact_record` ловить IntegrityError → вернуть существующий как duplicate.
        - **План:** Гонка двух complete → один канонический row.
        - **Как выполнить:** В `create_artifact_record`: `try/except IntegrityError`: rollback savepoint, `find_duplicate`, вернуть его с `duplicate=True`. Не второй job.
    3. Миграция: схлопнуть уже существующие дубли до создания индекса.
        - **План:** Индекс не создастся, пока есть дубли.
        - **Как выполнить:** До upgrade: SQL найти `(project_id, sha256)` count>1, оставить min(id), остальные archived/error + не трогать диск без решения. Затем unique.

### 🚨 [Критичность: Medium] | [Категория: Архитектура]
*   **Локация:** `app/app/models/speaker_profile.py` `ArtifactSpeakerLink.artifact_id: String(64)` без FK на `artifacts.artifact_id`; нет UniqueConstraint `(artifact_id, diar_label)`.
*   **Суть дефекта:** связь спикеров с артефактом — строка. `_save_links_from_payload` делает DELETE всех links артефакта и INSERT (`speaker_profiles.py` ~1302–1332) + свой `db.commit()`, вклиниваясь в чужую транзакцию пайплайна.
*   **Чем вызвана (Первопричина / Root Cause):** id артефакта для UI — строковый `art_...`, PK таблицы — integer. Авторы связали UI-ключ напрямую, не integer FK.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* commit внутри publish режет атомарность lab-публикации. Дубли diar_label при сбое mid-insert (FK нет, unique нет).
    - *При нагрузке:* DELETE+INSERT на каждый publish/recalc при занятом WAL.
    - *Для бизнеса:* «пропали» привязки имён после частично упавшего enroll.
*   **Шаги решения:**
    1. Unique `(artifact_id, diar_label)`; FK на `artifacts.artifact_id`.
        - **План:** Связь diar_label↔артефакт уникальна и ссылается на живой artifact_id.
        - **Как выполнить:** Модель `ArtifactSpeakerLink`: `ForeignKey("artifacts.artifact_id")`, `UniqueConstraint(artifact_id, diar_label)`. Миграция после чистки дублей.
    2. `_save_links_from_payload`: не делать внутренний `commit` — пусть вызывает внешняя транзакция publish.
        - **План:** Внутренний commit рвёт атомарность publish.
        - **Как выполнить:** Убрать `db.commit()` из `_save_links_from_payload`. Вызывающий (`publish_lab_to_prod` / enroll) коммитит сам. Тест: exception после save_links не оставляет полу-insert при rollback.
    3. UPSERT вместо DELETE всех links, если нужен частичный update.
        - **План:** Частичный update не должен DELETE всех links.
        - **Как выполнить:** SQLAlchemy `insert().on_conflict_do_update` (sqlite) по (artifact_id, diar_label) или merge по ключу. DELETE только исчезнувших меток.

### 🚨 [Критичность: Medium] | [Категория: Perf]
*   **Локация:** `app/app/models/artifact.py` `transcription_text: Text`; `list_artifacts` в `upload.py` грузит все колонки всех артефактов проекта каждые ~3 с с UI.
*   **Суть дефекта:** полная стенограмма дублируется в SQLite row и в `parsed/calls/{id}_final.txt`. Poll сериализует ORM-объекты с гигантским Text.
*   **Чем вызвана (Первопричина / Root Cause):** удобство `artifact.transcription_text` для knowledge без повторного чтения диска. Канон потом сместили на файл (`artifact_knowledge_text`), колонку не ужали.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* страница проекта → JSON всех стенограмм в RAM uvicorn worker.
    - *При нагрузке:* 20 пользователей × poll 3с × N часовых созвонов → постоянный полный scan SQLite + JSON encode. Event loop блокируется (sync def).
    - *Для бизнеса:* UI «тупит», ложные timeout, рост `app.db` до гигабайт.
*   **Шаги решения:**
    1. Убрать `transcription_text` из `ArtifactOut` list endpoint (отдельная колонка deferred / не грузить).
        - **План:** Список артефактов не возит полные стенограммы.
        - **Как выполнить:** `ArtifactOut` для list: без `transcription_text` (отдельная схема `ArtifactListOut`). Query: `load_only` нужных колонок или deferred на Text.
    2. Канон только файл `_final.txt`; в БД — nullable excerpt или NULL.
        - **План:** Канон текста — файл, колонка не дубль источника истины.
        - **Как выполнить:** `artifact_knowledge_text` уже читает диск; колонку оставить nullable excerpt (первые N символов) или перестать писать. Не удалять колонку в том же PR, что list, если knowledge ещё читает её — сначала grep.
    3. Фоновая миграция: обнулить Text у старых rows после сверки с диском.
        - **План:** Старые гигабайты Text в db схлопнуть.
        - **Как выполнить:** Скрипт: для каждого row сверить sha/path с `_final.txt`, UPDATE transcription_text=NULL. Бэкап db. Не в запросе list.

### 🚨 [Критичность: Low] | [Категория: Логика]
*   **Локация:** модели `DateTime(timezone=True)` при SQLite backend (`user.py`, `job.py`, …). `project_tasks.py` сравнивает aware `datetime.now(timezone.utc)` с `started` и при `tzinfo is None` подставляет UTC.
*   **Суть дефекта:** SQLite хранит naive datetime. Смешение aware/naive ловится костылём только в stale-tasks, не везде.
*   **Чем вызвана (Первопричина / Root Cause):** копирование PostgreSQL-стиля mapped_column на SQLite.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* TypeError в сравнении дат в неотловленных местах; JWT `exp` — UTC, это отдельно и корректно.
    - *При нагрузке:* n/a.
    - *Для бизнеса:* ложные «зависшие» задачи или наоборот вечные running.
*   **Шаги решения:**
    1. Единая функция `_aware(dt)` во всех сравнениях.
        - **План:** Сравнения дат не падают на naive из SQLite.
        - **Как выполнить:** Хелпер `_aware(dt)` в одном модуле (`app/app/timeutil.py`): None→None; naive→UTC; aware→UTC. Все `started`/`created_at` сравнения через него.
    2. Хранить ISO с Z в TEXT либо всегда naive UTC и документировать.
        - **План:** Один канон хранения.
        - **Как выполнить:** DECISIONS: SQLite DATETIME = naive UTC. Либо переход на ISO TEXT с Z. Не мешать DateTime(timezone=True) как будто это Postgres.
    3. Тест stale-tasks на naive из SQLite.
        - **План:** Stale-tasks проверяется на реальном типе из БД.
        - **Как выполнить:** В `test_project_tasks.py`: вставить running с naive datetime, вызвать sweep — не TypeError, помечается stale по порогу.

### 🚨 [Критичность: Low] | [Категория: Безопасность]
*   **Локация:** `app/app/db.py` `load_sqlite_vec` → `enable_load_extension(True)` на каждом connect, затем False.
*   **Суть дефекта:** окно загрузки расширений SQLite. После disable риск ниже. Если атакующий пишет в `app.db` и контролирует load path — theoretically RCE; для локального файла за процессом API это уже «машина взята».
*   **Чем вызвана (Первопричина / Root Cause):** sqlite-vec требует load_extension. Комментарий в db.py честный: пул крутит соединения.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* каждый checkout из пула грузит DLL vec.
    - *При нагрузке:* лишние syscall на connect.
    - *Для бизнеса:* при краже файла БД + замене расширения — эскалация; узкий сценарий.
*   **Шаги решения:**
    1. Оставить enable/disable как есть; не грузить иные DLL.
        - **План:** Окно enable_load_extension не расширять.
        - **Как выполнить:** Оставить enable→load sqlite_vec→disable как сейчас. Не добавлять другие DLL. Комментарий в `db.py` сохранить.
    2. Права на `app.db` только учётке сервиса; ACL каталога data/.
        - **План:** Файл БД не world-writable.
        - **Как выполнить:** ACL NTFS: только учётная запись сервиса на `data/` и `app.db`. Не шарить data по SMB. Документ в SETUP.
    3. Не считать это закрытым периметром — закрыть HTTP сначала.
        - **План:** Это не замена HTTP-auth.
        - **Как выполнить:** Не закрывать тикет «RCE sqlite» вместо Blocker report. Сначала A1–A3.

---

## Модуль C. Загрузка и очередь

### 🚨 [Критичность: High] | [Категория: Логика]
*   **Локация:** `app/app/routers/upload.py` `complete_resumable_upload` порядок: `shutil.move(part, dest)` → `cleanup_session` → `_register_file_on_disk` → `create_artifact_record` (commit).
*   **Суть дефекта:** байты покидают tmp **до** INSERT. Исключение после move (ffprobe 413 удаляет dest; иное исключение) оставляет сироту в `raw_inputs` или наоборот удаляет файл без записи.
*   **Чем вызвана (Первопричина / Root Cause):** нет unit-of-work «файл+row». Диск и SQLite — две системы без XA.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* сироты на диске; или 413 после move уже произошёл и dest.unlink в register — ок для лимита длительности. Падение между move и create — файл без артефакта, повтор complete невозможен (сессия снесена).
    - *При нагрузке:* утечка диска, не RAM.
    - *Для бизнеса:* «загрузка прошла, в таблице нет» — повторная ручная загрузка, дубли sha256.
*   **Шаги решения:**
    1. Сначала INSERT artifact+job в транзакции, потом move; при ошибке move — rollback + не cleanup session.
        - **План:** Сирота на диске без row хуже, чем row без файла (его можно докачать).
        - **Как выполнить:** Перестроить `complete_resumable_upload`: INSERT artifact+job (status=uploading/queued) → commit → `shutil.move`. Если move fail — status=error, сессию upload не cleanup (повтор complete). Если INSERT fail — part на месте.
    2. Либо move в `.../tmp/pending/{id}`, commit, rename в raw_inputs.
        - **План:** Альтернатива с тем же инвариантом.
        - **Как выполнить:** move в `raw_inputs/.pending/{artifact_id}` → commit → rename в канон. Pending не подхватывает воркер (фильтр пути).
    3. Скрипт: сироты в raw_inputs без row — список/карантин.
        - **План:** Существующие сироты найти.
        - **Как выполнить:** Скрипт: файлы `raw_inputs` без matching sha256/path в artifacts — список + карантин `raw_inputs/_orphans/`. Не авто-удалять.

### 🚨 [Критичность: Medium] | [Категория: Логика]
*   **Локация:** `app/app/services/resumable_upload.py` `write_chunk` — open r+b, seek, write, затем meta.json. Нет file lock между процессами. `create_session` при `client_key` возвращает существующую сессию.
*   **Суть дефекта:** два PUT одного index — last write wins; `received` идемпотентен. Два PUT разных index параллельно в один `payload.part` на Windows — неопределённость overlapping seeks без locking.
*   **Чем вызвана (Первопричина / Root Cause):** UI `app.js` грузит чанки с concurrency pool, рассчитывая на POSIX-safe независимые offset. На NTFS для непересекающихся диапазонов обычно ок; мета пишется целиком replace.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* редкий corrupt part → size mismatch на complete → 400, сессия уже может быть в плохом состоянии.
    - *При нагрузке:* много клиентов × большие файлы 2 ГиБ (`max_upload_bytes`) — диск tmp.
    - *Для бизнеса:* сорванная загрузка созвона.
*   **Шаги решения:**
    1. flock/msvcrt на `payload.part` на время write_chunk (короткий).
        - **План:** Параллельные PUT одного part не рвут NTFS overlapping write.
        - **Как выполнить:** В `write_chunk`: `msvcrt.locking` на handle `payload.part` на время seek+write (Windows) / `fcntl.flock` (если когда-либо POSIX). Короткий lock, не на всю сессию.
    2. Запретить overlapping size mismatch уже есть; добавить fsync part+meta.
        - **План:** Мета и part должны пережить kill.
        - **Как выполнить:** После write: `flush` + `os.fsync(fd)` на part и на atomic replace `meta.json`. Уже есть size check на complete — оставить.
    3. Тест параллельных PUT разных index.
        - **План:** Гонка разных index должна быть зелёной в CI.
        - **Как выполнить:** Тест: два thread PUT index=0 и index=1 на одну session, complete — size совпадает, sha256 стабилен.

### 🚨 [Критичность: Medium] | [Категория: Архитектура]
*   **Локация:** `app/app/routers/upload.py` `_register_file_on_disk`; `app/app/services/upload.py` `create_artifact_record`.
*   **Суть дефекта:** бизнес-правила (дедуп, лимит длительности, file_type, enqueue) живут в роутере + сервисе без границы. Роутер знает про HTTP 413.
*   **Чем вызвана (Первопричина / Root Cause):** тонкий слой «пока мало эндпоинтов».
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* второй вход (`POST /upload` multipart) дублирует вызов `_register_file_on_disk` — хотя бы DRY внутри файла.
    - *При нагрузке:* n/a.
    - *Для бизнеса:* расхождение правил, когда появится MCP/Telegram-upload.
*   **Шаги решения:**
    1. Перенести `_register_file_on_disk` целиком в `services/upload.py`.
        - **План:** Правила дедупа/длительности не живут в HTTP-слое.
        - **Как выполнить:** Перенести `_register_file_on_disk` в `services/upload.py` (`register_file_on_disk`). Роутер только path/headers.
    2. Роутер: HTTPException из доменных ValueError (слишком длинное аудио, duplicate).
        - **План:** HTTP-коды остаются в роутере.
        - **Как выполнить:** Сервис кидает доменные исключения (`AudioTooLong`, `DuplicateArtifact`). Роутер мапит в 413/200 duplicate. Не импортировать HTTPException в service.
    3. Один вход для resumable и multipart.
        - **План:** Resumable и multipart — один register.
        - **Как выполнить:** Оба эндпоинта вызывают одну функцию. Тест: одинаковый sha → одинаковый duplicate path.

### 🚨 [Критичность: High] | [Категория: Логика]
*   **Локация:** `app/app/workers/pipeline.py` `process_artifact`; `app/app/models/job.py` поле `phase`; `app/app/services/queue.py` `enqueue_process_artifact(..., phase=)`; `confirm_speakers_and_enqueue` пишет `phase="knowledge"`.
*   **Суть дефекта:** `gpu_worker.run_once` вызывает `process_artifact(db, settings, artifact)` **без job**. Ветвление только по `artifact.status`. `Job.phase` мёртво.
*   **Чем вызвана (Первопричина / Root Cause):** очередь проектировали как «фаза в job», диспетчер переписали на статусы артефакта (voice-gate), поле не удалили. Карта проекта до сих пор говорит «job phase=knowledge».
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* оператор/скрипт ставит knowledge-job при `speakers_pending` → воркер `return` сразу, `finish_job(ok=True)` — **успешный no-op**, knowledge не идёт.
    - *При нагрузке:* ложные done в jobs, очередь «зелёная», работа не сделана.
    - *Для бизнеса:* «подтвердили, в очереди, ничего не извлеклось» если статус не тот.
*   **Шаги решения:**
    1. Либо читать `job.phase` в `process_artifact(job, artifact)`: knowledge не no-op при pending.
        - **План:** Либо phase снова диспетчер, либо его нет в модели и карте.
        - **Как выполнить:** Предпочтительно: `process_artifact(db, settings, artifact, job)`: если `job.phase=="knowledge"` и status всё ещё `speakers_pending` → `finish_job(ok=False)` / error «нужен confirm», не silent done. Не запускать knowledge в обход gate.
    2. Либо удалить поле phase из модели+enqueue и править PROJECT-MAP.
        - **План:** Если phase не нужен — вычистить, чтобы карта не врала.
        - **Как выполнить:** Удалить колонку/поле из enqueue, confirm пишет только status+job без phase. `PROJECT-MAP` § очередь: диспетчер = artifact.status. `index_project_map`.
    3. Тест: queued knowledge + status pending → job error или wait, не done.
        - **План:** No-op success должен ломаться тестом.
        - **Как выполнить:** Fixture: artifact speakers_pending + job queued phase=knowledge → после run_once job.error или queued-wait, artifact не done, entities не созданы.

### 🚨 [Критичность: High] | [Категория: Логика]
*   **Локация:** `app/app/workers/gpu_worker.py` except после `process_artifact` (стр. ~99–117): при наличии transcript файла статус → `transcript_ready`.
*   **Суть дефекта:** обход `speakers_pending`. `publish_lab_to_prod` уже записал `_final.txt` и `transcription_file_path` **до** `set_status(speakers_pending)`. Crash в этом зазоре + except = knowledge без confirm на следующем успешном job (ветка else → resume → transcript_ready path).
*   **Чем вызвана (Первопричина / Root Cause):** except писали для «ASR доехал, knowledge упал — не гоняй ASR снова». Не учли voice-gate как обязательный продуктный стоп. `try_resume_transcript` ещё и считает lab `_final.txt` достаточным каноном.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* сущности/индекс/выжимка со `speaker_0` вместо имён; юридически неподтверждённая атрибуция реплик.
    - *При нагрузке:* n/a (это crash path).
    - *Для бизнеса:* протоколы встреч с безымянными/чужими метками, повторный ручной reindex.
*   **Шаги решения:**
    1. В except: если `voice_gate` и нет confirmed — ставить `speakers_pending`, не `transcript_ready`.
        - **План:** Crash после публикации файла не равен «можно knowledge».
        - **Как выполнить:** В `gpu_worker.py` except: если voice_gate включён и нет `speakers_confirmed` / нет confirm timestamp — `artifact.status="speakers_pending"`, не `transcript_ready`. Файл `_final.txt` не достаточный сигнал.
    2. `publish_lab_to_prod` и voice-gate — один статусный переход после обоих успехов либо явный промежуточный `asr_published`.
        - **План:** Два commit (publish + gate) схлопнуть в один переход.
        - **Как выполнить:** Либо `publish_lab_to_prod` не пишет product-status, а только файлы; затем один `set_status(speakers_pending)`. Либо явный `asr_published` между ними, except не трактует его как transcript_ready.
    3. Тест: mock crash после publish → retry не запускает knowledge без confirm.
        - **План:** Retry не должен уйти в knowledge.
        - **Как выполнить:** Тест: mock exception после publish, до gate → status speakers_pending; второй run_once не вызывает persist_entities.

### 🚨 [Критичность: Medium] | [Категория: Логика]
*   **Локация:** `app/app/services/queue.py` `claim_next_job` — SELECT LIMIT 1 затем UPDATE WHERE id AND status=queued. `requeue_orphaned_jobs` — все running → queued без фильтра attempts.
*   **Суть дефекта:** не `BEGIN IMMEDIATE`. Один воркер спасает `SingleInstance`. Рестарт воркера в середине 2-часового GigaAM ставит тот же job снова; overlay skip частично спасает, proof/voice могут частично повториться. `requeue` не смотрит `job_max_attempts` на уже running.
*   **Чем вызвана (Первопричина / Root Cause):** SQLite queue как Celery-замена. Lock файла воркера — отдельный механизм, не связан с claim.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* после kill python — повтор GPU. Двойной воркер при сломанном lock (сеть на lock file? локальный NTFS обычно ок) — два claim, второй UPDATE rowcount 0.
    - *При нагрузке:* один writer; очередь растёт линейно, latency = сумма ASR.
    - *Для бизнеса:* часы простоя GPU, счета Cursor на повтор sense/proof.
*   **Шаги решения:**
    1. `BEGIN IMMEDIATE` вокруг claim.
        - **План:** Claim должен быть эксклюзивным на SQLite writer.
        - **Как выполнить:** В `claim_next_job`: `session.execute(text("BEGIN IMMEDIATE"))` или connection.begin() + SELECT queued LIMIT 1 + UPDATE WHERE status=queued. Проверка rowcount.
    2. `requeue_orphaned_jobs`: не трогать attempts сверх max; помечать error.
        - **План:** Бесконечный requeue running после max attempts запрещён.
        - **Как выполнить:** `requeue_orphaned_jobs`: если `attempts >= job_max_attempts` → status=error, не queued. Иначе queued и attempts++. Тест на attempts=max.
    3. Overlay skip оставить; не рестартить proof если файл есть (уже частично так).
        - **План:** Повтор GPU на уже готовых стадиях не делать.
        - **Как выполнить:** Оставить overlay skip. В proof/voice: если `proof.json`/`voice.json` есть и checklist done — не subprocess. Уже частично так — закрыть тестом.

### 🚨 [Критичность: Medium] | [Категория: Архитектура]
*   **Локация:** `app/app/services/single_instance.py` — `msvcrt.locking` 1 байт; pid в соседнем файле.
*   **Суть дефекта:** lock живёт, пока процесс держит handle. Crash — OS снимет (заявлено в комментарии). Второй старт API+worker на одной машине — разные имена lock (`gpu_worker` vs нет lock на API). Два API — два uvicorn на один SQLite без координации (preflight порт).
*   **Чем вызвана (Первопричина / Root Cause):** защита только GPU-слота.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* два API → два poll reconcile, два sync LLM на одном db.
    - *При нагрузке:* `database is locked`.
    - *Для бизнеса:* «запустили второй раз» — деградация, не сразу видимая.
*   **Шаги решения:**
    1. Тот же SingleInstance для API (`api.lock`) или полагаться только на bind порта + preflight.
        - **План:** Второй процесс API на тот же db недопустим.
        - **Как выполнить:** Тот же `SingleInstance` с именем `api.lock` в lifespan `main.py` (как gpu_worker). Либо только bind+preflight, но тогда preflight обязан fail на занятый порт **и** на чужой pid, держащий db.
    2. Не запускать второй uvicorn на тот же `app.db`.
        - **План:** Операционно не стартовать два uvicorn.
        - **Как выполнить:** Документ запуска: один `python -m app.app.main`, один worker. Скрипт start проверяет lock.
    3. Документировать в preflight: один API, один worker.
        - **План:** Preflight называет инвариант явно.
        - **Как выполнить:** Сообщение: «один API, один GPU-воркер, один app.db». Не запускать worker из API процесса.

### 🚨 [Критичность: Medium] | [Категория: Perf]
*   **Локация:** `app/app/routers/upload.py` `list_artifacts` (стр. ~136–190): полный SELECT, цикл `reconcile_artifact_status`, для done audio `ensure_audio_report`. UI poll 3 с (`PROJECT-MAP` §5).
*   **Суть дефекта:** GET списка имеет побочные эффекты генерации HTML и commit. Нет пагинации, нет `limit`.
*   **Чем вызвана (Первопричина / Root Cause):** «подтянуть статус, если knowledge уже есть» засунули в list, чтобы UI не знал о гонках. Генерацию отчёта — чтобы ссылка не 404.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* каждый poll — диск + CPU markdown/HTML. Sync handler блокирует uvicorn.
    - *При нагрузке:* 20 вкладок × 3с = постоянный writer lock рядом с воркером.
    - *Для бизнеса:* «портал завис, пока идёт созвон в очереди».
*   **Шаги решения:**
    1. Убрать `ensure_audio_report` из GET list; отчёт собирать лениво на `/report`.
        - **План:** GET списка не генерирует HTML-отчёты.
        - **Как выполнить:** Убрать `ensure_audio_report` из `list_artifacts`. Генерация только в GET `/report` (лениво, под auth).
    2. `reconcile_artifact_status` — отдельный лёгкий SELECT статусов, не полная модель.
        - **План:** Reconcile не должен грузить полные ORM-стенограммы.
        - **Как выполнить:** Отдельный запрос: `id, status, progress, error_message`. `reconcile_artifact_status` по этому срезу. Не `select(Artifact)` все колонки.
    3. Пагинация / cursor; poll только `status+progress`, не весь ArtifactOut.
        - **План:** Poll — узкий JSON.
        - **Как выполнить:** Эндпоинт `GET /projects/{id}/artifacts/status` или query `?fields=status` с cursor/limit. UI poll 3с бьёт в него, полный list — по открытию страницы.
    4. Не commit'ить на каждый poll.
        - **План:** Poll не держит writer.
        - **Как выполнить:** Убрать `db.commit()` из GET list. Reconcile, если пишет, — редкий background или только при смене статуса с диска, не каждые 3с.

---

## Модуль D. Lab ASR и публикация

### 🚨 [Критичность: High] | [Категория: Логика]
*   **Локация:** `app/app/services/pipeline_lab.py` `process_lab_artifact` — серия `set_status`/`db.commit` / `mark_lab_step` на диск; `publish_lab_to_prod` commit; затем voice-gate commit.
*   **Суть дефекта:** нет одной транзакции «прогон принят». Чеклист JSON и БД расходятся при kill. Overlay skip смотрит файлы, статус БД может быть `asr_running` при готовом proof.
*   **Чем вызвана (Первопричина / Root Cause):** длинный GPU (часы) нельзя держать открытую SQLite-транзакцию (один writer). Сознательно мелко комитили прогресс для UI pipeline live. Цена — неатомарность.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* UI показывает running, на диске уже proof; или наоборот. Retry по sha256 run.json.
    - *При нагрузке:* n/a (один job).
    - *Для бизнеса:* ручной rerun, путаница «какая стенограмма канон».
*   **Шаги решения:**
    1. Чеклист на диске — source of truth прогресса UI; статус БД обновлять реже: preprocessing → asr_running → speakers_pending|error.
        - **План:** UI прогресса читает диск, БД — редкие product-статусы.
        - **Как выполнить:** `lab_checklist.json` остаётся источником шагов. `set_status` только preprocessing / asr_running / speakers_pending|error. Не commit на каждый mark_lab_step.
    2. Не коммитить `transcription_*` до финального product-status.
        - **План:** transcription_* в БД не писать до product-status.
        - **Как выполнить:** Путь/excerpt писать в том же commit, что speakers_pending, вместе с publish. Раньше — только файлы lab run.
    3. Идемпотентный retry по run_id+sha256 уже есть — покрыть тестом рассинхрона checklist vs status.
        - **План:** Рассинхрон checklist vs status ловится тестом, не оператором.
        - **Как выполнить:** Тест: checklist proof=done, status asr_running → retry/reconcile не запускает Whisper; либо подтягивает статус без второго ASR.

### 🚨 [Критичность: High] | [Категория: Perf]
*   **Локация:** `app/app/services/pipeline_lab.py` `_run_cmd` — `subprocess.run(..., capture_output=True)` timeout до 7200 с.
*   **Суть дефекта:** весь stdout/stderr дочернего ASR/LLM процесса буферизуется в RAM родительского воркера до завершения.
*   **Чем вызвана (Первопричина / Root Cause):** удобство `raise RuntimeError(tail[-2000:])`. Логи стадий пишутся ещё и в файлы lab, но pipe всё равно копится.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* многомегабайтный лог Parakeet/GigaAM → пик RSS воркера. Windows commit charge.
    - *При нагрузке:* один job; пик на длинном созвоне.
    - *Для бизнеса:* OOM воркера посреди 2-часового прогона, потеря прогресса (см. мелкие commit).
*   **Шаги решения:**
    1. `subprocess.Popen`, stdout/stderr в файлы `lab/runs/.../logs/{step}.log`, в RAM только tail.
        - **План:** Часы ASR не должны копиться в RAM воркера.
        - **Как выполнить:** `_run_cmd`: `Popen`, stdout/stderr → `lab/runs/{run}/logs/{step}.stdout.log`. `communicate` не использовать для шагов с timeout>60.
    2. Не использовать `capture_output=True` на шагах > 60 с.
        - **План:** Короткий helper может остаться capture.
        - **Как выполнить:** Шаги <60с (ffprobe) — capture ок. Lab python/docker — файлы.
    3. RuntimeError — последние N КБ из лог-файла.
        - **План:** Ошибка пользователю — хвост лога, не весь буфер.
        - **Как выполнить:** `RuntimeError`: последние 8–32 КБ файла. Не `[-2000:]` из RAM всего pipe.

### 🚨 [Критичность: Medium] | [Категория: Безопасность]
*   **Локация:** `app/app/services/pipeline_audio.py` `_asr_env` — `os.environ.copy()` + `CURSOR_API_KEY`, `GIGAAM_API_KEY`, `LMSTUDIO_API_KEY` в env дочерних `python.exe`.
*   **Суть дефекта:** ключи в environment всех lab-скриптов. Любой дамп процесса / ошибка, печатающая env, утечка. Windows job object наследует env.
*   **Чем вызвана (Первопричина / Root Cause):** CLI-скрипты читают ключ из env, не из секрет-хранилища. Единый `_asr_env` для Docker и Cursor.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* `lab_sense.py` видит GigaAM key и наоборот.
    - *При нагрузке:* n/a.
    - *Для бизнеса:* компрометация Cursor key = чужие агенты от вашего биллинга.
*   **Шаги решения:**
    1. Передавать Cursor/GigaAM ключ только тем скриптам, которым он нужен (sense/proof vs giga vs voice).
        - **План:** Дочерний процесс видит только свой ключ.
        - **Как выполнить:** `_asr_env(step)`: sense/proof — CURSOR_*; giga docker — GIGAAM_*; voice — без Cursor. Не класть все ключи во все шаги.
    2. Не `os.environ.copy()` целиком, если можно минимальный env.
        - **План:** Не копировать весь os.environ, если достаточно whitelist.
        - **Как выполнить:** Собрать dict: PATH, VIRTUAL_ENV, DATA_DIR, PRIMARY_ANALITICS_*, нужный ключ, SYSTEMROOT на Windows. Не HOME с лишними секретами если можно.
    3. Запретить дочерним печатать env; scrub в логах.
        - **План:** Логи/исключения не печатают env.
        - **Как выполнить:** Запретить `print(os.environ)` в lab-скриптах. В `_run_cmd` scrub `API_KEY|TOKEN|SECRET` в хвосте лога перед RuntimeError.

### 🚨 [Критичность: Medium] | [Категория: Архитектура]
*   **Локация:** `app/app/services/pipeline_lab.py` `write_prod_view` / `replicas_to_turns` — `sys.path.insert` + импорт `asr/speakers.py`, `transcript_view.py`.
*   **Суть дефекта:** домен ASR вшит в app-сервис. Два интерпретатора (venv vs venvs\lab) уже разделены для ONNX; публикация снова смешивает.
*   **Чем вызвана (Первопричина / Root Cause):** cutover lab→prod без стабильного пакета `primary_analitics_asr`.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* importlib побочные эффекты, конфликт зависимостей если asr потянет иной numpy.
    - *При нагрузке:* n/a.
    - *Для бизнеса:* хрупкие релизы, «починили HTML — упал воркер».
*   **Шаги решения:**
    1. Вынести публикацию (`write_prod_view`, Turn) в пакет, который app ставит как dependency, без `sys.path.insert`.
        - **План:** Публикация prod-view — зависимость app, не import с диска asr/.
        - **Как выполнить:** Вынести `Turn`, `write_prod_view`, форматтеры в пакет `primary_analitics_transcript` (или `app/app/services/transcript_view.py` копия стабильного API) без onnx. App requirements ставят его. Убрать `sys.path.insert`.
    2. Не импортировать `transcript_view` из lab venv.
        - **План:** Lab venv не импортируется в API/worker Python.
        - **Как выполнить:** Worker venv≠lab: subprocess lab, публикация в app venv. Не `import transcript_view` из `venvs/lab`.
    3. Тесты публикации в app/tests без подтягивания onnx.
        - **План:** Тесты HTML без GPU.
        - **Как выполнить:** `app/app/tests/test_transcript_view.py`: turns → html/txt, без parakeet. CI green без CUDA.

### 🚨 [Критичность: Medium] | [Категория: Логика]
*   **Локация:** `app/app/workers/pipeline.py` else-ветка: нет lab run → `process_audio_artifact` (legacy Whisper/T-one путь в `pipeline_audio.py`).
*   **Суть дефекта:** DECISIONS запрещают тройной ансамбль в проде; код оставляет скрытый fallback. Воркер «не вызывает» его только пока lab run существует.
*   **Чем вызвана (Первопричина / Root Cause):** страховка для старых артефактов на диске.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* битый latest.json → уход в legacy, другие файлы, другая семантика спикеров.
    - *При нагрузке:* случайный старт Whisper-контейнера (если этот путь дернет docker).
    - *Для бизнеса:* стенограмма «не из канона», скрытый регресс качества.
*   **Шаги решения:**
    1. Удалить вызов `process_audio_artifact` из прод-ветки `process_artifact` или спрятать за явный `ALLOW_LEGACY_ASR=1`.
        - **План:** Прод-воркер не вызывает Whisper/T-one.
        - **Как выполнить:** В `process_artifact` else: не `process_audio_artifact`. Если нет lab run — `set_status(error, "rerun lab")`. Флаг `ALLOW_LEGACY_ASR=1` только для явного CLI.
    2. Старые артефакты без lab run — error «нужен rerun lab», не Whisper.
        - **План:** Старые артефакты без lab — ошибка, не другой канон.
        - **Как выполнить:** Сообщение в UI: нужен повтор lab. Не генерировать docker_merged из whisper.
    3. Обновить DECISIONS: legacy только CLI.
        - **План:** Документ запрещает скрытый fallback.
        - **Как выполнить:** DECISIONS: legacy ASR только ручной CLI. PROJECT-MAP: ветка else = error. `lightrag_sync_docs`.

### 🚨 [Критичность: Medium] | [Категория: Архитектура]
*   **Локация:** `app/app/services/docker_runtime.py` `ASR_CONTAINERS = (whisper, gigaam, tone)`; `_compose_up` = `docker compose up -d` **всего** файла; `ensure_container("primaryanalitics-gigaam")` при отсутствии контейнера вызывает полный compose.
*   **Суть дефекта:** поднятие GigaAM может стартануть Whisper и T-one вопреки инварианту «воркер их не вызывает». Они занимают GPU (`gpus: all` в `asr/docker-compose.yml`).
*   **Чем вызвана (Первопричина / Root Cause):** compose как «пакет ASR», точечный start не отделён. `pipeline_lab` зовёт только gigaam, но create-path широкий.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* VRAM 24 ГБ съедена idle Whisper+Tone. Parakeet/Sortformer/GigaAM деградируют или OOM.
    - *При нагрузке:* один GPU — взаимное вытеснение.
    - *Для бизнеса:* «лаб не влезает в память» без понимания, что поднялись откатные контейнеры.
*   **Шаги решения:**
    1. `ensure_container("primaryanalitics-gigaam")`: `docker compose up -d gigaam` **сервис**, не весь файл.
        - **План:** Поднять только сервис gigaam.
        - **Как выполнить:** `docker compose up -d gigaam` (имя сервиса из `asr/docker-compose.yml`), не `up -d` всего файла. `ensure_container("primaryanalitics-gigaam")` только этот сервис.
    2. `ASR_CONTAINERS` для prod = только gigaam.
        - **План:** Прод-список контейнеров не включает whisper/tone.
        - **Как выполнить:** `ASR_CONTAINERS` prod = `(gigaam,)`. Whisper/tone не в ensure path воркера.
    3. Whisper/Tone — profiles `legacy` в compose, не `up -d` по умолчанию.
        - **План:** Откатные образы не стартуют по умолчанию.
        - **Как выполнить:** В compose: `profiles: [legacy]` на whisper и tone. Без `--profile legacy` они не в `up -d gigaam`.

### 🚨 [Критичность: Low] | [Категория: Логика]
*   **Локация:** `app/app/services/notify.py` `send_telegram(settings, chat_id, ...)`; вызовы в `pipeline_lab.py` / `pipeline_knowledge.py` — `str(artifact.user_id)`.
*   **Суть дефекта:** в Telegram уходит SQLite PK, не `users.telegram_id`. При пустом токене — stub в лог (ок). При живом токене — 400 от Telegram или сообщение не тому.
*   **Чем вызвана (Первопричина / Root Cause):** поле назвали user_id в Artifact, в notify ждали chat_id. Путаница идентификаторов.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* уведомления «подтвердите спикеров» / «готово» не доходят.
    - *При нагрузке:* n/a.
    - *Для бизнеса:* оператор не узнает о паузе voice-gate, созвон висит в `speakers_pending`.
*   **Шаги решения:**
    1. Передавать `user.telegram_id` (загрузить User по `artifact.user_id`).
        - **План:** В Telegram уходит chat id пользователя, не PK sqlite.
        - **Как выполнить:** Перед `send_telegram`: `user = db.get(User, artifact.user_id)`; `chat_id=user.telegram_id`. Нет user — skip + log.
    2. Если telegram_id не похож на chat id — не слать, залогировать.
        - **План:** Мусорный telegram_id не слать в Bot API.
        - **Как выполнить:** Если значение не int-like / не похоже на chat id — не httpx, `logger.warning`. Login telegram_id как строка — документировать формат.
    3. Тест: mock httpx, assert json chat_id.
        - **План:** Контракт JSON к Telegram проверяется моком.
        - **Как выполнить:** Тест: mock httpx, artifact.user_id=1, User.telegram_id="12345" → payload chat_id 12345, не "1".

### 🚨 [Критичность: Low] | [Категория: Безопасность]
*   **Локация:** `asr/SETUP.md` — документирован утекший HF-токен `hf_LbPnWW...` (авторы просят отозвать).
*   **Суть дефекта:** секрет когда-то лежал в yaml в git-init зоне. Если репозиторий пушился — токен скомпрометирован навсегда до revoke.
*   **Чем вызвана (Первопричина / Root Cause):** копипаста docker-compose из вендора.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* n/a для текущего `.env`.
    - *При нагрузке:* n/a.
    - *Для бизнеса:* квота HF, доступ к private models, если токен не отозвали.
*   **Шаги решения:**
    1. Отозвать токен на huggingface.co если не отозван.
        - **План:** Утекший токен считать скомпрометированным.
        - **Как выполнить:** На huggingface.co отозвать `hf_LbPnWW...` если ещё жив. Новый токен только в `.env`, не в md.
    2. Вычистить значение из SETUP.md (оставить «токен был в yaml, отозвать»).
        - **План:** В репозитории не должно быть значения.
        - **Как выполнить:** `SETUP.md`: «токен попадал в yaml, отозвать, в git не хранить». Grep `hf_` по docs/asr — пусто кроме этой пометки.
    3. `git log`/`git rev-list` — убедиться, что yaml не в remote.
        - **План:** Проверить, не уехал ли yaml на remote.
        - **Как выполнить:** `git log -S hf_ --all` / rev-list. Если был push — revoke обязателен, history rewrite только если пользователь явно просит.

---

## Модуль E. Спикеры (сервисный слой)

### 🚨 [Критичность: High] | [Категория: Логика]
*   **Локация:** `app/app/services/speaker_profiles.py` `confirm_speakers_and_enqueue` (~1685–1731); `speakers_confirm_status` (~1606–1650). Нет `track_project_task`. `api_merge_speakers` тоже без task lock (в отличие от enroll).
*   **Суть дефекта:** идемпотентность confirm — только `status == speakers_pending` без SELECT FOR UPDATE (его нет в SQLite так). Два параллельных POST оба проходят проверку → два knowledge job. Merge без 409.
*   **Чем вызвана (Первопричина / Root Cause):** 409 сделали для долгих LLM-кнопок (summary/reindex/enroll), confirm показался «коротким UPDATE». Global blend `sync_confirmed_to_global` внутри — свои commit на каждый профиль.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* двойной knowledge: повторный DELETE entities, повторный LLM, повторный index. Merge параллельно с enroll — рваный match JSON.
    - *При нагрузке:* двойной Cursor bill.
    - *Для бизнеса:* мерцание имён, дубли выжимок.
*   **Шаги решения:**
    1. `track_project_task` на confirm и merge, как на enroll.
        - **План:** Confirm/merge — долгие мутации с тем же lock, что enroll.
        - **Как выполнить:** Обернуть `confirm_speakers_and_enqueue` и merge в `track_project_task` (тип `speakers_confirm` / `speakers_merge`). Второй вызов — 409.
    2. Атомарно: `UPDATE artifacts SET status='speakers_confirmed' WHERE id=? AND status='speakers_pending'` и проверять rowcount.
        - **План:** Смена статуса атомарна относительно pending.
        - **Как выполнить:** `UPDATE artifacts SET status='speakers_confirmed' WHERE id=? AND status='speakers_pending'`. rowcount!=1 → 409/400, не второй job. Затем enqueue knowledge.
    3. Unique running task + 409.
        - **План:** БД не пускает два running одной задачи.
        - **Как выполнить:** Связка с G2: unique running. Роутер ловит TaskAlreadyRunning → 409.
    4. `guardButton` на UI confirm.
        - **План:** Двойной клик в UI не должен уходить двумя fetch до ответа.
        - **Как выполнить:** `PrimaryAnalitics.guardButton` на `.speaker-confirm-btn` в `project.html` (как на reindex). Сервер всё равно атомарный.

### 🚨 [Критичность: Medium] | [Категория: Логика]
*   **Локация:** `sync_confirmed_to_global` (~138–176): грузит **все** `scope=global` профили в Python, cosine в цикле, порог merge, `_blend_embedding`, commit.
*   **Суть дефекта:** O(n) по всей глобальной базе голосов на каждый confirmed label при confirm. Нет индекса по эмбеддингу (и не будет в SQLite BLOB без vec).
*   **Чем вызвана (Первопричина / Root Cause):** WeSpeaker 256-d, «база маленькая». Нет ANN.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* confirm линейно замедляется с ростом global pool.
    - *При нагрузке:* 10k профилей — секунды Python cosine + полная таблица в RAM.
    - *Для бизнеса:* ошибочный merge разных людей с похожим именем (`score = max(cosine, threshold)` если имя совпало) — **склейка чужих голосов в один global профиль**.
*   **Шаги решения:**
    1. Не поднимать score до порога только из-за совпадения имени.
        - **План:** Совпадение отображаемого имени не повышает cosine до merge-порога.
        - **Как выполнить:** Убрать `score = max(cosine, threshold)` при name match. Имя — отдельный сигнал для UI, не автосклейка эмбеддингов.
    2. Сравнивать только кандидатов с тем же folded name **или** top-k по грубому индексу, не всю таблицу вслепую с name-boost.
        - **План:** Не сканировать всю global таблицу вслепую.
        - **Как выполнить:** Кандидаты: тот же folded name **или** ограничить top-k (случайный sample недопустим). Пока нет ANN — хотя бы фильтр scope+name. Не грузить все BLOB если имя разное и нет kNN.
    3. Порог merge имени и голоса развести; UI «это тот же человек?» на серых случаях.
        - **План:** Серая зона — человеку, не blend.
        - **Как выполнить:** Пороги name vs voice развести в config. Если голос серый — не `_blend_embedding`, флаг в UI «тот же человек?».

### 🚨 [Критичность: Medium] | [Категория: Архитектура]
*   **Локация:** `app/app/services/speaker_profiles.py` (~1730 строк) — enroll, match JSON, HTML rebuild, confirm, reindex, summary, cosine, global blend.
*   **Суть дефекта:** God-object. Изменение подписи HTML трогает voice DB. Роутер тонкий только формально.
*   **Чем вызвана (Первопричина / Root Cause):** cutover lab/prod + Sortformer registry в одном файле, чтобы «всё про спикеров рядом».
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* высокий риск регрессий, циклические импорты (`pipeline_lab` ↔ `speaker_profiles`).
    - *При нагрузке:* n/a.
    - *Для бизнеса:* скорость разработки падает, баги имён в стенограмме (уже фиксировались в DECISIONS про `build_transcript_view`).
*   **Шаги решения:**
    1. Разрезать на: `speaker_registry` (DB), `match_json` (файлы), `publish_view`, `confirm_jobs`.
        - **План:** Один файл не владеет DB, JSON, HTML и jobs.
        - **Как выполнить:** Разрезать `speaker_profiles.py` (сервис): `speaker_registry.py` (CRUD профилей), `speaker_match_files.py` (json/html), `speaker_confirm.py` (status+enqueue). Публичные функции — тонкие фасады, чтобы роутер не раздуть в одном PR.
    2. Роутер только HTTP + auth + 409.
        - **План:** Роутер не вызывает GPU/LLM напрямую сверх HTTP.
        - **Как выполнить:** Роутер: parse body, auth, 409, вызов сервиса. Без `str(exc)` (E4).
    3. Запретить импорт `pipeline_lab` из registry и наоборот — через узкий модуль publish.
        - **План:** Цикл import pipeline_lab ↔ speakers разорвать.
        - **Как выполнить:** Общий `publish_prod_view.py`. Registry не импортирует `pipeline_lab`. Lab вызывает publish, не наоборот.

### 🚨 [Критичность: Low] | [Категория: Безопасность]
*   **Локация:** `app/app/routers/speaker_profiles.py` except Exception → `HTTPException(500, detail=str(exc))`.
*   **Суть дефекта:** внутренние пути, traceback-сообщения, отсутствие файлов lab утекают клиенту.
*   **Чем вызвана (Первопричина / Root Cause):** отладка UI «почему enroll не встал».
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* разведка FS layout `D:\Primary_Analitics\data\projects\...`.
    - *При нагрузке:* n/a.
    - *Для бизнеса:* облегчение целевой атаки на открытый API.
*   **Шаги решения:**
    1. Все except в speaker router: log + `detail="Internal Server Error"`.
        - **План:** 500 без внутренних путей.
        - **Как выполнить:** Все широкие except в `routers/speaker_profiles.py`: log + generic detail. Не `detail=str(exc)`.
    2. 400 только на ValueError с безопасным текстом (pending labels ок).
        - **План:** 400 только на ожидаемые доменные ошибки.
        - **Как выполнить:** Pending labels, validation — ValueError с фиксированными сообщениями без path. FileNotFoundError → 409/400 «нет прогона», не path.
    3. Тест: FileNotFoundError не содержит абсолютный путь в JSON.
        - **План:** Регрессия утечки path в JSON.
        - **Как выполнить:** Тест: подменить enroll FileNotFoundError(`C:\...`) → response.json()["detail"] не содержит `C:` и `data/projects`.

---

## Модуль F. Knowledge, граф, индекс, ТЗ

### 🚨 [Критичность: High] | [Категория: Логика]
*   **Локация:** `app/app/services/entities.py` `persist_entities` (~509–567): embed **снаружи** lock, затем DELETE entities + vectors, INSERT, `flush`, запись `structured/{id}_entities.json`, **без commit**. Commit делает `set_status` в `pipeline_knowledge.py`. `gpu_worker` except делает `db.commit()` на той же сессии.
*   **Суть дефекта:** падение после DELETE+flush, до успешного конца knowledge: except воркера **закоммитит пустой набор сущностей** (или частичный flush). JSON на диске может быть новым, БД — пустой, или наоборот.
*   **Чем вызвана (Первопричина / Root Cause):** комментарий в `indexing.py` честно объясняет: не держать write lock на время Ollama. Паттерн скопировали в persist, но except воркера не делает rollback, он commit'ит статус error **вместе с грязной сессией**.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* граф и дедлайны пустые при `status=error` или error с вычищенными entities. Повтор knowledge может восстановить, если job retry (attempts < 3) и статус позволит.
    - *При нагрузке:* n/a.
    - *Для бизнеса:* «пропали задачи из созвона» после сбоя Ollama mid-write.
*   **Шаги решения:**
    1. В `gpu_worker` except: `db.rollback()` **до** выставления status=error и нового commit.
        - **План:** Грязная сессия после DELETE entities не коммитится как error.
        - **Как выполнить:** В `gpu_worker` except **первой** строкой `db.rollback()`. Потом заново load artifact, status=error, commit. Не commit той же сессии после persist flush.
    2. `persist_entities`/`index_artifact`: запись JSON на диск после успешного commit или во временный файл+replace.
        - **План:** JSON на диске не опережает успешный commit.
        - **Как выполнить:** `persist_entities`: писать `*_entities.json` во `.tmp` и replace **после** commit knowledge-шага, либо сразу после успешного commit в pipeline_knowledge. Не до commit.
    3. Тест: exception mid-persist не оставляет 0 entities при rollback.
        - **План:** Rollback оставляет старые entities.
        - **Как выполнить:** Тест: N entities, mock exception после DELETE+flush → rollback → count всё ещё N, status error.

### 🚨 [Критичность: High] | [Категория: Perf]
*   **Локация:** `app/app/services/graph.py` `seed_graph` (~73–144): вложенный цикл всех новых × всех prior active entities проекта, Python `_cosine` 1024-d, на каждую пару выше порога — `SELECT Link.id` exists, затем `db.add`.
*   **Суть дефекта:** O(n·m) CPU + O(n·m) потенциальных SQL. sqlite-vec для сущностей не kNN (таблица BLOB `entity_vectors`), хотя векторы уже есть.
*   **Чем вызвана (Первопричина / Root Cause):** граф «на таблицах» как этап до Neo4j (`PLAN.md`). kNN не довели.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* 500 сущностей × 500 prior = 250k cosine в интерпретаторе + до 250k EXISTS. Воркер стоит, GPU простаивает, SQLite locked для API poll.
    - *При нагрузке:* 10k пользователей не нужно — хватит десятков созвонов в одном проекте, чтобы knowledge стал часами.
    - *Для бизнеса:* ТЗ и дедлайны запаздывают, очередь ASR стопорится (один воркер).
*   **Шаги решения:**
    1. Для каждой новой сущности — kNN по `entity_vectors` (если перевести в vec0) или batch numpy cosine, не Python zip.
        - **План:** Связи не считать Python-циклом всех пар.
        - **Как выполнить:** Batch: сложить векторы в numpy, cosine matrix **или** sqlite-vec kNN если entity_vectors в vec0. Порог → кандидаты. Не zip вложенный по всем prior.
    2. Убрать per-pair EXISTS: загрузить существующие пары одним SELECT, set в памяти.
        - **План:** EXISTS на каждую пару убрать.
        - **Как выполнить:** Один `SELECT source_id, target_id FROM links WHERE project_id=?` в set. Insert только отсутствующих.
    3. Лимит prior entities (свежие N / тот же type) пока нет ANN.
        - **План:** Пока нет ANN — резать prior.
        - **Как выполнить:** Лимит: prior того же type и/или последние N по id. Config. Документировать неполноту графа.

### 🚨 [Критичность: Medium] | [Категория: Логика]
*   **Локация:** `graph.py` `build_graph_payload` (~187–240): `limit=200` на artifacts, entities, links независимо, потом `nodes[:limit]`. Рёбра CONTAINS добавляются для entities, которые могут не попасть в обрезанные nodes.
*   **Суть дефекта:** Cytoscape получает висячие edges / неполный граф без сигнала обрезания.
*   **Чем вызвана (Первопричина / Root Cause):** защита UI от гигантского JSON. Обрезка наивная.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* «графа нет» визуально при живых данных в БД.
    - *При нагрузке:* ответ всё ещё может быть тяжёлым до slice.
    - *Для бизнеса:* недоверие к модулю графа, ручной поиск в обход.
*   **Шаги решения:**
    1. Обрезать согласованно: сначала entities, затем только их CONTAINS и links.
        - **План:** Обрезка JSON не оставляет висячие рёбра.
        - **Как выполнить:** `build_graph_payload`: выбрать entities до limit, затем CONTAINS только этих id, links только если оба конца в наборе. Артефакты — родители выбранных.
    2. В JSON флаг `truncated: true`, counts.
        - **План:** Клиент знает, что граф неполный.
        - **Как выполнить:** Поля `truncated: true`, `entity_count`, `shown`. UI бейдж.
    3. Пагинация/фильтр по типу в UI.
        - **План:** Дальше — фильтр, не один безлимитный JSON.
        - **Как выполнить:** Query type= / search на граф-странице. Не поднимать limit до 10k в одном ответе.

### 🚨 [Критичность: Medium] | [Категория: Логика]
*   **Локация:** `app/app/services/pipeline_knowledge.py` порядок: entities → index → `run_call_summary` → graph → done → `maybe_auto_rebuild_tz`. Выжимка созвона **после** index, но **из файлов стенограммы**, не из чанков. Падение summary не откатывает index (отдельные commit через set_status).
*   **Суть дефекта:** частичный knowledge — норма. `done` ставится даже если summary вернула False? Нужно смотреть `run_call_summary` — она логирует и возвращает bool, вызов игнорирует возврат.
*   **Чем вызвана (Первопричина / Root Cause):** выжимка «не должна блокировать индекс». Игнор bool — следствие.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* `done` без `call_summary.md`; UI пустой протокол.
    - *При нагрузке:* n/a.
    - *Для бизнеса:* созвон «готов», протокола нет, кнопку rebuild жмут руками (открытый API).
*   **Шаги решения:**
    1. Если `run_call_summary` False — не ставить `done` или ставить `done` с `error_message`/флагом `summary_missing`.
        - **План:** `done` не означает «протокол есть», если bool игнорируется.
        - **Как выполнить:** Если `run_call_summary` False: либо не `set_status(done)`, статус `knowledge_partial` / error_message=`summary_missing`, либо done + флаг в artifact (колонка/json). Выбрать одно в DECISIONS; UI должен видеть.
    2. UI badge «нет протокола».
        - **План:** Оператор видит дыру без открытия файла.
        - **Как выполнить:** В `project.html` бейдж «нет протокола» если нет `call_summary.md` или флаг. Кнопка rebuild (уже есть) под auth.
    3. Retry summary без полного knowledge.
        - **План:** Повтор выжимки без полного knowledge.
        - **Как выполнить:** `rebuild-summary` (уже API) не должен заново extract entities. Убедиться в коде; тест: entities count не меняется.

### 🚨 [Критичность: Medium] | [Категория: Perf]
*   **Локация:** `app/app/services/indexing.py` `index_artifact`: DELETE vectors по одному id (`vector_index.delete_vectors_for_chunks` цикл), INSERT vec по одному.
*   **Суть дефекта:** N round-trips SQL на чанки часового созвона (десятки–сотни).
*   **Чем вызвана (Первопричина / Root Cause):** sqlite-vec virtual table, осторожность с lock, копипаста upsert.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* длинная транзакция (flush+много execute) vs API poll.
    - *При нагрузке:* writer starvation.
    - *Для бизнеса:* медленный reindex с кнопки.
*   **Шаги решения:**
    1. `executemany` / один INSERT OR REPLACE на пачку.
        - **План:** Индекс чанков — пачка SQL, не цикл execute.
        - **Как выполнить:** `vector_index`: `executemany` / один INSERT OR REPLACE. sqlite-vec: допустимый batch API если есть, иначе executemany.
    2. DELETE FROM chunk_vectors WHERE chunk_id IN (...) одним запросом.
        - **План:** Удаление старых векторов одним предикатом.
        - **Как выполнить:** `DELETE FROM chunk_vectors WHERE chunk_id IN (...)` пачками по 500, не по одному id в Python-цикле.
    3. Замер на артефакте с 200 чанками.
        - **План:** Замерить, не гадать.
        - **Как выполнить:** Скрипт/тест на 200 чанков: время index до/после. Не регрессировать correctness (search находит тот же chunk).

### 🚨 [Критичность: Medium] | [Категория: Логика]
*   **Локация:** `app/app/services/tz.py` `maybe_auto_rebuild_tz`: для audio `count(status==done) % TZ_REBUILD_EVERY_N == 0`. Счётчик **всех** done проекта, не «N новых с прошлого rebuild». Документ всегда триггерит полный rebuild в том же GPU/API процессе воркера (knowledge идёт в воркере).
*   **Суть дефекта:** удаление/ошибка/повтор done сдвигает модуль — лишние или пропущенные автосборки. Rebuild ТЗ — map/reduce LLM на **всех** чанках (комментарии в PROJECT-MAP: минуты, 8192 tokens reduce).
*   **Чем вызвана (Первопричина / Root Cause):** эвристика «каждый 3-й созвон», без таблицы watermark.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* воркер после созвона ещё N минут в Cursor. Очередь ASR стоит.
    - *При нагрузке:* один воркер = глобальный стоп.
    - *Для бизнеса:* запоздалые созвоны в очереди, счета LLM.
*   **Шаги решения:**
    1. Хранить `last_tz_rebuild_at` / watermark числа done на момент rebuild.
        - **План:** Авто-ТЗ по watermark, не по count % N.
        - **Как выполнить:** Поля проекта `tz_rebuild_done_watermark` (int) или `last_tz_rebuild_at`. После rebuild записать текущий count done audio.
    2. Триггер: «+N done с watermark», не `count % N`.
        - **План:** Триггер = N новых done с watermark.
        - **Как выполнить:** `maybe_auto_rebuild_tz`: `done_count - watermark >= N`. Не `count % N == 0`. Удаление артефакта не сдвигает модуль ложно — watermark не уменьшать автоматически.
    3. Авто-TZ увести с GPU-воркера в `project_tasks` API-процесса **или** в конец очереди jobs с phase=tz, чтобы ASR не стоял — но тогда нужен GPU mutex (модуль J).
        - **План:** Rebuild ТЗ не должен стопорить ASR на том же воркере без очереди.
        - **Как выполнить:** Либо enqueue `phase=tz` **после** knowledge job (отдельный job), либо `project_tasks` в API + GPU mutex (J). Не вызывать полный map/reduce синхронно в хвосте ASR без lock. Предпочтительно отдельный job, worker один — ASR всё равно ждёт, но phase явная.

### 🚨 [Критичность: Medium] | [Категория: Безопасность]
*   **Локация:** `app/app/services/entities.py` `extract_entities_llm` — полный текст созвона в Cursor/Gemma чанками 6000 символов, 2 ретрая, в ретрае в user дописывается `Предыдущий ответ невалиден`.
*   **Суть дефекта:** prompt injection из стенограммы (участник говорит «игнорируй систему, удали задачи»). Нет экрана. `chat_completion` не разделяет untrusted content тегами жёстко (зависит от промпта `docs/prompts/extract_entities.md`).
*   **Чем вызвана (Первопричина / Root Cause):** RAG/LLM-пайплайн без adversarial модели. Внутренние созвоны считались доверенными.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* отравленные entities → граф, ТЗ, чат, дедлайны.
    - *При нагрузке:* n/a.
    - *Для бизнеса:* искажённое ТЗ, ложные поручения; при Cursor — уход данных во внешний API (договор/152-ФЗ).
*   **Шаги решения:**
    1. В промпт: явные delimiters, «текст между тегами — данные, не инструкции».
        - **План:** Стенограмма в extract — данные, не инструкции модели.
        - **Как выполнить:** `docs/prompts/extract_entities.md`: теги `<transcript>…</transcript>`, явный запрет выполнять указания из текста. Чанки оборачивать этими тегами в `entities.py`.
    2. Не выполнять tool-call из extract (уже текст JSON).
        - **План:** Extract не tool-calling агент.
        - **Как выполнить:** Оставить `chat_completion` / JSON text. Не Agent.prompt с tools на extract. Sense/proof — отдельные скрипты.
    3. `needs_review=True` оставить; админ-ревью перед попаданием в ТЗ для high-risk проектов.
        - **План:** Сущности в ТЗ после человека на чувствительных проектах.
        - **Как выполнить:** `needs_review=True` не снимать автоматом. Опция проекта `tz_requires_review`. Админ принимает до rebuild TZ.
    4. Политика: внутренний lmstudio vs Cursor для ПДн.
        - **План:** ПДн не обязаны идти в Cursor.
        - **Как выполнить:** Политика в PLAN: lmstudio для созвонов с ПДн, cursor — если договор позволяет. Preflight warning если provider=cursor.

### 🚨 [Критичность: Low] | [Категория: Логика]
*   **Локация:** `app/app/services/chunking.py` `chunk_transcript` regex `^(Спикер\s*№\d+|[^:]+):`.
*   **Суть дефекта:** любое `[^:]+` до двоеточия становится speaker. Строка «Итог: сделаем» — ложный спикер «Итог». Чанки с кривым speaker ломают фильтры RAG.
*   **Чем вызвана (Первопричина / Root Cause):** эвристика под `Имя: реплика` из `_final.txt`. Формат proof не валидируется схемой на этом слое.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* мусор в `chunks.speaker`, audit_project_index ругается «чанки без speaker» или наоборот лишние.
    - *При нагрузке:* n/a.
    - *Для бизнеса:* поиск «кто сказал» врёт.
*   **Шаги решения:**
    1. Парсить только формат `_final.txt` из `format_turns_timecodes` (`[mm:ss] name: text`), не произвольное `[^:]+`.
        - **План:** Спикер чанка только из канонического формата реплики.
        - **Как выполнить:** `chunk_transcript`: парсить `^\[\d+:\d+\]\s+(.+?):\s` как в `format_turns_timecodes`, не `[^:]+` с начала строки.
    2. Тесты на строках «Итог:», «Примечание:».
        - **План:** Строки «Итог:» не становятся спикером.
        - **Как выполнить:** Тесты: «Итог: сделаем», «Примечание:» → speaker None или не «Итог». Нормальная реплика `[00:01] Иван: привет` → Иван.
    3. Fallback speaker=None, не выдуманная метка.
        - **План:** Нет метки — None, не выдумка.
        - **Как выполнить:** Fallback `speaker=None`. RAG-фильтр «без спикера» уже ожидаем; не подставлять speaker_0.

---

## Модуль G. Чат, LLM-адаптер, Ollama

### 🚨 [Критичность: High] | [Категория: Perf]
*   **Локация:** `app/app/routers/knowledge.py` `chat_endpoint` — sync `def`; `app/app/services/chat.py` `answer_chat` → `track_project_task` → `_answer_chat_body` → `chat_completion` (httpx timeout до сотен секунд / Cursor Agent.prompt). Тот же процесс, что отдаёт UI и upload.
*   **Суть дефекта:** один uvicorn worker (типичный запуск `python -m app.app.main`) блокируется на время LLM. Poll артефактов, login, health — в очереди того же потока (Starlette sync в threadpool, но default threads ограничены).
*   **Чем вызвана (Первопричина / Root Cause):** knowledge «долгий» унесли в GPU-воркер; чат оставили sync в API «чтобы сразу JSON». `track_project_task` спасает от двойного чата **одного user**, не от блокировки процесса.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* вкладка чата → весь портал ждет. Загрузка чанков upload PUT может зависнуть.
    - *При нагрузке:* 20 человек спрашивают чат — threadpool исчерпан, 504, SQLite lock от параллельных begin_task.
    - *Для бизнеса:* «система умерла, когда спросили ИИ».
*   **Шаги решения:**
    1. Чат — очередь `jobs` или всегда `project_tasks` + фон (thread/process), HTTP сразу 202 + poll history (частично уже pending).
        - **План:** HTTP чата сразу 202, LLM в фоне.
        - **Как выполнить:** Уже есть pending+poll history — довести: handler не вызывает `chat_completion` sync дольше короткого begin_task. Worker thread/process или job `phase=chat`. Ответ 202 `{task_id}`.
    2. Не держать LLM в sync handler дольше N секунд.
        - **План:** Sync handler не держит Gemma/Cursor минуты.
        - **Как выполнить:** Если оставить sync: жёсткий timeout N (например 8с) → 202 если не успел (уже частично). Не 600с в том же thread что upload.
    3. Увеличить threadpool не считать решением — увести работу.
        - **План:** Увеличение threadpool не цель.
        - **Как выполнить:** Не крутить `anyio` workers как фикс. Увести работу. Комментарий в knowledge.py.

### 🚨 [Критичность: Medium] | [Категория: Логика]
*   **Локация:** `app/app/services/project_tasks.py` `begin_task`: SELECT running, потом INSERT running, два commit. Нет unique constraint `(task_type, project_id, artifact_id, user_id) WHERE running`.
*   **Суть дефекта:** TOCTOU двойной chat/tz. SQLite default isolation.
*   **Чем вызвана (Первопричина / Root Cause):** 409 как мягкая блокировка UI, не как DB invariant.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* два Cursor run, две строки chat pending.
    - *При нагрузке:* двойные списания API.
    - *Для бизнеса:* двойные ответы, рассинхрон истории.
*   **Шаги решения:**
    1. Unique index на running task (SQLite partial unique WHERE status='running').
        - **План:** Один running task данного ключа — инвариант SQLite.
        - **Как выполнить:** Partial unique index: `UNIQUE(task_type, project_id, artifact_id, user_id) WHERE status='running'` (NULL artifact — отдельное выражение). Миграция.
    2. IntegrityError → TaskAlreadyRunning.
        - **План:** Гонка двух begin → 409, не две строки.
        - **Как выполнить:** `IntegrityError` → `TaskAlreadyRunning`. Роутер 409. Тест двух thread begin_task.
    3. `BEGIN IMMEDIATE` в begin_task.
        - **План:** Сериализация writer на begin.
        - **Как выполнить:** `BEGIN IMMEDIATE` в `begin_task` до SELECT/INSERT. Коротко.

### 🚨 [Критичность: Medium] | [Категория: Безопасность]
*   **Локация:** `app/app/services/llm.py` `cursor_chat_raw`: `LocalAgentOptions(cwd=str(ROOT))` — корень **всего** репозитория `D:\Primary_Analitics`. Промпт просит не вызывать инструменты, SDK Agent.prompt исторически может иметь tools. `timeout` параметр **не abort'ит** run: только warning если `duration_ms > timeout*1000`.
*   **Суть дефекта:** агент с cwd=монорепо теоретически видит `.env`, `data/`, исходники. Таймаут не убивает процесс агента.
*   **Чем вызвана (Первопричина / Root Cause):** Cursor SDK local runtime «работает из проекта». Sense/proof гоняются теми же опциями.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* утечка секретов в облако Cursor, если runtime cloud или если local агент читает файлы. Зависший агент ест RAM/CPU.
    - *При нагрузке:* 3 ретрая × sleep 5s — умножение висящих run.
    - *Для бизнеса:* .env (SECRET_KEY, CURSOR_API_KEY, HF_TOKEN) в контексте агента.
*   **Шаги решения:**
    1. `LocalAgentOptions(cwd=...)` — пустой temp dir или каталог без `.env`/`data`.
        - **План:** Агент не стартует с cwd=монорепо.
        - **Как выполнить:** `LocalAgentOptions(cwd=tempfile.mkdtemp())` или `data/empty_agent_cwd/` без .env и без data/projects. Sense/proof — тот же принцип (промпт+файлы явно передаются).
    2. Явно выключить tools в SDK, если есть флаг.
        - **План:** Tools выключены, если SDK даёт флаг.
        - **Как выполнить:** Прочитать cursor-sdk skill: `tools`/`mcp` off для chat_raw и extract. Явно `[]`.
    3. Таймаут: не ретраить бесконечно; убивать run по id если SDK позволяет.
        - **План:** Timeout должен обрывать run, не только warning.
        - **Как выполнить:** Если SDK даёт cancel по run id — вызвать. Иначе не ретраить 3× на уже висящий. Счётчик run, kill процесс если документировано.
    4. Не класть SECRET в cwd агента.
        - **План:** Секреты не лежат в cwd агента.
        - **Как выполнить:** Не копировать `.env` в temp cwd. Ключ только env процесса API, не файлом в cwd.

### 🚨 [Критичность: Medium] | [Категория: Perf]
*   **Локация:** `app/app/services/llm.py` `lmstudio_chat_raw` — синхронный `httpx.Client(timeout=timeout)` до 600 с.
*   **Суть дефекта:** то же блокирование, плюс Gemma 31B на той же GPU, что ASR, если забыли unload. `prepare_for_llm` skip при cursor.
*   **Чем вызвана (Первопричина / Root Cause):** OpenAI-compatible локальный сервер, один клиент на вызов.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* VRAM contention если API chat во время ASR (разные процессы: API vs worker). API chat + worker knowledge одновременно — два процесса бьются за GPU/LM Studio.
    - *При нагрузке:* LM Studio queue, 500.
    - *Для бизнеса:* срыв расшифровки из-за «быстрого вопроса» в чате.
*   **Шаги решения:**
    1. Тот же GPU mutex, что для ASR (модуль J): чат в API не вызывает Gemma пока worker в asr_running.
        - **План:** Чат не грузит Gemma во время ASR.
        - **Как выполнить:** Общий `gpu_slot.lock` (J1): `lmstudio_chat_raw` / `prepare_for_llm` acquire. Если воркер в asr — API 409. Не второй load.
    2. При cursor — не грузить Gemma (уже skip prepare_for_llm).
        - **План:** Режим cursor не трогает VRAM Gemma.
        - **Как выполнить:** Оставить skip `prepare_for_llm` при LLM_PROVIDER=cursor. Проверить chat path не вызывает lms load.
    3. Очередь LLM-задач в одном процессе (worker).
        - **План:** Один потребитель GPU для LLM.
        - **Как выполнить:** Предпочтительно: chat/TZ/summary jobs в gpu_worker очереди. API только enqueue. Тогда mutex внутри одного процесса.

### 🚨 [Критичность: Low] | [Категория: Логика]
*   **Локация:** `app/app/services/chat.py` except в `answer_chat` wrapper (~697): пишет «Не удалось», `raise`. `track_project_task` then `fail_task`. История содержит pending+error. Prompt injection в вопрос пользователя идёт в RAG без санитайза (ожидаемо).
*   **Суть дефекта:** пользовательский текст в БД `chat_messages` без лимита кроме 4000 на API. Guidance из ответа пользователя при clarification сохраняется и влияет на будущий RAG (`save_chat_guidance`).
*   **Чем вызвана (Первопричина / Root Cause):** feature «уточнения».
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* отравленный guidance до `is_active` сброса (если есть).
    - *При нагрузке:* таблица chat растёт, `CHAT_HISTORY_LIMIT = 200`.
    - *Для бизнеса:* чат начинает «помнить» ложные указания.
*   **Шаги решения:**
    1. Guidance — opt-in, TTL, кнопка «сбросить указания».
        - **План:** Guidance не пишется молча из любого ответа.
        - **Как выполнить:** Opt-in чекбокс «запомнить уточнение». TTL (например 7 дней) в row. Кнопка сброса `is_active=False`.
    2. Не писать произвольный ответ пользователя в guidance без классификатора.
        - **План:** Классификатор или явный intent, не весь user text.
        - **Как выполнить:** Писать guidance только если clarification-flow и user подтвердил. Не из произвольного chat message.
    3. Лимит длины guidance; не подмешивать в extract_entities.
        - **План:** Не травить extract_entities.
        - **Как выполнить:** Grep: guidance не передаётся в `extract_entities_llm`. Лимит длины колонки (например 2k). Тест.

### 🚨 [Критичность: Low] | [Категория: Perf]
*   **Локация:** `app/app/services/ollama_client.py` `embed_texts` timeout 120 на **батч**, цикл батчей 64. `ensure_ollama` может стартовать процесс.
*   **Суть дефекта:** индекс часового созвона = много последовательных HTTP. Падение середины — persist/index частично (см. F).
*   **Чем вызвана (Первопричина / Root Cause):** лимит tokenize Ollama (комментарий в файле).
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* минуты CPU Ollama (num_gpu=0 — CPU).
    - *При нагрузке:* один Ollama на хост, API search тоже embed'ит запрос — очередь за воркером.
    - *Для бизнеса:* медленный поиск сразу после загрузки.
*   **Шаги решения:**
    1. Оставить батч 64; при fail батча — не DELETE всех chunks до успеха всех embed (embed полностью, потом одна транзакция write).
        - **План:** Падение батча embed не оставляет DELETE без INSERT.
        - **Как выполнить:** Сначала все `embed_texts` в память/tmp, **потом** одна транзакция DELETE+INSERT chunks/vectors. Fail embed → rollback, старый индекс жив (стык F1).
    2. Это стыкуется с F1 rollback.
        - **План:** Связка с rollback воркера.
        - **Как выполнить:** Не commit промежуточный после DELETE. F1 rollback покрывает exception.
    3. Search embed не стартовать Ollama-install на каждом /health.
        - **План:** Health не ставит Ollama.
        - **Как выполнить:** `ensure_ollama` не вызывать из `/health`. Search может ensure один раз с lock, не каждый poll бейджа.

---

## Модуль H. Knowledge HTTP (заметки, связи, review, search)

### 🚨 [Критичность: High] | [Категория: Безопасность]
*   **Локация:** `app/app/routers/knowledge.py` `create_link` (`POST /links`): нет проверки, что `project_id` существует/active, что entities принадлежат этому проекту, что user имеет право. Любой JWT.
*   **Суть дефекта:** можно связать сущности чужих проектов или несуществующие id (FK выключены — строка запишется). Graph injection.
*   **Чем вызвана (Первопричина / Root Cause):** endpoint «для будущего / интеграций» (PROJECT-MAP §5.8) без тех же `_project_or_404` + принадлежности, что у search.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* мусорные рёбра, ложные конфликты, ТЗ тянет links.
    - *При нагрузке:* n/a.
    - *Для бизнеса:* порча графа знаний, скрытый канал между проектами (все и так видят все проекты — но целостность графа).
*   **Шаги решения:**
    1. `_project_or_404`; обе entity существуют, `artifact.project_id == body.project_id`.
        - **План:** Ребро только внутри существующего активного проекта и его сущностей.
        - **Как выполнить:** `create_link`: `_project_or_404` (is_active). Load обеих Entity; `entity.artifact.project_id == body.project_id` (или entity.project_id если есть). Иначе 404.
    2. 404 если нет.
        - **План:** Несуществующие id — 404, не INSERT сироты.
        - **Как выполнить:** Даже после включения FK — явная проверка до insert, понятный JSON.
    3. Тест: link на entity другого проекта — 404.
        - **План:** Межпроектная связь ломается тестом.
        - **Как выполнить:** Тест: entity проекта A, POST link с project B — 404, links count не растёт.

### 🚨 [Критичность: Medium] | [Категория: Безопасность]
*   **Локация:** `knowledge.py` `review_entity` — любой авторизованный, не admin. `EntityReview.value_json` сырая строка в колонку. Нет проверки project membership (все проекты общие).
*   **Суть дефекта:** role `user` принимает/правит сущности. PLAN: resolve конфликтов и правка ТЗ — admin; review сущностей в PLAN «needs_review пока человек не принял» без ограничения роли. Расхождение ожиданий ИБ.
*   **Чем вызвана (Первопричина / Root Cause):** не довели RBAC дальше admin/manager на TZ и conflicts.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* любой сотрудник переписывает `value_json` и эмбеддинг.
    - *При нагрузке:* n/a.
    - *Для бизнеса:* неавторизованная «правка фактов» в памяти проекта.
*   **Шаги решения:**
    1. Зафиксировать в PLAN: кто review. Вариант: accept/reject — manager+, edit — admin.
        - **План:** Кто принимает сущности — явно в PLAN, не «любой JWT».
        - **Как выполнить:** Вариант: accept/reject `require_role("manager","admin")`, edit value_json — admin. Записать в PLAN § RBAC. Код `require_role` на `review_entity`.
    2. Валидировать `value_json` как JSON object, не произвольная строка гигабайт.
        - **План:** `value_json` — объект ограниченного размера.
        - **Как выполнить:** Pydantic: parse JSON, max length (например 32KiB), тип object. 422 на мусор. Не сырая строка в Text без лимита.
    3. Audit log user_id.
        - **План:** Кто правил факт — в аудите.
        - **Как выполнить:** Писать `EntityReview.user_id` (уже может быть) + `log_event` review. Не только timestamp.

### 🚨 [Критичность: Medium] | [Категория: Безопасность]
*   **Локация:** `knowledge.py` `create_note`: имя файла `user_note_{ts}_{user.name[:20]}.txt` без `sanitize_display_name`. `NoteCreate.refs` — `list[dict]`, `int(ref["id"])` KeyError/ValueError → 500.
*   **Суть дефекта:** `user.name` с `..` / слешами — path. `target_id` без FK — висячие refs.
*   **Чем вызвана (Первопричина / Root Cause):** имя с логина считали безопасным. sanitize есть для имени проекта, не переиспользовали.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* запись заметки вне `project_notes` при хитром name (зависит от `notes_dir / fname` — Path может уйти, если name содержит `..`).
    - *При нагрузке:* n/a.
    - *Для бизнеса:* порча FS, 500 на кривых refs.
*   **Шаги решения:**
    1. Имя файла только `user_note_{ts}_{user.id}.txt`.
        - **План:** Имя файла заметки не из user.name.
        - **Как выполнить:** `user_note_{unix_ts}_{user.id}.txt`. Имя пользователя только внутри файла как заголовок, после sanitize.
    2. `ensure_inside(notes_dir, path)`.
        - **План:** Path не выходит из notes_dir.
        - **Как выполнить:** `path.resolve()` + `ensure_inside(notes_dir, path)` (как LabRun.resolve). 400 если нет.
    3. Pydantic модель Ref: kind + id, 400 на ошибке, не 500.
        - **План:** Кривой refs — 400, не 500.
        - **Как выполнить:** Модель `NoteRefIn: kind: Literal[...]; id: int`. ValidationError → 422. Нет `int(ref["id"])` вручную.

### 🚨 [Критичность: Medium] | [Категория: Логика]
*   **Локация:** `knowledge.py` `resolve_conflict`: `winning_entity_id` не обязан быть source или target. Тогда `losing = source`, в Resolution пишется произвольный winner. Нет проверки `link.project` vs admin. Повторный resolve — unique `link_id` на Resolution → IntegrityError не обработан явно.
*   **Суть дефекта:** полузакрытый автомат конфликтов.
*   **Чем вызвана (Первопричина / Root Cause):** тонкий admin API.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* `loser.is_active = False` на источнике при неверном winner; граф врёт.
    - *При нагрузке:* n/a.
    - *Для бизнеса:* «решили конфликт» с уничтожением не той сущности.
*   **Шаги решения:**
    1. `winning_entity_id in {source, target}` иначе 400.
        - **План:** Победитель только source или target конфликта.
        - **Как выполнить:** `if winning_entity_id not in {link.source_id, link.target_id}: 400`. Не произвольный id.
    2. IntegrityError unique link → 409 already resolved.
        - **План:** Повторный resolve — 409.
        - **Как выполнить:** `except IntegrityError` unique link_id → 409 already resolved. Не 500.
    3. loser только противоположная сторона.
        - **План:** loser — противоположный конец ребра.
        - **Как выполнить:** `loser = target if winner==source else source`. `is_active=False` только на loser. Тест неверного winner не гасит source ошибочно.

### 🚨 [Критичность: Medium] | [Категория: Perf]
*   **Локация:** `knowledge.py` `list_conflicts` — N+1: на каждый POTENTIAL_CONFLICT SELECT Resolution, get Entity src/tgt.
*   **Суть дефекта:** классический ORM N+1.
*   **Чем вызвана (Первопричина / Root Cause):** мало конфликтов на старте.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* admin страница тормозит после seed_graph на большом проекте.
    - *При нагрузке:* SQLite.
    - *Для бизнеса:* очередь конфликтов не открывают, живут с противоречиями в ТЗ.
*   **Шаги решения:**
    1. `selectinload` / JOIN entities + LEFT JOIN resolutions WHERE resolution.id IS NULL.
        - **План:** Список конфликтов — один JOIN, не цикл SELECT.
        - **Как выполнить:** `selectinload` Entity src/tgt + outerjoin Resolution, `WHERE resolution.id IS NULL`. Один execute → DTO.
    2. Один запрос на список.
        - **План:** Страница admin не делает N+1.
        - **Как выполнить:** Тест/explain: при 20 конфликтах не 40+ queries. Можно assert query count в тесте с listener.

### 🚨 [Критичность: Low] | [Категория: Perf]
*   **Локация:** `knowledge.py` `semantic_search` `limit: int = 10` без `Query(le=...)`.
*   **Суть дефекта:** клиент ставит limit=1e9 → vec search LIMIT огромный, потом rerank.
*   **Чем вызвана (Первопричина / Root Cause):** забыли bound.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* RAM на hits, CPU cosine rerank.
    - *При нагрузке:* DoS authenticated.
    - *Для бизнеса:* простой поиска.
*   **Шаги решения:**
    1. `limit: Annotated[int, Query(ge=1, le=50)]`.
        - **План:** limit ограничен на границе API.
        - **Как выполнить:** `limit: Annotated[int, Query(ge=1, le=50)]` на `semantic_search`. Default 10.
    2. Тест limit=999 → 422.
        - **План:** Сверхлимит — 422, не vec LIMIT 1e9.
        - **Как выполнить:** Тест GET search `limit=999` → 422. `limit=50` → 200.

### 🚨 [Критичность: Low] | [Категория: Архитектура]
*   **Локация:** `app/app/routers/projects.py` `archive_project` — soft delete, файлы и jobs не трогает. `list_projects` скрывает. MCP/прямой artifact_id report всё ещё работает (pages не смотрит is_active проекта).
*   **Суть дефекта:** архив ≠ изоляция. Отчёт по id жив.
*   **Чем вызвана (Первопричина / Root Cause):** is_active как «убрать из списка».
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* утечка через старые закладки /report.
    - *При нагрузке:* n/a.
    - *Для бизнеса:* «проект удалили», запись созвона по URL на месте (усугубляется отсутствием JWT на report).
*   **Шаги решения:**
    1. Report/audio/MCP: 404 если `project.is_active is False`.
        - **План:** Архив скрывает байты и MCP, не только список.
        - **Как выполнить:** GET report/audio/lab file и MCP tools: если `project.is_active is False` → 404. Тот же хелпер `_project_active_or_404`.
    2. Опционально: физическое перемещение дерева в `archived/`.
        - **План:** Опционально унести дерево с канонического пути.
        - **Как выполнить:** Не обязательно в первом PR. Если делать: move `data/projects/{id}` → `data/archived/{id}` + запись path. Сломает относительные пути — отдельное решение.
    3. Jobs archived проекта не claim'ить.
        - **План:** Воркер не берёт job архива.
        - **Как выполнить:** `claim_next_job` JOIN projects is_active. Archived → job error или skip. Тест.

---

## Модуль I. MCP

### 🚨 [Критичность: High] | [Категория: Безопасность]
*   **Локация:** `app/app/routers/project_mcp.py` `mcp_project_endpoint` GET+POST; `app/app/services/project_mcp_tokens.py` SHA-256 без pepper; `authenticate_project_mcp_token` commit `last_used_at` **на каждый** вызов.
*   **Суть дефекта:** токен `primary_analitics_mcp_` + 32 urlsafe — энтропия ок, хранение sha256 ок для high-entropy. Но: нет срока жизни, нет ротации, нет привязки IP, read-only tools отдают **полные стенограммы** (`get_artifact_content`). `WEB_PUBLIC_URL` вшивается в cursor_config. Commit last_used на read — write amp.
*   **Чем вызвана (Первопричина / Root Cause):** MCP как «ключ на проект вместо JWT портала». Удобство Cursor с интернета.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* украденный ключ из чата/скрина = постоянный read всего проекта, в обход 12h JWT. last_used commit конфликтует с воркером (locked).
    - *При нагрузке:* Cursor стучит tools часто → непрерывные WRITE SQLite.
    - *Для бизнеса:* внешний агент с ключом читает все созвоны; ключ в JSON example в UI легко уходит в тикет.
*   **Шаги решения:**
    1. TTL + last_used throttle (обновлять раз в N минут, не каждый tool call).
        - **План:** Ключ живёт ограниченно; last_used не пишет БД каждый tool.
        - **Как выполнить:** Колонка `expires_at`. Auth fail если now>exp. `last_used_at` обновлять если now-last > 5–15 мин (throttle). Короче TTL по умолчанию (например 7 дней).
    2. Pepper HMAC(SECRET_KEY, token) вместо голого sha256 — по желанию.
        - **План:** Pepper по желанию, не вместо TTL.
        - **Как выполнить:** Хранить HMAC(SECRET_KEY, token) вместо sha256(token) — миграция: перевыпуск ключей. Можно вторым PR.
    3. Не отдавать полный transcript tool'ом без отдельного scope; или короткий TTL ключей.
        - **План:** Полный transcript — отдельный scope или короткий ключ.
        - **Как выполнить:** Tools: `get_artifact_content` требует scope `transcript` при выпуске. Default ключ — метаданные+поиск без полного текста. Либо TTL 24ч если полный текст включён.
    4. Не показывать сырой токен повторно; предупреждение в UI что ключ = доступ ко всем созвонам проекта.
        - **План:** UI не показывает сырой ключ повторно.
        - **Как выполнить:** После create — один раз plaintext. List — только prefix+created. Warning в шаблоне: ключ = read всех созвонов проекта.
    5. Отзыв при archive проекта.
        - **План:** Архив проекта отзывает MCP.
        - **Как выполнить:** В `archive_project`: revoke all tokens проекта (deleted_at / expires_at=now). Тест MCP 401 после archive.

### 🚨 [Критичность: Medium] | [Категория: Безопасность]
*   **Локация:** `project_mcp.py` `mcp_connection_info` доступен любому JWT; выпуск токена admin/manager. `user` роль видит URL и инструкцию, не список токенов (list — manager+).
*   **Суть дефекта:** информация о внешнем endpoint публикуется широко внутри орг. Само по себе не дыра, в связке с public URL — карта атаки.
*   **Чем вызвана (Первопричина / Root Cause):** прозрачность для настройки Cursor.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* n/a.
    - *При нагрузке:* n/a.
    - *Для бизнеса:* social engineering «дай MCP ключ».
*   **Шаги решения:**
    1. `mcp/info` — только admin/manager.
        - **План:** Карта внешнего endpoint не для роли user.
        - **Как выполнить:** `mcp_connection_info`: `require_role("admin","manager")`. 403 user.
    2. user видит «нет доступа к MCP» без URL белого IP.
        - **План:** User не видит белый IP в инструкции.
        - **Как выполнить:** Ответ user: `{ "allowed": false }` без WEB_PUBLIC_URL. Не палить порт.

---

## Модуль J. GPU-оркестрация и процессы

### 🚨 [Критичность: High] | [Категория: Архитектура]
*   **Локация:** `app/app/services/gpu_phase.py` `prepare_for_asr` / `prepare_for_llm`; вызовы из worker lab и из **API** (`pipeline_knowledge.prepare_for_llm`, chat тоже может пойти в llm без unload ASR).
*   **Суть дефекта:** инвариант «ASR и Gemma не сосуществуют» соблюдается **внутри воркера** при lmstudio. API-процесс может загрузить Gemma или дернуть Cursor, пока воркер на ONNX. Нет глобального GPU mutex между API и worker, только файл lock самого воркера.
*   **Чем вызвана (Первопричина / Root Cause):** sync-кнопки (чат, TZ, summary) сознательно оставили в API (`PROJECT-MAP` §3). GPU skill описывает фазы только для worker pipeline.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* CUDA OOM, драйвер reset, смерть lab_pipeline.
    - *При нагрузке:* один пользователь в чате + один созвон в очереди = гарантированный конфликт на 24 ГБ.
    - *Для бизнеса:* сорванные расшифровки, перезагрузка машины.
*   **Шаги решения:**
    1. Файл-lock `gpu_slot.lock` общий для API LLM-кнопок и worker ASR (acquire в prepare_for_asr/llm и chat).
        - **План:** Один слот GPU на хост между API и worker.
        - **Как выполнить:** Файл `data/locks/gpu_slot.lock` (msvcrt как SingleInstance). `prepare_for_asr`, `prepare_for_llm`, chat lmstudio — acquire. Worker держит на весь ASR. API — на время LLM.
    2. Чат/TZ/summary при занятом ASR — 409 «GPU занят расшифровкой».
        - **План:** Чат во время расшифровки — 409, не CUDA OOM.
        - **Как выполнить:** Если lock занят воркером: HTTP 409 `GPU busy (asr)`. UI сообщение. Не ждать 120с nvidia-smi в запросе чата.
    3. Либо все LLM только в worker queue (предпочтительно для lmstudio).
        - **План:** Для lmstudio предпочтительно все LLM в worker.
        - **Как выполнить:** Enqueue chat/summary/tz как jobs. API не вызывает lms load. Cursor-режим: mutex всё равно, чтобы не бить CPU/RAM агентами параллельно ASR по желанию — минимум для Gemma.

### 🚨 [Критичность: Medium] | [Категория: Perf]
*   **Локация:** `gpu_phase.py` `wait_vram_below` poll nvidia-smi каждые 3 с до 120 с; `lms unload/load` subprocess.
*   **Суть дефекта:** грубые CLI-фазы, нет учёта фрагментации VRAM. threshold 2500 MiB — эвристика.
*   **Чем вызвана (Первопричина / Root Cause):** нет NVML-интеграции глубже csv.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* ложный RuntimeError «VRAM still X», job error, retry ASR.
    - *При нагрузке:* n/a.
    - *Для бизнеса:* ложные сбои пайплайна.
*   **Шаги решения:**
    1. Повторить nvidia-smi после unload с бóльшим timeout.
        - **План:** Unload может занять больше 120с при фрагментации.
        - **Как выполнить:** Увеличить timeout wait_vram / повторный nvidia-smi цикл после `lms unload`. Не один проход.
    2. Логировать fragmentation; не fail job сразу — retry unload.
        - **План:** Ложный fail не должен сразу убивать job.
        - **Как выполнить:** Retry unload 2–3 раза, log used MiB. Потом error. Не RuntimeError на первом превышении 2500.
    3. Порог вынести в .env, не магическое 2500.
        - **План:** Порог не магическое число в коде.
        - **Как выполнить:** `VRAM_FREE_THRESHOLD_MIB` в Settings/.env. `.env.example` с комментарием. gpu_phase читает settings.

### 🚨 [Критичность: Low] | [Категория: Perf]
*   **Локация:** `app/app/main.py` `health` → `nvidia_memory_used_mib()` каждый запрос; бейдж LLM poll.
*   **Суть дефекта:** nvidia-smi spawn на каждый health.
*   **Чем вызвана (Первопричина / Root Cause):** удобный JSON для UI.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* лишние процессы.
    - *При нагрузке:* health-check шторм.
    - *Для бизнеса:* шум в логах, микролаги.
*   **Шаги решения:**
    1. Убрать nvidia-smi из публичного `/health`.
        - **План:** Публичный health не спавнит nvidia-smi.
        - **Как выполнить:** Связка A8: убрать `nvidia_memory_used_mib()` из `/health`.
    2. Кэш VRAM 5–10 с если оставить в ready.
        - **План:** Ready может кэшировать VRAM.
        - **Как выполнить:** Кэш 5–10с в модуле gpu_phase (process-local). Ready с auth/loopback читает кэш. Не каждый poll бейджа — spawn.

---

## Модуль K. Фронтенд (портал)

### 🚨 [Критичность: Medium] | [Категория: Безопасность]
*   **Локация:** `app/app/templates/project.html` `escapeHtml` + `renderMarkdown` / `innerHTML` (~994+). Порядок: escape, потом `**` → `<strong>`. Chat/TZ так же. `conflicts.html` `JSON.stringify` в innerHTML без escape — ключи/значения сущностей с `<` ломают HTML/XSS если conflict API когда-либо отдаст HTML.
*   **Суть дефекта:** conflicts — admin API, но XSS в admin = кража JWT. report HTML вне этого санитайза.
*   **Чем вызвана (Первопричина / Root Cause):** самописный markdown вместо DOMPurify. conflicts — dump JSON «для отладки».
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* XSS при `<` в value_json конфликта.
    - *При нагрузке:* n/a.
    - *Для бизнеса:* захват admin-сессии.
*   **Шаги решения:**
    1. `escapeHtml(JSON.stringify(...))` или textContent.
        - **План:** JSON конфликта не идёт в innerHTML сырьём.
        - **Как выполнить:** `conflicts.html`: `textContent` или `escapeHtml(JSON.stringify(...))` перед innerHTML. Сущности с `<` не исполняются.
    2. CSP `default-src 'self'` в base.html; report HTML — свои nonce/ограничения.
        - **План:** CSP на портале и на report.
        - **Как выполнить:** `base.html` CSP (A6). Генератор `transcript.html`: без inline script с данными пользователя; script только nonce/свой static. Не `unsafe-inline` без нужды.
    3. DOMPurify только если нужен богатый markdown.
        - **План:** Богатый markdown — только с санитайзером.
        - **Как выполнить:** Если нужен HTML из markdown — DOMPurify в static vendor. Иначе оставить escape+ограниченный `**`. Не подключать CDN Purify (A9).

### 🚨 [Критичность: Medium] | [Категория: Архитектура]
*   **Локация:** `project.html` ~2000 строк JS в шаблоне; дубли chat.html.
*   **Суть дефекта:** два клиента чата, расхождение багов. Нет CSP в `base.html`.
*   **Чем вызвана (Первопричина / Root Cause):** Jinja MVP, страница чата скопирована.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* фикс XSS в одном месте не чинит другое.
    - *При нагрузке:* тяжёлый parse JS.
    - *Для бизнеса:* медленный UI, двойные баги.
*   **Шаги решения:**
    1. Один `static/chat.js` подключать в project.html и chat.html.
        - **План:** Один клиент чата.
        - **Как выполнить:** Вынести логику в `app/app/static/chat.js`. `project.html` и `chat.html` подключают его. Удалить вторую копию.
    2. CSP в base.
        - **План:** CSP после выноса inline.
        - **Как выполнить:** Когда JS в файле — CSP без огромного inline. nonce на остатки или внешний файл only.
    3. Не держать 2000 строк в Jinja.
        - **План:** Не держать 2000 строк в Jinja.
        - **Как выполнить:** Шаблон: разметка + data-атрибуты project id. Логика в static. Ревью размера файла.

### 🚨 [Критичность: Low] | [Категория: Логика]
*   **Локация:** `project.html` confirm: клиент смотрит `speakers-status`, сервер всё равно проверяет. `btn.disabled` снимается только на ошибке; успех ждёт poll. Двойной клик до disabled — два fetch (гонка сервера, модуль E).
*   **Суть дефекта:** UI lock не синхронен с сервером.
*   **Чем вызвана (Первопричина / Root Cause):** нет `guardButton` на confirm (есть на других actions).
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* двойной knowledge.
    - *При нагрузке:* n/a.
    - *Для бизнеса:* см. confirm race.
*   **Шаги решения:**
    1. `PrimaryAnalitics.guardButton` на `.speaker-confirm-btn`.
        - **План:** Кнопка confirm с тем же guard, что тяжёлые действия.
        - **Как выполнить:** `bindSpeakerConfirmButtons`: `PrimaryAnalitics.guardButton` до fetch. disabled до ответа.
    2. Серверная атомарность (E1) важнее UI.
        - **План:** UI не заменяет атомарный UPDATE (E1).
        - **Как выполнить:** Не считать задачу закрытой после guardButton. Сервер — источник. Тест UI не обязателен, серверный тест E1 обязателен.

---

## Модуль L. Проекты, RBAC-модель, отсутствие тенантов

### 🚨 [Критичность: Medium] | [Категория: Архитектура]
*   **Локация:** нет таблицы membership. `list_projects` — все `is_active`. `get_current_user` без фильтра проекта. PLAN это описывает.
*   **Суть дефекта:** любой user читает все проекты, загружает в любой id. Это не баг относительно PLAN, это **жёсткий** trust boundary «вся фирма = один тенант». На публичном IP это один пароль роли от всех созвонов всех проектов.
*   **Чем вызвана (Первопричина / Root Cause):** 20 человек, один отдел.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* IDOR между проектами «разрешён».
    - *При нагрузке:* один db на всех.
    - *Для бизнеса:* при расширении на подрядчиков — моментальная утечка. Регулятор не отличит «так задумано» от дыры, если договор «разграничение проектов».
*   **Шаги решения:**
    1. Явно задокументировать в PLAN/DECISIONS: single-tenant, все видят всё.
        - **План:** Single-tenant — явное решение, не молчание.
        - **Как выполнить:** DECISIONS + PLAN: все роли видят все активные проекты. IDOR между проектами «разрешён» внутри фирмы.
    2. Если появятся подрядчики — таблица `project_members` и фильтр во всех SELECT, MCP, report.
        - **План:** Подрядчики потребуют membership.
        - **Как выполнить:** Когда понадобится: таблица `project_members`, фильтр во всех list/get, MCP, report. Не делать таблицу «на вырост» пустой сейчас, если пользователь не просил — только документ.
    3. До этого — не вешать портал в интернет без VPN.
        - **План:** Публичный IP без VPN несовместим с одной ролью на все созвоны.
        - **Как выполнить:** До membership: reverse-proxy allowlist/VPN (A5). Preflight warning на не-loopback bind.

### 🚨 [Критичность: Low] | [Категория: Логика]
*   **Локация:** `projects.py` `create_project`: commit строки, потом `create_project_tree`. Падение mkdir — проект в БД без дерева.
*   **Суть дефекта:** снова диск vs row.
*   **Чем вызвана (Первопричина / Root Cause):** то же, что upload.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* 500 на upload в проект без папок (часть кода mkdir parents=True — может самопочиниться).
    - *При нагрузке:* n/a.
    - *Для бизнеса:* «проект есть, загрузка падает».
*   **Шаги решения:**
    1. Сначала mkdir tree, потом commit; или commit + compensating delete row.
        - **План:** Каталоги существуют до commit row, либо row откатывается.
        - **Как выполнить:** `create_project`: `create_project_tree` (идемпотентный mkdir) **затем** INSERT+commit. Если INSERT fail — дерево пустое допустимо (сирота папок лучше сироты row для upload). Либо commit + compensating delete row при mkdir fail.
    2. `create_project_tree` идемпотентен (уже mkdir).
        - **План:** mkdir уже можно звать повторно.
        - **Как выполнить:** Не менять `exist_ok=True`. Тест: повтор create_project_tree не падает.
    3. Транзакция не покроет диск — порядок «дерево затем row» безопаснее для upload.
        - **План:** Диск и SQLite без XA — важен порядок.
        - **Как выполнить:** Комментарий в `projects.py`: дерево затем row, чтобы upload сразу видел path. Обратный порядок только с delete row compensating.

---

## Модуль M. Наблюдаемость, секреты, тесты

### 🚨 [Критичность: Medium] | [Категория: Архитектура]
*   **Локация:** `app/app/logging_setup.py` `FileHandler` без rotation; `_SECRET_KEYS` фильтр только на ключи полей `log_event`, не на `logger.exception` и не на `error=exc` в gpu_worker.
*   **Суть дефекта:** `data/logs/api.log` / `worker.log` растут безгранично. exception может содержать куски стенограммы / пути. `login.attempt` пишет telegram_id (PII).
*   **Чем вызвана (Первопричина / Root Cause):** файловый лог для отладки пайплайна.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* диск data/.
    - *При нагрузке:* диск full → падение записи артефактов.
    - *Для бизнеса:* PII в логах, нет retention.
*   **Шаги решения:**
    1. `RotatingFileHandler` / TimedRotating, лимит размера.
        - **План:** Логи не растут безлимитно.
        - **Как выполнить:** `RotatingFileHandler` (например 20 МиБ × 5) в `logging_setup.py` для api.log и worker.log. Не один FileHandler.
    2. `log_event` уже фильтрует ключи; для exception — не логировать полный transcript.
        - **План:** exception не содержит стенограмму.
        - **Как выполнить:** Не логировать полный transcript в `logger.exception`. Обрезать сообщение. `log_event` scrub уже есть — не обходить его через extra=exc с текстом чанка.
    3. telegram_id в login — hash или truncate.
        - **План:** telegram_id в login — ПДн.
        - **Как выполнить:** `login.attempt`: hash/truncate id (последние 4). Не полный идентификатор в api.log.
    4. Retention политика на data/logs.
        - **План:** Retention на диске.
        - **Как выполнить:** Документ: хранить N дней / N файлов rotation. Cron не обязателен если rotation хватает. Не коммитить data/logs.

### 🚨 [Критичность: Medium] | [Категория: Безопасность]
*   **Локация:** `app/app/tests/test_lab_report.py` GET report-lab без токена assert 200; `test_audio_playback.py` GET `/audio` без токена; `test_artifact_download.py` наоборот ждёт 401.
*   **Суть дефекта:** регрессионный набор **фиксирует** дыру просмотра и **запрещает** дыру скачивания. Следующий разработчик «починивший» JWT на report сломает CI и откатится.
*   **Чем вызвана (Первопричина / Root Cause):** тесты писались как контракт текущего (небезопасного) UX плеера.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* n/a.
    - *При нагрузке:* n/a.
    - *Для бизнеса:* институционализация уязвимости. ИБ-фикс конфликтует с «зелёным пайплайном».
*   **Шаги решения:**
    1. Вместе с закрытием report/audio: переписать тесты на 401 anonymous.
        - **План:** CI требует сессию на report/audio.
        - **Как выполнить:** Вместе с A1: `test_lab_report.py` / `test_audio_playback.py` anonymous 401. Не 200.
    2. Фикстура авторизованного клиента для 200.
        - **План:** Позитивный путь с фикстурой логина.
        - **Как выполнить:** Общая фикстура `auth_client` как download-тесты. GET report 200, тело HTML.
    3. Явный тест «download и report оба требуют сессию».
        - **План:** Симметрия download и report.
        - **Как выполнить:** Один тест: оба эндпоинта без сессии 401, с сессией 200. Чтобы нельзя было откатить только одно.

### 🚨 [Критичность: Low] | [Категория: Архитектура]
*   **Локация:** `docs/DECISIONS.md` всё ещё содержит слои Whisper/Tone/Gemma-склейки окон как канон доверия в середине документа, при том что прод — lab dual-hear. `PROJECT-MAP` ближе к коду.
*   **Суть дефекта:** три источника истины (DECISIONS / MAP / код) расходятся. Агент и человек чинят не тот конвейер.
*   **Чем вызвана (Первопричина / Root Cause):** cutover 2026-08-26 без вычистки исторических разделов DECISIONS.
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* n/a.
    - *При нагрузке:* n/a.
    - *Для бизнеса:* регрессии, возврат запрещённого ансамбля «по документу».
*   **Шаги решения:**
    1. Пометить исторические разделы Whisper/Tone/Gemma-merge как SUPERSEDED, канон — dual-hear § cutover.
        - **План:** Исторический ансамбль помечен, не канон.
        - **Как выполнить:** В `DECISIONS.md` блоки Whisper/T-one/Gemma-merge окон: заголовок SUPERSEDED, дата cutover, ссылка на dual-hear. Не удалять историю молча — пометить.
    2. Одна отсылка на PROJECT-MAP §4.1.
        - **План:** Один указатель на актуальную карту.
        - **Как выполнить:** В начале DECISIONS: «конвейер см. PROJECT-MAP §4.1». Не дублировать пайплайн целиком.
    3. `lightrag_sync_docs` после правки.
        - **План:** Граф доков обновить.
        - **Как выполнить:** `venv\Scripts\python.exe scripts\lightrag_sync_docs.py` и index --wait. `index_project_map.py` если карта менялась.

### 🚨 [Критичность: Low] | [Категория: Безопасность]
*   **Локация:** git status в рабочей копии содержит `data/app.db`, артефакты прогонов, `data/gigaam/embed_*.json`. `.env.example` с `GIGAAM_API_KEY=gam_change_me`.
*   **Суть дефекта:** если db/артефакты попадут в commit — полная утечка созвонов. Default GigaAM key если не сменён — открытый второй слух на localhost (compose 127.0.0.1, риск ниже).
*   **Чем вызвана (Первопричина / Root Cause):** data/ должен быть в gitignore; локальные прогоны рядом с кодом по инварианту «всё в D:\Primary_Analitics».
*   **На что влияет (Влияние / Impact Analysis):**
    - *В рантайме:* n/a.
    - *При нагрузке:* n/a.
    - *Для бизнеса:* один ошибочный `git add data` уничтожает периметр.
*   **Шаги решения:**
    1. `data/app.db` уже в gitignore — проверить `git status` и не `git add -f`.
        - **План:** Рабочая копия не стейджит db и созвоны.
        - **Как выполнить:** `git status`: не `git add -f data/app.db`. Проверить `.gitignore` покрывает `data/*.db`, `data/projects/`. Локальные untracked оставить.
    2. Pre-commit: reject `data/projects`, `*.db`.
        - **План:** Hook режет случайный add.
        - **Как выполнить:** Pre-commit (если появится) reject path `data/projects`, `*.db`, `*.wav`. До hook — дисциплина + gitignore.
    3. Сменить default `GIGAAM_API_KEY` в example на пустой; preflight fail если `gam_change_me` на проде.
        - **План:** Пример ключа GigaAM не рабочий default.
        - **Как выполнить:** `.env.example`: `GIGAAM_API_KEY=` пустой. Preflight fail если значение `gam_change_me` и не loopback-only compose. Не ломать локальный docker без ключа сообщением, а не тихим default.

---

## Сводка плотности дефектов (без приоритизации «спринтов»)

| Зона | Blocker | High | Medium | Low |
|---|---|---|---|---|
| HTTP auth / reports / speaker API | 3 | 3 | 4 | 1 |
| SQLite / схема | 0 | 2 | 3 | 2 |
| Upload / jobs / worker | 0 | 3 | 5 | 0 |
| Lab / Docker / GPU | 0 | 3 | 5 | 2 |
| Speakers service | 0 | 1 | 2 | 1 |
| Knowledge / graph / TZ | 0 | 2 | 6 | 1 |
| Chat / LLM / Ollama | 0 | 1 | 3 | 2 |
| Knowledge HTTP / MCP / RBAC | 0 | 2 | 6 | 2 |
| Frontend / tests / docs / ops | 0 | 0 | 4 | 3 |

Физический корень большинства Blocker/High: **сессия живёт в JS, а канонические байты созвона (HTML, WAV, lab FS) и мутации спикеров живут как анонимные HTTP-ресурсы**, при том что диск и SQLite **не в одной транзакции**, а единственный GPU-воркер **сериализует часы работы** и разделяет карту с API-процессом без mutex. Остальное — следствия этой развилки (мелкие commit, мёртвый `job.phase`, except→`transcript_ready`, O(n·m) граф, блокирующий чат).

---

## Совместимость с текущим функционалом

Съёмка UX/контрактов: `app.js` (Bearer + localStorage), `login.html`, `pages.py`, `speaker_profiles` confirm, `gpu_worker` except, `pipeline.py` диспетчер по `artifact.status`, `project.html` чат, `artifact_audio.py` `clip=240`, `docker_runtime.py`, `test_health.py`, `schemas/artifact.py`.

**Вердикт:** дефекты в отчёте подтверждаются кодом. **Ремонт безболезненен только при двойном контуре и порядке ниже.** Если взять шаги буквально и выкатить пакетом — пользователь увидит «не логинится», «нет звука», «пустой ответ ИИ», «не запоминается голос», «портал не стартует».

### Что в шагах уже расходится с кодом (не ломать повторно)

*   `ArtifactOut` **уже без** `transcription_text` (`schemas/artifact.py`). JSON списка стенограмму не возит. Колонку ORM всё равно грузит; в `list_artifacts` она нужна ветке документов: `elif row.transcription_text` → URL отчёта. Обнулить Text (B5.3) **без** замены на «есть файл отчёта / parsed txt» — у документов пропадёт ссылка на HTML.
*   Чат **уже** умеет pending + poll `chat/history` каждые 3 с. Но `POST /chat` всё ещё sync и отдаёт `{answer, sources}`. `project.html` при 202 (`r.ok === true`) рисует «Пустой ответ» и **не** стартует poll. Шаг G1 без правки этого fetch — регресс, не улучшение.
*   Страница `/login` есть (`login.html`, `pages.py`). Редирект шеллов на неё безопасен, если `/login` и `/static` **исключены** из auth middleware. Иначе цикл редиректов.
*   Confirm **уже** ставит `speakers_confirmed` и только потом `Job(phase="knowledge")`. Воркер ветвится по **статусу**, не по phase. Включать чтение `job.phase` как диспетчер — риск no-op/error на живом confirm. Безопаснее вычистить phase из карты, не оживлять.
*   Cursor SDK уже `setting_sources=[]`. Ломать sense/proof сменой cwd без проверки, что скрипты не читают относительные пути из корня репо.
*   Канонический `transcript.html` / match: `audio` **без** `clip`. `clip=240` живёт в two-voice/raw diar (`artifact_audio.py` ~566). Удаление query `clip` → 4-минутные HTML на диске просят `?clip=240` и получают 422/тишину. Правильно: **игнорировать** clip, всегда свой WAV артефакта; HTML пересобрать лениво.
*   `SECRET_KEY=change-me` в `.env.example`, `Field(min_length=8)`. Шаг `min_length=32` без ротации ключа: стенд с коротким ключом **не стартует**. Ротация ключа = все сессии мертвы, всем заново войти (это ок, если предупредить).
*   API создания пользователя **нет**. Выключить автопровижининг (A5.1) без `POST /admin/users` и экрана у admin — новый сотрудник с новым Telegram ID не входит. Это текущая модель PLAN §2.

### Жёсткие поломки (процесс «лежит» или тихий регресс пайплайна)

| Если сделать буквально | Что увидит пользователь / воркер | Как сделать без боли |
|---|---|---|
| Cookie `Secure` при `WEB_PUBLIC_URL=http://203.0.113.10:777` | Браузер **не сохранит** cookie на HTTP. После «успешного» логина снова `/login`. Весь портал недоступен с белого IP. | `Secure` только если request/URL https. На текущем http — SameSite=Lax **без** Secure. |
| CSP `default-src 'self'` до выноса JS | Все экраны пустые: `base.html` + `project.html` (~2000 строк) — **inline** `<script>`. Bootstrap с CDN тоже умрёт, пока vendor не локальный. | Сначала vendor Bootstrap (A9), потом `static/*.js`, **потом** CSP. Не наоборот. |
| `Depends(get_current_user)` на report/audio **до** cookie | Плеер `<audio src>` и закладки отчёта — 401, тишина. Скачивание через `PrimaryAnalitics.api` ещё работает (Bearer). «Отчёт открылся, звука нет». | Сначала login ставит cookie **и** по-прежнему принимает Bearer. Потом закрыть GET. Старые JWT в localStorage живут до истечения 720 мин. |
| Auth на весь `speaker_profiles.py` **до** cookie | Кнопки в match-HTML (`fetch` без `Authorization`) — 401. «Запомнить голос» / confirm с отчёта мертвы. С проекта (`PrimaryAnalitics.api`) ещё живы. | Тот же двойной контур: cookie на same-origin `fetch` уходит сама (`credentials` по умолчанию same-origin). Bearer оставить для `app.js`. |
| INSERT артефакта+job **queued**, потом `move` | Воркер успеет claim до появления файла → `error`, созвон «сломан», повтор complete невозможен. | Статус `uploading` / не queued, пока move не ок; **затем** enqueue. Либо pending-каталог, который воркер не берёт. |
| `PRAGMA foreign_keys=ON` на живой БД с сиротами | Первый INSERT/DELETE с FK — `IntegrityError`, 500 на загрузке/confirm. | Сначала скрипт сирот на **копии** db, потом pragma. |
| Unique `(project_id, sha256)` при уже существующих дублях | Alembic/upgrade падает, API не поднимается. | Схлопнуть дубли, потом индекс. |
| `create_all` убрать из lifespan, alembic не stamp | Тесты и стенд: «таблицы нет». | Тесты оставляют create_all; прод — `stamp` + upgrade. Один деплой. |
| `except` → `speakers_pending`, Telegram всё ещё шлёт PK sqlite | Созвон честно встанет на confirm (правильно), но пуш «подтвердите» **по-прежнему не дойдёт**. Оператор без открытой вкладки не узнает. | В том же изменении: `user.telegram_id`. Иначе «безболезненно» только для тех, кто смотрит UI. |
| `docker compose up -d gigaam` + `profiles: legacy`, а `ASR_CONTAINERS` и `ensure_asr_containers` ещё тройка | `docker start primaryanalitics-whisper` → ошибка / GPU снова забит, если старые контейнеры живут. Прод-воркер lab зовёт `ensure_container(gigaam)`, но CLI `asr/run_pipeline.py` зовёт **всю** тройку. | Менять кортеж, `_compose_up`, CLI и тесты `test_docker_runtime.py` одним коммитом. Существующие whisper/tone — `docker stop` отдельно, не в горячем ASR. |
| `process_audio_artifact` вырезать, старые артефакты без lab run | Повтор job → `error` «нужен rerun lab». Старый корпус без lab не доедет сам. | Сообщение в UI + явная кнопка lab; не молчаливый Whisper (это цель), но не «тихий fail» без текста. |
| Чат/TZ 409 GPU busy без текста в UI | `throw new Error('chat failed')` — красное «Не удалось получить ответ», не «идёт расшифровка». | Маппинг 409 → человеческая строка в `askProjectChat` / TZ. Поведение правильное, формулировка сейчас сломает доверие. |

### Боль без падения процесса (пользователь «чинит руками»)

*   **JWT 720 → 15–60 мин без refresh:** за время одного lab-прогона (часы) сессия умрёт. Poll 3 с получит 401 → `clearSession` → выкинет на логин **посреди ожидания спикеров**. Не укорачивать TTL, пока нет refresh или пока TTL ≥ длительности типичного ASR.
*   **Rate limit login по IP:** ~20 человек за NAT / одним белым IP. 5 неверных паролей одного — **лок всего офиса**. Лимит по `(telegram_id)` + мягче по IP; не только IP.
*   **Origin CSRF:** заходят и по IP, и по DNS. Проверка Origin === один host режет половину POST. Allowlist из `WEB_PUBLIC_URL` + фактический Host.
*   **MCP TTL на уже выданные ключи:** Cursor у всех перестанет читать проект, пока не выпустят новый токен. TTL только на **новые** ключи; старые — предупреждение в UI, не мгновенный revoke (revoke — при archive, это ок).
*   **Scope без полного transcript:** агенты, которые уже зовут `get_artifact_content`, сломаются. Default старых ключей оставить полным текстом **или** один раз показать «перевыпустите ключ».
*   **`mcp/info` только manager+:** в `project.html` блок MCP грузится у всех залогиненных. User увидит 403 / пустой URL. Скрыть блок по роли на клиенте в том же изменении.
*   **review только manager+:** роль `user` сейчас принимает сущности. Если так задумано PLAN — ок с объявлением; иначе «кнопка Review 403» без объяснения.
*   **Авто-склейка голосов без name-boost:** уже смешанные global-профили не разъедутся сами. Новые confirm могут дать **второго** «Ивана». Не баг старта, ручная чистка merge.
*   **Первый клик «Отчёт» медленнее,** если убрать `ensure_audio_report` из poll списка. Ссылка не 404: GET `/report` сам соберёт HTML. Это пауза, не ошибка.
*   **Закладки `/artifacts/{id}/report` без сессии** перестанут открываться — это цель ИБ. Сообщить команде: «ссылка на стенограмму только после входа». Скачивание уже 401 — поведение выровняется.
*   **Смена `SECRET_KEY`:** все должны нажать Войти ещё раз. Не делать в том же окне, что укорочение TTL.

### Обязательный порядок (иначе каждый следующий шаг ломает предыдущий UX)

1. **Двойная сессия:** login `Set-Cookie` + JSON `access_token` как сейчас; `get_current_user` читает cookie **или** Bearer. `app.js` не трогать в этом коммите (кроме `credentials` не обязателен на same-origin). Никто не перелогинивается принудительно.
2. **Vendor Bootstrap** в `/static` (A9). CDN можно убрать сразу: UI тот же. Не CSP.
3. **clip:** игнорировать query, свой файл; не 422. Старые HTML с `?clip=240` играют **этот** созвон целиком — лучше, чем 4 минуты чужого WAV.
4. **Auth report/audio + speaker API + тесты 401** — один коммит. Иначе CI откатит ИБ, а match-страница умрёт на полшага.
5. **Telegram `telegram_id` + except → `speakers_pending`** вместе. Иначе честный стоп без пуша.
6. **Upload:** не queued до байт на месте. Потом unique sha256 (после схлопывания дублей).
7. **FK / alembic** — после скрипта сирот, на копии db, окно «портал 30 с».
8. **Health:** `{status:ok}` на `/health`; поля очереди/LLM на `/health/ready`. В том же коммите: `test_health.py`, бейдж в `app.js`, `urlopen` в `main.py` (ему достаточно 200).
9. **Чат 202 / GPU 409** — только вместе с ветками в `project.html` / `chat.html`. До этого оставлять sync JSON.
10. **CSP, короткий JWT, выключение провижининга, MCP TTL, RBAC review** — после того, как сессия и экраны стабильны; каждое с объявлением пользователям.

### Что можно чинить сразу, пользователь не заметит

Исключения без `str(exc)` наружу; PyJWT вместо jose (те же HS256 JWT, Bearer живой); лимит `search?limit=`; `winning_entity_id in {source,target}`; имя файла заметки `user.id`; `BEGIN IMMEDIATE` на claim; rollback в except воркера **до** commit error; не `capture_output` на длинных lab-шагах; compose up **сервиса** gigaam (если не трогать profiles в том же PR); не печатать env в RuntimeError; pre-commit на `data/*.db`.

### Критерий «ремонт прошёл без ошибок»

После каждого пакета: логин → список проектов → открыть проект (poll 3 с) → открыть `/report` **звук есть** → match «запомнить» → confirm → job knowledge не no-op → чат ответ не «Пустой» → download 200 с сессией и 401 без. Пока этот контур зелёный, следующий пакет. Если выкатить A1–M4 одним диффом — контур не зелёный.

---

## План выполнения (рабочий порядок)

Не этап PLAN.md и не спринт: пакеты, которые можно выкатить по одному. Следующий пакет не начинается, пока зелёный контур выше и pytest затронутых тестов. Карта (`PROJECT-MAP` §5.2/§5.13, `manifest.yaml`) обновляется **вместе** с пакетом, который меняет кнопку/endpoint/статус.

Инварианты на весь ремонт: dual-hear не трогать; `job.phase` не оживлять; JWT TTL 720 не резать; cookie `Secure` только на https; CSP не включать, пока JS в Jinja.

### Пакет 0 — опора (нет смены поведения)

*   Фикстура авторизованного TestClient (как download-тесты) + явный anonymous-клиент. Её будут ждать пакеты 1–2.
*   Не деплой, не смена `.env`.

### Пакет 1 — двойная сессия + clip + Bootstrap с диска

Цель: ничего не отнять у уже залогиненных; убрать межсозвонную утечку `clip`.

*   `auth.py` login: `Set-Cookie` (HttpOnly, SameSite=Lax, `Secure` iff https). JSON `access_token` как сейчас.
*   `dependencies.py` `get_current_user`: cookie **или** Bearer. TTL не менять.
*   Logout: сброс cookie + как сейчас `clearSession`.
*   `pages.py` `artifact_audio_file`: query `clip` **игнорировать**, всегда WAV/AAC **этого** артефакта. Не 422.
*   `artifact_audio.py`: перестать писать `?clip=240` в новые HTML.
*   Bootstrap 5.3.3 в `app/app/static/vendor/`; `base.html` / `index.html` без jsDelivr.
*   Тесты: login ставит cookie; Bearer без cookie всё ещё 200 на `/projects`; два артефакта, `?clip=` не отдаёт чужой файл.
*   `app.js` не ломать (localStorage остаётся).

Стоп: старый логин работает без повторного входа; плеер на открытом report без auth **ещё** играет (периметр — пакет 2).

### Пакет 2 — периметр HTTP (один коммит)

Цель: закрыть Blocker, не убить match/плеер.

*   Cookie уже есть → `Depends(get_current_user)` на report*/audio/lab file **и** на весь `speaker_profiles.py`.
*   HTML-шеллы: нет сессии → 302 `/login`; `/login` и `/static` исключить. `/admin/conflicts` + `require_role("admin")`.
*   `except` в speaker router: generic 500, не `str(exc)`.
*   Тесты инвертировать: anonymous 401 на report/audio/enroll/confirm; 200 с cookie или Bearer. `test_lab_report.py`, `test_audio_playback.py`, `test_lab_enroll.py`, `test_artifact_download.py`.
*   PROJECT-MAP: report не анонимный; login cookie+Bearer.

Стоп: контур «логин → report со звуком → запомнить → confirm с проекта и с match».

### Пакет 3 — воркер не врёт статусом

*   `gpu_worker` except: `rollback()` первым; если voice-gate и нет confirm → `speakers_pending`, не `transcript_ready`.
*   `notify.py`: `users.telegram_id`, не PK.
*   Тест crash после publish; mock httpx chat_id.
*   `test_voice_gate_pipeline.py` расширить.

Стоп: после сбоя созвон снова кнопка «Подтвердить», не тихий knowledge.

### Пакет 4 — тихий харденинг (можно параллелить после 2, не с 1)

По одному PR/коммиту, без смены UX:

*   PyJWT вместо python-jose (`test_auth.py`).
*   `search?limit=` 1–50; resolve_conflict winner ∈ {source,target}; заметки `user_note_{ts}_{user.id}.txt`.
*   `claim_next_job` + `BEGIN IMMEDIATE`; `requeue` уважает max attempts.
*   Lab `_run_cmd`: логи в файл, не `capture_output` на длинных шагах.
*   `docker compose up -d gigaam` в `_compose_up`/`ensure_container`; **не** profiles и не вырезать Whisper из CLI в этом пакете.
*   Scrub секретов в RuntimeError env.

### Пакет 5 — загрузка и дедуп

*   Complete: байты на месте **потом** queued. Не queued до move.
*   Скрипт дублей sha256 → unique `(project_id, sha256)` + IntegrityError → существующий artifact.
*   `_register_file_on_disk` в сервис.

Стоп: повтор complete того же файла — duplicate, не второй ASR; обрыв после move не оставляет вечный queued без файла.

### Пакет 6 — health

*   `/health` = `{status:ok}`; `/health/ready` loopback или auth.
*   `app.js` бейдж → ready с cookie; `test_health.py`; `urlopen` в `main.py` остаётся на `/health`.

### Пакет 7 — канон ASR в проде

*   `ASR_CONTAINERS` prod = gigaam; whisper/tone `profiles: [legacy]`; `asr/run_pipeline.py` + `test_docker_runtime.py`.
*   Прод `process_artifact`: нет lab run → error «нужен rerun lab», не Whisper. DECISIONS SUPERSEDED пометка.
*   `docker stop` idle whisper/tone — отдельная ops-команда, не в горячем job.

### Пакет 8 — чат/GPU (только вместе с UI)

*   GPU file-lock; 409 «идёт расшифровка» в API **и** в `project.html`/`chat.html`.
*   202+poll для чата — только если fetch обрабатывает 202 как pending, не как пустой answer.
*   До готовности UI — не менять тело `POST /chat`.

### Пакет 9 — схема (окно «портал ~минута»)

*   Скрипт сирот на **копии** `app.db`.
*   `PRAGMA foreign_keys=ON`; alembic baseline stamp; тесты оставляют `create_all`.
*   Не обнулять `transcription_text`, пока list документов не смотрит на файл отчёта.

### Пакет 10 — политика (объявление людям)

Только после стабильных 1–2: выключение автопровижининга (нужен admin API пользователей), MCP TTL на **новые** ключи, `mcp/info` manager+ со скрытием блока у user, короткий JWT+refresh, CSP после выноса JS, rate limit login по telegram_id.

**Старт:** пакет 0 → пакет 1. Журнал фактических правок: [`docs/AUDIT-REPAIR-LOG.md`](AUDIT-REPAIR-LOG.md). К пакету 2 не переходить, пока 1 не зелёный на контуре и pytest.

Конец отчёта.

