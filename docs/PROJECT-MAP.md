# Карта проекта SKKKK

> **Назначение:** единый ориентир «где что лежит, как связано, за что отвечает».  
> **Обновлять вместе с кодом** при добавлении модулей, пайплайнов, API, скриптов.  
> **Семантический поиск:** `py scripts/search_project_map.py "как работает merge"`  
> **Переиндексация:** `py scripts/index_project_map.py`  
> **LightRAG (граф):** `py scripts/lightrag_query.py "…"` — см. `tools/lightrag/README.md`

Связанные документы: [PLAN.md](PLAN.md) · [DECISIONS.md](DECISIONS.md) · [TESTING.md](TESTING.md) · [HOWTO-MANAGER.md](HOWTO-MANAGER.md) · [manifest.yaml](project-map/manifest.yaml)

**Снимок стека/кодеков/версий:** [§1.1](#11-снимок-стека-актуально-2026-08-27). **Кнопки прод-созвона:** [§5.13](#513-прод-созвон-кнопка--api--функция-актуально-2026-08-27). Полный перечень UI: [§5](#5-ui-страницы-кнопки-кто-выполняет).

---

## 1. Архитектура (одним взглядом)

```mermaid
flowchart TB
    subgraph UI["Web UI (Jinja2 + Bootstrap)"]
        Pages[pages.py]
        Upload[upload.py]
        Knowledge[knowledge.py]
        Speakers[speaker_profiles.py]
    end

    subgraph API["FastAPI :1777"]
        Main[main.py]
    end

    subgraph Edge["Edge (main all / api)"]
        Caddy[Caddy :777]
        LK[LiveKit :7880]
        VC[voice_cleaner skkkk-dfn]
    end

    subgraph Worker["GPU Worker (jobs queue)"]
        GW[gpu_worker.py]
        PL[pipeline_lab]
        PK[pipeline_knowledge]
        PD[pipeline_documents]
    end

    subgraph ASR["Lab ASR + Docker"]
        LP[lab_pipeline.py]
        PKT[Parakeet ONNX]
        SF[Sortformer ONNX]
        GA[skkkk-gigaam :18102]
        PR[lab_proof Cursor]
        BT[build_transcript_view]
    end

    subgraph LLM["LLM (единый адаптер)"]
        LLMsvc[services/llm.py]
        LS[LM Studio Gemma]
        CR[Cursor API]
    end

    subgraph Store["Хранилища"]
        DB[(SQLite app.db)]
        VEC[(sqlite-vec chunks + entity BLOB)]
        PM[(project_map.db)]
        FS[data/projects/]
    end

    subgraph Embed["Embeddings CPU"]
        OL[Ollama bge-m3 :11434]
    end

    UI --> API
    Main --> Caddy & LK & VC
    API -->|sync actions| Speakers
    API --> DB
    GW --> PL & PK & PD
    PL --> LP --> PKT & SF
    PL --> GA
    PL --> PR --> LLMsvc
    PL -->|write_prod_view| FS
    BT -.->|legacy only| FS
    PK --> LLMsvc & OL
    PK --> VEC
    LLMsvc --> LS & CR
    PL & PK --> FS
    PK --> DB
```

Снимок стека, кодеков и версий — **§1.1**. Кнопки прод-созвона — [§5.13](#513-прод-созвон-кнопка--api--функция-актуально-2026-08-27).

### 1.1. Снимок стека (актуально 2026-08-27)

Зафиксировано по коду и живой машине. Менять версии моделей/кодеков только вместе с этой таблицей и `docs/DECISIONS.md`.

#### Платформа

| Что | Как сейчас |
|-----|------------|
| ОС | Windows 11 (`10.0.26200`), одна GPU **RTX 3090 24 ГБ**, драйвер **577.00** |
| CUDA (хост, lab ONNX) | Toolkit **12.8** (nvcc 12.8.61) + cuDNN **9.16.0.29** |
| Корень | только `C:\1C_NEW_SKKKK` |
| Python (портал `venv/`) | **3.10.9** |
| Python (лаборатория `venvs\lab`) | **3.10.9** (отдельный venv: `onnxruntime-gpu` vs `onnxruntime` в app) |
| Web | FastAPI **0.141.1**, Starlette **1.6.0**, Uvicorn **0.52.3**, Jinja2 **3.1.6**, Bootstrap 5 (vendored) |
| ORM | SQLAlchemy **2.0.52**, Alembic **1.19.1**, Pydantic **2.13.4** |
| Порт UI | Снаружи **Caddy `:777` HTTPS**. Портал — `WEB_HOST=127.0.0.1` `WEB_PORT=1777`. Порт **8000** занят MCP 1С |
| БД | SQLite `data/app.db`, `PRAGMA foreign_keys=ON`, WAL. Хеши личных паролей — отдельно: `DATA_DIR/credentials.db` (`CREDENTIALS_DB_PATH`, Argon2id). Карточки: `user_profiles` (1:1 с `users`), `companies` (из `Companies.txt`); фото — файлы `data/people/<user_id>/avatar-<ключ>-512|96.webp` |
| Векторы | sqlite-vec **0.1.9** (`chunks` + `entity_vectors` BLOB) |
| Auth | JWT + cookie `skkkk_session` на **30 дней**; вход только если логин есть в `Login_up.txt` (без учёта регистра); Bearer из localStorage ещё принимается. `PERSONAL_PASSWORD=required` (по умолчанию; `off` — тесты и аварийный откат): шаг 2 — личный пароль, шаг 3 — карточка. В токене `pv` — версия пароля, `pp` — пароль пройден, `pf` — карточка заполнена; сессия проверяет ещё и саму карточку (сброс админом закрывает сессии) |
| Очередь | таблица `jobs`, **один** GPU-воркер |
| LLM | `LLM_PROVIDER=cursor` **или** `lmstudio` (переключатель при старте) |

#### Модели слуха и голоса (двойной слух)

| Роль | Модель / артефакт | Версия / ревизия | Как запускается |
|------|-------------------|------------------|-----------------|
| Слова + таймлайн (канон) | Parakeet-TDT | **0.6B v3**, HF `istupakov/parakeet-tdt-0.6b-v3-onnx` rev `8f23f0c` | `venvs\lab`, onnx-asr **0.12.0**, ONNX Runtime GPU **1.23.2** |
| Кто говорит | Sortformer 4spk | **v2.1** ONNX `Scrybl/diar_streaming_sortformer_4spk-v2.1` rev `3be92087` | тот же lab; **не больше 4** спикеров |
| VAD | Silero | `istupakov/silero-vad-onnx` | onnx-asr load_vad |
| Второй слух / вычитка | GigaAM-v3 e2e RNNT | HF `ai-sage/GigaAM-v3` rev **`e2e_rnnt`** | Docker `skkkk-gigaam` `:18102` |
| Голос (enroll/match) | WeSpeaker | `pipeline._embedding` внутри GigaAMGUI | lab_voice / docker exec |
| Эмбеддинги текста | bge-m3 | Ollama, **CPU** (`OLLAMA_NUM_GPU=0`) | `:11434` |
| LLM текст | Cursor SDK **1.0.28** или LM Studio `qwen/qwen3.8-27b` | `CURSOR_MODEL` (напр. composer-2.5) / `LMSTUDIO_MODEL` `:1234` | sense, proof, сущности, ТЗ, чат, vision |

Whisper `large-v3` и T-one в compose с `profiles: [legacy]` — на диске для отката, **воркер не вызывает**.

GigaAM-контейнер (`skkkk-gigaam`): образ из `vendor/GigaAMGUI`, CUDA **12.4**, torch **2.6+cu124**, `ASR_MODEL=v3_e2e_rnnt`, `MODEL_REVISION=e2e_rnnt`. Lab ONNX: onnxruntime-gpu **1.23.2**, onnx-asr **0.12.0**, cuDNN **9.16.0.29**. SHA256 весов и эталонный прогон — `asr/lab/FREEZE.md` / `asr/lab/freeze.json`.

#### Кодеки и звук (FFmpeg)

Мастер для ASR всегда один: **WAV PCM s16le, 16 кГц, моно** → `artifacts/{id}/audio/input_16k.wav`. Бинарь: PATH / WinGet **Gyan.FFmpeg 9.0-full_build** (код помечает muxer ffmpeg 9+). DeepFilterNet на прод-пути **выключен** (`01_audio` = `copy_16k`).

| Назначение | Файл | Кодек | Параметры |
|------------|------|-------|-----------|
| ASR / диаризация / WeSpeaker | `input_16k.wav` | PCM s16le | 16 kHz mono (`-vn -ac 1 -ar 16000 -c:a pcm_s16le`) |
| Плеер в HTML (`GET /artifacts/{id}/audio`) | `playback.m4a` | **AAC** | 48 kbps, mono, `+faststart`; если encode падает — отдача WAV |
| Скачать созвон | `download.mp3` | **MP3** libmp3lame | 64 kbps, mono |
| Загрузка | `.wav .mp3 .m4a .ogg .flac .webm .mp4 .mkv` | как пришло | видео: звук вынимается, оригинал может быть снесён после проверки WAV |

Документы: PDF (PyMuPDF **1.28.2**, таблицы — `find_tables`), DOCX (python-docx **1.2.0**, `docx_extract.py`; .doc/.odt/.rtf — через LibreOffice), Excel/CSV (openpyxl **3.1.5**, `xlsx_extract.py`; .xls/.ods — через LibreOffice), PPTX (python-pptx **1.0.2**, `pptx_extract.py`), HTML (`html_extract.py`: таблицы и выгрузки Google Sheets), картинки OCR Tesseract / vision через LLM. Таблицы всех форматов — один markdown-формат `table_md.py` (подпись `Таблица N: …` + GFM). Неподдерживаемый формат отклоняется при загрузке (415).

#### Как устроена итоговая выжимка проекта

Не «перечитать сырые файлы». `tz.rebuild_tz()` собирает **уже готовое**:

1. Артефакты `status=done`, порядок: вес ↓, дата ↓.
2. **Созвон:** канон `parsed/calls/{id}_final.txt` (proof + имена). Нет файла — чанки БД. Плюс `*_call_summary.md` (протокол **этого** созвона).
3. **Документ:** чанки `chunks`; нет индекса — `transcription_text`.
4. Плюс активные сущности только `decision`/`task` (mention / participant / fact / summary в дамп не кладутся), `user_notes`, конфликты `POTENTIAL_CONFLICT` ± `resolutions`.
5. Нарезка ~6000 симв. → если кусков >1, **последовательный** map (LLM, 1200 tok) → при переполнении контекста — **иерархическое слияние черновиков** (`tz._collapse_drafts`, промежуточный merge-проход по группам ~9000 tok, пока всё не уложится в ~12000 tok) → reduce (8192 tok). Предыдущая `current_tz.md` в промпт **не** подставляется (убрана по промпту). Промпт `docs/prompts/rebuild_tz.md` (6 блоков: map → merge → reduce).
6. Результат: `project_state/current_tz.md` + архив `versions/` + `changelog`.

Кнопка **Пересобрать** — только **admin**, синхронно в FastAPI (минуты из‑за N+1 LLM). Авто (с 2026-09-29, решение владельца): с автопроверкой — задачей очереди `auto_review` scope `summary` за проверками нового материала, одна на партию (документы и созвоны); без автопроверки (режим off) — сразу: документ — в конце партии, созвоны — каждые `TZ_REBUILD_EVERY_N` (по умолчанию 3). Подробно — «Итоговая выжимка по проекту» ниже. Чат — другое: top‑12 чанков + до 12 ближайших `decision`/`task`/`fact` + хвост `current_tz.md`.

#### Удаление

| Что | Кто | Что стирается | Что остаётся |
|-----|-----|---------------|--------------|
| Проект | admin, manager | `data/projects/{id}/` + строки БД этого проекта | другие проекты, global `speaker_profiles`, пользователи |
| Артефакт | любой залогиненный | workspace, оригинал, parsed/structured `{id}_*`, jobs, chunks, entities **и их links/resolutions**, project-scoped голоса с этого файла | проект, чат, ТЗ, чужие файлы, global голоса |

Перед `DELETE FROM entities` — `graph.delete_entities_with_deps` (иначе SQLite FK). UI артефакта: иконка корзины, серый контур, модалка + чекбокс.

---

## 2. Корень и каталоги

| Путь | Назначение |
|------|------------|
| `C:\1C_NEW_SKKKK\` | Единственный корень проекта |
| `app/app/` | FastAPI: роутеры, сервисы, модели, воркер, шаблоны |
| `asr/` | Lab ASR (Parakeet/Sortformer/GigaAM-сверка), view, speaker match |
| `asr/lab/` | Канонический конвейер расшифровки (изоляция прогонов) |
| `asr/docker/` | Образ GigaAM; Whisper/T-one на диске для отката |
| `asr/tests/` | Unit-тесты ASR-модулей |
| `data/app.db` | SQLite: users, projects, artifacts, jobs, entities, links, chunks |
| `data/logs/` | api.log, worker.log, monitor_loop.log |
| `data/projects/{id}/` | Файлы проекта (см. §6) |
| `data/project_map.db` | Векторный индекс карты проекта (sqlite-vec, для агентов) |
| `docs/` | План, решения, промпты, этапы, **эта карта** |
| `docs/prompts/` | Промпты LLM (merge, entities, summary, TZ, chat, vision) |
| `docs/stages/` | STAGE-NN: критерии готовности этапов |
| `scripts/` | Утилиты, мониторинг, rerun, индекс карты |
| `tools/lightrag/` | **LightRAG** Docker (:9621) — граф знаний по docs (кнопки, API, БД) |
| `.cursor/rules/` | Правила агента Cursor |
| `.cursor/skills/` | Скилы (gpu-orchestrator, knowledge-pipeline, …) |
| `venv/` | Python **3.10.9** (портал); `venvs/lab` — тот же 3.10.9 под ONNX |
| `.env` | Секреты (не в git), шаблон `.env.example` |

### Кто что видит

Правило: **сначала сервер, потом кнопка.** Чужой проект — **404** (как неактивный), не 403. Лаб / match / «Пересобрать» на своём проекте — **403**, если роли не хватает. Охранник: `services/project_access.py` (`require_project`, `require_artifact_project`).

| | admin | manager | user |
|---|---|---|---|
| Список `GET /projects` | все активные | свои: создал / загрузил / писал в чат / **выдали доступ** | те же свои, даже если сейчас пароль `user` |
| Карточка / чат / отчёт `/report` / загрузка | любой проект | те же свои | те же свои |
| **лаб**, `report-lab`, **лаб встречи**, `report-meeting-lab`, lab enroll/merge | да | нет (кнопка скрыта, URL 403) | нет |
| **match**, «Подтвердить спикеров», enroll/merge | да | на своих, если пароль manager | нет (кнопка скрыта, URL 403). Пароль `user` — нет, даже у создателя |
| «Пересобрать» ТЗ | да | нет (уже admin) | нет |
| «Пересобрать выжимку» / reindex / «Пересчитать» | да | на своих | нет (403) |
| Удалить проект | любой | только свой | 403 (роль) |
| MCP-ключи портала | любой проект | только свой | 403 (роль). Сырой `/mcp/projects/{id}` — по ключу, не JWT |

Роль меняется паролем, список проектов — нет: «свои» считаются по логину (создал, загрузил файл, писал в чат или строка в `project_members`), не по текущей роли. Чужой проект без следа этого логина — 404. Удалить проект может admin или тот, кто его создал. Блок **Выдать доступ** — admin или менеджер проекта; роль в проекте только `manager` / `user`, не `admin`.

---

## 3. Процессы runtime

| Процесс | Команда | Порт / роль |
|---------|---------|-------------|
| **API + UI** | `python -m app.app.main` / `main all` | Preflight → `edge_runtime` (Caddy + LiveKit + `voice_cleaner`) → GPU-воркер → портал `WEB_PORT` (**1777**). Повторный `main` при живом `/health` — no-op |
| **Только портал** | `python -m app.app.main api` | То же без воркера. Воркер (`main worker`) edge **не** поднимает |
| **Caddy** | поднимает `main` (`ensure_tls_cert` → `ensure_caddy`); опц. служба `SKKKK-CADDY` | `:777` HTTPS (готовый файл, Chrome к IP часто без SNI) + `:8088` HTTP-01 + `:8443` выпуск/продление Let's Encrypt **на IP** (`shortlived`). При старте `main` проверяет файл сертификата и перевыпускает, если его нет или он недействителен. Конфиг — `tools/caddy/Caddyfile` |
| **LiveKit** | поднимает `main` (`ensure_livekit`) | Native `tools/livekit/livekit-server.exe` `:7880`. Не Docker (STUN ломается) |
| **Voice cleaner** | поднимает `main` после `:7880` | `venvs/voice` + `tools/livekit/voice_cleaner.py`, участник `skkkk-dfn` (**не** hidden: у hidden LiveKit прячет и дорожки). Чистит живой звук потоком (`_LiveFilter`: STFT без сброса, память GRU и нормировок между кусками по 80 мс — как обработка всей записи, +30 мс), публикует `clean-<identity>` — **одну на человека на всё время в комнате** (`get_out`/`drop_out`: переключатель шумодава и переподключение меняют только задачу, не публикацию); чистит только голос, не звук показа экрана (`is_voice_source`); сам поднимает себе приоритет (above normal); не больше `MEETING_VOICE_MAX_TRACKS` (8) дорожек на процесс; очередь входа > 150 мс выбрасывается, буфер выхода 120 мс; раз в 10 с сверяет с сервером, что он в комнате (`MemberWatch`). Пишет сырые дорожки в `data/meetings/{room}/` (`MEETING_RECORD=1`). Ставит `scripts/install_meeting_voice.py`. GPU не трогает (`CUDA_VISIBLE_DEVICES=-1`). Нет venv — комната без серверной чистки и без записи |
| **Screen recorder** | поднимает `main` после `voice_cleaner` | `venvs/voice` + `tools/livekit/screen_recorder.py`, участник `skkkk-rec` (hidden, без publish; `MemberWatch` — перезаход, если сервер его потерял). Пишет `tracks/{identity}-screen_video-{sid}.mp4` в ту же кучу, 1 fps, холст 1920×1080, 5 Мбит/с. Камеру не пишет. `MEETING_SCREEN_RECORD=0` — без файлов |
| **GPU Worker** | `python -m app.app.main worker` | Очередь `jobs` — lab ASR, knowledge, один job за раз |
| **GigaAM** | Docker `skkkk-gigaam` | `:18102` — второй слух + **embed_voice** |
| **Whisper / T-one** | Docker (не стартуют) | `:18101` / `:18103` — откат, воркер не вызывает |
| **Ollama** | вне проекта | `:11434` — `bge-m3`, CPU |
| **LightRAG** | `docker compose up` в `tools/lightrag/` | `:9621` — граф карты проекта (agents) |
| **LM Studio** | вне проекта, запуск `run_server.ps1` | `:1234` — Local LLM (если `LLM_PROVIDER=lmstudio`) |
| **Cursor API** | облако/local SDK | если `LLM_PROVIDER=cursor` |

**Важно:** кнопки «Пересчитать» / «Пересобрать выжимку» / «Переиндексировать» идут через **API-процесс**, не через GPU-воркер.

### 3.0. Как поднять сервер: установка, запуск, перезапуск, HTTPS

**Порядок старта `python -m app.app.main all`** (его вызывает `scripts/start.ps1`): preflight (проверки `.env`, моделей, портов; `--skip-checks` пропускает) → `edge_runtime`: сертификат (`tls_cert.ensure_tls_cert`) → Caddy → LiveKit → `voice_cleaner` → `screen_recorder` → GPU-воркер (`data/locks/gpu_worker.pid`) → портал Uvicorn `127.0.0.1:WEB_PORT` (1777). Падение LiveKit портал не валит.

| Задача | Команда | Что делает |
|--------|---------|------------|
| Установка на новом ПК | `INSTALL.bat` (корень) → `scripts/bootstrap.ps1` | полная установка одной командой; окружение после переноса папки — `scripts/setup_env.ps1`; проверка окружения — `scripts/check_env.ps1` (отчёт `data/logs/env-check.txt`) |
| Локальная LLM | `run_server.ps1` (корень) | запускает LM Studio и грузит Swift-Qwen 27B с пресетом `swift-qwen-24gb` (один слот, flash-attn, KV q8_0, MTP; `-NoMtp` — без MTP). Сервер SKKKK не поднимает |
| **Перезапуск (основной способ)** | `scripts/restart.ps1` [`-Llm saved\|gemma\|cursor`] [`-Force`] [`-SkipChecks`] | 1) **отказ, если идёт встреча** (`scripts/live_meetings.py`, код 3) — иначе оборвётся запись голоса; `-Force` перезапускает всё равно; 2) встреч нет — гасит все копии `voice_cleaner` / `screen_recorder` и их pid-файлы; 3) проверки: Ollama `:11434`, при `lmstudio` — LM Studio `:1234` и загружена ли `LMSTUDIO_MODEL`; 4) `start.ps1 -SkipChecks` в фоне, лог `data/logs/restart.log` (+ `.err`); 5) ждёт `/health` **и новый PID воркера** (старый сервер может ещё отвечать); 6) в фоне обновляет индекс проекта |
| Запуск | `scripts/start.ps1` [`-Llm cursor\|gemma`] [`-NoStop`]; обёртки `start-saved.ps1` (LLM из `.env`), `start-gemma.ps1`, `start-cursor.ps1`, `run_all.ps1` | сначала останавливает старое (PID воркера + процесс на `WEB_PORT`, снимает `gpu_worker.lock/.pid`), затем `main all` |
| Остановка | `scripts/stop.ps1` | гасит API и GPU-воркер |
| Проверка без запуска | `scripts/check_systems.ps1` | только проверки всех систем |
| Службы Windows | `scripts/install_services.ps1` (от администратора, NSSM) | службы `SKKKK-API` (`main api --use-saved-llm --llm-provider cursor`) и `SKKKK-GPU` (`main worker`); Caddy отдельной службой — `scripts/install_caddy.ps1` (`SKKKK-CADDY`) |
| Бэкап БД | `scripts/backup_db.ps1` [`-Keep 7`] | копия SQLite через `VACUUM INTO`: `app-*.db` и `credentials-*.db` (личные пароли), фото — `people-*.zip`; проверка — `scripts/verify_backup.py` |
| Автоподъём | `scripts/monitor_skkkk_loop.ps1` | раз в 5 мин `GET /health`, при сбое убивает все `app.app.main` и поднимает `main all --llm-provider cursor`. **Устарел:** проверяет `http://127.0.0.1:777/health`, а на `:777` теперь HTTPS (Caddy) — проверка не пройдёт; встречи не учитывает. Не запускать, пока не исправлен |

Зависимости, которые портал поднимает сам: Docker Desktop и контейнер GigaAM перед аудио (`docker_runtime.py`), Ollama перед индексом (`ollama_runtime.py`). LM Studio — вручную или `run_server.ps1`.

**HTTPS (Caddy, `tools/caddy/Caddyfile`).**

| Порт | Что | Куда |
|------|-----|------|
| `:777` (`SKKKK_HTTPS_PORT`) | вход для пользователей, TLS готовым файлом (`SKKKK_TLS_CERT` / `SKKKK_TLS_KEY`) | `/livekit/*` → LiveKit `127.0.0.1:7880`; всё остальное → портал `127.0.0.1:1777` (ожидание ответа до 15 мин) |
| `:8088` (`SKKKK_ACME_HTTP_PORT`) | HTTP-01 проверка Let's Encrypt | `/.well-known/acme-challenge/*` отвечает Caddy; остальное — редирект на `https://SKKKK_PUBLIC_HOST:777` |
| `:8443` (`SKKKK_ACME_TLS_PORT`) | выпуск и продление сертификата на IP (профиль `shortlived`) | отвечает `ok` |

Сертификат: Caddy хранит его в `data/caddy/certificates/<CA>/<host>/<host>.crt|.key` (`SKKKK_CADDY_STORAGE`). При старте `tls_cert.py` проверяет файл для `:777`: есть ли, не staging ли, не истекает ли раньше `SKKKK_TLS_RENEW_HOURS`; иначе перевыпускает через `:8443` и перезагружает Caddy. Если явный `SKKKK_TLS_CERT` отсутствует или staging, а CA боевой — берётся файл из хранилища Caddy. Логи: `data/logs/caddy.log` (запросы `:777`), `data/logs/caddy-proc.log` (сам Caddy). Почта для ACME — `SKKKK_ACME_EMAIL`, CA — `SKKKK_ACME_CA`.

### 3.1. Изоляция арtefactов (не смешивать файлы)

| Зона | Путь | Изоляция |
|------|------|----------|
| **Канон арtefactа** | `data/projects/{id}/artifacts/{art_id}/` | audio, asr, reports — только этот job |
| **Parsed / structured** | `data/projects/{id}/parsed/`, `structured/` | имена с `{artifact_id}_*` |
| **Docker bridge** | `asr/work/` | общий mount GigaAM; прод-изоляция — `lab/runs/{run_id}/`, не чужой файл |
| **Dev fallback** | `asr/work`, `asr/out` | только если `SKKKK_ASR_*` не заданы (локальный прогон) |
| **GigaAM embed job** | `data/gigaam/embed_job.json` | общий файл; один GPU-воркер — OK; параллель без staging — нет |
| **Tone sidecar txt** | `asr/out/input_raw_tone.txt` | dev-арtefact; prod пишет в `SKKKK_ASR_OUT/docker_tone.*` |

Инварианты: lab-прогон в `lab/runs/{run_id}/`; GigaAM embed — stage + hash-check; prod-код **не** читает `asr/out` как fallback. Whisper/T-one воркер не вызывает. `tone_for_align` — lex/duration guard; `finalize_audio_outputs` — всегда перезаписывает parsed raw-копии; prod-код **не** читает `asr/out` как fallback для арtefact-отчётов.

---

## 4. Конвейеры данных

### 4.1. Аудио (основной)

```
upload → job queued
  → preprocessing: FFmpeg 16 kHz WAV (атомарная запись + lock; опрос списка **не** извлекает)
  → asr_running: pipeline_lab.py
      ├── lab_pipeline.py --no-voice (venvs\lab: Parakeet + Sortformer;
      │     `--run-id` только если sha256 в run.json совпадает с текущим WAV)
      ├── lab_sense.py (Cursor)
      ├── lab_giga.py (Docker GigaAM)
      ├── lab_reconcile.py + lab_proof.py
      ├── lab_voice.py (WeSpeaker)
      ├── публикация turns/match/parsed + HTML /report
  → speakers_pending (стенограмма уже есть, speaker_0…)
  → в таблице: **match** (есть метки) → Запомнить имена → **Подтвердить** (все named) → job knowledge
  → speakers_confirmed → apply_confirmed_speaker_names → transcript_ready
  → knowledge pipeline (worker):
      entities (LLM) → index (Ollama, автозапуск) → call_summary / doc_summary → graph → done
```

### 4.2. Документы / изображения

```
upload → extract (PDF/DOCX/XLSX/CSV/PPTX/OCR/vision; таблицы → markdown на своих местах)
  → normalize (LLM только проза, таблицы дословно; Excel без LLM) → text_extracted
  → knowledge pipeline (entities → index → doc_summary → graph → TZ trigger → done)
```

### 4.3. Статусы артефакта

`uploaded` → `preprocessing` → `asr_running` → `speakers_pending` → `speakers_confirmed` → `transcript_ready`  
→ `entities_extracted` → `indexed` → `graph_seeded` → `done` | `error`

(Документы: `text_extracted` вместо ASR-статусов.)

### 4.4. Итоговая выжимка проекта vs выжимка созвона

См. **§1.1**. Кратко: проектная выжимка = map/reduce по **готовым** стенограммам/чанкам + сущности `decision`/`task` + заметки + конфликты + `*_call_summary.md`. Выжимка созвона = `summarize_call.py` по подписанному `docker_gigaam_speakers.json` → `call_summary.md` одного файла.

---

## 5. UI: страницы, кнопки, кто выполняет

> **Правило:** кнопки с `POST /api/artifacts/…` и `POST /projects/…/rebuild-tz` идут **синхронно через FastAPI** (`:777`).  
> **Блокировка повторного нажатия:** `SKKKK.guardButton` / `SKKKK.guardSubmit` (портал) и `guardActionButton` (стенограмма) — спиннер, фиксированная ширина, как у «Скачать».  
> **Долгие операции:** таблица `project_tasks`, `GET …/tasks/active`. Кнопка «Пересобрать» на `/projects/{id}` и `/projects/{id}/tz` опрашивает задачу каждые **3 с** (в том числе автосбор после документа) и блокируется так же, как при клике: `disabled` + spinner + `aria-busy`. Повторный POST → **409**.  
> Загрузка файлов → **job в GPU-воркер**. Авто-опрос статусов на странице проекта — каждые **3 с**.

### 5.1. Роутеры (backend)

| Файл | Prefix | Назначение |
|------|--------|------------|
| `routers/pages.py` | `/` | HTML-страницы, отчёты арtefact, audio stream |
| `routers/auth.py` | `/auth` | POST login: логин из `Login_up.txt` (без регистра); пароль = роль (`admin`/`manager`/`user`); Telegram ID необязателен (поле на форме скрыто с 2026-09-28, `hide_telegram` в `pages.login_page`): указан — пишется, нет — прежний или заглушка `placeholder_telegram_id`; JWT **и** cookie. При `PERSONAL_PASSWORD=required` login отдаёт `{step: setup|personal, ticket}` (пропуск 5 мин, одноразовый, годится только для своего шага) → `POST /auth/login/setup` (новый + повтор) или `/auth/login/personal`; карточка не заполнена → `{step: profile, ticket}` (15 мин) → `POST /auth/login/profile/options` (компании без стороны, текст уведомления о голосе) и `POST /auth/login/profile` (multipart: ФИО, компания, должность, фото) → сессия. `GET /auth/password/state`, `POST /auth/password/change` (меню в шапке) |
| `routers/people.py` | `/people`, `/me` | `GET /people/{id}/avatar?size=96|512&v=` (вошедшим; с текущей версией — кеш навсегда, без — 5 мин); `GET|PUT /me/profile` (ФИО, должность; компанию и согласие не меняет — 422), `PUT /me/avatar` |
| `routers/projects.py` | `/projects` | список, создание, **полное удаление** (`DELETE /{id}`) |
| `routers/upload.py` | `/projects` | Resumable upload, list artifacts (`speakers_match_available` / `speakers_confirm_ready`), **DELETE** одного артефакта |
| `routers/knowledge.py` | `/projects/{id}/…`, `/chat` | search, graph, TZ, chat, deadlines, reextract-entities, conflicts, notes |
| `routers/project_members.py` | `/projects/{id}/members` | выдача доступа: GET список, POST роль manager/user, DELETE участника |
| `routers/project_mcp.py` | `/projects/{id}/mcp`, `/mcp` | ключи MCP портала + Streamable HTTP |
| `routers/project_mcp.py` (обмен) | `/projects/{id}/agent-*` | обмен с внешним агентом: `agent-exchange` (экран), `agent-exchange/summary`, `agent-settings` GET/PUT, `agent-verdicts` + `/{entity_id}/{action}`, `agent-inbox` + `/{qid}/draft`, `/{qid}/answer` (ответы и вердикты — только admin) |
| `routers/requirement_review.py` | `/projects/{id}/requirements` | подтверждение требований человеком: `review`, `confirm`, `/{rid}/{action}`; читают admin и manager, решают только admin |
| `routers/admin_map.py` | `/admin/map`, `/admin/map/search` | страница «Карта проекта» (только admin): поиск по индексу кода и связей; читает `data/project_map.db`, боевую БД не трогает |
| `routers/acme.py` | `/.well-known/acme-challenge` | HTTP-01 → Caddy (Let's Encrypt на IP) |
| `routers/meetings.py` | `/projects/{id}/meetings`, `/api/meetings/public` | встречи проекта (+ идущие сейчас `GET …/meetings/live` для полоски «Идёт созвон») + гостевой join/leave/heartbeat по токену + сотрудник: `/projects/{id}/meetings/{mid}/heartbeat` и `/leave` (identity только `member:{user.id}`); `GET /api/meetings/public/{token}/avatars/{user_id}` — фото сотрудника гостю, только участника этой встречи. Сотрудник входит под «Имя Фамилия» из карточки |
| `routers/speaker_profiles.py` | `/api` | enroll, merge, lab enroll/merge, recalculate, speakers-confirm, rebuild-summary, reindex |

### 5.2. Общая навигация

| Элемент | URL | Шаблон | Действие |
|---------|-----|--------|----------|
| Логотип **Primary Analytics** | `/` | `base.html` | Список проектов |
| **Выйти** (иконка у ника) | `/auth/logout` | `base.html` `#nav-logout` | `POST /auth/logout`, сброс cookie и localStorage |
| **к проектам** | `/` | `project.html` nav | То же |
| `/login` | `/login` | `login.html` | Вход только для логинов из `Login_up.txt` (регистр не важен); JWT + cookie `skkkk_session` |
| Гостевая встреча | `/m/{token}` | `meeting_public.html` | Без JWT. Нет навигации по проектам. Сырой токен не хранится — только sha256 |
| HTML-оболочки (`/`, проект, граф, ТЗ, чат, `/report*`) | те же URL | Jinja | Без сессии — **302** на `/login` (`require_html_user`) |
| `/audio`, `/download`, speaker API | те же URL | — | Без сессии — **401** |
| `/health` | `/health` | — | Публично только `{"status":"ok"}` |
| `/health/ready` | `/health/ready` | бейдж LLM в nav (`#nav-llm`: «Local LLM» при LM Studio, «Cursor») + значок «Нужен перезапуск» (админ) | Очередь/VRAM/LLM + `build` (код на диске новее API/воркера, `services/build_info.py`) — только с сессией |
| `/admin/conflicts` | `/admin/conflicts` | `conflicts.html` | Очередь конфликтов (**admin**, HTML 302→login / 403 не-admin) |
| `/projects/{id}/graph` | graph | `graph.html` | Cytoscape-граф (GET graph/data) |
| `/projects/{id}/tz` | tz | `tz.html` | Отдельная страница ТЗ (дублирует rebuild-tz) |
| `/projects/{id}/chat` | chat | `chat.html` | Отдельный чат (дублирует чат на project) |

### 5.3. Список проектов — `/` (`index.html`)

| Кнопка / элемент | API | Кто | Роль | Результат |
|------------------|-----|-----|------|-----------|
| **Новый проект** | — | UI | admin, manager | Открывает модалку |
| **Создать** | `POST /projects` | FastAPI | admin, manager | Проект + `data/projects/{id}/` tree |
| **Отмена** | — | UI | — | Закрыть модалку |
| **Исправлено** (шапка, слева от бейджа LLM, `nav-changelog`) | — | UI | auth | Модалка `#changelogModal`: список изменений для пользователя, новые сверху. Данные — `app/app/changelog.json`, читает `load_changelog()` в `routers/pages.py` при рендере `/`. Дополнять после каждой правки функционала/качества (правило в `AGENTS.md`) |
| Клик по проекту | — | navigate | свои или admin | `/projects/{id}`; чужой URL — **404** |
| **Описание** (стрелка справа от названия) | — | UI | auth | Сворачиваемый блок: текст описания слева, **Удалить проект** справа (admin/manager). Без описания стрелка всё равно есть, чтобы открыть удаление |
| **Удалить проект** | `GET /projects/{id}/delete-preview` → `DELETE /projects/{id}` | FastAPI | admin любой; manager только свой | Сначала модалка «Подтверждение удаления»: счётчики из `delete-preview` (`#delete-project-counts`: документы, созвоны, встречи, заметки) + чекбокс «Подтверждаю полное удаление» (`#delete-project-ack`); без галочки кнопка «Удалить» неактивна. Затем полное удаление дерева и записей БД |
| Авто-загрузка | `GET /projects` | FastAPI | auth | admin — все активные; иначе создал / загрузил / чат / `project_members`. Пустой список: «Нет своих проектов.» Кнопка **Новый проект** только admin/manager |

### 5.4. Страница проекта — `/projects/{id}` (`project.html`)

#### Макет: выжимка и чат (строка `project-insights-row`)

На **lg+** (≥992px) левая колонка «Итоговая выжимка по проекту» и правая «Чат с ИИ» — **одинаковая высота**: `min(70vh, 720px)`, `position: sticky; top: 0.75rem`. Карточки — flex-колонки (`insights-card`, `chat-card`); контент (**`#tz-content`**, **`#chat-log`**) скроллится **внутри** панели, поле ввода чата прижато к низу (`chat-composer`, `margin-top: auto`). Страница при длинной переписке не растягивается.

На **мобильном** (<768px): `#tz-content` до `50vh`, `#chat-log` до `40vh`, отдельный внутренний скролл.

#### Загрузка материалов

| Кнопка | API | Кто | Результат |
|--------|-----|-----|-----------|
| **Загрузить** | `POST …/uploads/init` → `PUT …/chunks/{n}` → `POST …/complete` | FastAPI + **GPU worker** (job) | Файл в `raw_inputs/`, job `queued`, воркер гонит ASR/knowledge |

Тип (`source_type`): `call` | `document` | `note` | `screenshot` — влияет на пайплайн воркера.

#### Полоска «Идёт созвон» (между загрузкой и материалами)

`#live-meetings` — по строке на идущую встречу: «Идёт созвон · в комнате: имена» (уже 576 px — имена второй строкой) и **Присоединиться** (`/projects/{id}/meetings/{mid}/room`); несколько встреч сразу — с номером «#id». Только **admin/manager** (кто может войти в комнату); у `user` не рендерится. Идущая = статус `live` **и** в комнате есть участник на связи: не вышел, вход или пульс за 10 мин (`services/meetings.ongoing_meetings`, то же правило у `scripts/live_meetings.py` для `restart.ps1`). Забытая `live` без людей полоску не держит.

| Элемент | API | Поведение |
|---------|-----|-----------|
| Первая отрисовка | — (`project_page` передаёт `live_meetings`) | с сервера, чтобы страница не «прыгала» после загрузки; без встреч блок `hidden` |
| Обновление | `GET /projects/{id}/meetings/live` | тик опроса страницы (3 с, не в скрытой вкладке) и сразу после действий во «Встречах»; DOM перестраивается только при смене разметки. Встречу завершили или все вышли — полоска пропадает |

#### Таблица артефактов (авто-обновление 3 с)

| Элемент | API | Поведение |
|---------|-----|-----------|
| Badge статуса | `GET /projects/{id}/artifacts` | `reconcile_artifact_status` подтягивает `done`, если knowledge уже есть. Для аудио те же флаги кнопок: `speakers_match_available`, `speakers_match_ready`, `speakers_confirm_ready` (`speaker_action_flags`) |
| Ссылка на имя файла | — | При `status=done` → `/artifacts/{id}/report` (стенogramма или отчёт документа) |
| **match** | — | `/artifacts/{id}/report-speaker-match`. Видна **admin/manager**, когда в `sortformer_speaker_match.json` уже есть метки `speaker_N`. У `user` кнопки нет; URL — 403. Во время ASR / подготовки не показывается. |
| **лаб** | — | `/artifacts/{id}/report-lab`. Только **admin** (кнопка и URL). Менеджеру `lab_runs` в JSON = 0. |
| **лаб встречи** | — | `/artifacts/{id}/report-meeting-lab`. Только **admin**. У созвона (`source_type=call`, `kind=meeting`) вместо обычной лаб. Дорожка за дорожкой: вход/выход стадий до итоговой расшифровки. |
| **Подтвердить спикеров** | `POST /api/artifacts/{id}/speakers-confirm` | Видна **admin/manager** при `speakers_pending`, когда все `speaker_N` уже названы (enroll на match). Тогда `speakers_confirmed` + job **knowledge**. У `user` нет. |
| **Pipeline live** (раскрывашка) | тот же GET | lab checklist / knowledge из `pipeline_progress.py` |
| **Скачать** | `GET /artifacts/{id}/download` | Созвон/видео → **MP3** (`download.mp3`, 64 kbps); документ → оригинал из `raw_inputs/` |
| **Удалить** | `DELETE /projects/{id}/artifacts/{art_id}` | Узкая колонка после «Ошибка», **иконка корзины** (`aria-label` + подсказка). Модалка «Подтверждение удаления» + чекбокс; без галочки кнопка неактивна. Стирает файл, workspace, parsed, джобы, чанки, сущности этого артефакта. Проект и остальные файлы остаются. Только владелец проекта или admin. |
| Toast «обработан» | — | Когда статус сменился на `done` |

Статусы в UI: В очереди → Подготовка → ASR → … → **Готово** / **Ошибка**.

#### Итоговая выжимка по проекту (левая колонка)

| Кнопка | API | Кто | На чём | Время | Роль |
|--------|-----|-----|--------|-------|------|
| *(авто load)* | `GET /projects/{id}/tz/content` | FastAPI | `project_state/current_tz.md` | — | все |
| **Пересобрать** | `POST /projects/{id}/rebuild-tz` | FastAPI → `tz.rebuild_tz_fast()` (с 2026-09-18: было `tz.rebuild_tz()`, см. §5.10/§5.11) → top-K векторный поиск (chunks + `decision`/`task` сущности, MMR на разнообразие) + **один** LLM reduce-вызов | Не читает весь проект — top-36 чанков + top-36 сущностей по 7 обобщённым поисковым запросам (не завязаны на тематику конкретного заказчика) + заметки + справочный блок предыдущей версии (фильтр противоречий, не источник фактов). Пока `tz_rebuild` running — кнопка disabled/spinner, POST 409. | ~30-35 с | **admin** |

Источник — **не** обрезанный excerpt сырого текста. Если у аудио нет `_final.txt` — fallback на чанки / `transcription_text`. Роут в `knowledge.py`.

Результат: `data/projects/{id}/project_state/current_tz.md`, архив в `versions/`, запись `changelog`.

Авто-пересборка (решение владельца 2026-09-29): материал в конце знаний (`pipeline_knowledge._enqueue_auto_review`) ставит за своими проверками artifact → topics → relevance → conflicts задачу `auto_review` scope `summary` → `requirements_reextract.summary_for_review` → `rebuild_project_summary` (`prepare_for_llm` + `rebuild_tz_fast`). Воркер один, задачи идут по порядку постановки, поэтому выжимка собирается, когда актуальность и конфликты уже выключили заменённое. Одна на партию: задача пропускается без вызова модели, если в очереди ждёт ещё одна выжимка проекта (`tz.summary_jobs_queued`); `maybe_auto_rebuild_tz` при такой задаче не собирает сразу (`reason=after_checks`), в том числе после упавшего последнего файла партии. Раньше собиралась сразу после знаний, до проверок: новый документ, заменивший требование, оставлял старое в выжимке и в ответах чата. Без автопроверки (режим off) — как раньше: документ — в конце партии (`_other_pending_jobs`), созвоны — каждые **N** готовых материалов (`TZ_REBUILD_EVERY_N`, по умолчанию 3).

#### Чат с ИИ по проекту (правая колонка)

| Кнопка | API | Кто | На чём |
|--------|-----|-----|--------|
| **Спросить** | `POST /chat` `{project_id, question}` | FastAPI → `chat.answer_chat()` | RAG: sqlite-vec **чанки** + `search_entities` (`decision`/`task`/`fact` по `entity_vectors`) + документы + выжимки + хвост `current_tz.md` (если файл есть) + **chat_guidance** + **вся лента `chat_messages` этого логина в проекте** (та же, что на экране; лимит ~32k символов с хвоста) + LLM. «Не найдено» только если нет чанков, сущностей, документов, выжимки и своей ленты. |
| **История чата** | `GET /projects/{id}/chat/history` | FastAPI → `chat.list_chat_messages()` | Лента UI + `waiting`, `meta` (уточнения) |
| **Журнал действий** | `GET /projects/{id}/activity` | FastAPI → `user_activity.list_activity()` | API для аудита и памяти чата; **в UI чата не показывается** |
| **Статус чата** | `GET /projects/{id}/chat/status` | FastAPI → `chat.chat_is_pending()` | `{pending, label}` |

Enter в textarea — тоже отправка. Отдельная страница `/projects/{id}/chat` — тот же endpoint. Повторный `POST /chat` при активном ответе — **409**.

#### Дедлайны (сворачиваемый блок, над MCP)

Поручения **со сроком** из сущностей `entity_type=task` (поля `description`, `assignee`, `deadline`, `status`) по всем артефактам проекта. В API и списке — **только** задачи с непустым `deadline`; дедупликация по описанию + ответственный + срок. Оценки длительности этапа («4–6 нед.») и очередность («перед этапом 2») **не** считаются дедлайном.

| Элемент | API | Поведение |
|---------|-----|-----------|
| **Список** | `GET /projects/{id}/deadlines` (`knowledge.py` → `list_project_deadlines`) | Только tasks с `deadline`; таблица: задача, ответственный, срок, статус, источник |
| **Обновить из материалов** | `POST /projects/{id}/reextract-entities` (`knowledge.py` → `reextract_project_entities`) | admin/manager — LLM по lab `_final.txt` / `transcription_text`; пишет `entities`, **не** переиндексирует; 409 если уже идёт |
| **Календарь** | тот же GET | **FullCalendar** — только задачи с распознанной датой (ISO, `DD.MM.YYYY`, «15 марта 2026») |
| Загрузка | — | При развороте блока; авто-обновление после `done` артефакта, если панель открыта |

**UI:** Bootstrap `collapse`, `#deadlines-panel`, по умолчанию **сложен**; переключатель «Список» / «Календарь».

#### Встречи (сворачиваемый блок, над MCP)

Онлайн-встреча проекта + **отдельный гостевой портал** `/m/{token}`. Карточка **не рендерится при пароле `user`**. Создание, старт, отзыв ссылки — **admin/manager** проекта. Гость не видит `project_id` и не может открыть `/projects/{id}` (чужой — 404). Токен показывается **один раз**; в БД только `public_token_hash`. Отзыв делает `/m/{old}` 404; «Новая ссылка» выдаёт другой токен.

**UI:** тот же fold, что MCP — `#meetings-panel`, по умолчанию сложен; `#meetings-toggle-hint` `(развернуть)` / `(свернуть)`.

| Элемент UI | API | Кто |
|------------|-----|-----|
| **Создать встречу** | `POST /projects/{id}/meetings` | admin, manager |
| Список | `GET /projects/{id}/meetings` | admin, manager; `public_token` всегда `null` |
| Идущие сейчас (полоска «Идёт созвон») | `GET /projects/{id}/meetings/live` | admin, manager; `[{id, started_at, people, room_url}]` — только встречи с людьми на связи (`ongoing_meetings`). Маршрут объявлен до `/meetings/{mid}` |
| **Начать** | `POST /projects/{id}/meetings/{mid}/start` | admin, manager |
| **Завершить** | `POST /projects/{id}/meetings/{mid}/end` | admin, manager |
| **Отозвать** | `POST /projects/{id}/meetings/{mid}/revoke-link` | admin, manager |
| **Новая ссылка** | `POST /projects/{id}/meetings/{mid}/regenerate-link` | admin, manager; сырой токен один раз |
| **Проанализировать** | `POST /projects/{id}/meetings/{mid}/analyze` | admin, manager. Один артефакт + job. Повтор `done`/`error` — **Повторить**, тот же артефакт. Воркер для встречи всегда гонит `process_meeting_artifact`, не `asr.resume` со старой стенограммы. queued/running — 409 |
| **лаб встречи** | `GET /artifacts/{id}/report-meeting-lab` | только admin. В колонке анализа, если уже есть `analyze_jobs[0].artifact_id`; и в таблице файлов у артефакта встречи |
| **Удалить созвон** | `DELETE /projects/{id}/meetings/{mid}` | admin, manager. Иконка корзины в строке (как у файлов). Стирает `meetings` / `meeting_participants`, `data/meetings/{room}/`, артефакт `встреча-{id}.wav` + дочерние lab-папки и jobs очереди |
| Гостевая страница | `GET /m/{token}` | без входа; лобби (`guest_id` в `localStorage`), затем `#meeting-stage` / `#meeting-dock`. Флаги `SKKKK_MEETING_VOICE` из `pages.meeting_voice_flags`. Канон звука — сервер `skkkk-dfn` (`MEETING_VOICE_SERVER=1`). `#meeting-denoise` — **Шумодав** у себя: выкл → все слышат сырой мик этого человека (громкость остаётся). Сигнал `publishData` topic `skkkk-denoise`; metadata — запасной. Свой `clean-*` не играется (`publication.trackName`). Что играть решает один `_routeAudio` (`voicePlan`): на человека **ровно одна** дорожка голоса — самая свежая `clean-*`, если он не выключил шумодав, иначе сырой микрофон; нет `clean-*` (готовится, предел дорожек, шумодав переподключается) — сырой; звук показа экрана — всегда как есть. Остальные дорожки и «осиротевшие» `<audio>` снимаются; сверка по событиям LiveKit и раз в 2 с (`_startAudioRoute`). Чужой звук — `<audio>`, не `AudioContext.destination` (иначе Chrome AEC молчит и начинается петля). Клиентский DFN3 выкл. `#meeting-sound` — отписка чужого аудио (`setSubscribed(false)` на все remote audio, включая `clean-*`), не `muted`. Флаги мик/кам — `flagState` (`none`/`off`/`silent`/`on`) + rAF `redraw` на `TrackPublished`/`Unpublished`/`Subscribed`/`StreamStateChanged`/`ParticipantNameChanged`/`ParticipantAttributesChanged`/`ConnectionQualityChanged`; не `isMicrophoneEnabled`. Тост «Микрофон молчит» + `setAttributes({mic_silent:"1"})` (не metadata). Чип качества (`poor`/`lost`); пресеты `VideoPresets.h360`/`h540`, simulcast h180+h360. PWA: `<link rel="manifest">` → `GET /meeting.webmanifest`. На узком экране (`max-width: 767.98px`) в доке мик/звук/кам/экран/`#meeting-more`/выход; сетка, участники, ссылка, динамик, fullscreen, denoise — в `#meeting-more-tray`. Динамик — иконка `#meeting-speaker-btn` + меню устройств, не текст в доке. Флаги плитки — иконки мик/кам. Список — bottom sheet + `#meeting-roster-backdrop`. На телефоне старт в сетке: все плитки в один экран (`phoneGridCols` / `--meeting-cols`); шаринг — верхняя плитка, остальные рядом. Режим спикера по кнопке. На десктопе плитки 16:9 одинакового размера, шаринг не растягивает сетку. «На весь экран» = сцена + док. Экран на телефоне — `getDisplayMedia` |
| Статус для гостя | `GET /api/meetings/public/{token}` | `project_id: null` |
| **Войти во встречу** | `POST /api/meetings/public/{token}/join` `{display_name, guest_id}` | `guest_id` из `localStorage` `skkkk-guest-{token}`; identity = `guest:` + sha256(hash токена + guest_id)[:32]; перезаход — та же identity и одна строка `meeting_participants`. Без `guest_id` — как раньше (`token_hex`). Невалидный id → 400. До start → 409; после → LiveKit JWT (`canPublish`, без `canPublishSources`) |
| **Войти в комнату** (сотрудник) | `POST /projects/{id}/meetings/{mid}/join` и страница `/projects/{id}/meetings/{mid}/room` | admin, manager; тот же LiveKit room. `#meeting-end` → `POST .../end` для всех. «Скопировать ссылку»: сырой токен не хранится (вариант A) — тост «ссылка во вкладке, где встречу создали, или через «Новая ссылка»»; `sessionStorage` `skkkk-meeting-invite-{id}` если ещё открыта вкладка создания |
| Сигналинг LiveKit | `wss://<хост>:777/livekit` → Caddy → `:7880` | Поднимает `main` (`edge_runtime`). Native exe, не Docker. ICE = `node_ip` `SKKKK_PUBLIC_HOST` + LAN + 127.0.0.1, без STUN (иначе рекламируется чужой исходящий IP). TURN = тот же хост. Медиа: TCP 7881, UDP mux 50000, TURN 3478. Смена `livekit.yaml` перезапускает exe. Чистильщик ждёт `:7880`, затем `LIVEKIT_RTC_INTERNAL` |
| Запись дорожек | `data/meetings/{room_name}/manifest.json` | `voice_cleaner` пишет сырой звук каждого `member:`/`guest:`; `skkkk-rec` — видео шаринга (`source: screen_video`, identity = кто включил экран). Связь созвона = `meetings.room_name`. Повторный `track_subscribed` той же личности переключает запись на новый трек (как DFN), не оставляет первую тихую. `MEETING_RECORD=0` выкл. Камера не пишется |
| Анализ дорожек | `data/meetings/{room_name}/analyze.json` + `events.jsonl` | `meeting_analyze.py`: очередь GPU на каждую WAV с речью; lab `03_diar` пишет одного `speaker_0` без Sortformer |

**Пульт встречи (`#meeting-dock`, `static/meeting_room.js`)** — одинаков на гостевой странице и в комнате сотрудника; работает в браузере через LiveKit, без API портала (кроме выхода, завершения и пульса).

| Кнопка | Действие |
|--------|----------|
| **Микрофон** (`#meeting-mic`) / **Звук** (`#meeting-sound`) / **Камера** (`#meeting-cam`) / **Экран** (`#meeting-screen`) | вкл/выкл своего микрофона, звука собеседников, камеры, показа экрана |
| **Выбрать микрофон** (`#meeting-mic-pick-btn`, создаётся скриптом) | список микрофонов; смена на ходу `switchActiveDevice(audioinput)` без перезахода |
| **Динамик** (`#meeting-speaker-btn`) | выбор устройства вывода (`setSinkId`); если браузер не умеет — в меню подсказка |
| **Сменить камеру** (`#meeting-cam-switch`) | следующая камера `switchActiveDevice(videoinput)`; видна, когда камера включена и камер 2+ |
| **Ещё** (`#meeting-more`) → **Экран сверху** (`#meeting-screen-lead`), **Спикер** (`#meeting-layout`, раскладка), **Участники** (`#meeting-roster`; закрыть — `#meeting-roster-close`), **Скопировать ссылку** (`#meeting-copy`), **На весь экран** (`#meeting-fs`), **Шумодав** (`#meeting-denoise`) | вид и сервис, без сервера |
| **Развернуть окно** (у плитки) | плитка на весь экран |
| **Выйти** (`#meeting-leave`) | гость: `POST /api/meetings/public/{token}/leave`; сотрудник: `POST /projects/{id}/meetings/{mid}/leave` |
| **Завершить для всех** (`#meeting-end`, только сотрудник) | `POST /projects/{id}/meetings/{mid}/end` |
| *(фон)* пульс присутствия | гость `POST /api/meetings/public/{token}/heartbeat`, сотрудник `…/meetings/{mid}/heartbeat` |

Лобби гостя: имя (`#guest-name`), свёрнутый блок **Проверить камеру и микрофон (по желанию)** (`#guest-devices-check`, выбор `#guest-mic` / `#guest-cam`), **Войти во встречу** (`#guest-join`).

**Плитки экрана.** Каждая screen-публикация — плитка `meeting-tile-screen-{sid}` (`tileOrder` в `static/meeting_layout.js`). Последняя по `publishedAt` — `lead` (`.meeting-tiles .meeting-tile-screen.is-lead` на всю ширину, правило **вне** phone-media). Новый экран снимает ручной pin; «Экран сверху» в `#meeting-more-tray` возвращает lead. Камера — отдельная плитка (`chosenVideoPub` только `Source.Camera`). Публикация экрана: `contentHint: "detail"`, `ScreenSharePresets.h720fps5`, **одним слоем** (`simulcast: false`): с двумя слоями (540p/1080p) зритель переключался между ними при каждой смене размера плитки и видел скачки резкости («мерцание»).

**Телефон горизонтально** (`@media (max-width: 896px) and (orientation: landscape)` в `app.css`): в фокусе (экран или выбранный участник, `.has-focus`) главное окно слева во всю высоту, остальные — лентой справа по строке на участника (`--meeting-film` из `applyPhoneGrid`); в сетке с экраном строка экрана вдвое выше. Раньше в фокусе действовали правила ПК (одна колонка), и миниатюры 16:9 наезжали друг на друга.

**Устойчивость комнаты (2026-09-29, `meeting_room.js`):**
- камера: намерение человека `_camWanted` (кнопка, вход, возврат); погашенную системой камеру (телефон: наушники, звонок, фон — livekit-client 2.15 глушит трек, который не смог перезапустить) `_scheduleCamRevive` возвращает через 1,5 / 4 / 10 с — по `devicechange`, `visibilitychange`, `TrackMuted` своей камеры; выключенную человеком не трогает;
- метка «микрофон молчит» (атрибут `mic_silent`) уходит на сервер только при смене состояния (`_micSilentSent`; раньше — каждый кадр речи, ~12 запросов/с), `unmute` трека её снимает;
- перерисовка (`_renderTiles`) переставляет плитку в DOM, только если она не на своём месте: перенос живого `<video>` мигает, а `adaptiveStream` на миг считал его скрытым;
- качество связи участника (`_quality`) забывается при его входе и выходе; после возврата циклом `_rejoinLoop` сбрасываются своё качество и `mic_silent`, камера включается по `_camWanted`, а не по последнему видимому состоянию.

**Закрытие комнаты и рекордеры:** `close_livekit_room` — по внутреннему `livekit_rtc_internal`, в своём потоке (работает и из асинхронного кода). Рекордеры (`voice_cleaner`, `screen_recorder`) заходят только в комнаты встреч со статусом `live` (`meeting_recordings.live_room_names`, база только на чтение; нет базы — пишут все, как раньше): комнату завершённой встречи мог заново создать клиент со старым токеном в момент завершения, и боты держали её открытой, записывая удалённую встречу.

**Путь звука (проверка 2026-09-30, `scripts/qa_media_path.py`):** браузер (шумодав браузера, AGC) → Opus/RED → LiveKit → `skkkk-dfn` пишет сырой WAV (96 000 Б/с) и публикует `clean-*` → слушатель играет ровно одну дорожку человека: `clean-*`, если он есть и человек не выключил «Шумодав», иначе сырую. Замер: шум без речи −14…−26 дБ сверх шумодава браузера, речь −1…−2 дБ; `clean-*` отстаёт от сырой на 0,2–0,36 с (синтетический слушатель, приоритет above normal; с обычным приоритетом на занятой машине — 0,25–0,75 с и ~2 с выброшенного звука в минуту). Камера: simulcast 180/360/540, VP8; экран — один слой, 5 к/с.

Камера у гостя выключена. Join до `start` — 409. Удаление созвона или проекта стирает `meetings`, `meeting_participants`, очередь GPU и `data/meetings/{room_name}/`.

#### Удалённый доступ MCP (низ страницы проекта)

Read-only MCP (Streamable HTTP) к знаниям **только этого проекта**. Авторизация — ключ `skkkk_mcp_…`, не JWT портала. Карточка MCP **не рендерится при пароле `user`** (и нет «Выдать доступ»); `GET /projects/{id}/mcp/info` и выпуск ключей — **admin/manager**.

**UI:** блок **сворачиваемый** (Bootstrap `collapse`, `#mcp-access-panel`). По умолчанию **сложен**; клик по заголовку «Удалённый доступ (MCP)» — развернуть/свернуть, подсказка `(развернуть)` / `(свернуть)` в `#mcp-toggle-hint`. Badge «только чтение» в шапке всегда виден.

| Элемент UI | API | Кто |
|------------|-----|-----|
| **Копировать** (URL сервера `#mcp-url`) / **Копировать JSON** (`#mcp-copy-config`, пример `.cursor/mcp.json`) | — (буфер обмена) | admin, manager |
| Права нового ключа (`#mcp-rights`): **Только чтение** / **Чтение + вопросы** / **Чтение + вопросы и вердикты**; **Имя агента** (`#mcp-agent-name`, для журнала); **Лимит за 10 мин** (`#mcp-rate-limit`) | уходят в `POST …/mcp/tokens` | admin, manager |
| **Создать ключ** | `POST /projects/{id}/mcp/tokens` | admin, manager; ключ показывается один раз (`#mcp-new-token`) |
| **Права ключа** (select в строке ключа) | `POST /projects/{id}/mcp/tokens/{token_id}/scope` `{scope, agent_name, rate_limit}` | admin, manager |
| Разрешить ключу класть вопросы/вердикты | `POST /projects/{id}/mcp/tokens/{token_id}/submit` `{enabled}` (флаг `can_submit`, по умолчанию выкл) | admin, manager |
| **Отозвать ключ** | `DELETE /projects/{id}/mcp/tokens/{token_id}` | admin, manager |
| **URL / JSON для Cursor** | `GET /projects/{id}/mcp/info` | admin, manager (свой проект) |
| **MCP endpoint** | `POST /mcp/projects/{id}` + `Authorization: Bearer <ключ>` | держатель ключа |

Инструменты MCP (`app/app/mcp/project_protocol.py`, `MCP_TOOLS`):
- чтение: `get_guide` (инструкция агенту), `project_overview`, `list_artifacts`, `get_project_summary`, `search_project`, `list_entities`, `get_knowledge_graph`, `list_notes`, `list_conflicts`, `get_artifact_content`, `get_artifact_meta`, `get_requirements`, `get_tz`, `get_changes`, `get_conflicts`, `get_quote` (дословная цитата), `get_code_anchors`;
- обмен (только ключ с правами «вопросы» / «вопросы и вердикты»): `submit_question`, `submit_verdicts`, `wait_reply`, `exchange_status`.

Публичный URL для MCP задаётся в `.env`: `WEB_PUBLIC_URL=https://<публичный-адрес>:777`. Без него в UI подставляется адрес из текущего запроса.

#### Обмен с агентами — три карточки (`partials/px_agent_cards.html` + `static/px_agent.js`)

Сворачиваемые карточки (`#px-card-review`, `#px-card-exchange`, `#px-card-settings`), по умолчанию сложены; открытость помнит `localStorage` (`px_open_*`). Все вызовы — `/projects/{id}/…` (`routers/requirement_review.py`, `routers/project_mcp.py`). Читают admin и manager, решения — только admin.

| Карточка / элемент | API | Кто |
|--------------------|-----|-----|
| **Подтверждение требований**: фильтры проверки / статуса / стороны (`#px-rv-verification`, `#px-rv-status`, `#px-rv-side`), **Поиск по тексту** (`#px-rv-q`), **Показать ещё** (`#px-rv-more`) | `GET …/requirements/review` | admin, manager |
| **Подтвердить** / **Отклонить** / **Изменить статус** (с причиной) у требования | `POST …/requirements/{rid}/{action}` | admin |
| **Вернуть решение модели** | `POST …/requirements/entity/{entity_id}/undo-model` | admin |
| Галочки + **Подтвердить выбранные** (`#px-rv-confirm-selected`) | `POST …/requirements/confirm` | admin |
| **Проверить моделью всё накопленное** (`#px-rv-backlog`) | `POST …/auto-review/backlog` `{scope: "all"}` | admin |
| **Обмен с агентами**: вкладки **Вопросы** / **Вердикты** / **Журнал** (фильтр решения шлюза `#px-j-decision`, источник `#px-j-source`) | `GET …/agent-inbox`, `GET …/agent-verdicts`, `GET …/agent-exchange`, `GET …/agent-exchange/summary` | admin, manager |
| Вопрос: **Черновик локальной модели** | `POST …/agent-inbox/{qid}/draft` | admin |
| Вопрос: **Одобрить черновик** / **Ответить** (поле «Ваш ответ агенту») | `POST …/agent-inbox/{qid}/answer` | admin |
| Вердикт: **Подтвердить** / **Отклонить** | `POST …/agent-verdicts/{entity_id}/{action}` | admin |
| **Настройки для агентов**: часовой пояс (`#px-s-tz`), пакет якорей кода (`#px-s-pack`), стороны участников (`#px-s-participants`, «Имя = заказчик/исполнитель»), подсказки заказчика/исполнителя (`#px-s-hint-customer`, `#px-s-hint-vendor`), права ключа по умолчанию (`#px-s-key-scope`), обмен вкл/выкл (`#px-s-enabled`), **Сохранить** | `GET` / `PUT …/agent-settings` | чтение admin, manager; сохранение admin |

#### Выдать доступ (после MCP)

Таблица `project_members`: кого добавили — видит проект и работает по **роли в этом проекте** (`manager` или `user`). Портального `admin` назначить нельзя. Выпадающий список — **все логины из `Login_up.txt`** (даже если человек ещё не входил); создатель и уже добавленные скрыты. Выдавать могут portal admin, создатель и менеджер проекта (`can_grant_access`). Участник с ролью `user` блок не видит. Если учётки ещё нет — она создаётся при выдаче, вход потом подхватит тот же логин.

**UI:** тот же fold, что MCP — Bootstrap `collapse`, `#grant-access-panel`, по умолчанию **сложен**; подсказка `#grant-toggle-hint` `(развернуть)` / `(свернуть)`.

| Элемент UI | API | Кто |
|------------|-----|-----|
| Список + кандидаты | `GET /projects/{id}/members` | admin / менеджер проекта |
| **Добавить** | `POST /projects/{id}/members` `{login, role}` (или `user_id`) | те же; `role` только `manager`\|`user`; логин должен быть в `Login_up.txt` |
| **Убрать** | `DELETE /projects/{id}/members/{user_id}` | те же |

Эффективная роль: portal admin → `admin`; создатель → `manager`; иначе строка `project_members`; иначе (загрузка/чат без выдачи) — роль от пароля.

### 5.5. Стенogramма созвона — `/artifacts/{id}/report` (`transcript.html` / `transcript_view.py`)

Открывается из таблицы проекта (status=done). Встроенный HTML с плеером.

#### Блок резюме созвона (верх)

Две вкладки на одной карточке (переключение без перезагрузки; после reload — `sessionStorage`):

- **Итоговое резюме созвона** — `call_summary.md`, промпт `docs/prompts/call_summary.md` (протокол, таблица «Кто / Что / Срок»).
- **Краткое резюме созвона** — `call_brief.md`, промпт `docs/prompts/call_brief.md` (пока заготовка: «Отрази просто суть созвона»).

Кнопка **Пересобрать выжимку** пишет **оба** файла (если brief падает — итоговый всё равно сохраняется). ТЗ/чат берут только полный протокол. Word: полная выжимка и краткое резюме — отдельные кнопки.

#### Скачать материалы (под плеером)

| Кнопка | API | Файл |
|--------|-----|------|
| **Выжимка Word** | `GET /artifacts/{id}/download/summary.docx` | `.docx` из `call_summary.md` |
| **Краткое резюме созвона** | `GET /artifacts/{id}/download/brief.docx` | `.docx` из `call_brief.md` |
| **Стенограмма .txt** | `GET /artifacts/{id}/download/transcript.txt` | `[таймкод] имя: текст` из `docker_gigaam_speakers.json` |

#### Панель «Спикеры и инструменты» (registry)

| Кнопка | API | Кто | На чём | Время |
|--------|-----|-----|--------|-------|
| **▶** (у спикера) | — | browser | seek audio | мгновенно |
| **Запомнить** / **Сохранить** | `POST /api/artifacts/{id}/speaker-enroll` | FastAPI → DB + `write_prod_view` | match JSON, `speaker_profiles`; lab: proof + имена → speakers.json/HTML, **не** `build_transcript_view`, **не** `voice_turn_audit` / GigaAM по всем репликам | ~1–5 с |
| **Слить** (два ID) | `POST /api/artifacts/{id}/speaker-merge` | FastAPI | merge_map + lab `write_prod_view` | ~1 с |
| **Пересчитать** (`#recalc`) | `POST /api/artifacts/{id}/speaker-recalculate` | FastAPI, **admin/manager** | Тот же API, что «Пересчитать подсказки» на match. Lab: cosine `speaker_N` ↔ база. Legacy: Sortformer JSON **этого** артефакта. У `user` кнопка скрыта, POST 403 | ~1–20 с |
| **Пересобрать выжимку** | `POST /api/artifacts/{id}/rebuild-summary` | FastAPI, **admin/manager** | имена в speakers.json → `summarize_call.py` → **LLM**. Lab: proof turns с именами, затем HTML из `write_prod_view`. Промпты `call_summary.md` **и** `call_brief.md` → `call_summary.md` + `call_brief.md`. У `user` скрыто, POST 403 | ~1–5 мин |
| **Обновить и переиндексировать** | `POST /api/artifacts/{id}/reindex` | FastAPI, **admin/manager** | rebuild view → `*_final.txt` → **Ollama** index chunks. У `user` скрыто, POST 403 | ~30 с |

После POST-кнопок — `location.reload()`.

#### Legacy-режим (без registry, старые отчёты)

| Кнопка | Куда | Примечание |
|--------|------|------------|
| **Применить имена** | `localStorage` | Только клиент, **не** сервер |
| **Слить** (select) | `localStorage` | Только клиент |

#### Интерактив без API

| Элемент | Действие |
|---------|----------|
| Chip фильтр спикера | Фильтр реплик; `unknown` (бесхозные слова lab) на пилюлях нет |
| Поле поиска `#q` | Фильтр по тексту |
| Клик по времени / реплике | Seek аудио |
| `GET /artifacts/{id}/audio` | Поток WAV/M4A **этого** артефакта; query `clip` игнорируется (не общий `asr/work`) |

#### Отчёт документа — тот же `/artifacts/{id}/report` (`artifact_reports.py`)

Открывается из таблицы проекта по клику на документ / заметку / скриншот (`status=done`). Полный нормализованный текст (`*_clean.txt`) остаётся каноном индекса, чата и ТЗ. На странице человеку показывается протокол.

- **Итоговое резюме** — `{id}_doc_summary.md`, промпт `docs/prompts/doc_summary.md` (map/reduce, ~6000 символов). Карточка как у созвона (Segoe UI, `#f8fafc`, `--accent #5b7fa6`).
- Полный текст — свёрнутый блок «Полный текст», не основной экран. На странице нет `artifact_id` и внутренних терминов.
- Пока файла выжимки нет — «Итоговое резюме ещё не собрано»; открытие страницы пересобирает HTML (`doc-summary-v2`) без вызова LLM.

| Кнопка | API | Кто | На чём | Время |
|--------|-----|-----|--------|-------|
| **Пересобрать выжимку** | `POST /api/artifacts/{id}/rebuild-summary` | FastAPI → `summarize_document.run_document_summary` | только `*_clean.txt` / `transcription_text`; HTML переписывается; **без** reindex | ~1–5 мин |
| **Выжимка Word** | `GET /artifacts/{id}/download/summary.docx` | FastAPI | `{id}_doc_summary.md` | мгновенно |
| **Полный текст** | `GET /artifacts/{id}/download/transcript.txt` | FastAPI | `*_clean.txt` | мгновенно |

Падение выжимки в knowledge **не** откатывает index/entities: артефакт всё равно `done`. Тот же POST `rebuild-summary`, что у созвона: audio → call summary, иначе → document summary.

#### Экранная выжимка (screen vision) в стенограмме

Для встреч с записью экрана (`MEETING_SCREEN_RECORD=1`) в стенограмме появляются **подсвеченные блоки слов**, привязанные к кадрам экрана. В начале каждого блока слов (в момент кадра) и в конце каждой реплики — кнопка **▣**; клик открывает **модальное окно** со скриншотом кадра (jpg), подписью «на экране — {кто показывал}» и сворачиваемым блоком `<details class="sm-fold">` с читаемым markdown-описанием содержимого кадра (vision-модель через LLM), а не сырой OCR.

**Как работает:**

1. `meeting_frames.py` собирает кадры по ярусам (речь издателя / сцена 0.15 / чужая речь / пол 15 с), пишет `frames.json` + `speech_screen_links.json` на merge. Определение смен сцены — `scene_times()`: ffmpeg `select='gt(scene,0.15)',showinfo` с **`-fps_mode vfr`** (в ffmpeg 9.0 опция `-vsync vfr` удалена; со старым ключом команда молча падает → `[]` → кадры ложились «полом» 15 с мимо реальных смен картинки).
2. Если `MEETING_SCREEN_VISION=1` — `redescribe_existing_frames()` отправляет каждый кадр vision-модели (`LMSTUDIO_MODEL`) и получает читаемое описание (~2000+ символов). Результат в `video_transcript.json` (`text_source: "vision"`).
3. `pipeline_lab.py` → `write_prod_view()` → `transcript_view.py` строит HTML со словами, привязанными к таймкодам кадров.
4. Слова в пределах одного кадра группируются в блок `<span class="wgrp" data-seg="i">` с подсказкой времени (`title="01:01–01:11"`).
5. В начале каждого блока слов — кнопка `<button class="screen-dot" data-seg="i">▣</button>` (в начале реплики — `<span class="screen-badge">` в конце). Кнопка ставится **перед** словами блока — на границе момента кадра, чтобы не разрывать фразу: слово приклеивается к сегменту, если его `start` попадает в окно `[seg.start, seg.end)` (`_word_seg_index`). Если в окне кадра нет ни одного слова, кнопка ставится отдельно — перед первым словом, начавшимся не раньше момента кадра (`turn_hits` ограничивает висящие кадры сегментами, пересекающими реплику). Клик по `▣` открывает `renderScreenModal([сегмент])`; клик по бейджу реплики — все сегменты, пересекающиеся с репликой (`screenSegmentsFor`).
6. Модалка (`#screen-modal-wrap`, `renderScreenModal`/`closeScreenModal`) показывает кадр `<img class="sm-img">` + подпись «на экране — {identity}» + текст описания в сворачиваемом `<details class="sm-fold">` (по умолчанию свёрнут, стрелка `▸`/`▾`). Закрытие — крестик, клик по фону, Escape. Клик по `▣`/бейджу не перематывает аудио (guard в seek-обработчике `#turns`). Кадр отдаётся браузерным эндпоинтом `GET /artifacts/{id}/lab/{run_id}/frame/{relative}` (только `.jpg` внутри `frames/`, права как у отчёта — не admin-only); URL кладётся в сегмент как `frame_url` (`pipeline_lab._frame_url_map()` в `load_run_screen_segments`).
7. **Индексация в векторную БД:** `pipeline_lab._append_screen_context()` дописывает vision-описания в `docker_merged*.txt` перед `finalize_audio_outputs` блоками `[MM:SS - MM:SS] Экран (показывал {спикер}): {текст кадра одной строкой}` (без переносов строк — чтобы `chunk_transcript` не нарезал на фейковых спикеров по двоеточиям) и `[MM:SS - MM:SS] Речь на экране: {слова реплик, пересекающихся с кадром по времени}`. Так ИИ-чат отвечает на вопрос «что показывалось на экране» по релевантным чанкам. **Источник vision-прогона авто-выбирается:** `artifact_lab.vision_run_id()` ищет среди лабораторных прогонов самый свежий с сегментами `text_source: "vision"` (кадры на всех прогонах идентичны), иначе берёт `latest_run_id` — так в прод не попадает сырой OCR из более свежего прогона. `_append_screen_context` вызывается в `republish_named_transcript` и `publish_lab_to_prod` (покрывает и «Пересобрать выжимку»), которые затем делают `finalize_audio_outputs`.

**Файлы на диске (meeting recording):**

```
data/meetings/{room_name}/
  ├── manifest.json              # merged tracks + timeline
  ├── tracks/                    # raw WAV + screen_video mp4
  ├── events.jsonl               # meeting events (join/leave/publish)
  ├── frames.json                # screen frames by tier
  ├── speech_screen_links.json   # frame→speech alignment (merge)
  ├── video_transcript.json      # vision descriptions (MEETING_SCREEN_VISION=1)
  └── analyze.json               # meeting_analyze.py output
```

**CLI скрипт для пересборки vision-описаний:**

```powershell
python scripts/redescribe_screen_frames.py {artifact_id} --run {run_id} [--rebuild-only]
```

Пересобирает vision-описания кадров и/или HTML стенограммы (`--rebuild-only` — только пересборка HTML без повторного vision).

---

### 5.6. Match спикеров — `/artifacts/{id}/report-speaker-match` (`speaker_match_test.html`)

Страница для подписи голосов: карточки встречи + прокручиваемая **база голосов**. Таймлайна и списка сегментов нет (они в `/report`). Ссылка **match** в таблице проекта ведёт сюда и видна только после публикации меток; **Подтвердить** на проекте появляется, когда все карточки названы.

| Кнопка / ссылка | API | Кто | Примечание |
|-----------------|-----|-----|------------|
| **← Проект** / **Стенограмма** | GET | pages | соседние экраны |
| **Пересчитать подсказки** | `POST …/speaker-recalculate` | FastAPI, admin/manager | Lab: cosine `speaker_N` ↔ база, confirmed не сбрасываются. Страница match — не для `user` |
| **Запомнить** / **Обновить** | `POST …/speaker-enroll` | FastAPI | На карточке; leftover project-копия после подъёма в global удаляется |
| **Объединить** | `POST …/speaker-merge` | FastAPI | На карточке |
| **Прослушать** | — | audio seek | эталонный клип спикера |

Пул в сайдбаре: `list_profiles` — одно имя на свёрнутый ключ (`Игорь (Пилот)` = `Игорь Пилот`), prefer global. Уборка БД: `cleanup_voice_profile_pool()` / `scripts/dedupe_speaker_profiles.py` — точные имена, варианты написания, cosine ≥ 0.95, сироты project.

### 5.7. Прочие отчёты аудио (только просмотр + seek)

| URL | Файл-генератор | Содержимое |
|-----|----------------|------------|
| `/artifacts/{id}/report-sortformer` | `build_sortformer_transcript_view.py` | Sortformer timeline |
| `/artifacts/{id}/report-diar-raw` | `build_raw_diar_view.py` | Whisper/Tone/GigaAM tracks |
| `/artifacts/{id}/report-diar-two-voice` | two-voice diar | 2-speaker view |
| `/artifacts/{id}/report-lab?run={run_id}` | `build_lab_view.py` | **Только admin.** Лаборатория: снимок 01–07, оверлей Cursor, сверка GigaAM, карточки голосов; итоговая стенограмма как на проде (пилюли MM:SS, цветные имена). Менеджер/user — 403 |
| `/artifacts/{id}/report-meeting-lab` | `meeting_lab.py` + `meeting_lab.html` | **Только admin.** Проверки шкалы (`PASS`/`WARN`/`FAIL`/`NO_DATA`; «нет данных ≠ пройдено»), таблица протечек, бейджи `silent`/`screen`, ссылка `events.jsonl`, итог на шкале: время / кто / текст (`CONFLICT` подсвечен). Пока нет ленты — reload 4 с. Менеджер/user — 403 |
| `/artifacts/{id}/meeting-lab/{child}/lab/{run}/file/{путь}` | — | **Только admin.** Файлы дочернего прогона `{parent}__{identity}`. Чужой child — 404 |
| `/artifacts/{id}/lab/{run_id}/file/{путь}` | — | **Только admin.** Файлы прогона (WAV, json, npy, логи) без выхода за каталог |

Кнопка **лаб** в списке артефактов — **только admin**, и только когда у аудио есть хотя бы один
лабораторный прогон (`lab_runs`). На report-lab после `asr/lab_voice.py` —
карточки **Запомнить** / **Слить** (`POST /api/artifacts/{id}/lab/{run_id}/speaker-enroll`
и `…/speaker-merge`, тоже только admin). Они пишут в ту же `speaker_profiles`, файл —
`voice/match.json` прогона, не боевой match.

#### 5.7.1. Лаборатория расшифровки

Изолированный конвейер (`asr/lab/`) — **он же боевой** ASR. CLI ниже — отладка той же линии, что гоняет воркер (`process_lab_artifact`). UI: `/report-lab` диагностика, `/report` прод.

```powershell
venvs\lab\Scripts\python.exe asr\lab_pipeline.py --project-id 1 --artifact-id art_... --report
```

Стадии: `01_audio` копия 16 кГц без денойза → `02_asr` Parakeet-TDT v3 (окна ≤80 с) →
`03_diar` Sortformer 4spk v2.1 (ONNX, сырые вероятности) → `04_paint` покраска
слов → `05_voice` WeSpeaker в `skkkk-gigaam` → `06_transcript` реплики без LLM →
`07_verify` второй проход через Silero VAD. Провал (`fail`) — только если слов
на тишине больше 2%; слова на громком звуке, который VAD не принял за речь, дают
`warn` с участками времени.

Каждая стадия пишет `stage.json` с sha256 входов и выходов; повтор пропускается
по отпечатку (хеши входов + параметры). Отсутствие входа — падение, подстановки
файла из другого прогона нет.

Принятый снимок (`asr/lab/FREEZE.md`), эталон `20260826T174104Z_48a796`:

1. этап 1 — конвейер 01–07 без DeepFilterNet;
2. этап 2 — прогон Cursor (`asr/lab_sense.py`), только каталог `sense/`;
3. этап 3 — свой прогон GigaAM и сверка: `giga/`, `reconcile/`, `proof/`;
4. этап 4 — слепки WeSpeaker в `voice/` (`asr/lab_voice.py`); «Запомнить» /
   «Слить» на report-lab пишут в `speaker_profiles` с меткой `speaker_N`.
   В FREEZE не входит, пока нет явного «принимаем».

Нарезка и таймкоды GigaAM в итог не идут — только его мнение о спикере в спорных
местах и его вариант слова. Боевой `docker_gigaam_speakers.json` пишет
`write_prod_view` из **proof**, не из сырого GigaAM timeline.

```powershell
venv\Scripts\python.exe asr\lab_sense.py --project-id 1 --artifact-id art_... --run-id 20260826T174104Z_48a796
venv\Scripts\python.exe asr\lab_giga.py --project-id 1 --artifact-id art_... --run-id 20260826T174104Z_48a796
venv\Scripts\python.exe asr\lab_reconcile.py --project-id 1 --artifact-id art_... --run-id 20260826T174104Z_48a796
venv\Scripts\python.exe asr\lab_proof.py --project-id 1 --artifact-id art_... --run-id 20260826T174104Z_48a796
venv\Scripts\python.exe asr\lab_voice.py --project-id 1 --artifact-id art_... --run-id 20260826T174104Z_48a796
```

### 5.8. Отдельные страницы

| Страница | Кнопки | API |
|----------|--------|-----|
| `/projects/{id}/tz` | **Пересобрать ТЗ** | = `rebuild-tz` (admin) |
| `/projects/{id}/chat` | **Спросить** | `POST /chat` |
| `/projects/{id}/chat` | *(авто при открытии)* | `GET /projects/{id}/chat/history` |
| `/projects/{id}/chat` | *(авто при pending)* | `GET /projects/{id}/chat/status` + poll history |
| `/projects/{id}/graph` | — | `GET …/graph/data` (Cytoscape) |
| `/admin/conflicts` | вид **Ждут вас** / **Важное**; у конфликта поле «Причина (обязательна)» + **В силе первое** / **В силе второе**; **Вернуть решение модели** | `GET /projects/{id}/conflicts?view=…`, `POST /conflicts/{id}/resolve`, `POST /conflicts/{id}/undo-model` (admin) |
| `/m/{token}` | лобби + `#meeting-dock` / `#meeting-more`; слушает `skkkk-dfn` | `GET /m/{token}`, `POST /api/meetings/public/{token}/join` |
| `/projects/{id}/meetings/{mid}/room` | тот же док + `#meeting-end`; тот же cleaner | `POST /projects/{id}/meetings/{mid}/join` |
| `/login` | **Войти** | `POST /auth/login` (JSON token + Set-Cookie `skkkk_session`) |
| все HTML-оболочки | **Выйти** (иконка у ника в шапке) | `POST /auth/logout` — сброс cookie + localStorage |
| `/auth/logout` | *(JS `clearSession` / `#nav-logout`)* | `POST /auth/logout` — сброс cookie |

API без кнопок в UI (для будущего / интеграций):  
`GET /projects/{id}/search`, `GET …/activity`, `POST /projects/{id}/notes`, `POST /links`, `POST /entities/{id}/review`, `GET /projects/{id}/artifacts/{aid}/entities` (сущности одного артефакта), `GET /projects/{id}/tz/versions` (журнал `changelog` + файлы `versions/current_tz_*.md`), `GET /api/projects/{id}/speaker-profiles` (база голосов проекта), `POST /projects/{id}/upload` (простая загрузка multipart без докачки, `source_type`; UI использует `uploads/init`).

Служебные опросы из JS (без отдельной кнопки):  
`GET /projects/{id}/tasks/active` (блокировка «Пересобрать» на проекте и ТЗ), `GET /api/artifacts/{id}/tasks/active` (стенограмма: идёт ли задача по артефакту), `GET /api/artifacts/{id}/speakers-status` (страница проекта: можно ли «Подтвердить спикеров»), `GET /.well-known/acme-challenge/{token}` (выпуск сертификата).

### 5.9. Сводка: кто выполняет действие

| Исполнитель | Что |
|-------------|-----|
| **GPU worker** | lab ASR, knowledge (entities, index, summary, graph) после upload и после confirm |
| **`main` edge** | Caddy, LiveKit, `voice_cleaner` (`skkkk-dfn`) — только `all`/`api`, не worker |
| **FastAPI sync** | recalculate, enroll, merge, rebuild-summary, reindex, rebuild-tz, chat |
| **Docker GigaAM** | второй слух + voice embed |
| **LLM** (Cursor / Local LLM) | sense, proof, summary, entities, TZ, chat |
| **Ollama bge-m3** | index/reindex chunks, semantic search |
| **Browser only** | фильтры, seek, legacy rename localStorage |

### 5.10. Два разных «Пересобрать» — не путать

| UI | Кнопка | Scope | Output |
|----|--------|-------|--------|
| `/projects/{id}` | **Пересобрать** (выжимка проекта) | top-K по проекту (не весь) | `current_tz.md` через `rebuild_tz_fast()` — векторный поиск + один reduce-вызов, см. §5.11 |
| `/artifacts/…/report` (созвон) | **Пересобрать выжимку** | один созвон | `call_summary.md` из полной стенограммы |
| `/artifacts/…/report` (документ) | **Пересобрать выжимку** | один документ | `{id}_doc_summary.md` из `*_clean.txt`; чанки не трогает |

Есть и третий, ручной путь — полный `tz.rebuild_tz()` (map/reduce по **всем** чанкам проекта, а не top-K): больше не вызывается ни из UI, ни из авто-пересборки, только из кода/скриптов напрямую. Для проектов с большим объёмом низкоизбыточного контента (проверено: 578 чанков) **не сходится** — упирается в контекст модели (32K) уже на 2-м уровне слияния черновиков и падает с `RuntimeError`, даже после исправления связанных багов (reasoning-налог, упаковка групп слияния). Держать про запас для небольших/средних проектов, не как основной путь.

### 5.11. Ограничения выжимок (контекст LLM)

| Выжимка | Вход | Ограничение |
|---------|------|-------------|
| **Созвон** (`call_summary.md` + `call_brief.md`) | Вся стенограмма | итог: map ~6000 × N + reduce **4096**; кратко: тот же map + reduce **1024** |
| **Документ** (`{id}_doc_summary.md`) | Нормализованный `*_clean.txt` | map ~6000 × N **последовательно** + reduce **4096**; индекс по-прежнему из полного текста |
| **Проект** (`current_tz.md`, дефолт: `rebuild_tz_fast`) | top-36 чанков + top-36 `decision`/`task` сущностей по 7 обобщённым запросам (MMR-отбор на разнообразие) + заметки | **один** reduce-вызов, ~20-35 tokens/с генерации на используемой модели; ~30-35 с итого |
| **Проект — полный путь** (`rebuild_tz()`, только вручную из кода, см. §5.10) | Аудио: lab `_final.txt` (нет файла → чанки); документы: `chunks`; плюс `decision`/`task` / notes / conflicts / `_call_summary.md` | map ~6000 симв. × N **последовательно** + иерархический merge (группы ~12000 оцен. токенов) + reduce; минуты, и не гарантированно сходится для больших проектов (см. §5.10) |
| **Чат** | top‑12 чанков (каждый с шапкой «источник · дата записи · таймкод») + до 12 `decision`/`task`/`fact` по `entity_vectors` (строкой «кто сказал / кто утвердил / когда») + хвост `current_tz.md` (~2500) | только к вопросу; выжимка — обзор, не замена фрагментов |

### 5.12. BPMN: кнопки → API → сервисы → БД/файлы

Схема зависимостей UI-кнопок (страница проекта, стенogramma, match). Исполнитель: **W** = GPU worker, **S** = FastAPI sync, **B** = browser.

```mermaid
flowchart TB
  subgraph project_ui["/projects/{id} — project.html"]
    UPL[Загрузить] -->|POST …/uploads/…/complete| UP_S[S: upload + jobs]
    CONF[Подтвердить спикеров] -->|POST /api/artifacts/{id}/speakers-confirm| CF_S[S: speakers-confirm]
    TZ[Пересобрать ТЗ] -->|POST /projects/{id}/rebuild-tz| TZ_S[S: tz rebuild LLM]
    CHAT[Спросить] -->|POST /chat| CH_S[S: RAG + LLM]
    DED[Обновить дедлайны] -->|POST /projects/{id}/reextract-entities| DE_S[S: entities LLM]
  end

  subgraph report_ui["/artifacts/{id}/report — transcript.html"]
    ENR[Запомнить / Сохранить] -->|POST …/speaker-enroll| EN_S[S: enroll + write_prod_view]
    MRG[Слить] -->|POST …/speaker-merge| MG_S[S: merge_map + write_prod_view]
    REC[Пересчитать] -->|POST …/speaker-recalculate| RC_S[S: lab cosine speaker_N]
    SUM[Пересобрать выжимку] -->|POST …/rebuild-summary| RS_S[S: summarize LLM]
    RIX[Обновить и переиндексировать] -->|POST …/reindex| RI_S[S: Ollama index]
  end

  UP_S --> J[(jobs)]
  UP_S --> A[(artifacts)]
  J --> W_ASR[W: pipeline_lab + knowledge]

  CF_S --> J2[(jobs phase=knowledge)]
  CF_S --> ASL[(artifact_speaker_links)]
  CF_S --> MJ[sortformer_speaker_match.json]
  J2 --> W_NAMES[W: apply_confirmed_speaker_names]
  W_NAMES --> W_KNOW[W: entities index summary graph]

  RC_S --> SP[(speaker_profiles pool)]
  RC_S --> MJ
  RC_S --> TH[transcript.html + docker_gigaam_speakers.json]

  EN_S --> SP
  EN_S --> MJ
  EN_S --> ASL
  EN_S --> TH
  MG_S --> MJ
  MG_S --> TH

  W_ASR --> ASR_FILES[lab proof + asr/docker_gigaam_speakers.json]
  W_KNOW --> ENT[(entities links chunks)]
  W_ASR --> TH
  RS_S --> CS[call_summary.md]
  RS_S --> TH
  RI_S --> ENT
  TZ_S --> TZMD[current_tz.md]
  CH_S --> ENT

  TH --> PARSED[parsed/calls/*_final.txt]
  MJ --> MATCH_HTML[report-speaker-match]
```

**Цепочка «Пересчитать» (критично для спикеров):**

1. UI `#recalc` → `POST /api/artifacts/{id}/speaker-recalculate`
2. Если есть lab-прогон с `voice/match.json` — пересчёт cosine `speaker_N` по базе; confirmed/merge сохраняются. **Не** вызывать `match_sortformer_speakers` и не брать эталон из `experiments/`.
3. Иначе legacy: `recalculate_speaker_match()` сохраняет **prev** `sortformer_speaker_match.json` и гоняет `match_sortformer_speakers.run()` только по JSON **этого** артефакта.
4. `_finalize_match_payload()` — `_restore_merge_map` + `_restore_confirmed_matches`
5. `sync_match_json_from_links` → `artifact_speaker_links`
6. `rebuild_match_report_html` + `rebuild_transcript_report_html` (lab: `write_prod_view` из proof + match JSON; **не** `build_transcript_view.py`)

**Таблицы БД по кнопкам:**

| Кнопка | artifacts | jobs | speaker_profiles | artifact_speaker_links | entities/chunks |
|--------|-----------|------|------------------|------------------------|-----------------|
| Загрузить | insert | insert | — | — | — |
| Подтвердить спикеров | status→phase2 | insert | read | read/write confirmed | entities: `rebind_call_entities` (имена, без LLM) |
| Пересчитать | — | — | read pool | rewrite | — |
| Запомнить | — | — | insert/update | write | — |
| Запомнить (лаб) | — | — | insert/update (`speaker_N`) | write lab match JSON | — |
| Слить | — | — | — | merge_map in JSON | entities: `rebind_call_entities` (имена, без LLM) |
| GPU pipeline done | status=done | done | backfill global | from match | insert index |

**Скрипт дедупликации голосов:** `scripts/dedupe_speaker_profiles.py` → `cleanup_voice_profile_pool()` (точные имена, `Игорь (Пилот)`/`Игорь Пилот`, cosine ≥ 0.95, сироты project).

### 5.13. Прод-созвон: кнопка → API → функция (актуально 2026-08-27)

Канон стенограммы — **lab proof** (`lab/runs/{run_id}/proof/transcript.json`), метки `speaker_N`. Имена — из `reports/sortformer_speaker_match.json`. HTML/JSON для `/report` пишет **`write_prod_view`**. Скрипт `asr/build_transcript_view.py` на lab-артефакте **запрещён**: он нарезает Sortformer `SPEAKER_00` поверх proof и сбрасывает имена.

#### Загрузка и воркер

| Шаг | Где | Функция | Что делает |
|-----|-----|---------|------------|
| **Загрузить** | `upload.py` complete | создаёт `jobs.kind=process_artifact` | файл в `raw_inputs/`, статус `uploaded`; джоба в **общую** очередь (FIFO), один GPU-воркер |
| job | `workers/pipeline.py` `process_artifact` | `process_lab_artifact` | если `uploaded` / `asr_running` / `error` |
| WAV | `artifact_audio.ensure_audio_wav` | ffmpeg + lock + `.partial` (`-f wav`, иначе muxer не угадывает расширение) | опрос списка **не** извлекает WAV |
| ONNX | `pipeline_lab._run_cmd` | `asr/lab_pipeline.py --no-voice` | `--run-id` только если sha256 WAV = `run.json` |
| sense/giga/proof/voice | `pipeline_lab` | `lab_sense.py` → `lab_giga.py` → `lab_reconcile.py` → `lab_proof.py` → `lab_voice.py` | Cursor + GigaAM + WeSpeaker |
| публикация | `publish_lab_to_prod` | proof + `voice/match.json` | `write_prod_view` → `_append_screen_context` (vision-прогон авто-выбирается) → match HTML, `speakers_pending` |
| стоп | `process_artifact` | `return` | пока статус `speakers_pending` |
| knowledge retry (`transcript_ready` / индекс) | `process_artifact` else | lab: `_final.txt` / `apply_confirmed_speaker_names`; **не** `process_audio_artifact` (Whisper/T-one) | иначе старый `pipeline_checklist.json` не находится и срабатывает тройной ASR |

#### Имена спикеров

| Кнопка / событие | API | Функция | Результат |
|------------------|-----|---------|-----------|
| **Запомнить** / **Сохранить** (`/report` и `/report-speaker-match`) | `POST /api/artifacts/{id}/speaker-enroll` | `speaker_profiles` enroll + `_safe_rebuild_artifact_views` | БД `speaker_profiles` + links; lab: `apply_confirmed_speaker_names` → `write_prod_view` (имена сразу в turns/HTML/`_final.txt`). Lab **не** вызывает `voice_turn_audit` (GigaAM по всем репликам) — иначе «Запомнить» висит и блокирует следующих спикеров |
| **Слить** / **Объединить** | `POST …/speaker-merge` | merge_map + тот же rebuild | два `speaker_N` → один человек |
| **Пересчитать подсказки** (lab) | `POST …/speaker-recalculate` | cosine `voice/match.json` ↔ `speaker_profiles` | **не** `match_sortformer_speakers`, **не** эталон `experiments/`; confirmed не сбрасываются |
| **Пересчитать** (legacy, нет lab-прогона) | тот же POST | `recalculate_speaker_match` → `match_sortformer_speakers.run` | только Sortformer JSON **этого** артефакта |
| **Подтвердить спикеров** (таблица проекта) | `POST …/speakers-confirm` | `confirm_speakers_and_enqueue` | кнопка только когда все `speaker_N` named; затем `speakers_confirmed` + job `phase=knowledge` (**не** Whisper/T-one) |
| воркер после confirm | `process_artifact` | `apply_confirmed_speaker_names` → `set_status(transcript_ready)` → `run_knowledge_pipeline` | имена ещё раз в файлы, затем сущности / индекс / выжимка / граф |
| GET `/report` | `pages.py` | `ensure_audio_report` → lab: `republish_named_transcript` | пересборка HTML из proof+match, без `build_transcript_view` |

Enroll **не** требует нажимать «Подтвердить», чтобы имена появились в стенограмме. Confirm — шлагбаум на **knowledge** (сущности, индекс, протокол, граф).

На `/report` кнопка `#recalc` подписана **«Пересчитать»**; на `/report-speaker-match` — **«Пересчитать подсказки»** (lab) или **«Обновить и пересчитать»** (legacy). Оба бьют в один POST.

#### Python: кнопка → функция

| UI | Роутер | Сервис |
|----|--------|--------|
| Загрузить complete | `upload.complete_resumable_upload` | создаёт `Job(kind=process_artifact)` |
| GPU job (аудио, до confirm) | `workers.pipeline.process_artifact` | `pipeline_lab.process_lab_artifact` |
| GPU job (`speakers_pending`) | тот же | **стоп**, ждёт **Подтвердить** (кнопка только после всех имён на match) |
| GPU job (`speakers_confirmed`) | тот же | `apply_confirmed_speaker_names` → `run_knowledge_pipeline` |
| Запомнить | `speaker_profiles.api_enroll_speaker` | `enroll_speaker` → `_safe_rebuild_artifact_views` → lab: `apply_confirmed_speaker_names` |
| Слить | `api_merge_speakers` | `merge_speakers` → тот же rebuild |
| Запомнить (лаб) | `api_lab_enroll_speaker` | enroll в `voice/match.json` прогона, не прод JSON |
| Пересчитать | `api_recalculate_speakers` | lab: `_recalculate_lab_speaker_match`; иначе `recalculate_speaker_match` |
| Подтвердить | `api_speakers_confirm` | `confirm_speakers_and_enqueue` (`phase=knowledge`) |
| Пересобрать выжимку | `api_rebuild_summary` | `rebuild_artifact_call_summary` |
| Переиндексировать | `api_reindex_artifact` | `refresh_artifact_project_index` → lab-rebuild + `finalize_audio_outputs` → `index_artifact` |
| Пересобрать ТЗ | `knowledge.rebuild_tz_endpoint` | `tz.rebuild_tz` — lab `_final.txt` |
| Список дедлайнов | `knowledge.project_deadlines` | `list_project_deadlines` |
| Обновить дедлайны | `knowledge.reextract_entities_endpoint` | `reextract_project_entities` (не reindex) |
| Чат | `knowledge.chat_endpoint` | `chat.answer_chat` — чанки + `entity_vectors` + хвост `current_tz.md` |
| GET `/report` | `pages.artifact_report_page` | `artifact_audio.ensure_audio_report` → lab: `republish_named_transcript` |
| Выжимка Word | `pages.artifact_summary_docx_download` | `transcript_export.build_summary_docx_bytes` |
| Краткое резюме Word | `pages.artifact_brief_docx_download` | `transcript_export.resolve_brief_docx` |
| Стенограмма .txt | `pages.artifact_transcript_txt_download` | `resolve_transcript_txt` |

#### Выжимка созвона и HTML

| Кнопка | API | Функция | Результат |
|--------|-----|---------|-----------|
| **Пересобрать выжимку** | `POST …/rebuild-summary` | `rebuild_artifact_call_summary` | **Аудио:** lab имена → `summarize_call.py` → `call_summary.md` + `call_brief.md` + `write_prod_view`. **Документ:** `run_document_summary` → `{id}_doc_summary.md` из `*_clean.txt` + `{id}_report.html`; без reindex. Падение LLM не откатывает чанки |
| **Выжимка Word** | `GET …/download/summary.docx` | `transcript_export.build_summary_docx_bytes` | Calibri (в т.ч. кириллица), поля A4, шапка таблицы `#5B7FA6`, zebra, списки с выступом, `**`/`*`/`` ` `` |
| **Краткое резюме созвона** | `GET …/download/brief.docx` | `transcript_export.resolve_brief_docx` | тот же рендер, заголовок «Краткое резюме созвона», файл `{имя}_краткое_резюме.docx` |
| **Стенограмма .txt** | `GET …/download/transcript.txt` | `resolve_transcript_txt` | таймкоды + имена из speakers.json |
| **Обновить и переиндексировать** | `POST …/reindex` | `refresh_artifact_project_index` | lab-rebuild HTML → `write_prod_view` → `finalize_audio_outputs` (экран дописывается в `republish_named_transcript`/`publish_lab_to_prod` с vision-прогоном) → Ollama chunks |

Промпт выжимки требует **GFM-таблицу** «Кто / Что / Срок» (шапка + `| --- | --- | --- |` + строки), не «набросать пайпы». Рендер: `transcript_view._summary_to_html` → `<table class="summary-table">`.

Фильтр-пилюли на `/report`: имена из turns; метка lab **`unknown`** (бесхозное слово) на чипах **не** показывается (`_color_map` / `displayName`).

#### Ключевые файлы на диске (lab-прод)

| Файл | Кто пишет | Кто читает |
|------|-----------|------------|
| `lab/runs/{id}/proof/transcript.json` | `lab_proof.py` | `write_prod_view` / `republish_named_transcript` — канон реплик |
| `lab/runs/{id}/voice/match.json` | `lab_voice.py` | enroll/recalc lab |
| `reports/sortformer_speaker_match.json` | publish + enroll | имена, registry UI |
| `asr/docker_gigaam_speakers.json` | `write_prod_view` | `/report`, `summarize_call`, скачать .txt |
| `asr/call_summary.md` | summarize_call.py | вкладка «Итоговое резюме», Word |
| `asr/call_brief.md` | summarize_call.py | вкладка «Краткое резюме» |
| `reports/transcript.html` | `write_prod_view` + `finalize_audio_outputs` | GET `/report` |
| `parsed/calls/{id}_final.txt` | `finalize_audio_outputs` | индекс, ТЗ; на lab-артефакте содержит и экранные блоки «Экран (показывал …)» + «Речь на экране» из `video_transcript.json` |

#### Что нельзя делать на lab-артефакте

- Вызывать `build_transcript_view.py` / `match_sortformer_speakers` с чужим JSON (`experiments/…`).
- Считать WAV готовым, пока ffmpeg пишет (опрос `GET …/artifacts` не должен вызывать extract).
- Резюмировать `--run-id`, если sha256 WAV ≠ `run.json`.
- Подставлять `SPEAKER_00` вместо `speaker_N`.

---

## 6. Layout артефакта на диске

```
data/projects/{project_id}/
├── raw_inputs/{calls|documents|notes}/   # оригинал загрузки
│   # видео (.mp4/.mkv/.webm/…) удаляется после проверенного извлечения WAV
├── artifacts/{artifact_id}/
│   ├── audio/input_16k.wav               # рабочий звук (16 kHz mono)
│   ├── audio/playback.m4a              # AAC для плеера в UI
│   ├── asr/                              # рабочая зона ASR
│   │   ├── docker_whisper.json|.txt
│   │   ├── docker_tone.json|.txt
│   │   ├── docker_gigaam.txt
│   │   ├── sortformer_segments.json
│   │   ├── docker_merged_windows.json    # окна LLM-merge
│   │   ├── docker_merged_plain.txt       # plain после integrity
│   │   ├── docker_gigaam_speakers.json   # реплики для UI/summary
│   │   ├── transcript_integrity.json
│   │   ├── call_summary.md
│   │   ├── call_brief.md
│   │   └── transcript.html
│   ├── reports/                          # копии для UI
│   │   ├── transcript.html
│   │   ├── call_summary.md
│   │   ├── call_brief.md
│   │   └── sortformer_speaker_match.json
│   └── lab/                              # боевой ASR: изоляция прогонов
│       ├── latest.json                   # указатель на последний прогон
│       └── runs/{run_id}/                # run_id = 20260826T101500Z_ab12cd
│           ├── run.json                  # исходник с sha256, artifact/project
│           ├── report.html               # страница прогона
│           ├── logs/{stage}.log
│           ├── 01_audio/{stage.json,clean_16k.wav}
│           ├── 02_asr/{stage.json,words.json}
│           ├── 03_diar/{stage.json,diar.json,probs_clean.npy,heatmap.json,compare.json}
│           ├── 04_paint/{stage.json,paint.json}
│           ├── 05_voice/{stage.json,voice.json,embed_raw.json}
│           ├── 06_transcript/{stage.json,transcript.json,transcript.txt}
│           ├── 07_verify/{stage.json,verify.json}
│           ├── sense/                    # этап 2
│           ├── giga/ reconcile/ proof/   # этап 3
│           └── voice/{voice.json,match.json,embed_raw.json}  # этап 4
├── parsed/calls/
│   ├── {id}_final.txt
│   ├── {id}_transcript.html
│   ├── {id}_call_summary.md
│   └── {id}_call_brief.md
├── parsed/documents/  (и notes)
│   ├── {id}_raw.txt
│   ├── {id}_clean.txt              # канон индекса / чата / ТЗ
│   ├── {id}_doc_summary.md         # протокол для UI
│   └── {id}_report.html
└── structured/{id}_entities.json
├── meetings/{room_name}/               # встречи (не внутри projects/)
│   ├── manifest.json                   # merged tracks + timeline
│   ├── tracks/                         # raw WAV + screen_video mp4
│   ├── events.jsonl                    # meeting events (join/leave/publish)
│   ├── frames.json                     # screen frames by tier
│   ├── speech_screen_links.json        # frame→speech alignment (merge)
│   ├── video_transcript.json           # vision descriptions (MEETING_SCREEN_VISION=1)
│   └── analyze.json                    # meeting_analyze.py output
```

---

## 7. Сервисы (`app/app/services/`)

| Модуль | Ответственность |
|--------|-----------------|
| `llm.py` | Единый адаптер: lmstudio / cursor |
| `gpu_phase.py` | Load/unload Gemma, VRAM-фазы |
| `queue.py` | jobs: claim — сначала `kind=process_artifact`, затем фоновая `auto_review`; внутри вида FIFO (`created_at`, `id`); finish, requeue; `enqueue_auto_review` / `enqueue_knowledge_refresh` без дублей; очередь **глобальная**, не per-user |
| `workers/review.py` | задачи `auto_review` (scope `reextract` / `artifact` / `topics` / `relevance` / `conflicts` / `summary`, режим shadow/live): исполнители регистрируются по области; ошибка не трогает статус артефакта; неизвестный `kind` — ошибка задачи, не запуск расшифровки |
| `auto_review.py` | режим автопроверки (`AUTO_REVIEW_MODE` / настройка проекта), вердикт модели в `value_json.model_review`, «Вернуть», контрольная выборка (каждое N-е) |
| `review_requirements.py` | этап 2 автопроверки: decision/task → источник (окно ±4 реплик канона / абзац документа), дубли без LLM, пачки по `AUTO_REVIEW_BATCH` в `review_requirement.md`, пороги confirm ≥ 0.7 / reject ≥ 0.8, «кто утвердил» через `_validated_decided_by`; контракт: `verification` = human_confirmed / model_confirmed / extracted; очередь «Ждут вас» и «Вернуть» (`POST /projects/{id}/requirements/entity/{entity_id}/undo-model`); замер — `scripts/eval_auto_review.py` (копия БД) |
| `speaker_auto.py` | этап 3 автопроверки: автопринятие голоса в lab `voice/match.json` до публикации (cosine ≥ `VOICE_AUTO_ACCEPT_MIN`, отрыв ≥ `VOICE_AUTO_ACCEPT_MARGIN`, пометка `matches[label].auto`, без sync в global); шлагбаум `VOICE_GATE_MODE` strict/soft/off (по умолчанию soft — `transcript_ready` сразу, неназванные остаются `speaker_N`); подсказка имени моделью (`suggest_speaker_names.md` → `matches[label].llm_hint`, только подсказка) в конце knowledge; имя позже → `rebuild_transcript_report_html` → `rebind` + задача `refresh_knowledge` (индекс + выжимка) |
| `review_conflicts.py` | этап 4 автопроверки + CONFLICT-AUTONOMY-PLAN: `POTENTIAL_CONFLICT` без Resolution → пары не decision/task/fact закрываются правилом `not_conflict`; явная отмена в источнике нового пункта — правилом `explicit_cancel` без модели (`rule_before_model`); остальные — LLM (`review_conflict_v3.md`): модель называет **отношение** (replaces / contradicts / refines / same / different_objects / not_decision / misextracted), победителя даёт `interpret` по весу источника (`conflict_rules`, Р1). live: `*_wins` → `Resolution(status=model_resolved, resolved_by=NULL)` + проигравший выключен с `model_review.reject_kind` (superseded / not_decision / not_said); `not_conflict` → `model_dismissed`; победитель без опоры в источнике (`_source_supports`) и подтверждённая человеком сторона → unsure. Деньги/сроки и любое выключение более нового пункта в пользу более раннего (`older_wins`, решение владельца 2026-09-28) → `model_review.important` (вкладка «Важное», метка «выключено более новое»). Стороны пары — с `customer_answer` (ответ заказчика в переписке, `project_sides`). Автооткат: > 5 % «Вернуть» из ≥ 10 авторешений за 14 дней → `conflict_review_mode=shadow` в настройках проекта (`check_undo_rate`, `effective_mode`). Экран `/conflicts` (вкладки «Ждут вас» / «Важное» / «Решены моделью»), `GET /projects/{id}/conflicts?view=pending\|important\|model`, `POST /conflicts/{link_id}/undo-model` (ответ `rolled_back_to_shadow`); вердикт — `links.model_review` (rule, relation, reject_kind, important) |
| `conflict_rules.py` | правила Р1 без модели: `source_weight` (документ заказчика 3 > утверждённое решение / внутренний документ / ответ заказчика на вопрос исполнителя в переписке 2 > высказывание 1 > вопрос 0; стороны — `project_sides` → `contract._participant_sides`, `customer_answer` по `answer_to`/`asked_by` привязки), `cancel_allowed` (ответ в переписке не отменяет документ заказчика), `winner_by_weight`, `CANCEL_PATTERNS` (без «исправляем»), `is_material_topic` (деньги, сроки; «8.3» — не дата) |
| `conflict_context.py` | противоречия для LLM чата (`_supplement_context`, только по найденным материалам; в допконтексте первыми и без обрезки лимитом `CHAT_SUPPLEMENT_CHARS`) и ТЗ (`_collect_project_material`): «В силе / Раньше считалось / Основание», открытые — «Не решено: 1) … 2) …»; `model_dismissed` не выводится; сырой `value_json` не отдаётся (и в `_graph_snippet` тоже) |
| Эталон конфликтов | один постоянный `data/eval/conflicts_gold.csv` на все проекты: `scripts/eval_auto_review.py --export-gold --project N [--only-undone]` (дописать пары для разметки), `--gold` (контрольный прогон на копии БД: правила + модель; код 0 — можно live: 0 неверных победителей, ≥ 20 размеченных, «не уверена» ≤ 30 %); обязателен при смене модели, промпта или извлечения |
| `extraction_context.py` | REQUIREMENTS-PLAN этап 3: контекст извлечения — карточка проекта `project_state/project_card.md` (+ `project_card.json`: source model/human; черновик моделью `project_card.md` из выжимки проекта, выжимок созвонов, участников; правка admin главнее), оглавление созвона `parsed/calls/<aid>_outline.json` (`call_outline.md`, части по 20000 знаков), темы проекта; `build_extraction_context` вызывают `pipeline_knowledge` и `reextract_artifact_entities`. API: `GET/PUT /projects/{id}/card`, `POST …/card/rebuild` (409, если правил человек и нет `discard_human`) |
| `topics.py` | REQUIREMENTS-PLAN этап 5: таблица `topics`, `entities.topic_id`; раскладка по темам моделью (`assign_topics.md`, пачки по 60; тема — потребность или объект, не способ решения, А2); подсказка `value.topic`, совпавшая с темой, — без модели; новые пункты сбрасывают `Topic.reviewed_at`; очередь `auto_review` scope `topics`; `merge_topics` склеивает только группы с новой темой — просмотренные темы в настройке проекта `topics_merge_seen` (иначе темы сливались при каждом прогоне) |
| `review_relevance.py` | REQUIREMENTS-PLAN этапы 6–7: пункты темы по хронологии → `review_relevance.md` → current / superseded / duplicate / done / unclear в `value.relevance` (отдельно от `model_review`). Замена — позже по дате записи (А4: обе даты известны), опора в источнике, вес Р1, А6 (подтверждено человеком до появления заменяющего материала → `over_human`, «Важное»); оформляется `POTENTIAL_CONFLICT` + `apply_conflict_verdict(rule:topic_superseded)`. Дубль в live — `reject_kind=duplicate`, `merged_into`. Scope `relevance` ждёт, пока проект не разобран (`_project_busy`); «Это актуально» — `mark_current` |
| `requirements_reextract.py` | REQUIREMENTS-PLAN этап 9: `queue_project_reextract` — сброс тем, задача `auto_review` scope `reextract` на каждый готовый материал (без файлов обмена `analysis_*.json`); исполнитель: `reextract_artifact_entities` (v2, перенос ручных решений) → `seed_graph` → проверки artifact/topics/relevance/conflicts. Итоговую выжимку (`current_tz.md`) исполнитель не собирает: последнее переизвлечение проекта (`_reextract_left` = 0, в том числе упавшее) ставит за своими проверками задачу scope `summary` → `summary_for_review` → `rebuild_project_summary` (`prepare_for_llm` + `rebuild_tz_fast`). Воркер один, задачи идут по порядку постановки, поэтому выжимка собирается после конфликтов последнего материала, когда актуальность и конфликты уже выключили заменённое (раньше собиралась до них — 2026-09-28, проект 34). Ту же задачу с 2026-09-29 ставит за своими проверками каждый новый материал (`pipeline_knowledge._enqueue_auto_review`); `summary_for_review` пропускает её без вызова модели, если в очереди ждёт ещё одна выжимка проекта (`tz.summary_jobs_queued`) — одна сборка на партию. API `POST /projects/{id}/requirements/reextract` (admin, кнопка «Переизвлечь требования»), CLI `scripts/reextract_project.py --project N [--apply]` |
| `chat_export.py` | SOURCE-ANCHORS-PLAN этап 1: выгрузка Telegram Desktop (`messages.html`) разбирается `html.parser` по разметке — номер (`id="message106"`), время с поясом (`title="25.08.2026 15:58:22 UTC+03:00"`), автор (`joined` — автор предыдущего), ответ, вложения «[фото]» / «[файл: …]»; служебные пропускаются. `pipeline_documents` → `parsed/documents/<aid>_messages.json`, чистый текст «**день**» / «**ЧЧ:ММ Автор:** текст» без нормализации моделью, дата материала — `recorded_at_source=messages` (первое сообщение; период — `material_period_end`). Я1: `owned_elsewhere` — сообщения более ранней выгрузки той же переписки (по названию группы) не извлекаются. CLI `scripts/chat_exports.py --project N [--apply]` (копия прежнего текста `_clean.before_chat.txt`, переиндексация). Копия чата Битрикс24 (`.txt`: «[Имя Фамилия]» — начало сообщения, «{Цитата} Автор … {Конец цитаты}» — ответ): `looks_like_bitrix_copy` / `parse_bitrix_copy` / `ingest_bitrix_copy` — номера по порядку, `reply_to` по автору и началу цитаты, времени нет (`at` — полночь дня материала, `at_precision="day"`: время нигде не показывается и не сравнивается), текст для индекса без повтора цитат (`render_bitrix_text`) |
| Дата материала (автоматически) | `recorded_at.resolve_recorded_at` при загрузке: встреча (минута) → имя файла с временем `filename_time` (минута) → имя файла `filename` → метаданные записи `media` (аудио) → свойства документа `doc_meta` (docx/xlsx/pptx core.xml modified, PDF ModDate, EXIF снимка) → дата загрузки `upload` (решение владельца 2026-09-28: `material_date` отдаёт её как дату материала, правила «что позже» по ней работают; интерфейс подписывает «по дате загрузки», контракт — `date_basis=upload`). Имя файла: «22.09.26 14-00-34» (Телемост) — с временем, «14 мая» — дата словом. После разбора документа `refine_from_text`: дата из шапки («Дата: 14.08.2026», «Протокол от 19 августа 2026») → `doc_text` (главнее `doc_meta` и `upload`; ручную и из имени не трогает). Переписка — `messages`, ручная — `manual` / `manual_time`. Дозаполнить старые — `scripts/material_dates.py --project N|--all --apply` (плюс встреча площадки `встреча-N.wav` → начало встречи N, выгрузка переписки → первое сообщение, иначе дата загрузки). Карточка проекта-черновик сама пересобирается, когда выжимка новее (`extraction_context._tz_newer_than_card`); отпечаток кэша контракта учитывает темы, вердикты актуальности и даты материалов |
| `source_links.py` + `static/report-jump.js` | SOURCE-ANCHORS-PLAN этап 4: `source_url` — созвон `/artifacts/<aid>/report?t=754`, переписка `?m=106&q=…`, документ `?q=<80 знаков>`; `pages._serve_report` подмешивает `report-jump.js` (реплика из `data.turns` подсвечивается и прокручивается, плеер перематывается без звука + «▶ Воспроизвести с …», Я4; сообщение по якорю `#m106` — отчёт переписки рисует сообщения `chat-msg`; цитата — поиск без учёта регистра и пробелов, `<mark>`). Ссылки: источники в очереди требований и «Вопросах к заказчику» (`url`), стороны конфликта (`conflict_rows`), источники ответа чата |
| Разбор проекта моделью целиком | `scripts/review_project.py --project N\|--all [--mode shadow\|live] [--reextract] [--skip N …] [--summary-only]` (очередь воркера должна быть пуста): даты материалов (`material_dates.fill_dates`) → с `--reextract` переизвлечение v2 с контекстом проекта без очереди (темы собираются заново, ручные решения переносятся, `seed_graph`; итоговая выжимка `requirements_reextract.rebuild_project_summary` — в самом конце, после конфликтов; воркер в очереди так же собирает её задачей scope `summary` после проверок последнего материала) → `entities.rebind_chat_entities` (пункты переписки → сообщения, без модели) → `review_requirements` (источник переписки — `_chat_source`, документа — абзац от 25 % совпадения; «источник не найден» не вердикт и проверяется заново) → `topics.assign_topics` + `merge_topics` (`merge_topics.md`: склейка тем одной потребности, разведённых по способу решения) → `review_relevance.review_topics` (промпт `review_relevance_v2.md`: к пунктам темы — до 12 похожих пунктов из других тем, косинус ≥ 0.55, как кандидаты в «заменивший» / «основной»; тема больше 40 пунктов — порциями по времени (`_batches`), соседи берутся и из других порций той же темы, каждый пункт оценивается один раз; тема из одного пункта тоже проверяется; в live сначала `promote_shadow_relevance` — теневые дубли и замены без повторного вызова модели) → `review_conflicts` → сводка «что осталось человеку» и в тени — прогноз для live (`if_live`). Экран конфликтов «Ждут вас» (`conflict_rows`, view `pending`) не показывает пары, где одна сторона уже выключена (дубль, устарело, отклонено) |
| Эталоны требований | `data/eval/extraction_gold.csv` (`eval_auto_review.py --export-extraction` / `--extraction-gold`: понятно ≥ 90 %, не требование ≤ 5 %, кто и когда ≥ 95 %), `data/eval/relevance_gold.csv` (`--export-relevance` / `--relevance-gold`: 0 ошибочно снятых на ≥ 30) |
| Автопроверка: накопленное | кнопка admin «Проверить моделью всё накопленное» (карточка «Подтверждение требований») → `POST /projects/{id}/auto-review/backlog` → `auto_review.queue_backlog`; CLI `scripts/auto_review_backlog.py --project N [--mode live] [--dry-list]`; ставит только если `queue.running_worker_kinds` видит `auto_review` в `worker.started kinds=`; live применяет теневые вердикты без модели (`promote_shadow_verdicts`, `_promote_shadow`) |
| `pipeline_lab.py` | Боевой ASR-оркестратор: `process_lab_artifact`, `publish_lab_to_prod`, `write_prod_view`, `_append_screen_context`, `republish_named_transcript`, `apply_confirmed_speaker_names`; экран автоматически берётся из vision-прогона (`artifact_lab.vision_run_id`); повтор джобы пропускает готовые оверлеи (sense/giga/reconcile), `lab_proof` — нет `proof/transcript.json` |
| `pipeline_audio.py` | Legacy ASR; `run_call_summary` (выжимка созвона) |
| `pipeline_voice_gate.py` | Legacy voice-gate до triple ASR (воркер lab-путь не вызывает) |
| `pipeline_knowledge.py` | entities → index → call_summary / doc_summary → graph → done → очередь `auto_review`: artifact → topics → relevance → conflicts → `summary` (выжимка проекта за проверками, одна на партию); без автопроверки — `maybe_auto_rebuild_tz` сразу |
| `pipeline_documents.py` | PDF (текст + таблицы `find_tables` на своих местах)/DOCX/XLSX/PPTX/OCR pipeline; нормализация, не резюме. Переписка (выгрузка Telegram, копия чата Битрикс24 в `.txt`) — сообщения дословно, без нормализации моделью и без даты «из шапки» |
| `table_md.py` | общий формат таблиц: `Table`, `clean_cell`, `make_table`, `render_md` (подпись `Таблица N: …` + GFM), `split_blocks` (текст → проза/таблицы с путём заголовков), `render_record` |
| `docx_extract.py` | DOCX в порядке документа: заголовки (стиль или «3.2. Текст»), списки, таблицы на своих местах (merge, вложенные), надписи, колонтитулы; .doc/.odt/.rtf через LibreOffice |
| `xlsx_extract.py` | Excel/CSV → `## Лист: имя` + таблицы по блокам листа: объединения, двухуровневая шапка, форматы чисел/дат, скрытые листы пропускаются, лимит 5000 строк / 60 столбцов |
| `office_convert.py` | `find_soffice` / `convert_with_soffice` — LibreOffice headless для .doc/.xls/.ods/.ppt |
| `html_extract.py` | HTML → текст: блоки разметки дают переносы строк, `<table>` → сетка (`colspan`/`rowspan`), линейка выгрузки Google Sheets (A, B, C… и номера строк) убирается, дальше `xlsx_extract.grid_markdown`; вёрсточные таблицы остаются текстом |
| `pptx_extract.py` | .pptx (и старый .ppt — через LibreOffice headless, `SOFFICE_PATH`) → markdown по слайдам (`## Слайд N: заголовок`): текст, таблицы, диаграммы, SmartArt, заметки докладчика, OCR/vision для слайдов-картинок |
| `summarize_document.py` | Map/reduce `{id}_doc_summary.md` из `*_clean.txt`; fail-open |
| `artifact_reports.py` | HTML отчёта документа: протокол сверху, полный текст в `<details>` |
| `pipeline_resume.py` | Resume ASR без повторного merge |
| `pipeline_progress.py` | UI progress: lab checklist + knowledge |
| `voice_gate_checklist.json` | в `asr/` артефакта — шаги gate до confirm спикеров |
| `transcript_export.py` | export transcript.txt / document `*_текст.txt` + summary.docx |
| `artifact_audio.py` | finalize, sync reports, rebuild HTML; атомарный extract `input_16k.wav` + lock |
| `artifact_lab.py` | Лаборатория: список прогонов, выбор прогона, отдача файлов; fallback запрещён; `vision_run_id()` — автовыбор прогона с vision-описаниями экрана |
| `media_cleanup.py` | удаление видео-оригинала после проверки `input_16k.wav` |
| `speaker_profiles.py` | enroll/merge/recalculate; lab enroll в `voice/match.json`; **global pool**; `speaker_action_flags` для кнопок match/confirm в таблице; `refresh_artifact_project_index` — lab-rebuild HTML + `finalize_audio_outputs` + `index_artifact` (экран дописывается на уровне `republish_named_transcript`) |
| `entities.py` | LLM extract + persist; созвон подаётся пронумерованными репликами `T<n>` (`_numbered_turn_parts`), `turns`/`decided_by` от LLM проверяются кодом. Комната: привязка к `source_span_id` (`speaker_source=track`), без источника → `needs_review`. Загруженный созвон: speaker/время по репликам `_final.txt` (`speaker_source=voice`), без совпадения → `source_status=unbound`. `rebind_call_entities` — имена после переименования спикеров, на месте. Leak-текст не пишется в БД и `*_entities.json`. Промпт `extract_entities_v2.md` (REQUIREMENTS-PLAN этап 4): контекст (карточка, оглавление, темы) + хвост предыдущей части (5 реплик); виды пунктов `kind` (decision: requirement/project, task: work/org), `object`, `topic`, `open_questions` → сущность `open_question`; пометка `unclear_wording`; при переизвлечении ручные решения (подтверждение, статус, отказ, проигрыш в конфликте человека) переносятся на новые пункты по основам слов и времени, отчёт — `project_state/reextract_human_transfer.json` (А10). Промпт `extract_entities_v3.md` (SOURCE-ANCHORS-PLAN этап 2): переписка — сообщения «M<n> [дата время] Автор (в ответ на M<n>): текст» (`_numbered_message_parts`, до 15 сообщений ранней выгрузки — контекст), `bind_chat_source`: `message_id`, `message_at`, `speaker`, `speaker_source=chat`; «утвердил» — другой участник ответом или в пределах 10 сообщений и 24 ч (Я2). `_chat_anchor`: лучшее совпадение — вопрос, а модель назвала ответ (в `messages` или `decided_by`) → источник — ответ; у ответа на вопрос другого участника — `answer_to` / `asked_by` (для Р1); без времени — `message_at_precision=day` |
| `indexing.py` | chunk → Ollama embed → chunks (+ `start_sec`/`end_sec`) + chunk_vectors; аудио из lab `_final.txt`; после записи — `rebind_call_entities`; `audit_project_index` (выравнивание по строкам окна, `timed_chunks`) |
| `vector_index.py` | sqlite-vec CRUD чанков (vec0 kNN) и кэш сущностей (`entity_vectors` BLOB по id); `delete_vectors_for_chunks` чистит и `chunk_fts`; `chunk_scores` — близость к заданным чанкам |
| `chunk_fts.py` | FTS5 `chunk_fts` (rowid = chunks.id, разряды чисел склеены): `init_chunk_fts` (создание + дозаполнение), `upsert_chunk_fts`, `search_chunk_fts` (основы слов + числа, bm25) |
| `retrieval.py` | rerank чанков по `priority_weight`; `fuse_chunk_hits` — вектор + FTS5 (RRF, первые 5 находок по словам проходят порог близости, первые 3 по смыслу — всегда в выдаче) для чата, `/search` и MCP; `search_entities` (cosine по `entity_vectors`, типы `decision`/`task`/`fact`); `chunk_header` (шапка фрагмента: источник · дата записи · таймкод) и `format_entity_line` (кто сказал / кто утвердил / когда) для чата и ТЗ |
| `graph.py` | seed/rebuild links, conflicts; `delete_entities_with_deps`; `purge_orphan_links` при старте; payload графа без рёбер к несуществующим узлам; читает `entity_vectors`; `POTENTIAL_CONFLICT` только для `decision`/`task`/`fact` (`CONFLICT_ENTITY_TYPES`); провенанс (`PROVENANCE_KEYS`) не входит в `entity_text`; связь без направления (A↔B — одна строка); пересборка сохраняет связи с решением человека; `_try_auto_resolve` — только конфликты своего артефакта, отмена в источнике нового пункта, новее — по дате материала; `repair_conflict_links` (обратные дубли, старые авторазрешения) — CLI `scripts/fix_conflict_links.py` (по умолчанию отчёт) |
| `tz.py` | rebuild TZ: `rebuild_tz_fast` (дефолт: top-K+MMR+квота «≥1 фрагмент на артефакт», текст строит `build_tz_fast_body` без записи, лог `tz.rebuild_fast.coverage`); полный `rebuild_tz` — с pre-flight (`TzTooLargeError` при >30 черновиков); блок «предыдущая версия» убран (детерминизм); автопересборка документов ждёт конца партии (`_other_pending_jobs`) |
| `prompts.py` | единая загрузка промптов из `docs/prompts/*.md` (`load_prompt_block`, ограждённые блоки; тест по всем файлам) |
| `build_info.py` | признак «код на диске новее API/воркера» → `/health/ready.build` |
| `chat.py` | RAG: чанки (гибридный поиск `fuse_chunk_hits`) + `search_entities` + хвост `current_tz.md`; блок документов — дата материала, начало файла + фрагменты этого документа по словам вопроса (фрагменты поиска, уже вошедшие в блок документа, не повторяются); допконтекст — сначала «Изменения решений / Нерешённые противоречия» без обрезки, затем карточка, пункты найденных материалов (ближайшие к вопросу, `CHAT_MATERIAL_ENTITIES`), заметки, выжимки созвонов; промпт `rag_chat.md`: более позднее уточнение заказчика главнее раннего документа, обе версии с датами; лента и память — `chat_messages` этого логина |
| `user_activity.py` | Журнал действий логина (`user_activity_log`) для аудита; чат в промпт берёт `chat_messages`, не этот журнал |
| `models/chat_message.py` | Per-user chat UI log (`project_id` + `user_id`) |
| `models/user_activity_log.py` | Append-only журнал: auth, upload, chat, tz, speakers, knowledge |
| `chunking.py` | документы — `chunk_document`: проза абзацами с разделом, таблица — фрагментами целых строк с разделом, подписью и шапкой (широкая > 8 столбцов — записями «Столбец: значение»); стенограмма → окна подряд идущих реплик (строки `_final.txt` дословно, перекрытие 1 реплика, экран отдельно), `start_sec`/`end_sec` окна; `speaker` только у окна одного говорящего |
| `storage.py` | paths: artifact_root, parsed, structured |
| `project_purge.py` | полное удаление проекта, артефакта или **созвона**: БД + файлы; `purge_meeting` снимает jobs, артефакт встречи, `meeting_participants` и `data/meetings/{room_name}/` (идущую — 409; каталог — с повторами 15/45/120 с: рекордер держит файлы после закрытия комнаты); `purge_project` закрывает комнаты идущих встреч проекта (`close_livekit_room`), каталоги записей удаляет с теми же повторами, занятый каталог проекта — предупреждение, а не 500 после записи в БД |
| `meetings.py` / `meeting_tokens.py` | статусы `created`/`live`/`ended`; heartbeat (гость и сотрудник) и stale TTL; best-effort DeleteRoom LiveKit — по внутреннему `livekit_rtc_internal` (`:7880`), не по публичному `livekit_url` (клиент livekit-api отбрасывает путь `/livekit`); успех — только `code=not_found`, голый 404 — ошибка в логе; публичный токен sha256; JWT LiveKit |
| `meeting_recordings.py` | сырые WAV + `screen_video` mp4 + `manifest.json` (merge, чтобы cleaner и rec не затирали друг друга) + `events.jsonl`; gap-pad; `MeetingEventLog`; `has_speech` / `window_rms`; `live_room_names` — комнаты идущих встреч для рекордеров (база только на чтение) |
| `meeting_analyze.py` | кнопка **Проанализировать**: полный `meeting.json` (epoch, frames, silent, screen); длительность = max(offset+duration) |
| `meeting_frames.py` | C2: кадры по ярусам (речь издателя / сцена 0.15 / чужая речь / пол 15 с), `frames.json` + `speech_screen_links.json` на merge, vision за `MEETING_SCREEN_VISION`; `scene_times()` — смены сцены через ffmpeg `select='gt(scene,0.15)',showinfo` + `-fps_mode vfr` (не `-vsync vfr`, удалён в ffmpeg 9.0); `_dotenv_value()` — fallback на `.env`, если ключа нет в `os.environ` (vision-флаг виден воркеру); `redescribe_existing_frames()` — переописание кадров vision-моделью (пропускает уже `text_source="vision"`); `_write_video_transcript()` — запись `video_transcript.json` |
| `meeting_checks.py` | проверки шкалы: track_span/frames/gaps/leaks/giga_share/head_loss/speaker_sync/clock_integrity |
| `pipeline_meeting.py` | дорожка → Parakeet/GigaAM/proof → `refine_leaks` с RMS окон дорожек (rank → RMS → CONFLICT) → одна стенограмма |
| `meeting_lab.py` | снимок лаб встречи: проверки шкалы, протечки, silent/screen, events.jsonl, итог на шкале |
| `static/meeting_layout.js` | чистые функции комнаты: `flagState`, `soundPlan`, `tileOrder`, `qualityBadge`; selfcheck `meeting_layout.selfcheck.js` |
| `meeting_merge.py` | сдвиг слов, `word_text` (пустой `final` не оживает), реплики по общей шкале (смена спикера + пауза), `find_leaks`/`leaks.json`, `source_span_id` |
| `meeting_overlap.py` | same-room (rank→RMS→окно diar / CONFLICT), one-mic (`unknown`, не carry-forward) |
| `lab_errors.py` | вычищает ANSI/ONNX warning из ошибки в таблице файлов |
| `edge_runtime.py` | `main all`/`api`: сертификат → Caddy → LiveKit → `voice_cleaner` → `screen_recorder`; LiveKit fail не валит портал; `voice_venv_ready` проверяет шумодав так же, как грузит его `voice_cleaner` (подмена `torchaudio.backend` для torchaudio 2.9+; голый импорт давал ложное «чистильщик не стартует») |
| `tls_cert.py` | при старте: файл `:777` есть и живой (не staging, не истекает раньше `SKKKK_TLS_RENEW_HOURS`); иначе перевыпуск Caddy `:8443` |
| `livekit_env.py` | `livekit.yaml` + ключи; ICE LAN + `127.0.0.1`; TURN = `SKKKK_PUBLIC_HOST` |
| `acme_challenge.py` | HTTP-01 для Let's Encrypt на IP |
| `project_access.py` | `user_project_ids` (создал / загрузил / чат / `project_members`); `can_access_project` / `require_project` / `require_artifact_project`: admin — все; иначе свои; чужой/неактивный → 404. `project_role` / `can_grant_access` / `require_project_staff`. `require_admin` — лаб |
| `project_members.py` | выдача доступа: роли `manager`/`user`, без `admin`; кандидаты из `Login_up.txt`; upsert по login, revoke |
| `status.py` | artifact status transitions |
| `auth.py` | JWT, role passwords |
| `user_identity.py` | Логин по имени; склейка дублей; Telegram ID только хранится (без него — `placeholder_telegram_id`: `pending-<логин>`) |
| `login_allowlist.py` | Допуск: `Login_up.txt`, без учёта регистра, файл читается на каждый вход |
| `personal_password.py` | личный пароль логина: Argon2id, правила (10–128, не логин и не пароль роли), `set_initial` / `change` / `reset`, `current_version` (кеш 5 с) для проверки сессии в `dependencies._check_personal_password` |
| `personal_password_console.py` | список логинов и сброс личного пароля из консоли: пункт 7 меню запуска и `scripts/personal_password.py` |
| `companies.py` | `Companies.txt` → таблица `companies` (при старте и при открытии анкеты); сторона `executor`/`customer` пользователю не показывается; убранная из файла — `active=False` |
| `user_profile.py` | карточка: правила полей, `complete_onboarding` (всё разом + уведомление о голосе = согласие, `CONSENT_TEXT_VERSION`), `update_own`, `replace_avatar`; админ: `set_company`, `withdraw_voice_consent` (удаляет слепки), `restore_voice_consent`, `reset_profile`; `merge_profiles` при склейке дублей |
| `avatars.py` | фото: тип по содержимому, ≤ 10 МБ, ≤ 40 Мп, поворот по EXIF, **без метаданных**, центральный квадрат 512/96 WebP в `data/people/<user_id>/` |
| `user_profile_console.py` | карточки из консоли: пункт 8 меню запуска и `scripts/user_profile.py` (компания, согласие на голос, сброс) |
| `voice_link.py` | задел: `speaker_profiles.user_id` — привязка слепка к человеку только при согласии; предложения по точному совпадению имени |
| `upload.py` / `resumable_upload.py` | file registration |
| `normalize.py` / `image_extract.py` / `tesseract_tools.py` | document text |
| `ffmpeg_tools.py` | audio prep |
| `ollama_client.py` | bge-m3 embeddings |
| `ollama_runtime.py` | автозапуск Ollama перед embed/index |
| `notify.py` | Telegram stub |
| `plain_text.py` | clean LLM outputs |

### Обмен с внешним агентом и контракт (сентябрь 2026)

Внешний агент — недоверенный источник, всё присланное — данные. Решения шлюза детерминированы, без LLM. См. `docs/PLAN-AGENT-EXCHANGE-V2.md`, `docs/agent-exchange/`.

| Файл | Назначение |
|------|------------|
| `agent_gateway.py` | единая точка входа: белый список, журнал `agent_exchange_log`, идемпотентность по `client_request_id` |
| `agent_inbox.py` | ящик вопросов агента: очередь, найденные фрагменты, черновик локальной модели, ответ человека |
| `agent_letters.py` | письма агенту в `docs/agent-exchange/letters/`; читаются через MCP `contract/letters/` |
| `agent_verdicts.py` | вердикты агента: список, подтверждение / отклонение человеком (только admin) |
| `agent_settings.py` | настройки проекта для обмена (`Project.settings_json → agent_exchange`): стороны участников, пояс, пакет якорей `data/anchor_packs/*.json`, вкл/выкл обмена |
| `agent_guide.py` | инструкции для клиента: что и как спрашивать, отдаются в `initialize` MCP |
| `contract.py`, `contract_text.py`, `project_mcp_contract.py` | контракт обмена: виртуальные файлы `contract/*.json`, разбор переписки, дословные цитаты, оценки часов, якоря кода |
| `requirement_review.py` | подтверждение требований (сущности decision/task): очередь по темам (вкладки «Ждут вас» / «Устарело» / «Склеено» / …; устаревшее и дубли прячутся из «Ждут вас» уже в тени — А3), у пункта: кто сказал и кто утвердил, где и когда по каждому источнику (`sources`, А9), ход обсуждения (`context`, до 5 реплик), вид пункта, «что заменило»; подтвердить (устаревшее — только с `force`, иначе 409, А6) / отклонить / сменить статус / «Это актуально» (`POST …/requirements/entity/{id}/mark-current`) |
| `project_mcp_data.py`, `project_mcp_tokens.py` | данные и ключи проектного MCP (`can_submit`); 16 инструментов |
| `project_deadlines.py`, `project_tasks.py` | дедлайны проекта (точный срок `deadline_at` от модели — приоритет над разбором текста, `resolve_due`; исполнитель-метка `speaker_N` → имя после переименования спикера через `assignee_start_sec`, промпт `extract_entities_v4.md`); фоновые задачи API (зависшие помечаются при старте) |

### Индекс проекта (`services/project_index/`, сентябрь 2026)

Поиск для людей и ИИ-агентов по коду, шаблонам, JS, документам, решениям, связям и истории. План и статус — `docs/plans/PROJECT-INDEX-PLAN.md`, эксплуатация — `docs/OPERATIONS.md` («Индекс проекта»). Хранилище `data/project_map.db` (SQLite + sqlite-vec + FTS5), эмбеддинги — Ollama `bge-m3` на CPU, GPU не занимается.

| Файл | Назначение |
|------|------------|
| `config.py` | какие файлы индексируем (`scripts/index_config.yaml`), исключения, версия схемы и нарезчика, маскирование секретов |
| `store.py` | схема БД, версии, кэш эмбеддингов, блокировка индексатора |
| `chunkers.py` | нарезка: функции и методы Python (`ast`), JS, шаблоны (элементы с `id`, inline-скрипты), CSS, Markdown (разделы, строки находок), манифест, конфиги; символы, вызовы, импорты |
| `analyzers.py` | производные знания: роуты, карта БД (модели, миграции, кто пишет/читает), окружение, связка элемент→JS→API, покрытие тестами, инварианты, тексты интерфейса, история git |
| `indexer.py` | инкрементальная индексация, векторы добираются отдельно (обрыв безопасен) |
| `search.py` | гибридный поиск (FTS5 + вектор + точный символ + намерения «кто вызывает», роут, таблица), режимы `--callers`, `--route` и др., единый вывод для веба и MCP |

Скрипты: `index_project_map.py`, `search_project_map.py`, `eval_index.py`, `index_doctor.py`, `check_map_drift.py`, `install_hooks.ps1`, `dev_changelog.py`; MCP-сервер `tools/projindex_mcp.py` (`.mcp.json`); словарь синонимов `scripts/index_glossary.yaml`.

### Прочие сервисы, появившиеся после 2026-08-27

| Файл | Назначение |
|------|------------|
| `single_instance.py` | защита от второго GPU-воркера (SQLite допускает одного писателя) |
| `docker_runtime.py` | автозапуск Docker Desktop и ASR-контейнеров перед аудио |
| `artifact_download.py` | доступность и подпись скачивания артефакта |
| `report_embed.py` | встраивание отчётов |
| `rate_limit.py` | ограничение попыток входа |
| `prompts.py`, `build_info.py` | реестр промптов; сведения о сборке |
| `meeting_frames.py` | кадры записи экрана и их описание |

### Модели (`app/app/models/`)

`user`, `project`, `project_member`, `artifact`, `job`, `meeting`, `meeting_participant`, `entity` (+ chunks, links, speaker_profile, `chat_message`, `chat_guidance`, `user_activity_log`, `project_task`, `project_mcp_token`, …), обмен с агентом: `agent_exchange` (журнал), `agent_question`, `contract`

### Точка входа (`app/app/`)

| Файл | Роль |
|------|------|
| `main.py` | `all` / `api` / `worker` / `check`; `_ensure_edge` только `all`/`api` |
| `preflight.py` | проверки до serve; в `all`/`api` — `meeting_voice` (WARN без `venvs/voice`); `personal_passwords` — argon2 и `credentials.db` при `PERSONAL_PASSWORD≠off` |
| `credentials_db.py` | отдельная база личных паролей `data/credentials.db`: свой движок и `CredentialsBase`, таблица `personal_passwords` (ключ — логин без регистра, хеш, `pwd_version`, `reset_pending`); в тестах `PATH_OVERRIDE` |
| `startup_llm.py` | выбор LLM при старте (`can_prompt` в PyCharm / консоли Windows); в PyCharm меню также даёт служебные пункты: **7** — сбросить личный пароль пользователя (список, номер или логин, «да»); **8** — карточки пользователей (сменить компанию, отозвать/вернуть согласие на голос, сбросить карточку); **9** — перевыпустить пароли ролей (с подтверждением, новые печатаются в консоль), после пункта меню показывается снова |

### Workers (`app/app/workers/`)

| Файл | Роль |
|------|------|
| `gpu_worker.py` | Loop: claim job → processor |
| `processor.py` | dry_run stubs; real routing в pipeline_* |

---

## 8. ASR-модули (`asr/`)

### 8.1. Оркестрация

| Скрипт | Роль |
|--------|------|
| `pipeline_lab.py` (сервис `app/…`) | Оркестратор: lab CLI + Cursor overlay + публикация в `asr/` / `reports/` |
| `lab_pipeline.py` | CLI ONNX стадий 01–07 (`venvs\lab`) |
| `run_pipeline.py` | Legacy: prepare → 3 engines → sortformer → merge (воркер не вызывает) |
| `run_all.py` | Legacy/local all-in-one |
| `prepare_audio.py` | FFmpeg → input_16k.wav |
| `pipeline_checklist.json` | Статусы шагов для resume/progress |

### 8.2. ASR движки

| Скрипт | Docker | Выход |
|--------|--------|-------|
| `run_whisper.py` | skkkk-whisper | docker_whisper.json |
| `run_tone.py` | skkkk-tone | docker_tone.json |
| `run_gigaam.py` | skkkk-gigaam | docker_gigaam.txt |
| `client.py` | HTTP к контейнерам | — |

### 8.3. Merge + качество

| Модуль | Роль |
|--------|------|
| `merge_ensemble.py` | Legacy: окна тишины → 3 ASR → LLM merge (не прод) |
| `merge_guard.py` | Audit окна: recall, anti-Whisper-loop, Cursor vs Gemma пороги |
| `transcript_integrity.py` | Global audit/repair plain, view audit, seams |
| `hallucination_guard.py` | Петли, density, scrub текста |
| `llm_client.py` | LLM для merge/summary из subprocess |
| `silence_energy.py` | Границы окон |

### 8.4. View + спикеры

| Модуль | Роль |
|--------|------|
| `build_transcript_view.py` | **Legacy UI**: align timing + match labels → speakers.json, HTML. Lab-прод **не** вызывает — иначе `SPEAKER_00` поверх proof |
| `timing_align.py` | Whisper/Tone/GigaAM clocks; Gemma text + Whisper word alignment |
| `clock_guard.py` | Выбор clock source |
| `match_sortformer_speakers.py` | WeSpeaker embed (GigaAM docker) → match JSON |
| `voice_turn_audit.py` | Проверка голос/текст, auto-fix |
| `docker_stage.py` | Копия WAV из `SKKKK_ASR_WORK` → `asr/work` перед T-one/GigaAM embed (контейнеры не видят per-artifact каталог) |
| `transcript_view.py` | HTML renderer + JS (recalc, summary, reindex); grouped words by segment (`wgrp`) с кнопкой `▣` (`screen-dot`) и бейджем реплики; модалка кадра (`#screen-modal-wrap`, `renderScreenModal`) со скриншотом `<img class="sm-img">` и сворачиваемым `<details class="sm-fold">` для текста; `fmtScreenText()` markdown→HTML |
| `build_speaker_match_view.py` | HTML страница match |
| `speaker_registry.py` | enroll/merge в UI |
| `summarize_call.py` | Map/reduce: `call_summary.md` (протокол) + `call_brief.md` (кратко) из `docker_gigaam_speakers.json`. Lab: HTML через `write_prod_view` |

### 8.5. Лаборатория (`asr/lab/`) — боевой конвейер

Канон расшифровки. Изоляция прогона: `LabRun.resolve()` не выпускает путь наружу.

| Модуль | Роль |
|--------|------|
| `lab/paths.py` | Каталоги прогона; `resolve()` не выпускает путь наружу |
| `lab/run.py` | Контракт стадии: sha256, `stage.json`, отпечаток, `MissingInput` |
| `lab/pipeline.py` | Порядок стадий, `LabOptions`, `--only` / `--from` |
| `lab/stages/s01…s07` | Стадии: копия 16 кГц, Parakeet, Sortformer, покраска, реплики, VAD |
| `lab_sense.py` / `lab_giga.py` / `lab_proof.py` / `lab_voice.py` | Оверлеи Cursor + второй слух + WeSpeaker |
| `lab_pipeline.py` | CLI ONNX |
| `build_lab_view.py` | Отладка `/report-lab` |

```
lab_pipeline
  → 01_audio  (копия исходника 16 кГц, без денойза)
  → 02_asr    (onnx-asr nemo-parakeet-tdt-0.6b-v3, окна ≤80 с, весь файл)
  → 03_diar   (sortformer.py, probs_clean.npy + свои пороги)
  → 04_paint  (слово × вероятности → assigned / contested / unassigned)
  → 05_voice  (docker_stage → skkkk-gigaam embed_voice.py, БД только на чтение)
  → 06_transcript (группировка, без LLM)
  → 07_verify (Parakeet + Silero VAD: слова в тишине — fail > 2%, громкий звук без речи — warn; расхождение проходов)
lab_sense (этап 2, Cursor) → sense/  # не стадия 01–07
lab_hint (имена с боя) → hint/      # не основа, не перенарезает
lab_giga (этап 3a, skkkk-gigaam) → giga/
  → lab_reconcile (этап 3b) → reconcile/   # спикеры: подтверждено / конфликт / открыт
  → lab_proof (этап 3в, Cursor) → proof/   # текст: выбор из двух услышанных вариантов
lab_voice (этап 4, WeSpeaker) → voice/     # не стадия 05; БД только по кнопке report-lab
  → build_lab_view
publish / Запомнить / выжимка
  → write_prod_view (proof + match names → docker_gigaam_speakers.json + transcript.html)
```

### 8.6. Граф зависимостей ASR (упрощённо)

**Прод (воркер):** см. ascii в §8.5 — `lab_pipeline` → sense/giga/proof/voice → `write_prod_view`. Не вызывать `run_pipeline` / `merge_ensemble` / `build_transcript_view`.

**Legacy** (на диске, воркер не вызывает после cutover):

```
run_pipeline
  → prepare_audio
  → run_whisper | run_tone | run_gigaam
       → run_tone: docker_stage (input_8k → asr/work, иначе stale / чужой файл)
  → dump_sortformer
  → merge_ensemble
       → merge_guard, llm_client, transcript_integrity, hallucination_guard
  → build_transcript_view
       → timing_align, clock_guard, speaker_labels
  → match_sortformer_speakers
  → build_transcript_view
  → voice_turn_audit
  → summarize_call (в knowledge phase)
```

Полный граф: [project-map/manifest.yaml](project-map/manifest.yaml)

---

## 9. Промпты LLM

| Файл | Используется в |
|------|----------------|
| `merge_transcript.md` | merge_ensemble.py |
| `call_summary.md` | summarize_call.py (итоговое резюме) |
| `call_brief.md` | summarize_call.py (краткое резюме) |
| `doc_summary.md` | summarize_document.py (протокол документа) |
| `extract_entities.md` | entities.py |
| `normalize_document.md` | pipeline_documents |
| `extract_image_vision.md` | image_extract |
| `rebuild_tz.md` | tz.py |
| `rag_chat.md` | chat.py (чанки + сущности по вопросу + хвост `current_tz.md` как обзор) |
| `lab_sense_speakers.md` | lab_sense.py (этап 2 лаборатории) |
| `lab_proof_text.md` | lab_proof.py (этап 3в: выбор между lab и giga) |

---

## 10. Scripts (`scripts/`)

| Скрипт | Назначение |
|--------|------------|
| `qa_all.py` | Единая команда QA: pytest + смоук + аудит целостности + отчёт здоровья (`--per-file`, `--evals`) |
| `health_report.py` | Отчёт здоровья по логам/БД с красными признаками |
| `verify_backup.py` | Проверка бэкапа БД: integrity_check и восстановление во временную папку |
| `audit_integrity.py` | Аудит целостности (только чтение): «сироты» в БД (23 проверки), векторы чанков (`rowid`), папки проектов и записи комнат без записи в БД, индекс поиска, не совпадающий с нарезкой текущего текста материала (стенограмму переписали после индексации); `--requeue-stale` — такие материалы задачей `refresh_knowledge` в очередь |
| `collect_ui_copy.py` | Реестр текстов интерфейса → `docs/UI-COPY.md` |
| `eval_tz.py` | Качество быстрой сводки ТЗ на эталонных фактах (`docs/eval/tz_facts_*.json`): recall/противоречия/стабильность; только чтение |
| `eval_chat.py` | Оценка чата на копии БД (`docs/eval/chat_questions_*.json`): пустые ответы, источники, отказ на ловушках |
| `restart.ps1` | Безопасный перезапуск: проверка Ollama/LM Studio/модели/порта → `start.ps1` → ждёт нового воркера и `/health` |
| `smoke/run_all.py` | Смоук на живом сервере (роли, presence, песочничный поток, комната); пароли из `SMOKE_*_PW` |
| `index_project_map.py` | Векторный индекс карты → `data/project_map.db` |
| `search_project_map.py` | Семантический поиск по карте |
| `rerun_cursor_merge.py` | Merge+view без ASR (Cursor tuning) |
| `rerun_transcript_integrity.py` | Только integrity + plain + view |
| `resume_knowledge.py` | Догнать knowledge phase |
| `reprocess_documents.py` | Отчёт «новое извлечение таблиц против текущего `_clean`» по документам проектов; `--enqueue` — копия БД и задачи `process_artifact` для переобработки |
| `inspect_project_index.py` | Сверка чанков sqlite-vec с lab `_final.txt` (`--project 18`) |
| `refresh_portal_knowledge.py` | Пересборка выжимок созвонов + индекс + сущности + ТЗ (`--project 19` / `--all`) |
| `rebuild_artifact_report.py` | Пересборка отчёта артефакта |
| `monitor_status.py` / `monitor_skkkk_loop.ps1` | Мониторинг сервера (`monitor_skkkk_loop.ps1` устарел — см. §3.0) |
| `install_caddy.ps1` | бинарь Caddy + опциональная служба `SKKKK-CADDY` |
| `install_meeting_voice.py` | изолированный `venvs/voice` + DeepFilterNet CPU (не app venv) |
| `screen_recorder.py` | `skkkk-rec`: VideoStream I420 → packed yuv420p → ffmpeg mp4 1 fps в `data/meetings/{room}/tracks/`; смена WxH — scale в размер первого кадра, не сырой хвост буфера |
| `fetch_meeting_dfn3.py` | WASM DFN3 в `static/vendor/dfn3/` — клиент выключен, канон сервер |
| `redescribe_screen_frames.py` | Vision-переописание кадров экрана и/или пересборка HTML стенограммы (`--rebuild-only`) |
| `scripts/start.ps1` | Запуск API+воркер: `-Llm cursor|gemma` или из `.env` |
| `scripts/start-cursor.ps1` / `start-gemma.ps1` | Быстрый старт с выбранным LLM |
| `scripts/stop.ps1` | Остановить сервер и снять lock воркера |
| `run_all.ps1` / `run_api.ps1` / `run_worker.ps1` | Запуск служб |
| `smoke_cycle.py` | Smoke test |
| `index_project_map.py` | индексатор проекта: инкрементально, `--full`, `--no-embed`, `--status` |
| `search_project_map.py` | поиск по индексу: вопрос или режимы `--symbol`, `--callers`, `--callees`, `--route`, `--table`, `--env`, `--ui`, `--tests`, `--history`, `--changed`, `--why-changed`, `--invariants`, `--ops` |
| `eval_index.py` | замер качества поиска на золотом наборе `docs/eval/index_questions.json` и контрольном `..._holdout.json` |
| `index_doctor.py` | диагностика индекса: sqlite-vec, FTS5, Ollama, возраст, устаревшие файлы, векторы, качество |
| `check_map_drift.py` | расхождения карты и кода; `--fix` пересоздаёт реестр модулей и добавляет узлы манифеста |
| `install_hooks.ps1` | git-хуки post-commit/post-merge: фоновое обновление индекса |
| `dev_changelog.py` | журнал разработки из git → `docs/CHANGELOG-DEV.md` |
| `fast_summarize_tz.py` | быстрая сводка ТЗ из CLI: top-K по sqlite-vec + заметки → один reduce → `current_tz.md` + changelog |
| `kb_inbox.py` | ящик вопросов внешнего агента из командной строки (`list`, ответ), без входа по паролю |
| `personal_password.py` | личные пароли без PyCharm: `list`, `reset <логин|номер> [--yes]` (база `credentials.db` напрямую) |
| `user_profile.py` | карточки без PyCharm: `list`, `set-company`, `withdraw-voice`, `restore-voice`, `reset` (`--yes`) |
| `link_voice_profiles.py` | слепки голоса ↔ карточки: `list`, `link` (на каждое — подтверждение), `unlink <id>` |
| `live_meetings.py` | печатает идущие встречи; код выхода 3, если есть люди на связи (для безопасного перезапуска). Правило — `services/meetings.ongoing_meetings`, общее с полоской «Идёт созвон» |
| `watch_server.py` | наблюдение за сервером, очередью и ошибками конвейера |
| `qa_pages.py` | аудит вёрстки страниц на разных экранах и движках (изолированный экземпляр с копией БД) |
| `qa_flows.py` | интерактивные сценарии по порталу в браузере |
| `qa_navbar.py` | проверка шапки на разных экранах |
| `qa_room.py` | комната созвона в Chromium с фейковыми устройствами, без паролей (песочница `SMOKE_PROJECT_ID`); проверки индикатора связи изолированы от настоящих оценок LiveKit |
| `qa_room_ui.py` | комната: раскладка телефона при повороте (с показом экрана и без), пульт (подсказки, переключатели, окна, Esc) на ПК и телефоне, камера после системного выключения, поток `mic_silent`, качество связи после переподключения |
| `qa_media_path.py` | путь звука и видео: что публикует гость (микрофон, слои камеры), что публикует шумодав, растёт ли сырая WAV; у слушателя — одна дорожка на человека, срез шума в дБ, громкость речи, задержка `clean-*`, потери/джиттер, кадры камеры; «Шумодав» туда-обратно и выпадение шумодава без тишины; запись продолжается; экран одним слоем |
| `qa_meeting_lifecycle.py` | жизненный цикл встречи на живом сервере: создание, вход до запуска, удаление без запуска, запуск, запись, повторный вход, пульс, отзыв и новая ссылка, запрет удалять идущую, завершение (комната закрыта, дорожки закрыты, не воскресает), удаление записанной встречи и проекта с идущей встречей; запись не возвращается на диск |
| `qa_analysis.py`, `qa_publish_wav.py` | сквозная проверка «созвон → запись → анализ» на синтезированной русской речи |
| `qa_call_stress.py` | стресс записи звонка на реальном сервере: рост дорожек, убийство рекордера |
| `qa_mobile_webkit.py`, `qa_mobile_android.py` | мобильные проверки: WebKit (iPhone/iPad) и настоящий Chrome на эмуляторе Android |
| `smoke/` | смоук-набор (`run_all.py`, общие настройки в `common.py`, секреты только из окружения): комната, плитки, права, счётчики, формы, `smoke_agent_cards.py`, `smoke_agent_exchange.py` |

---

## 11. Тесты

| Путь | Что покрывает |
|------|---------------|
| `app/app/tests/` | API routes, auth, cookie, TZ, artifact actions/purge, user scenarios, страница лаборатории, lab enroll |
| `app/app/tests/test_startup.py` | `all` → edge→worker→api; `api` → edge→portal; повторный старт no-op |
| `app/app/tests/test_edge_runtime.py` | TLS → Caddy → LiveKit → voice; портал жив если LiveKit упал |
| `app/app/tests/test_tls_cert.py` | проверка файла сертификата и перевыпуск при старте |
| `app/app/tests/test_preflight.py` | `meeting_voice` в plan `all`/`api`, не `worker` |
| `app/app/tests/test_meetings.py` / `test_livekit_env.py` | комнаты, JWT, yaml ICE, кнопка Проанализировать |
| `app/app/tests/test_meeting_recordings.py` | сырые дорожки одной комнаты в одном `manifest.json` |
| `app/app/tests/test_pipeline_lab.py` | publish `speaker_N`, `republish_named_transcript` пишет имена |
| `app/app/tests/test_artifact_audio.py` | lab rebuild не вызывает `build_transcript_view` |
| `asr/tests/test_summarize_lab_view.py` | `lab_proof_is_canonical` |
| `asr/tests/test_summary_markdown.py` | GFM-таблица в HTML; `unknown` не чип |
| `asr/tests/test_lab_contract.py` | Изоляция прогонов, sha256, пропуск по отпечатку, падение без входа |
| `asr/tests/test_lab_stages.py` | Слова из токенов, покраска, реплики, клипы, бинаризация |
| `asr/tests/test_lab_voice.py` | Клипы из overlay-слов, lab match.json без Docker |
| `app/app/tests/test_lab_enroll.py` | Enroll/merge пишет lab match и не трогает прод JSON |
| `app/app/tests/test_projects.py` | создание проекта, `DELETE /projects/{id}` |
| `app/app/tests/test_artifact_purge.py` | `DELETE` артефакта, FK entities/links, HTML иконки |
| `app/app/tests/test_user_scenarios.py` | сквозные сценарии портала (в т.ч. s31 purge) |
| `docs/TESTING.md` | Стратегия, команды pytest |

```powershell
venv\Scripts\python.exe -m pytest app/app/tests asr/tests -q
```

---

## 12. Внешние зависимости

| Система | Переменные .env | Когда нужна |
|---------|-----------------|-------------|
| LM Studio | `LMSTUDIO_*`, `LLM_PROVIDER=lmstudio` | Sense, proof, entities, summary, TZ, chat (`LMSTUDIO_MODEL` в VRAM; ASR в этот момент выгружен) |
| Cursor API | `CURSOR_*`, `LLM_PROVIDER=cursor` | То же, без VRAM Gemma |
| Ollama bge-m3 | `OLLAMA_*` | Индекс артефактов, search |
| Docker ASR | `GIGAAM_URL` | Прод: GigaAM (второй слух + embed). Whisper/T-one URL — откат |
| LiveKit | `LIVEKIT_*`, `LIVEKIT_RTC_INTERNAL` | комнаты; native server поднимает `main` |
| Meeting voice | `MEETING_VOICE_SERVER`, `MEETING_VOICE_DFN3`, `MEETING_VOICE_LOUDNESS`, `MEETING_VOICE_ATTEN_LIM`, `MEETING_VOICE_POST_FILTER`, `MEETING_RECORD`, `MEETING_SCREEN_RECORD`, `MEETING_SCREEN_FPS`, `MEETING_SCREEN_MAX_W`, `MEETING_SCREEN_HEIGHT`, `MEETING_SCREEN_BITRATE`, `MEETING_SCREEN_VISION`, `MEETING_FRAMES_MAX` | серверная чистка (канон); запись экрана `skkkk-rec` 1080p/5 Мбит; кадры/vision за флагом (`MEETING_SCREEN_VISION=1` — vision-описание кадров в `video_transcript.json`); клиентский DFN3 выкл |
| Telegram | `TELEGRAM_*` | Уведомления (stub) |

---

## 13. Инварианты (не ломать)

См. `.cursor/rules/01-nonnegotiables.mdc` и `docs/DECISIONS.md`:

- Двойной слух: Parakeet-TDT + GigaAM + Sortformer 4spk; Whisper/T-one/merge окон **не** вызываются
- DeepFilterNet в **комнате** (`skkkk-dfn`, CPU) ≠ lab `01_audio` (`copy_16k`). На ASR DFN не возвращать
- Очередь `jobs` + один GPU worker
- sqlite-vec + entities/links (не Qdrant/Neo4j до этапа 10)
- Секреты только в `.env`
- Обход reasoning имеет самопроверку при старте API и воркера (`llm.lmstudio_no_think_selfcheck`, `LMSTUDIO_NO_THINK=0` — принудительный откат на chat-эндпоинт); статус — `/health/ready.llm_no_think`
- Вход ограничен по попыткам (`services/rate_limit.py`: 8 неудач/5 мин на IP и логин → 429; неверный личный пароль считается так же, счётчик логина сбрасывает только полный вход); заголовки безопасности в `main.SECURITY_HEADERS` (HSTS/CSP сознательно нет)
- Стили: `static/app.css` (структура) + `static/design.css` (единая дизайн-система, токены `--ds-*`), см. DECISIONS; отчёты (документ и стенограмма) — сгенерированные HTML, тема `static/report-theme.css` подключается при отдаче в `_serve_report`, файлы отчётов не меняются
- Обмен с внешним агентом: `services/agent_gateway.py` (белый список, журнал `agent_exchange_log`), контракт — `services/contract.py` + `project_mcp_contract.py` (MCP-инструменты 16 шт., файлы `contract/*.json` виртуальные), правила — `docs/agent-exchange/`; файлы `analysis_reply_/analysis_request_*.json` в конвейер не идут
- `POST /client-log` принимает JS-ошибки браузеров (лимит), событие `client.js_error`; зависимость `livekit-api` обязательна (закрытие комнаты при завершении встречи)
- Тесты не имеют права писать в боевую БД: `SessionLocal` в тестах запрещён без фикстур `client`/`db_engine`
- Роль сессии — в JWT, роль общей учётки в БД при входе не перезаписывается (`dependencies.py`)
- LLM-вызовы, где важен результат (JSON/текст), — только через `*_no_think` (обход зависшего reasoning); страж — `test_llm_no_think_guard.py`
- Текст согласия гостя (`meeting_public.html`) должен совпадать с реальной записью: голос и экран, без камеры (зависит от `MEETING_RECORD`)
- Lab-артефакт: HTML/JSON только через `write_prod_view`, не `build_transcript_view.py`

---

## 15. Реестр модулей (генерируется)

<!-- generated:begin registry -->

> Генерируется `scripts/check_map_drift.py --fix` из кода. Не править руками: описание берётся из докстринга (для модулей без докстринга — прочерк, его стоит добавить). Смысловое описание — в разделах выше.

**`app/app/`**

| Файл | Назначение |
|---|---|
| `bootstrap.py` | Подготовка `.env` до того, как Settings прочитает файл. |
| `config.py` | — |
| `credentials_db.py` | Отдельная база личных паролей `data/credentials.db`: только хеши, отдельно от `app.db`. |
| `cursor_compat.py` | Совместимость cursor-sdk с Python < 3.12 (os.get_blocking / os.set_blocking). |
| `db.py` | — |
| `dependencies.py` | — |
| `llm_labels.py` | Подписи LLM для логов и UI в зависимости от провайдера (Local LLM / Cursor). |
| `llm_settings.py` | Выбор LLM-провайдера (Gemma / Cursor) — чтение и запись в `.env`. |
| `logging_setup.py` | Единая настройка логов: консоль (PyCharm) плюс файл в `data/logs`. |
| `main.py` | — |
| `preflight.py` | Проверка всех систем перед запуском. |
| `startup_llm.py` | Выбор LLM при запуске SKKKK: LM Studio (локально) или Cursor API. |

**`app/app/mcp/`**

| Файл | Назначение |
|---|---|
| `project_protocol.py` | Проектный MCP по JSON-RPC: инструменты чтения знаний проекта и приёма данных агента, разбор запросов и инструкции клиенту. |

**`app/app/models/`**

| Файл | Назначение |
|---|---|
| `agent_exchange.py` | Модель AgentExchangeLog — журнал обмена с внешним агентом (таблица agent_exchange_log). |
| `agent_question.py` | Модель AgentQuestion — вопрос внешнего агента в ящике вопросов проекта (таблица agent_questions). |
| `artifact.py` | Модель Artifact — загруженный файл проекта и его статус обработки (таблица artifacts). |
| `chat_guidance.py` | Модель ChatGuidance — указания пользователя чату по теме проекта (важнее созвонов при расхождении). |
| `chat_message.py` | Модель ChatMessage — сообщение чата проекта, в том числе ожидающее ответа. |
| `company.py` | Модель Company — компания из файла Companies.txt: название и сторона (исполнитель / заказчик). |
| `contract.py` | Модель ContractItem — элемент контракта обмена с внешним агентом. |
| `entity.py` | Модели знаний: сущности, связи, фрагменты, запуски ASR, заметки и ссылки, разрешения конфликтов, журнал изменений ТЗ. |
| `job.py` | Модель Job — задача очереди обработки (таблица jobs). |
| `meeting.py` | Модели встреч: Meeting и MeetingParticipant (присутствие участников). |
| `project.py` | Модель Project — проект и его настройки. |
| `project_mcp_token.py` | Модель ProjectMcpToken — ключ доступа проектного MCP (хеш, права, отзыв). |
| `project_member.py` | Модель ProjectMember — выданный доступ пользователя к проекту (роль manager/user, кто выдал). |
| `project_task.py` | Модель ProjectTask — фоновая задача проекта со статусом и ошибкой (таблица project_tasks). |
| `speaker_profile.py` | Модели голосов: SpeakerProfile и связь ArtifactSpeakerLink со спикерами артефакта. |
| `user.py` | Модель User — пользователь (имя, роль, Telegram). |
| `user_activity_log.py` | Модель UserActivityLog — журнал действий пользователей. |
| `user_profile.py` | Модель UserProfile — карточка человека (1:1 с users): ФИО, компания, должность, фото, согласие на голос. |

**`app/app/routers/`**

| Файл | Назначение |
|---|---|
| `acme.py` | Маршрут HTTP-01 проверки Let's Encrypt (`/.well-known/acme-challenge`). |
| `admin_map.py` | Страница «Карта проекта» для администратора: поиск по индексу кода, связей, БД, окружения и истории (/admin/map). |
| `auth.py` | Вход и выход: `/auth/login` (роль определяется паролем), `/auth/logout`. |
| `knowledge.py` | Знания проекта: поиск по смыслу, граф, сущности, заметки и связи, конфликты, ТЗ и его версии, чат, дедлайны. |
| `meetings.py` | Встречи проекта и гостевой вход: создание, старт, завершение, отзыв ссылки, вход и выход гостя, присутствие. |
| `pages.py` | HTML-страницы портала и отчёты: вход, проекты, встречи, отчёты артефактов, отдача аудио и скачивание. |
| `people.py` | Карточки людей: фото (`/people/{id}/avatar`) и своя карточка (`/me/profile`, `/me/avatar`). |
| `project_mcp.py` | Ключи проектного MCP, сам MCP-эндпоинт и обмен с внешним агентом: настройки, вердикты, ящик вопросов, журнал. |
| `project_members.py` | Выдача доступа к проекту: список участников, добавление с ролью manager/user, удаление (/projects/{id}/members). |
| `projects.py` | Проекты: список, создание, карточка, предпросмотр и полное удаление. |
| `requirement_review.py` | Подтверждение требований контракта (карточка «Подтверждение требований» на странице проекта). |
| `speaker_profiles.py` | Спикеры и голоса: профили, запись голоса, слияние, пересчёт, подтверждение и связанные фоновые задачи. |
| `upload.py` | Загрузка файлов (в том числе частями с докачкой) и список артефактов проекта, удаление артефакта. |

**`app/app/services/`**

| Файл | Назначение |
|---|---|
| `acme_challenge.py` | Проброс HTTP-01 проверки Let's Encrypt к Caddy: адрес проверки и прокси-ответ (`/.well-known/acme-challenge`). |
| `agent_gateway.py` | Шлюз обмена с внешним агентом (агент анализа конфигурации): всё, что он присылает, — недоверенные ДАННЫЕ. |
| `agent_guide.py` | Инструкция для внешних агентов: как пользоваться базой знаний, как просить нас о чём-то и как с нами общаться. |
| `agent_inbox.py` | Ящик вопросов внешнего агента: принять вопрос, найти опорные фрагменты, дать ответить человеку, Claude или локальной модели. |
| `agent_letters.py` | Письма базы знаний внешнему агенту: доставка через MCP, без переноса человеком. |
| `agent_settings.py` | Настройки проекта для обмена с внешними агентами (хранятся в Project.settings_json → "agent_exchange"). |
| `agent_verdicts.py` | Вердикты внешнего агента (analysis_verdict): список, подтверждение и отклонение человеком (только admin). |
| `artifact_audio.py` | Подготовка звука артефакта: атомарное извлечение WAV из исходника с блокировкой и проверкой полноты, MP3 для скачивания, AAC для проигрывани |
| `artifact_download.py` | Скачивание исходного файла артефакта: путь, имя и MIME (resolve_artifact_download), доступность и подпись кнопки «Скачать». |
| `artifact_lab.py` | Доступ к лабораторным прогонам артефакта из веба. |
| `artifact_reports.py` | HTML-отчёты по документам: путь и адрес отчёта, запись на диск, панель инструментов, разделы исходного текста и выжимки. |
| `auth.py` | Хеширование и проверка паролей. |
| `auto_review.py` | Автопроверка локальной моделью: режим (off / shadow / live), запись вердикта модели, отмена человеком, контрольная выборка. |
| `avatars.py` | Фото пользователя: проверка, выпрямление, удаление метаданных, квадрат 512 и 96 px WebP в `data/people/<user_id>/`. |
| `build_info.py` | Признак «код на диске новее запущенного сервера» (пункт плана 1.3). |
| `chat.py` | Чат с ИИ по материалам проекта: история и указания пользователя, уточняющие вопросы, поиск фрагментов и сущностей, сборка контекста, ответ L |
| `chat_export.py` | Выгрузка переписки Telegram Desktop (``messages.html``) → сообщения с номером, автором, временем и ответом |
| `chunk_fts.py` | Полнотекстовый поиск по фрагментам проекта (SQLite FTS5) — пара к векторному поиску для точных слов и чисел. |
| `chunking.py` | Нарезка текста на фрагменты для индексации: документы (проза + таблицы), реплики стенограммы, оценка токенов. |
| `companies.py` | Компании из файла `Companies.txt` (корень проекта, как `Login_up.txt`): разбор и перенос в таблицу `companies`. |
| `conflict_context.py` | Противоречия проекта для LLM чата и ТЗ — хронологией, человеческими строками (CONFLICT-AUTONOMY-PLAN этап 5, Р4). |
| `conflict_rules.py` | Правила разрешения противоречий без модели — docs/plans/CONFLICT-AUTONOMY-PLAN.md, решение Р1 (DECISIONS 2026-09-25). |
| `contract.py` | Контракт обмена «база знаний ↔ агент анализа конфигурации» (v1.0). |
| `contract_text.py` | Разбор текста для контракта обмена с агентом анализа: сообщения переписки, дословные цитаты, оценки часов, якоря кода. |
| `docker_runtime.py` | Автозапуск Docker Desktop и ASR-контейнеров перед обработкой аудио. |
| `docx_extract.py` | DOCX → markdown в порядке документа: заголовки (#), абзацы, списки, таблицы на своих местах, надписи, колонтитулы. |
| `edge_runtime.py` | HTTPS (Caddy) and LiveKit room — поднимает main вместе с порталом. |
| `entities.py` | Извлечение сущностей (задачи, упоминания, участники, решения, факты) из нормализованного текста через LLM: разбиение, промпт, разбор JSON и  |
| `extraction_context.py` | Контекст для извлечения требований (docs/plans/REQUIREMENTS-PLAN.md, этап 3): карточка проекта, оглавление созвона, |
| `ffmpeg_tools.py` | Поиск исполняемых файлов ffmpeg и ffprobe. |
| `gpu_phase.py` | Фазы GPU: замер видеопамяти (nvidia-smi), ожидание освобождения, выгрузка и загрузка модели LM Studio, чтобы ASR и LLM по очереди делили одн |
| `graph.py` | Граф знаний проекта: тексты сущностей, автосвязи по косинусной близости, пересборка графа, удаление сущностей с зависимостями, чистка «осиро |
| `html_extract.py` | HTML → текст с таблицами: блоки разметки дают переносы строк, ``<table>`` — markdown-таблицы на своих местах. |
| `image_extract.py` | Текст из изображений: проверка слабого OCR и распознавание через vision-модель LLM. |
| `indexing.py` | Индексация артефакта для поиска по смыслу (фрагменты и векторы в sqlite-vec) и сверка индекса проекта (`audit_project_index`). |
| `lab_errors.py` | User-facing lab errors. ONNX warnings and ANSI stay out of the UI. |
| `livekit_env.py` | Keys and livekit.yaml for the Meeting Mind room. |
| `llm.py` | Единый адаптер LLM (LM Studio или Cursor API): вызовы без reasoning (`*_no_think`), разбор JSON, самопроверка обхода reasoning при старте. |
| `login_allowlist.py` | Допуск на вход: только логины из Login_up.txt, без учёта регистра. |
| `media_cleanup.py` | Чистка исходников: удаление видео после проверки извлечённого WAV и сверка пути исходника артефакта. |
| `meeting_analyze.py` | Queue dual-hearing jobs for native meeting tracks. No Sortformer. |
| `meeting_checks.py` | Чистые проверки шкалы созвона. Без БД и без LiveKit. |
| `meeting_frames.py` | Кадры screen_video → vision. Не во время живой комнаты. |
| `meeting_lab.py` | Снимок лаборатории созвона: дорожка за дорожкой, входы и выходы стадий. |
| `meeting_merge.py` | Merge per-speaker meeting words onto one clock. No LLM. |
| `meeting_overlap.py` | Same-room и one-mic: не угадывать имя без технических признаков. |
| `meeting_recordings.py` | Raw per-speaker audio for one LiveKit room. Not DFN. Not ASR. |
| `meeting_tokens.py` | Публичные токены встреч: генерация, хеш и адрес гостевой ссылки. |
| `meeting_track_repair.py` | Ремонт реплик дорожки встречи, когда основной распознаватель (Parakeet) ошибся во времени или в содержании. |
| `meetings.py` | Встречи проекта: создание, старт и завершение, идентичность гостей и сотрудников, участники и их присутствие, идущие сейчас встречи, токены  |
| `normalize.py` | Нормализация текста документов: разбиение на части, промпт LLM и лёгкая нормализация без LLM. |
| `notify.py` | Уведомления в Telegram через HTTPS Bot API: о готовности файла и событиях для пользователя. |
| `office_convert.py` | Конвертация старых офисных форматов через LibreOffice headless: .doc/.rtf/.odt → .docx, .xls/.ods → .xlsx, .ppt → .pptx. |
| `ollama_client.py` | Клиент Ollama: эмбеддинги (bge-m3, на CPU) и список моделей. |
| `ollama_runtime.py` | Автозапуск Ollama перед эмбеддингами и индексацией. |
| `personal_password.py` | Личный пароль логина: хеш Argon2id, правила, создание, смена, сброс, версия для сессий. |
| `personal_password_console.py` | Сброс личного пароля из консоли: пункт 7 меню запуска `main` и `scripts/personal_password.py`. |
| `pipeline_audio.py` | Обработка аудио-артефакта: запуск лабораторного ASR-конвейера, стенограмма, сверка спикеров, аудит реплик и выжимка созвона (`process_audio_ |
| `pipeline_documents.py` | Извлечение текста из документов и изображений: TXT, RTF, PDF, DOCX, HTML, таблицы в Markdown, OCR. |
| `pipeline_knowledge.py` | Конвейер знаний после получения текста артефакта: сущности, индекс и граф (`run_knowledge_pipeline`). |
| `pipeline_lab.py` | Боевой ASR: лабораторный контур → публикация → speakers_pending. |
| `pipeline_meeting.py` | Native meeting: per-track dual hearing → merge by time → one transcript. |
| `pipeline_progress.py` | Пошаговый прогресс обработки артефакта для UI (чеклист ASR + фазы knowledge). |
| `pipeline_resume.py` | Продолжение обработки аудио после сбоя: восстановление стенограммы из рабочей папки ASR и решение, продолжать ли. |
| `pipeline_voice_gate.py` | Voice gate before triple ASR: wav → Sortformer → speaker match → speakers_pending. |
| `plain_text.py` | Convert common LLM LaTeX / math markup into plain Unicode for HTML display. |
| `pptx_extract.py` | Текст из презентаций .pptx → markdown по слайдам (вход для normalize / knowledge-пайплайна). |
| `project_access.py` | Доступ к проекту: свои + выданные участники; роль на проекте может отличаться от пароля. |
| `project_deadlines.py` | Дедлайны проекта: разбор сроков из текста («до пятницы», «на следующей неделе», даты) относительно даты артефакта и сведение задач в единый  |
| `project_mcp_contract.py` | MCP-инструменты контракта обмена с агентом анализа (только чтение). Формат и смысл полей — docs/agent-exchange/CONTRACT.md. |
| `project_mcp_data.py` | Данные проектного MCP (только чтение): обзор проекта, артефакты, сводка, поиск, сущности, граф и заметки — то, что отдают инструменты MCP. |
| `project_mcp_tokens.py` | Ключи доступа проектного MCP: выпуск, хеширование, список, отзыв, право отправки (can_submit) и проверка ключа при запросе. |
| `project_members.py` | Выдача доступа к проекту: роли manager и user, без admin. |
| `project_purge.py` | Удаление проекта целиком или одного артефакта: записи БД и файлы на диске. |
| `project_tasks.py` | Фоновые задачи API (пересборка ТЗ, переиндексация и т.п.): запись и статусы в таблице project_tasks, защита от параллельного запуска, очистк |
| `prompts.py` | Загрузка промптов из docs/prompts/*.md (пункт плана 2.2). |
| `queue.py` | Очередь задач (таблица jobs): захват следующей задачи, завершение, возврат «осиротевших», постановка обработки и повтора этапа знаний. |
| `rate_limit.py` | Ограничитель неудачных попыток (пункт плана 4.7): защита входа от перебора паролей ролей. |
| `recorded_at.py` | Когда записан материал (`artifacts.recorded_at`): встреча из комнаты, имя файла (с временем или без), метаданные записи, шапка и свойства до |
| `report_embed.py` | Режимы встраивания HTML-отчётов (прозрачный фон, без шапки) и скрытие элементов управления по роли пользователя. |
| `requirement_review.py` | Подтверждение требований человеком (только admin): очередь, подтвердить / отклонить / сменить статус. |
| `requirements_reextract.py` | Переизвлечение требований проекта по новой схеме (docs/plans/REQUIREMENTS-PLAN.md, этап 9; решение А10). |
| `resumable_upload.py` | Загрузка файлов частями с докачкой: сессии, части и сборка на диск. |
| `retrieval.py` | Поиск сущностей и фрагментов проекта: взвешенные оценки, обогащение и переранжирование результатов, порядок артефактов. |
| `review_conflicts.py` | Автопроверка конфликтов (POTENTIAL_CONFLICT) локальной моделью — этап 4 docs/plans/AUTO-REVIEW-PLAN.md, |
| `review_relevance.py` | Актуальность пунктов по теме (docs/plans/REQUIREMENTS-PLAN.md, этапы 6–7; решения А1, А3–А6). |
| `review_requirements.py` | Автопроверка требований (decision / task) локальной моделью по источнику — этап 2 docs/plans/AUTO-REVIEW-PLAN.md. |
| `single_instance.py` | Защита от запуска второго GPU-воркера. |
| `source_links.py` | Ссылка на место источника (docs/plans/SOURCE-ANCHORS-PLAN.md, этап 4): момент созвона, сообщение переписки, абзац документа. |
| `speaker_auto.py` | Спикеры без лишних подтверждений (этап 3 docs/plans/AUTO-REVIEW-PLAN.md). |
| `speaker_profiles.py` | Голоса и спикеры: профили и эмбеддинги голосов, сопоставление имён, подтверждение спикеров, слияние профилей и синхронизация подтверждённых  |
| `status.py` | Смена статуса артефакта. |
| `storage.py` | Каталоги проекта и артефактов на диске: имена, пути к оригиналу, аудио, воспроизведению и отчётам. |
| `summarize_document.py` | Выжимка документа: пути, загрузка исходного текста, промпты, сборка итогового Markdown и запуск (`run_document_summary`). |
| `table_md.py` | Таблицы документов в markdown: один формат для DOCX, Excel/CSV, PDF и разбор его обратно (нормализация, нарезка). |
| `tesseract_tools.py` | Поиск Tesseract и каталога tessdata, настройка pytesseract. |
| `tls_cert.py` | Публичный сертификат :777 — проверка при старте и перевыпуск через Caddy :8443. |
| `topics.py` | Темы проекта (docs/plans/REQUIREMENTS-PLAN.md, этап 5, решение А2): модель раскладывает решения, задачи и открытые |
| `transcript_export.py` | Экспорт стенограммы созвона в документы (Word): поиск данных спикеров, выжимки и таймкодов, оформление стилей и шрифтов. |
| `transcript_turns.py` | Реплики созвона из канона ``_final.txt``: кто, с какой по какую секунду, что сказал. |
| `tz.py` | Техническое задание проекта: пересборка `current_tz.md` из материалов (быстрый путь top-K и полный map/reduce), версии, оценка объёма, челов |
| `upload.py` | Приём файлов: определение типа, приоритет, длительность через ffprobe, сохранение потока, хеш, поиск дубликатов и создание записи артефакта. |
| `user_activity.py` | Журнал действий пользователя (аудит). |
| `user_identity.py` | Учётка по имени (логину). Telegram ID только хранится и обновляется. |
| `user_profile.py` | Карточка пользователя: проверка полей, заполнение при первом входе, правка своей карточки, действия админа. |
| `user_profile_console.py` | Карточки пользователей из консоли: пункт 8 меню запуска `main` и `scripts/user_profile.py`. |
| `vector_index.py` | Хранилище эмбеддингов в SQLite через sqlite-vec. |
| `voice_link.py` | Привязка слепков голоса к карточке сотрудника (задел: автосбор с дорожки созвона — отдельный план). |
| `xlsx_extract.py` | Excel/CSV → markdown: ``## Лист: имя`` и GFM-таблицы по блокам листа. |

**`app/app/services/project_index/`**

| Файл | Назначение |
|---|---|
| `analyzers.py` | Структурные знания о проекте, которые нельзя надёжно получить из текста: роуты, БД, окружение, |
| `chunkers.py` | Нарезка файлов проекта на фрагменты для индекса: Python, JS, шаблоны, CSS, Markdown, конфиги. |
| `config.py` | Конфигурация индекса проекта: какие файлы читать, что исключать, как прятать секреты. |
| `indexer.py` | Индексатор проекта: инкрементально, с кэшем эмбеддингов, устойчиво к обрыву. |
| `search.py` | Поиск по индексу проекта: гибрид (точный FTS5 + смысловой вектор + точное совпадение символа) и структурные режимы. |
| `store.py` | Хранилище индекса проекта: SQLite + sqlite-vec + FTS5 в data/project_map.db (отдельно от app.db). |

**`app/app/static/`**

| Файл | Назначение |
|---|---|
| `app.css` | Общие стили портала Primary Analytics: шапка, адаптив под телефон и планшет, таблицы, кнопки |
| `app.js` | Общий JS портала (объект SKKKK): сессия и навигация, бейдж LLM, API-запросы, блокировка кнопок, склонения. |
| `design.css` | Единая дизайн-система Primary Analytics (v2, 2026-09-19). |
| `errors_ru.js` | Единая карта «английский detail API → русский текст» (пункт плана 3.14). |
| `meeting_layout.js` | Раскладка плиток встречи (SKKKKLayout): порядок экранов и камер, флаги микрофона/камеры, план звука. |
| `meeting_layout.selfcheck.js` | Самопроверка meeting_layout.js в браузере (console.assert), без тестового фреймворка. |
| `meeting_room.js` | Клиент встречи LiveKit: вход, микрофон/камера/экран, смена устройств на ходу, плитки, список участников. |
| `px_agent.css` | Карточки обмена с агентами (px_agent.js). Только токены дизайн-системы --ds-*: нейтральная палитра, тонкие линии. |
| `px_agent.js` | Карточки обмена с агентами на странице проекта: подтверждение требований, обмен (вопросы/вердикты/журнал), настройки. |
| `report-jump.js` | Переход по ссылке к месту источника на странице отчёта (docs/plans/SOURCE-ANCHORS-PLAN.md, этап 4). |
| `report-theme.css` | Тема отчётов (документ и стенограмма созвона) в единой дизайн-системе. |

**`app/app/templates/`**

| Файл | Назначение |
|---|---|
| `admin_map.html` | Страница «Карта проекта» (/admin/map, только admin): поиск по индексу кода, документов и связей |
| `base.html` | Общий каркас страниц портала: шапка (логотип, бейдж LLM, «Нужен перезапуск», пользователь, «Выйти»), блоки для страниц |
| `chat.html` | Отдельная страница чата с ИИ по материалам проекта (/projects/{id}/chat) |
| `conflicts.html` | Очередь конфликтов фактов (/admin/conflicts, только admin): выбрать верное утверждение с причиной |
| `error.html` | Страница ошибки HTML (403/404/500) с понятным текстом на русском |
| `graph.html` | Граф знаний проекта в Cytoscape (/projects/{id}/graph) |
| `index.html` | Список проектов (/): создание, описание, удаление проекта, кнопка «Исправлено» со списком изменений |
| `login.html` | Страница входа: логин из Login_up.txt, Telegram ID, пароль роли; шаг 2 — личный пароль (создать или ввести); шаг 3 — карточка (ФИО, компания |
| `meeting_lab.html` | Лаборатория созвона (admin): этапы обработки каждой дорожки встречи; пока идёт прогон, страница обновляется сама |
| `meeting_public.html` | Гостевая страница встречи /m/{token}: лобби, проверка камеры и микрофона, комната без входа в портал |
| `meeting_room.html` | Комната встречи для сотрудника (/projects/{id}/meetings/{mid}/room) |
| `project.html` | Страница проекта: загрузка материалов, таблица артефактов, выжимка, чат, дедлайны, встречи, MCP, доступ, обмен с агентом |
| `tz.html` | Отдельная страница ТЗ проекта (/projects/{id}/tz): текст ТЗ и кнопка «Пересобрать ТЗ» |

**`app/app/templates/partials/`**

| Файл | Назначение |
|---|---|
| `px_agent_cards.html` | Карточки обмена с агентами: подтверждение требований, обмен (вопросы/вердикты/журнал), настройки. Логика: static/px_agent.js |

**`app/app/workers/`**

| Файл | Назначение |
|---|---|
| `gpu_worker.py` | GPU-воркер: один процесс, берёт задачи из очереди по одной, ждёт зависимости и запускает обработку артефактов. |
| `pipeline.py` | Обработка артефакта воркером: выбор конвейера по типу файла (`process_artifact`). |
| `processor.py` | Заглушка обработки для режима DRY_RUN: проходит статусы без моделей. |
| `review.py` | Фоновая автопроверка артефакта локальной моделью (задачи очереди ``kind=auto_review``). |

**`asr/`**

| Файл | Назначение |
|---|---|
| `build_lab_view.py` | Страница лабораторного прогона: стадии, хеши, A/B, тепловая карта, стенограмма. |
| `build_raw_diar_view.py` | Сырая диаризация для прослушивания: без Gemma, без сглаживания, несколько треков. |
| `build_sortformer_transcript_view.py` | Тестовая стенogramma: текст ASR + интервалы Sortformer v2.1 (GGUF). |
| `build_speaker_match_view.py` | Страница голосов созвона: диаризация + match/запоминание голосов (Bootstrap 5). |
| `build_transcript_view.py` | Собрать реплики: Sortformer + Gemma + (опционально) база голосов → HTML. |
| `client.py` | Клиент SKKKK к трём ASR-контейнерам. |
| `clock_guard.py` | Диагностика петель Whisper и выбор надёжного якоря часов для alignment. |
| `docker_stage.py` | Синхронизация аудио в asr/work для Docker-контейнеров ASR. |
| `dump_pyannote.py` | Снять сырые интервалы PyAnnote из контейнера skkkk-gigaam. |
| `dump_sortformer.py` | Sortformer v2.1 GGUF → sortformer_segments.json в OUT (и reports/ артефакта). |
| `engine_timeline.py` | Фильтрация таймлайнов ASR-движков перед склейкой и alignment. |
| `hallucination_guard.py` | Защита стенограммы от галлюцинаций ASR на тишине и от «простыни в 4 секунды». |
| `lab_giga.py` | Этап 3a — прогон GigaAM (Docker) на аудио того же лабораторного прогона. |
| `lab_hint.py` | Подписать лабораторных спикеров именами с боя — без копирования нарезки. |
| `lab_pipeline.py` | CLI лабораторного конвейера расшифровки. |
| `lab_proof.py` | Этап 3в — вычитка текста снимка 1+2 по прогону GigaAM. |
| `lab_reconcile.py` | Этап 3b — построчная сверка снимка 1+2 с прогоном GigaAM. |
| `lab_sense.py` | Оверлей Cursor API: разнести спорные слова по смыслу, не трогая снимок 01–07. |
| `lab_voice.py` | Этап 4 — слепки голосов лаборатории в voice/, без записи в БД. |
| `llm_client.py` | Единый LLM-клиент для ASR-скриптов (merge, summary). |
| `match_sortformer_speakers.py` | Match Sortformer SPEAKER_XX → speaker_profiles (проект + global). |
| `merge_ensemble.py` | Склейка трёх ASR через Gemma по окнам 120–180 с. |
| `merge_guard.py` | Проверка полноты склейки LLM и восстановление пропусков из ASR. |
| `paths.py` | Рабочие каталоги ASR: из env (prod) или asr/work + asr/out (dev). |
| `pipeline_checklist.py` | Чеклист этапов ASR-пайплайна. |
| `prepare_audio.py` | Готовит аудио для трёх ASR-движков. |
| `run_all.py` | Запускает три ASR-движка ПАРАЛЛЕЛЬНО, каждый в своём процессе и своём окружении. |
| `run_gigaam.py` | Движок 2: GigaAM v3 (переработка того, что лежит в AudioToText). |
| `run_pipeline.py` | Сквозной прогон: видео → три ASR → Gemma → спикеры PyAnnote → HTML. |
| `run_tone.py` | Движок 3: T-one (t-tech/T-one), телефонный русский, 8 кГц. |
| `run_whisper.py` | Движок 1: Whisper large-v3 Russian Podlodka (bond005). |
| `seed_artifact_asr.py` | — |
| `serve.py` | HTTP-сервис ASR-движка. Контейнер живёт постоянно; модель грузится при первом /transcribe. |
| `silence_energy.py` | Грубая оценка «есть ли речь» по энергии WAV без сдвига таймлайна. |
| `speaker_labels.py` | Подписи Sortformer ID → имена из sortformer_speaker_match.json. |
| `speaker_registry.py` | Match Sortformer/PyAnnote SPEAKER_XX к speaker_profiles (WeSpeaker cosine). |
| `speakers.py` | Диаризация отдельно от ASR: реплики, сглаживание границ, overlay текста, имена. |
| `summarize_call.py` | Выжимки созвона: итоговый протокол и краткое резюме, только из текста стенограммы. |
| `timing_align.py` | Привязка склейки Gemma к часам ASR и спикерам. |
| `transcript_integrity.py` | Страховка от потерь текста: audit merge + UI и глобальный backfill. |
| `transcript_view.py` | Статическая страница стенограммы: цвета спикеров, клик к таймкоду, фильтр. |
| `voice_turn_audit.py` | Автопроверка меток спикеров по голосу (WeSpeaker) после нарезки реплик. |

**`asr/lab/`**

| Файл | Назначение |
|---|---|
| `audio.py` | Чтение и ресемпл WAV для стадий лаборатории. |
| `diar_models.py` | Движки диаризации лаборатории: Nemotron 3 (по умолчанию) и Sortformer 4spk (откат). |
| `giga.py` | Разбор ответа GigaAM (Docker) для лаборатории. |
| `hint.py` | Подписи имён с боевого проекта — не основа лаборатории. |
| `nemotron_runner.py` | Nemotron 3 Diarization в отдельном процессе ``venvs\nemo-diar`` (NeMo + torch). |
| `onnx_env.py` | Общая настройка onnxruntime и каталогов моделей для лаборатории. |
| `paths.py` | Пути лабораторного прогона. Единственный источник — этот модуль. |
| `pipeline.py` | Оркестратор лабораторных стадий. |
| `proof.py` | Вычитка текста: пословные расхождения лаборатории и GigaAM (этап 3в). |
| `reconcile.py` | Построчная сверка снимка 1+2 с независимым мнением GigaAM (этап 3b). |
| `run.py` | Контракт стадии: хеши входов и выходов, честное падение, пропуск по отпечатку. |
| `sense.py` | Смысловая разноска спорных фраз — оверлей над зафиксированной покраской. |
| `sortformer.py` | Streaming Sortformer 4spk v2.1 на ONNX Runtime, без NeMo и без torch. |
| `voice.py` | Слепки голосов лаборатории: клипы из слов, WeSpeaker, match.json прогона. |
| `words.py` | Сборка слов из токенов SentencePiece. |

**`scripts/`**

| Файл | Назначение |
|---|---|
| `audit_integrity.py` | Аудит целостности данных (пункт плана 4.9): осиротевшие строки и файлы, устаревший индекс поиска. Только чтение. |
| `auto_review_backlog.py` | Проверить моделью всё накопленное в проекте (этап 5 docs/plans/AUTO-REVIEW-PLAN.md) — ставит фоновые задачи в очередь. |
| `backup_db.ps1` | SKKKK — резервная копия SQLite (VACUUM INTO): app.db и база личных паролей credentials.db |
| `bootstrap.ps1` | Полная установка проекта на новом ПК одной командой (запускается через INSTALL.bat в корне). |
| `chat_exports.py` | Разобрать уже загруженные выгрузки переписки Telegram на сообщения (docs/plans/SOURCE-ANCHORS-PLAN.md, этап 1). |
| `check_env.ps1` | — |
| `check_map_drift.py` | Проверка дрейфа: карта проекта и манифест против реального кода. Только чтение, если нет --fix. |
| `check_systems.ps1` | SKKKK — только проверка всех систем, без запуска |
| `collect_ui_copy.py` | Реестр текстов интерфейса (пункт плана 3.9): собирает русские подписи, подсказки, aria-label и строки JS. |
| `dedupe_speaker_profiles.py` | Удалить дубликаты speaker_profiles (имена и похожие голоса). |
| `dev_changelog.py` | Журнал разработки из git: docs/CHANGELOG-DEV.md (генерируется, руками не править). |
| `eval_auto_review.py` | Замер автопроверки требований (план docs/plans/AUTO-REVIEW-PLAN.md, §9). |
| `eval_chat.py` | Оценка чата-ассистента (пункт плана 2.9). |
| `eval_index.py` | Оценка индекса проекта на золотом наборе вопросов (docs/eval/index_questions.json). |
| `eval_tz.py` | Оценка качества быстрой сводки ТЗ (пункт плана 2.3). |
| `fast_summarize_tz.py` | Быстрый векторный свод: retrieval top-K из sqlite-vec (чанки+сущности) + заметки |
| `fetch_meeting_dfn3.py` | Download DeepFilterNet3 browser assets. Live meeting only, not lab ASR. |
| `fix_conflict_links.py` | Разовая чистка конфликтов после исправления ошибок графа (docs/plans/CONFLICT-AUTONOMY-PLAN.md, этап 1). |
| `health_report.py` | Отчёт о здоровье системы (пункт плана 5.2). Только чтение логов и БД. |
| `index_doctor.py` | Диагностика индекса проекта: всё ли в порядке и что чинить. Только чтение. |
| `index_project_map.py` | Индексатор проекта (карта, код Python/JS/шаблонов/CSS, документы, символы, роуты, БД, окружение, история). |
| `inspect_project_index.py` | Сверка векторного индекса проекта: чанки ↔ sqlite-vec ↔ lab _final.txt. |
| `install_caddy.ps1` | SKKKK — Caddy как HTTPS-прокси перед порталом (запуск от администратора) |
| `install_hooks.ps1` | Установить (или снять) git-хуки, которые после коммита и слияния обновляют индекс проекта. |
| `install_meeting_voice.py` | Isolated CPU venv for the LiveKit DeepFilterNet cleaner. Not the app venv. |
| `install_services.ps1` | SKKKK — установка служб Windows через NSSM (запуск от администратора) |
| `kb_inbox.py` | Ящик вопросов внешнего агента из командной строки (для оператора/Claude на сервере, без входа по паролю). |
| `lightrag_doctor.py` | Проверка LightRAG: Docker, Ollama embed, LLM, inputs. |
| `lightrag_index.py` | Загрузить docs в LightRAG (POST /documents/text) и дождаться pipeline. |
| `lightrag_query.py` | Запрос к LightRAG по карте SKKKK. |
| `lightrag_sync_docs.py` | Скопировать документацию SKKKK в LightRAG inputs для /documents/scan. |
| `link_voice_profiles.py` | Привязка слепков голоса к карточкам сотрудников — только по подтверждению, ничего не привязывается само. |
| `live_meetings.py` | Печатает идущие встречи и завершается кодом 0, если их нет, и 3, если есть. |
| `make_hashes.py` | Print bcrypt hashes for the three role passwords (paste into .env). |
| `material_dates.py` | Даты материалов проекта: откуда взята дата записи (docs/plans/REQUIREMENTS-PLAN.md, этап 1). |
| `monitor_skkkk_loop.ps1` | SKKKK — автоподъём сервера раз в 5 мин по /health (УСТАРЕЛ: проверяет http на :777, где теперь HTTPS; не запускать, см. PROJECT-MAP §3.0) |
| `monitor_status.py` | Лёгкий монитор SKKKK: health, очередь, ошибки job — пишет в data/logs/monitor_loop.log. |
| `personal_password.py` | Личные пароли из командной строки — на случай, когда портал запущен службой, а не из PyCharm. |
| `qa_all.py` | Единая команда QA (пункт плана 5.5): одна команда — одна картина «стабильно ли продукт». |
| `qa_analysis.py` | Сквозная проверка «созвон → запись → анализ» без людей, на настоящей русской речи. |
| `qa_call_stress.py` | Стресс-проверка записи звонка на РЕАЛЬНОМ сервере (рекордеры, LiveKit): песочный проект SMOKE_PROJECT_ID (29), два браузерных гостя. |
| `qa_flows.py` | Интерактивные сценарии по всему порталу в браузере (изолированный экземпляр с копией базы, как в qa_pages.py; боевой сервер не затрагивается |
| `qa_media_path.py` | Путь звука и видео созвона в настоящем браузере: от издателя через LiveKit и шумодав `skkkk-dfn` к слушателю. |
| `qa_meeting_lifecycle.py` | Жизненный цикл встречи на живом сервере: создание, запуск, ссылка, вход и выход, завершение, удаление (в том числе без запуска |
| `qa_mobile_android.py` | Проверка комнаты на настоящем Chrome для Android: эмулятор Pixel 7 (Android 14, папка tools/android) или телефон по USB с отладкой. |
| `qa_mobile_webkit.py` | Проверка гостевого входа и комнаты на мобильных профилях: WebKit (движок Safari) для iPhone/iPad и Chromium для Android-профилей. |
| `qa_navbar.py` | Проверка шапки: на разных экранах шапка не шире экрана, не выше 2 строк на телефоне, проект обрезается многоточием, ничего не наезжает. |
| `qa_pages.py` | Аудит вёрстки страниц портала на разных экранах и движках: переполнение по ширине, наезд элементов, размеры кнопок и полей, мелкий шрифт, |
| `qa_publish_wav.py` | Публикует wav-файл как микрофон участника в комнату LiveKit (для проверки записи и анализа без людей). |
| `qa_room.py` | Проверка комнаты созвона в настоящем браузере (Chromium + фейковые камера/микрофон), без паролей: встреча создаётся кодом приложения |
| `qa_room_ui.py` | Комната созвона: раскладка на телефоне при повороте, пульт и окна, устойчивость индикаторов и камеры (живой LiveKit, песочница). |
| `rebuild_artifact_report.py` | Пересобрать reports/transcript.html для артефакта: Sortformer + match + новый UI. |
| `redescribe_screen_frames.py` | Переописать кадры шэринга экрана прогона vision-моделью и пересобрать прод-HTML. |
| `reextract_project.py` | Переизвлечь требования проекта по новой схеме (docs/plans/REQUIREMENTS-PLAN.md, этап 9). Без --apply — только отчёт. |
| `refresh_portal_knowledge.py` | Пересобрать выжимки созвонов, индекс, сущности и ТЗ по проекту(ам). |
| `reprocess_documents.py` | Переобработка загруженных документов новым извлечением таблиц (docs/plans/DOCUMENT-TABLES-PLAN.md, шаг 8). |
| `rerun_cursor_merge.py` | Пересклейка merge + view для артефакта (Cursor, без ASR и без Gemma). |
| `rerun_transcript_integrity.py` | Пересчёт global integrity + plain без повторного LLM-merge. |
| `restart.ps1` | SKKKK — безопасный перезапуск: остановка -> проверки зависимостей -> старт -> ожидание /health |
| `resume_knowledge.py` | Resume knowledge pipeline for an artifact with finished ASR/transcript. |
| `review_project.py` | Разобрать проект локальной моделью целиком: пусть система решит всё, что может, и оставит человеку только спорное. |
| `run_all.ps1` | SKKKK — весь проект одной командой (LLM из .env) |
| `run_api.ps1` | SKKKK — только API (LLM из .env) |
| `run_worker.ps1` | SKKKK — GPU-воркер очереди |
| `search_project_map.py` | Поиск по индексу проекта: смысл + точные имена + связи. Каждый ответ — с адресом `путь:строка`. |
| `setup_env.ps1` | Установка окружения проекта после переноса папки на другой ПК (или с нуля). |
| `smoke_cycle.py` | Прогон полного цикла через HTTP API: вход, проект, загрузка, обработка. |
| `start-cursor.ps1` | — |
| `start-gemma.ps1` | — |
| `start-saved.ps1` | — |
| `start.ps1` | SKKKK — запуск API + GPU-воркер с выбором LLM |
| `stop.ps1` | SKKKK — остановить API и GPU-воркер |
| `user_profile.py` | Карточки пользователей из командной строки — когда портал запущен службой, а не из PyCharm. |
| `verify_backup.py` | Проверка бэкапа БД (пункт плана 5.6): резервная копия целая и её можно восстановить. |
| `watch_server.py` | Watch SKKKK server, queue, and pipeline errors (stdout + monitor_loop.log). |

**`scripts/smoke/`**

| Файл | Назначение |
|---|---|
| `common.py` | Общие настройки смоук-набора. Всё берётся из переменных окружения — секретов в коде нет. |
| `run_all.py` | Запуск всего смоук-набора. Код выхода 0 — всё зелёное (SKIP не считается провалом). |
| `smoke_agent_cards.py` | Карточки обмена с агентами на странице проекта (песочница): раскрываются, грузят данные, нет ошибок консоли и горизонтального скролла; |
| `smoke_agent_exchange.py` | Обмен с внешним агентом (шлюз + MCP-контракт) на живом сервере, только песочница: ключ создаётся и отзывается. |
| `smoke_console.py` | Нет ошибок консоли и упавших запросов на основных страницах (пункт 3.6). |
| `smoke_counters.py` | Согласованность счётчиков проекта (пункт 3.16): Док. + Созвоны = знаменатель «Готово» = число строк таблицы = ответ API. |
| `smoke_delete_live_guest.py` | Удаление встречи при подключённом госте (находка аудита 4.9): гость покидает комнату, запись не возобновляется. |
| `smoke_double_click.py` | Двойной клик = один запрос (пункт 3.18): «Создать встречу» и «Пересобрать» ТЗ. Только песочница. |
| `smoke_duplicate_tab.py` | Дубль-вкладка сотрудника (пункт 2.6): первая вкладка получает понятную причину и заблокированные кнопки. |
| `smoke_end_confirm.py` | «Завершить для всех» (пункт 3.13): своё окно подтверждения; «Отмена» встречу не завершает, «Завершить» — завершает. |
| `smoke_flow.py` | Песочница: загрузка документа -> встреча -> гость и сотрудник входят (API). |
| `smoke_forms.py` | Формы (пункт 3.18): вход по Enter; название проекта из пробелов отклоняется по-русски и ничего не создаёт. |
| `smoke_guest_lobby.py` | Гостевой вход (пункты 3.3/3.12): кнопка «Войти во встречу» видна без прокрутки на типовых размерах. |
| `smoke_perm.py` | Роли на одной учётке не перебивают друг друга (пункт 1.2). |
| `smoke_presence.py` | Presence симметричен (пункт 1.1): у сотрудника есть heartbeat и leave. |
| `smoke_room.py` | Комната: два участника, завершение встречи замечается, кнопка смены камеры (браузер). |
| `smoke_stable_dom.py` | Автообновление не перестраивает DOM без изменений (пункт 3.7): узел кнопки встречи живёт дольше двух опросов. |
| `smoke_tiles.py` | Плитки комнаты (пункт 3.11): N участников × размеры окна — нет пересечений, выходов за экран и перекрытия панелью. |
| `smoke_widths_a11y.py` | Планшетные ширины и базовая доступность (пункты 4.2, 4.8): нет горизонтального скролла; у кнопок/ссылок/полей есть имя. |

**`tools/`**

| Файл | Назначение |
|---|---|
| `_monitor_snapshot.py` | — |
| `projindex_mcp.py` | Локальный MCP-сервер индекса проекта (stdio): поиск по коду, связям, БД, окружению и истории. Только чтение. |

**`tools/livekit/`**

| Файл | Назначение |
|---|---|
| `ensure_livekit.py` | CLI: write LiveKit keys into .env and tools/livekit/livekit.yaml. |
| `screen_recorder.py` | CPU screen-share recorder for live rooms. Not DFN. Not ASR. Not camera. |
| `voice_cleaner.py` | CPU DeepFilterNet for live rooms. Not lab ASR. Not GPU. |

<!-- generated:end registry -->

---

## 14. Как поддерживать карту

1. **Меняешь модуль** → обнови §7/§8 и `docs/project-map/manifest.yaml` (node + edges).
2. **Новая кнопка / страница UI** → §5 (соответствующий подраздел) и при необходимости §5.13; для нетривиального layout — node `ui_pattern` в `manifest.yaml`.
3. **Новый API** → §5.1 + §5.9.
4. **Новый файл в data/** → §6.
5. **Версии моделей, кодеков, пакетов** → §1.1; SHA256 лаборатории → `asr/lab/FREEZE.md`. Не менять молча.
6. После архитектурных изменений → `venv\Scripts\python.exe scripts\index_project_map.py` (нужен Ollama bge-m3). Граф агентов: `lightrag_sync_docs.py` + `lightrag_index.py --wait`.
7. Агенты: правило `.cursor/rules/07-project-map.mdc`.

---

*Дополнение 2026-09-21: обмен с агентом, подтверждение требований, новые сервисы и скрипты QA (§5.1, §7, §10); фронт: `static/px_agent.js` + `px_agent.css` + `templates/partials/px_agent_cards.html` (карточки обмена на странице проекта), `static/errors_ru.js` + `templates/error.html` (русские ошибки), `static/design.css` (дизайн-система). БД: пул 10+20, таймаут 15 с, при исчерпании стеки держателей в `api.log` (`db.describe_pool_holders`). Комната: иконки выбора динамика и микрофона в `meeting_room.js` / шаблонах комнаты.*

*Последнее обновление карты: 2026-09-17 — легализованы рассуждения локальной LLM (подготовка к новому Swift-Qwen 27B): из `llm._lmstudio_chat_body` убраны хардкоды `chat_template_kwargs.enable_thinking=False` и `reasoning_effort="none"`; добавлены настройки `LMSTUDIO_ENABLE_THINKING` (default `true`) и `LMSTUDIO_REASONING_EFFORT` (default `medium`) в `config.Settings`, а `chat_completion`/`chat_from_env`/`lmstudio_chat_raw` резолвят их и передают `enable_thinking` + `reasoning_effort` (при `enable_thinking=false` усилие принудительно `none`). Сверено с сервером LM Studio: `reasoning_effort` принимает только `none/minimal/low/medium/high/xhigh`, иначе HTTP 400 (а не молчаливое отбрасывание); сервер сам маппит `none→off`, `minimal→low`, `xhigh→high`. Инфраструктура запуска (вне репозитория): пресет `%USERPROFILE%\.lmstudio\config-presets\swift-qwen-24gb.preset.json` (flash-attn, KV q8_0, draft-MTP, 1 слот, `enableThinking=true`) + `run_server.ps1` в корне проекта — в этой сборке `lms load` не умеет `--preset`/`--limit-slots`, поэтому поля пресета раскладываются в `user-concrete-model-default-config` конкретной модели, а один слот задаётся `--parallel 1`. Тесты: `app/app/tests/test_llm_provider.py` (16 passed). Ранее: иерархическая пересборка ТЗ: `tz._build_tz_body` при переполнении контекста LM Studio (`n_keep > n_ctx`) автоматически группирует черновики в промежуточные merge-вызовы (Проход 1.5, `tz.merge` в логах, ~9000 tok на группу) до тех пор, пока сводные черновики не укладываются в budget финального reduce; промпт `rebuild_tz.md` расширен до 6 блоков, `_parse_prompts` возвращает 6 кортежей. Исправлено: сборки с >73 чанками (напр. проект 18 с 76) раньше падали с `LM Studio HTTP 400 … n_keep >= n_ctx` — теперь проходят до конца. Ранее: фикс warning LM Studio «Reasoning setting 'off' is not a valid option» (`llm._lmstudio_chat_body`): невалидные значения маппились в `low`, а мышление глушилось `chat_template_kwargs.enable_thinking=False` (**отменено 2026-09-17, см. выше** — «none» в этой сборке валиден, а невалидное значение даёт 400); в лог `lmstudio.chat` добавлены `reasoning_tokens` и `has_reasoning` — теперь видно, если модель реально начала выдавать `<thinking>`. Ранее: приклейка кадров экрана в стенограмме (`transcript_view._words_html`): кнопка **▣** теперь ставится **перед** блоком слов (на границе момента кадра, фразы не разрываются), а кадры, в окне которых нет слов, получают отдельную кнопку перед первым словом после момента кадра (`turn_hits`); пересобраны все стенограммы с кадрами (2026-09-01 ×2, 2026-09-16 ×2, 2026-09-17). Ранее: фикс `scene_times()` (`-fps_mode vfr` вместо удалённого в ffmpeg 9.0 `-vsync vfr`): кадры экрана снова ложатся на реальные смены сцены, а не «пол 15 с»; `_dotenv_value()` даёт `screen_vision_enabled()`/`frames_max()` fallback на `.env`; `redescribe_existing_frames()` пропускает vision-кадры; авто-vision в `pipeline_meeting`; полный пересбор созвонов 2026-09-16 (jobs 74/75) — кадры vision в проде и чанках БД. Экран в стенограмме: hover pop-up заменён на кнопку **▣** в конце блока/реплики → модалка со скриншотом кадра (`frame_url`, эндпоинт `/lab/{run_id}/frame/{relative}`, права уровня отчёта) и сворачиваемым `<details>` с текстом описания.*


## Архив разработки

`_archive/` — файлы и код, оставшиеся от разработки и не используемые рабочим контуром (модели и окружения экспериментов, разовые скрипты, логи). Перенесены без удаления; список и способ возврата — в [`_archive/README.md`](../_archive/README.md).
