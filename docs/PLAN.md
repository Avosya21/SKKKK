# Единый пошаговый план SKKKK

Корпоративный портал файлов проекта + автоматическая память знаний на Windows 11 (GPU 24 ГБ VRAM).

Исходные части 1 (файловый сервер) и 2 (управление знаниями) здесь слиты в **один** план. Зафиксированные решения: [DECISIONS.md](DECISIONS.md).

Корень всего: `C:\1C_NEW_SKKKK`.

- Пофазовое исполнение с проверками и тестами: [stages/README.md](stages/README.md)
- **Карта проекта (архитектура, зависимости, API):** [PROJECT-MAP.md](PROJECT-MAP.md)
- Результаты ревизии плана и внесённые исправления: [PLAN-REVIEW.md](PLAN-REVIEW.md)
- Стратегия тестирования: [TESTING.md](TESTING.md)

> **Актуально на 2026-09-21.** Этапы 0–9 выполнены и работают; поверх исходного плана построены комната созвона, обмен с внешним агентом, MCP, публичный HTTPS и единая дизайн-система. Что именно и что ещё открыто — в [§15](#15-состояние-на-2026-09-21) и [§16](#16-открытое-и-ближайшие-шаги). Разделы §3–§13 ниже — исходный план с точечными правками; расхождения с кодом решает [PROJECT-MAP.md](PROJECT-MAP.md).

---

## 1. Что строим

Внутреннее веб-приложение на ~20 сотрудников.

Пользователь загружает в проект артефакты (аудио созвонов, PDF/DOCX, заметки, скриншоты). Система:

1. Сохраняет оригинал неизменным.
2. Для аудио гоняет **двойной слух** (Parakeet-TDT + сверка GigaAM) и Sortformer по спикерам, затем **LLM** подписывает спорное и собирает выжимку (локальная Gemma или Cursor API — см. §3.1 и §6).
3. Для документов и картинок извлекает и нормализует текст.
4. Достаёт сущности (решения, задачи, факты), кладёт эмбеддинги через Ollama `bge-m3`, сеет граф знаний.
5. Даёт заметки и ручные связи, разрешает противоречия, пересобирает актуальное ТЗ, отвечает в чате со ссылками на источники, пишет в Telegram когда готово.

Это не «расшифровщик». Это живая память проекта, у которой файловый портал — входная дверь.

---

## 2. Роли и вход

Упрощённая модель: три пароля в `.env` (bcrypt-хеши). Сотрудник вводит имя, Telegram ID, пароль роли.

| Роль | Права |
|---|---|
| `admin` | Всё: проекты, удаление, пользователи, конфликты, правка ТЗ |
| `manager` | Создание проектов, загрузка в любые, заметки и связи, без удаления и без resolve |
| `user` | Загрузка в существующие проекты, просмотр, свои заметки и связи |

Дополнительное поле `position` (`customer` / `analyst` / `developer` / …) — подпись, не ACL.

Логика `POST /auth/login`:

1. Найти пользователя по **имени (логину)**, без учёта регистра.
2. Если есть несколько с одним именем — склеить в одну учётку (история чата и файлы переезжают).
3. Сопоставить пароль с тремя хешами из `.env`. Какой пароль ввели — такая роль (в том числе у уже существующей учётки: роль перезаписывается).
4. Если учётки нет — создать с этой ролью; если есть — обновить роль и хеш, записать Telegram ID.
5. Не совпал ни с одной ролью — 401. Выдать JWT.

Ограничения API как в части 1 плюс: resolve конфликтов и ручная правка ТЗ — только `admin`.

---

## 3. Стек

| Слой | Выбор | Почему |
|---|---|---|
| ОС | Windows 11 | Целевой сервер |
| GPU | 24 ГБ VRAM | Одна карта, фазы оркестратора |
| API | Python 3.11, FastAPI, Uvicorn | Асинхронный портал |
| БД | SQLite в `C:\1C_NEW_SKKKK\data\app.db` | Достаточно на 20 человек; PostgreSQL позже |
| ORM | SQLAlchemy 2 + Alembic | |
| Очередь | Таблица `jobs` + один GPU-воркер (процесс) | Без Celery и без watchdog |
| LLM | **Два режима** (переключатель при старте, `LLM_PROVIDER` в `.env`): **lmstudio** — `LMSTUDIO_MODEL` (`qwen/qwen3.8-27b`), LM Studio `http://127.0.0.1:1234/v1`; **cursor** — Cursor SDK + `CURSOR_API_KEY` | Локально — приватность и GPU-фазы; облако — без VRAM под локальную LLM |
| Web | FastAPI + Uvicorn на `127.0.0.1:1777` (`WEB_PORT` в `.env`), снаружи — Caddy по HTTPS (§15.4) | Порт 8000 занят MCP 1С; 1777 вместо 1111 — за Caddy |
| Эмбеддинги | Ollama `bge-m3`, API `http://127.0.0.1:11434`, **CPU** | Не занимает VRAM |
| ASR | Parakeet-TDT v3 + GigaAM v3 (сверка) + Sortformer 4spk | Двойной слух, не ансамбль Whisper/T-one |
| Аудио | FFmpeg 16 кГц моно | Без обязательной нарезки 8 кГц под T-one |
| Документы | pymupdf / python-docx / python-pptx / Tesseract (rus+eng) | |
| Векторы (старт) | sqlite-vec, 1024 dim | Qdrant — этап после стабильного поиска |
| Граф (старт) | таблицы `entities` + `links` | Neo4j — этап после живого UI графа |
| UI | Jinja2 + Bootstrap 5 | MVP |
| Уведомления | HTTP Bot API Telegram (без polling-бота в том же процессе) | |
| Прокси | **Caddy** (обратный прокси + ACME-сертификат на IP), Nginx не используется | Решение в `DECISIONS.md` |
| Созвоны | LiveKit (native), запись дорожек, голосовой чистильщик, запись экрана | §15.1 |
| Службы | NSSM: API, GPU-воркер, Caddy | Установка — `scripts/` |

### 3.1. Выбор LLM-провайдера

Один и тот же конвейер (sense/proof, нормализация, сущности, ТЗ, чат, vision для картинок) идёт через **единый адаптер** `app/app/services/llm.py`. Провайдер задаётся в `.env` и выбирается при запуске.

| `LLM_PROVIDER` | Движок | GPU под LLM | Когда удобно |
|---|---|---|---|
| `lmstudio` (по умолчанию) | `LMSTUDIO_MODEL` (`qwen/qwen3.8-27b`) через LM Studio | Да: load/unload, фазы ASR ↔ LLM | Локальная разработка, приватность, длинные созвоны без API-биллинга |
| `cursor` | Cursor SDK (`cursor-sdk`), модель `CURSOR_MODEL` (напр. `composer-2.5`) | **Нет** — VRAM целиком под ASR | Опубликованный сервер, интернет всегда есть, LM Studio не нужен |

**Запуск и выбор:**

```powershell
python -m app.app.main all                    # окно/консоль: LM Studio или Cursor
python -m app.app.main all --llm-provider cursor
python -m app.app.main all --use-saved-llm    # взять из .env (службы NSSM)
```

**Переменные `.env`:** `LLM_PROVIDER`, `CURSOR_API_KEY`, `CURSOR_MODEL`, `CURSOR_RUNTIME` (`local` | `cloud`), `CURSOR_CLOUD_REPO` (для cloud). Шаблон — `.env.example`.

**Не меняется при любом провайдере:** Ollama `bge-m3`, sqlite-vec, граф, очередь `jobs`. Канон ASR — двойной слух, не тройной ансамбль.

---

## 4. Каталоги

```
C:\1C_NEW_SKKKK\
├── docs\                      # план, решения, промпты
├── .cursor\rules\             # правила агента
├── .cursor\skills\            # скилы агента
├── app\                       # код FastAPI
│   └── app\
│       ├── models\
│       ├── schemas\
│       ├── routers\
│       ├── services\
│       ├── workers\           # GPU-оркестратор и ASR
│       ├── templates\
│       ├── static\
│       ├── config.py
│       ├── dependencies.py
│       └── main.py
├── data\
│   ├── app.db
│   ├── logs\
│   └── projects\
│       └── {project_id}\
│           ├── raw_inputs\    # неизменяемые оригиналы
│           │   ├── calls\
│           │   ├── documents\
│           │   └── notes\
│           ├── parsed\
│           │   ├── calls\     # raw ASR + _final.txt репликами + _speakers.json
│           │   ├── documents\
│           │   └── notes\
│           ├── structured\    # *_entities.json
│           ├── project_notes\
│           ├── project_state\ # current_tz.md + versions\
│           └── tmp\           # VAD/WAV, чистить после успеха
├── venv\
└── .env
```

Имена артефактов: `art_YYYY-MM-DD_HHMMSS_{short}`.

---

## 5. Данные

### 5.1. Реляционные таблицы

`users` — id, name (логин, уникален без учёта регистра), telegram_id (хранится, на вход не влияет), role, position, password_hash, created_at

`projects` — id, name, description, created_by, created_at, is_active, settings_json (веса источников, порог схожести 0.82)

`artifacts` — id, artifact_id, project_id, user_id, original_filename, stored_filename, file_path, file_type (`audio|pdf|docx|pptx|txt|html|image|other`), source_type (`call|document|note|screenshot`), upload_date, status, priority_weight, transcription_text, transcription_file_path, telegram_notification_sent, error_message, **sha256** (дедупликация в рамках проекта), **duration_sec** (для аудио), **asr_engines_json** (какие движки отработали)

Статусы артефакта (строго по конвейеру):

`uploaded` → `preprocessing` → `asr_running` → `asr_completed` → `transcript_ready` | `text_extracted` → `entities_extracted` → `indexed` → `graph_seeded` → `done` | `error`

`entities` — id, artifact_id, entity_type (`decision|task|fact|participant|mention`), value_json, confidence, is_active, needs_review

`links` — id, source_entity_id, target_entity_id, link_type, similarity_score, is_automatic, created_by, description

`user_notes` — id, project_id, user_id, text, file_path, created_at

`note_refs` — note_id, target_kind (`artifact|entity|note`), target_id

`resolutions` — conflict_id, winning_entity_id, losing_entity_id, reason, resolved_by, status (`pending_manual|resolved`)

`changelog` — version_id, project_id, timestamp, artifact_ids_json, summary_of_changes, tz_path

`jobs` — id, artifact_id, kind, payload_json, status (`queued|running|done|error`), phase (`asr|llm|embed`), created_at, started_at, finished_at, error_message

`chunks` — id, artifact_id, chunk_index, text, speaker, token_estimate. Для стенограмм чанк = реплика (или несколько подряд одного человека), `speaker` обязателен.

`transcript_windows` — id, artifact_id, window_index, start_sec, end_sec, merged_text, status. Окна нарезки аудио, единица работы ASR и склейки.

`speaker_turns` — (на старте в `{id}_speakers.json`, позже таблица) artifact_id, start_sec, end_sec, speaker, text. Источник «кто говорит»; чанки и UI читают отсюда.

`asr_runs` — id, artifact_id, engine (`whisper|tone|gigaam`), status, duration_sec, error_message. Фиксирует полноту ансамбля по каждому артефакту.

Векторы чанков: sqlite-vec, dim 1024, payload = artifact_id, source_type, timestamp, author, chunk_index, speaker, priority_weight.

### 5.2. Граф (логика, без Neo4j на старте)

Узлы: Artifact, Entity, UserNote, SystemNote.  
Рёбра: CONTAINS, MENTIONS, REFINES, CONTRADICTS, SUPPORTS, DEPENDS_ON, CLARIFIES, EXAMPLES, REFERENCES, SUMMARIZES, SEMANTICALLY_RELATED, POTENTIAL_CONFLICT, RESOLVES.

UI графа читает `entities` + `links` (+ артефакты и заметки) и рисует сеть (vis-network / Cytoscape.js).

---

## 6. GPU-оркестратор (обязательный)

Одна карта. BGE на CPU. Поведение зависит от `LLM_PROVIDER`.

### Режим `lmstudio` (Gemma локально)

Две взаимоисключающие фазы на GPU:

```
[фаза ASR]  LM Studio UNLOAD → Parakeet+Sortformer (venvs\lab) → GigaAM в Docker
            → выгрузить процессы
[фаза LLM]  LM Studio LOAD gemma-4-31b-qat → sense/proof / нормализация / сущности / ТЗ / чат / vision
[фаза EMBED] Ollama bge-m3 на CPU в любой момент; GPU не трогает
```

### Режим `cursor` (LLM в облаке)

Gemma в VRAM **не загружается**. Шаги `gemma_load` / `gemma_unload` в ASR-пайплайне пропускаются.

```
[фаза ASR]  Parakeet+Sortformer ONNX → GigaAM Docker (последовательно, один воркер)
[фаза LLM]  запросы в Cursor API (sense, proof, сущности, чат, vision — без локальной Gemma)
[фаза EMBED] Ollama bge-m3 на CPU — как выше
```

### Общие правила

1. В режиме **lmstudio** в VRAM одновременно **не** живут Gemma и ASR. В режиме **cursor** Gemma в VRAM нет.
2. Двойной слух **последовательно** на одном GPU-воркере: сначала ONNX Parakeet+Sortformer (`venvs\lab`), затем GigaAM в Docker для сверки. Whisper и T-one не стартуют.
3. **Только lmstudio:** перед ASR — `lms unload --all`, дождаться падения занятости по `nvidia-smi`; после ASR — `lms load` той модели, что в `LMSTUDIO_MODEL`.
3a. **Только lmstudio:** в LM Studio **выключена автозагрузка модели по запросу (JIT)**. Иначе HTTP-запрос от пользователя во время ASR поднимет Gemma поверх ASR.
4. **Только lmstudio:** Gemma после загрузки **не выгружать** между sense/proof, нормализацией, сущностями, ТЗ и чатом. Выгрузка — только перед следующим аудио.
5. Чат и пересборка ТЗ ждут, если идёт ASR (очередь `jobs.phase`).
6. Один GPU-воркер на весь сервер (`python -m app.app.main worker`). Портал только ставит jobs.

Веса источников по умолчанию: документ заказчика 1.0, внутренний документ 0.8, созвон 0.5, заметка 0.6, скриншот 0.4. Админ может поменять в `projects.settings_json`.

---

## 7. Конвейер обработки

### Модуль 0. Создание проекта

Создать запись `projects`, дерево каталогов в `data\projects\{id}\`, пустые коллекции векторов для проекта. Права: admin, manager.

### Модуль 1. Приём артефакта

Любой файл до 2 ГБ. MIME + расширение → `file_type`. Контекст формы → `source_type`. Посчитать `priority_weight`. Скопировать в `raw_inputs\...`. Статус `uploaded`. Поставить job. Telegram не слать.

### Модуль 2. Текст

**2.1 Аудио (двойной слух + Sortformer)**

1. FFmpeg: мастер WAV 16 кГц моно в `artifacts/{id}/audio/input_16k.wav`.
2. **Только lmstudio:** выгрузить Gemma. В режиме **cursor** шаг пропускается.
3. `venvs\lab`: `lab_pipeline.py --no-voice` — Parakeet-TDT v3 (окна ≤80 с с перекрытием) → Sortformer 4spk → покраска слов → реплики → VAD. Всё в `lab/runs/{run_id}/`.
4. Основной venv: `lab_sense.py` (Cursor, только спорные спикеры) → `lab_giga.py` (Docker GigaAM) → `lab_reconcile.py` → `lab_proof.py` (выбор lab vs giga) → `lab_voice.py` (WeSpeaker).
5. Публикация: `proof/transcript.json` → `asr/docker_gigaam_speakers.json`; `voice/match.json` → `reports/sortformer_speaker_match.json` (метки `speaker_N`); `parsed/{id}_final.txt`; HTML `/report`.
6. Статус `speakers_pending`. Пользователь «Запомнить» / «Подтвердить спикеров».
7. Confirm подставляет имена в turns → `transcript_ready` → сущности → индекс → **выжимка-протокол** (`docs/prompts/call_summary.md`, map ~6000 символов, reduce 4096 tokens) → граф → `done`.

Whisper, T-one и `merge_ensemble.py` не вызываются. Контейнеры на диске — откат.

Если Parakeet и GigaAM оба пустые — LLM не вызывается, артефакт закрывается с пометкой «речь не обнаружена».

**2.2 PDF/DOCX/PPTX** — pymupdf / python-docx / python-pptx (слайд = секция `## Слайд N`, заметки докладчика, таблицы, данные диаграмм, OCR картинок на «пустых» слайдах); скан → Tesseract rus+eng. `_raw.txt` → LLM-нормализация → `_clean.txt`. Статус `text_extracted`.

**2.3 TXT / ручная заметка** — лёгкая зачистка пробелов, `_clean.txt`.

**2.4 Изображение** — OCR Tesseract; при слабом OCR — **vision через LLM** (Gemma multimodal или Cursor `SDKImage`). `_clean.txt`.

### Модуль 3. Структурирование текста

Единый проход LLM по `_clean.txt` / `_final.txt`. Промпт: [prompts/normalize_document.md](prompts/normalize_document.md).

### Модуль 4. Сущности

LLM → JSON. Промпт: [prompts/extract_entities.md](prompts/extract_entities.md). Файл `structured\{id}_entities.json` + строки `entities` с `needs_review=true`. Статус `entities_extracted`.

### Модуль 5. Индексация

Чанки 512 / overlap 128. Ollama `bge-m3` (CPU). sqlite-vec. Статус `indexed`.

### Модуль 6. Сейв графа

Узлы артефакта, сущностей, SystemNote (summary). Рёбра CONTAINS, MENTIONS, SUMMARIZES. Для новых сущностей — поиск похожих (порог 0.82) → SEMANTICALLY_RELATED; тот же тип и разные значения → POTENTIAL_CONFLICT. Статус `graph_seeded`.

### Модуль 7. Ручные заметки и связи

UI: текст + привязки к узлам. Файл в `project_notes\`. Рёбра REFERENCES / выбранный тип + комментарий. `is_automatic=false`. Админ может править любые.

### Модуль 8. Конфликты

Авто-resolve только при явном «отменяем / вместо / исправляем» в более новом артефакте. Иначе `pending_manual` и Telegram админу. Проигравшая сущность `is_active=false`.

### Модуль 9. Пересборка ТЗ

Не после каждого файла. Триггеры: кнопка «Пересобрать ТЗ», N новых `graph_seeded` (по умолчанию 3), ручной resolve. Промпт: [prompts/rebuild_tz.md](prompts/rebuild_tz.md). Архив в `project_state\versions\`. Запись `changelog`.

### Модуль 10. Граф в UI

Фильтры по типу / времени / автору. Клик — карточка узла.

### Модуль 11. Чат

Эмбеддинг вопроса (Ollama CPU) + соседи графа → LLM. Промпт: [prompts/rag_chat.md](prompts/rag_chat.md). Если идёт ASR — запрос в очередь `phase=llm`.

### Модуль 12. Версии ТЗ

Список версий, diff двух markdown, restore = копия в `current_tz.md` + новая запись changelog.

После `graph_seeded` (или `done`): Telegram загрузившему. Статус `done`.

---

## 8. API и страницы

Эндпоинты части 1 сохраняются. Добавляются:

| Метод | URL | Кто | Зачем |
|---|---|---|---|
| GET | `/projects/{id}/graph` | все | узлы и рёбра |
| POST | `/projects/{id}/notes` | все | заметка |
| POST | `/links` | все | ручная связь |
| GET | `/projects/{id}/conflicts` | admin | очередь конфликтов |
| POST | `/conflicts/{id}/resolve` | admin | победитель |
| POST | `/projects/{id}/rebuild-tz` | admin | пересборка |
| GET | `/projects/{id}/tz` | все | текущее ТЗ |
| GET | `/projects/{id}/tz/versions` | все | история |
| POST | `/chat` | все | RAG |

Страницы: `/login`, `/` (проекты), `/projects/{id}` (файлы + статусы polling), `/projects/{id}/graph`, `/projects/{id}/tz`, `/projects/{id}/chat`, `/history`, `/admin/conflicts`.

Загрузка — Fetch + прогресс. Статусы файлов — polling 3–5 с.

---

## 9. Telegram

Токен в `.env`. Отправка через HTTPS Bot API, без long polling в процессе FastAPI.

События: транскрипция/обработка готова; конфликт ждёт админа; ТЗ пересобрано (админ + загрузившие за период — по настройке).

---

## 10. Пошаговая реализация

Ниже — краткая карта этапов. **Подробное исполнение каждого этапа с шагами, ручными проверками, тестами и критерием готовности вынесено в отдельные файлы:** [stages/README.md](stages/README.md).

Правило перехода: следующий этап не начинается, пока не выполнен «Критерий готовности» предыдущего.

Код пишется только в `C:\1C_NEW_SKKKK`.

Каждый файл этапа теперь содержит раздел **«Полезные проекты на GitHub»** — 1–5 открытых проектов на этап с пояснением, что именно из них подсматривать (архитектура, конкретная функция, готовый паттерн), а что уже решено и трогать не надо (см. [DECISIONS.md](DECISIONS.md)). Это не список зависимостей на установку, а ускорение реализации за счёт готовых референсов. Закрытие этапа обязательно включает три вида проверки — тесты, живой прогон с открытием нужной страницы в браузере, скриншот «задумано vs получилось» — порядок и обоснование см. в [stages/README.md § «Единый порядок закрытия этапа»](stages/README.md#единый-порядок-закрытия-этапа).

### Этап 0. Стенд Windows (без прикладного кода) → [STAGE-00](stages/STAGE-00-environment.md)

1. `nvidia-smi` — драйвер жив, 24 ГБ видны.
2. Python 3.11 в PATH, Git, FFmpeg (`ffmpeg -version`).
3. Tesseract OCR с языками rus+eng, путь в PATH.
4. **LM Studio** (обязательно при `LLM_PROVIDER=lmstudio`): модель из `LMSTUDIO_MODEL` (`qwen/qwen3.8-27b`), сервер `:1234`, JIT выключен, CLI `lms`.
5. **Cursor API** (обязательно при `LLM_PROVIDER=cursor`): `CURSOR_API_KEY` в `.env`, preflight проверяет доступность API.
6. **Ollama:** `ollama list` содержит `bge-m3`, эмбеддинг на CPU.
7. Создать `.env` из `.env.example` (`LLM_PROVIDER`, секреты, URL, хеши паролей).

Результат: чеклист стенда в `data\logs\env-check.txt`.

### Этап 1. Каркас портала → [STAGE-01](stages/STAGE-01-skeleton.md)

venv, зависимости, FastAPI, SQLAlchemy модели, Alembic, JWT-логин, страницы login + список проектов, CRUD проектов по ролям. GPU ещё не трогаем. Референсы структуры: `benavlabs/FastAPI-boilerplate`, `justyn-clark/fastapi-sqlalchemy-starter`.

Результат: вход тремя паролями, создание проекта, дерево папок на диске.

### Этап 2. Загрузка и очередь → [STAGE-02](stages/STAGE-02-upload-queue.md)

Upload в `raw_inputs`, запись `artifacts` + `jobs`, UI статусов. GPU-воркер пока no-op: job → `done` без моделей (флаг `DRY_RUN=1`). Референсы паттерна очереди на SQLite: `KPouianou/GigQ`, `unkloud/liteq`.

Результат: файл лежит в дереве проекта, в БД статус меняется воркером.

### Этап 3. Двойной слух ASR → [STAGE-03](stages/STAGE-03-asr-ensemble.md)

Исторический этап (тройной ансамбль) выполнен. С 2026-08-26 канон — лабораторный контур: Parakeet + Sortformer + Cursor + GigaAM-сверка. Whisper/T-one/merge окон не вызываются.

### Этап 4. Документы и OCR → [STAGE-04](stages/STAGE-04-documents-ocr.md)

PDF/DOCX/TXT/изображения, нормализация LLM, vision для слабого OCR. Единый `_clean.txt`.

Результат: PDF и скриншот превращаются в текст в `parsed\`.

### Этап 5. Сущности и индекс → [STAGE-05](stages/STAGE-05-entities-index.md)

JSON сущностей, таблица `entities`, чанки, Ollama `bge-m3`, sqlite-vec. Референсы: `asg017/sqlite-vec` (сама СУБД), `natasha/natasha` / `urchade/GLiNER` (запасной NER без LLM).

Результат: поиск по смыслу внутри проекта возвращает чанки.

### Этап 6. Граф, заметки, конфликты → [STAGE-06](stages/STAGE-06-graph-notes-conflicts.md)

Сейв узлов/рёбер, UI графа, ручные заметки и связи, очередь конфликтов для admin. Референсы визуализации: `cytoscape/cytoscape.js`, `mostafaibrahim17/civo-knowledge-graph-generator` (готовый паттерн FastAPI + Cytoscape.js).

Результат: на графе видны артефакт → сущности, можно поставить ручную связь.

### Этап 7. ТЗ и чат → [STAGE-07](stages/STAGE-07-tz-chat.md)

Пересборка `current_tz.md`, версии, diff, RAG-чат. Сборка по разделам — тот же map/reduce приём, что уже обкатан в `asr/summarize_call.py` для выжимки созвона. Референсы RAG-по-графу: `HKUDS/LightRAG`.

Результат: после двух артефактов кнопка «Пересобрать ТЗ» даёт markdown; чат цитирует artifact_id.

### Этап 8. Укрепление → [STAGE-08](stages/STAGE-08-hardening.md)

Логи в `data\logs`, лимит 2 ГБ, права ролей на всём API, обработка падения одного из трёх ASR, таймауты LM Studio, бэкап `app.db`. Референсы: `hynek/structlog` (структурные логи), `laurentS/slowapi` (rate limiting без Redis).

### Этап 9. Службы Windows → [STAGE-09](stages/STAGE-09-services.md)

NSSM: `SKKKK-API` (uvicorn), `SKKKK-GPU` (воркер). Nginx reverse-proxy — по необходимости. Автозапуск LM Studio и Ollama (они уже должны быть службами/автозагрузкой пользователя). Референс: `nssm.cc` / `nssm-mirror/nssm`.

### Этап 10. Qdrant / Neo4j (не раньше) → [STAGE-10](stages/STAGE-10-scale-out.md)

Только когда sqlite-vec и табличный граф упрутся в объём. Перенос без смены API. Референсы: `qdrant/qdrant`, `neo4j/neo4j`, `getzep/graphiti` (схема миграции в граф с временными рёбрами).

---

## 11. Проверки приёмки

- Три роли: manager не удаляет проект, user не создаёт, admin удаляет.
- Аудио: три raw ASR + `_final.txt` репликами + `_speakers.json`, не один общий txt.
- Во время ASR процесс LM Studio не держит 20+ ГБ (модель выгружена).
- После ASR Gemma снова отвечает на `/v1/chat/completions`.
- Эмбеддинг не сажает VRAM (Ollama CPU).
- Документ заказчика vs заметка с противоречием → `pending_manual`, не тихая подмена ТЗ.
- Чат без источника не выдумывает требования.
- Всё созданное лежит только под `C:\1C_NEW_SKKKK`.

---

## 12. Зависимости Python (ориентир)

`fastapi`, `uvicorn[standard]`, `sqlalchemy`, `alembic`, `python-jose[cryptography]`, `passlib[bcrypt]`, `python-multipart`, `aiofiles`, `httpx`, `pydantic-settings`, `python-docx`, `pymupdf`, `pytesseract`, `pillow`, `faster-whisper`, пакеты T-one и GigaAM v3 по их README, `sqlite-vec`, `jinja2`.

Точные pin-версии — в `app\requirements.txt` на этапе 1.

---

## 13. Переменные окружения

См. `.env.example` в корне репозитория. Критичные:

- `SECRET_KEY`, три `*_PASSWORD_HASH`
- `LMSTUDIO_BASE_URL`, `LMSTUDIO_MODEL=qwen/qwen3.8-27b`
- `WEB_HOST=127.0.0.1`, `WEB_PORT=1777` (не 8000; в `config.py` дефолт 1111, боевое значение — из `.env`)
- `OLLAMA_BASE_URL`, `OLLAMA_EMBED_MODEL=bge-m3`, `OLLAMA_NUM_GPU=0`
- `TELEGRAM_BOT_TOKEN`
- `DATA_DIR=C:\1C_NEW_SKKKK\data`
- порог схожести, лимит файла, `DRY_RUN`
- `LIVEKIT_*` (URL, ключи), `SKKKK_HTTPS_PORT`; срок токена комнаты — константа `LIVEKIT_TOKEN_TTL_HOURS = 4` в `services/meetings.py`, не переменная окружения

---

## 14. Порядок работ агента

Исходная последовательность «этап 0 → этап 1 → …» пройдена. Сейчас работаем по [§16](#16-открытое-и-ближайшие-шаги): сначала закрываем открытые находки из [FINDINGS-2026-09.md](FINDINGS-2026-09.md), затем оставшиеся фазы [PLAN-AGENT-EXCHANGE-V2.md](PLAN-AGENT-EXCHANGE-V2.md) и этапы A/B/C из [plans/MEETING-PLAN.md](plans/MEETING-PLAN.md). Не упрощать ASR до одной модели: канон — лабораторный контур (Parakeet + Sortformer + GigaAM-сверка + LLM).

---

## 15. Состояние на 2026-09-21

### 15.1. Созвоны (комната SKKKK)

- Комната на LiveKit: публичная гостевая страница `/meeting/...` (токен, без `project_id` в публичном ответе), комната для сотрудников, раскладки (сетка/докладчик), запись экрана, выбор камеры/микрофона/динамика прямо в комнате, мобильный режим (iOS/Android, ландшафт).
- Запись по дорожке на человека, лог событий `events.jsonl`, контракт `meeting.json`, реплики по дорожкам, обнаружение протечек, атрибуция имён из participant mapping (спорное — WARN/CONFLICT/UNKNOWN). Голосовой чистильщик (`voice_cleaner`), кадры экрана (`meeting_frames`).
- Токен комнаты живёт 4 часа. Планировщика чистки медиа нет: удаление только вручную из UI ([DECISIONS.md](DECISIONS.md)).
- Подробно и по этапам A/B/C — [plans/MEETING-PLAN.md](plans/MEETING-PLAN.md).

### 15.2. Аудио и LLM

- Боевой ASR-контур — лабораторный: Parakeet + Sortformer + GigaAM-сверка + LLM (sense/proof). Whisper/T-one не вызываются.
- LLM: LM Studio (`swift-qwen3.8-27b…`) или Cursor API, выбор при старте. Все вызовы идут в режиме «без reasoning» (`*_no_think`), есть тест-сторож по всему коду.
- Итоговая сводка ТЗ по умолчанию — быстрый top-K путь (`rebuild_tz_fast`, детерминированный); полный map/reduce остаётся запасным.
- Гейт GPU-фаз (`gpu_phase`) переносит модель LLM/ASR по VRAM; очередь `jobs` + один GPU-воркер.

### 15.3. Обмен с внешним агентом и MCP

- Проектный MCP: 16 инструментов, контракт читается виртуальными файлами `contract/*.json`; ключи доступа с флагом `can_submit`.
- Шлюз обмена (`agent_gateway`): белый список, решения детерминированы и не зависят от LLM; вопросы, вердикты, журнал, черновик ответа локальной модели, подтверждение требований (только admin).
- Статус фаз v2: 0–4 сделаны; 5 (права ключей), 6 (честность эвристик), 7 (поиск), 8 (автоответ) не сделаны; 9 (живая проверка карточек) ждёт смоука. См. [PLAN-AGENT-EXCHANGE-V2.md](PLAN-AGENT-EXCHANGE-V2.md).

### 15.4. Доступ, роли, безопасность

- Публичный доступ — Caddy, `https://<IP>:777`, сертификат Let's Encrypt на IP (короткоживущий, продление контролируется при старте).
- Роль сессии хранится **в токене**, а не в БД: общий логин, роль определяется паролем при входе (правка §2 п.3–4: роль в БД больше не перезаписывается).
- Ограничение частоты входа, проверки IDOR по всем роутерам (`test_idor_sweep`), русские тексты ошибок и страница ошибки, время в БД — UTC.

### 15.5. Интерфейс

- Единая дизайн-система `static/design.css` (токены `--ds-*`), тема отчётов `report-theme.css`.
- 2026-09-21: в комнате иконка «Выбрать динамик» — ползунки + динамик, «Выбрать микрофон» — микрофон с шестерёнкой; подпись на кнопке микрофона скрыта (раньше не помещалась в круг).

### 15.6. Надёжность БД (2026-09-21)

- Симптом: `QueuePool limit of size 5 overflow 10`, 500 на `/projects/{id}/artifacts`.
- **Причина найдена:** обработчики (в первую очередь чат) держали открытую транзакцию, а значит соединение пула, всё время ответа LLM. Исправлено `db.commit()` перед вызовами LLM во всех местах, где есть сессия; детектор и статический тест не дают вернуть ошибку (DECISIONS 21.09, находка 64).
- Пул увеличен до 10+20, таймаут 15 с; очистка участников встречи вынесена из event loop; опрос страницы проекта не накладывается и не работает в скрытой вкладке; при исчерпании пула в `api.log` пишутся стеки держателей.

---

## 16. Открытое и ближайшие шаги

0. ~~**Индекс проекта**~~ — сделано 21.09.2026 (recall@5: 38 % → 93 % на наборе для настройки, 77 % на контрольном): [plans/PROJECT-INDEX-PLAN.md](plans/PROJECT-INDEX-PLAN.md). Докстринги у всех модулей добавлены; дальше — пополнение словаря синонимов по реальным промахам.
1. ~~**Пул БД**~~ — причина найдена и исправлена 21.09 (§15.6). Остаётся наблюдение: если предупреждение «LLM-вызов при открытой транзакции БД» появится в `api.log`, в нём будет стек места.
2. ~~**Частый опрос страницы проекта**~~ — сделано 21.09: опрос не накладывается (медленный ответ не копит запросы) и не работает в скрытой вкладке; интервал 3 с сохранён. Свести три запроса в один эндпоинт — по-прежнему возможное улучшение.
3. **Предупреждения preflight** — поставить голосовой чистильщик (`scripts/install_meeting_voice.py`), поднять ASR (`asr/docker-compose.yml`).
4. **Обмен с агентом** — фаза 5 (права ключей) сделана 21.09; остаются фазы 6–8 (нужна ручная разметка требований, живые данные и замер на настоящих вопросах) и живая проверка карточек (нужен `SMOKE_ADMIN_PW`).
5. **Открытые находки** — список со статусом «открыто» в [FINDINGS-2026-09.md](FINDINGS-2026-09.md): скорость чата (~38 с), потеря 4 из 12 эталонных фактов в быстрой сводке, живые тестовые пароли, срок жизни cookie и неотзываемая роль в токене, каталоги записей, которые Windows не даёт удалить.
6. **Созвон** — этапы A/B/C до Definition of Done (§11 плана созвона).
7. **Этап 10 (Qdrant/Neo4j)** — только при упоре sqlite-vec и табличного графа в объём.
