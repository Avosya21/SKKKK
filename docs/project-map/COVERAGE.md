# Охват карты проекта

Сгенерировано `venv\Scripts\python.exe scripts\projectmap.py report` (2026-10-06 02:36). Не править руками.

**Что это.** Для каждого слоя карты инструмент `projectmap` отдельно находит, что есть в проекте, и считает, сколько из этого
есть в графе зависимостей (`data/project_map_graph.json`). Это измеримая полнота карты: цели заданы в `.projectmap/config.yaml`,
осознанные исключения (сторонний код, данные, архив) — там же, с причинами. Строка ✗ — охват ниже цели.
Подробности и план закрытия пробелов — `docs/plans/PROJECTMAP-PLAN.md`.

| Слой | Охват | Ниже цели |
|---|---|---|
| Структура | 100% | 0 |
| Интерфейсы | 89% | 0 |
| Данные | 100% | 0 |
| Настройки | 61% | 0 |
| Тесты | 64% | 0 |
| История | 95% | 0 |
| Смысл | 91% | 0 |
| Метрики | 100% | 0 |
| Среда | 100% | 0 |

| Слой | Проба | Есть в проекте | В графе | Охват | Цель | |
|---|---|---|---|---|---|---|
| Структура | Файлы проекта в графе | 783 | 783 | 100% | 97% | ✓ |
| Структура | Функции в скриптах оболочки в графе | 26 | 26 | 100% | 90% | ✓ |
| Интерфейсы | Интерактивные элементы связаны с обработчиком или API | 196 | 174 | 89% | 85% | ✓ |
| Данные | Таблицы с известным читателем или писателем | 34 | 34 | 100% | 100% | ✓ |
| Настройки | Читаемые кодом переменные окружения описаны в образце | 93 | 71 | 76% | 75% | ✓ |
| Настройки | Описанные в образце переменные читаются кодом | 154 | 71 | 46% | — | ✓ |
| Тесты | Файлы кода с определениями, которые покрывает хотя бы один тест | 377 | 243 | 64% | 63% | ✓ |
| История | Коммиты проекта в графе | 183 | 174 | 95% | 90% | ✓ |
| Смысл | Ссылки документов на файлы ведут на существующее | 908 | 870 | 96% | 95% | ✓ |
| Смысл | Ссылки документ → файл есть в графе | 765 | 756 | 99% | 90% | ✓ |
| Смысл | Сущности графа (роуты, таблицы, переменные, сервисы) упомянуты в карте | 389 | 308 | 79% | 75% | ✓ |
| Смысл | Строки карты с вписанным описанием | 0 | 0 | — | — | · |
| Метрики | Файлы кода с метриками (fan-in/out, группа) | 584 | 584 | 100% | 95% | ✓ |
| Среда | Сервисы compose-файлов в графе | 5 | 5 | 100% | 90% | ✓ |
| Среда | Запуски «скрипт → скрипт» в графе | 108 | 108 | 100% | 70% | ✓ |

**Примеры пропусков**

- Ссылки документов на файлы ведут на существующее: `AUDIT_REPORT.md → app/app/services/transcript_view.py`, `AUDIT_REPORT.md → app/app/static/chat.js`, `AUDIT_REPORT.md → app/app/tests/test_transcript_view.py`, `AUDIT_REPORT.md → app/app/timeutil.py`, `AUDIT_REPORT.md → scripts/db_orphans.py`, `docs/AGENT1C-BRIDGE.md → scripts/run.ps1`, `docs/DECISIONS.md → asr/call_summary.md`, `docs/DECISIONS.md → asr/docker_gigaam_speakers.json` и ещё 30 (пропуск = битая ссылка)
- Ссылки документ → файл есть в графе: `docs/OPERATIONS.md → docs/eval/index_questions.json`, `docs/PROJECT-MAP.md → _archive/README.md`, `docs/PROJECT-MAP.md → docs/PROJECT-MAP.md`, `docs/PROJECT-MAP.md → docs/eval/index_questions.json`, `docs/PROJECT-MAP.md → docs/eval/index_questions_holdout.json`, `docs/PROJECT-MAP.md → scripts/qa_data_speech_a.wav`, `docs/PROJECT-MAP.md → scripts/qa_data_speech_b.wav`, `docs/plans/PROJECT-INDEX-PLAN.md → docs/eval/index_questions.json` и ещё 1
- Интерактивные элементы связаны с обработчиком или API: `ui:app/app/templates/admin_users.html#f-from`, `ui:app/app/templates/admin_users.html#f-q`, `ui:app/app/templates/admin_users.html#f-to`, `ui:app/app/templates/base.html#me-submit`, `ui:app/app/templates/base.html#password-submit`, `ui:app/app/templates/index.html#project-cancel`, `ui:app/app/templates/index.html#project-submit`, `ui:app/app/templates/login.html#login-name` и ещё 14
- Читаемые кодом переменные окружения описаны в образце: `env:ALLOW_LEGACY_ASR`, `env:CREDENTIALS_DB_PATH`, `env:FFMPEG_BIN`, `env:HF_HUB_DOWNLOAD_TIMEOUT`, `env:HF_HUB_OFFLINE`, `env:JOB_MAX_ATTEMPTS`, `env:LIGHTRAG_URL`, `env:LIVEKIT_HTTP_INTERNAL` и ещё 14
- Описанные в образце переменные читаются кодом: `env:API_HOST`, `env:API_KEYS_FILE`, `env:API_PORT`, `env:ASR_BACKEND`, `env:ASR_MODEL`, `env:ASR_VAD_DEVICE`, `env:AUDIO_PREPROCESSING_MODE`, `env:DIARIZATION_MAX_SPEAKERS` и ещё 75 (пропуск = переменная описана, но код её не читает (или читает другой язык вне разбора))
- Файлы кода с определениями, которые покрывает хотя бы один тест: `app/app/cursor_compat.py`, `app/app/logging_setup.py`, `app/app/models/project_mcp_token.py`, `app/app/models/project_member.py`, `app/app/routers/acme.py`, `app/app/schemas/artifact.py`, `app/app/schemas/auth.py`, `app/app/schemas/project.py` и ещё 126
- Коммиты проекта в графе: `289dc014`, `2dd38584`, `2e9978f1`, `3ace60ba`, `43d3903d`, `53acb24c`, `7d35d632`, `bfbb417f` и ещё 1 (коммиты вне разобранных файлов в граф не попадают)
- Сущности графа (роуты, таблицы, переменные, сервисы) упомянуты в карте: `GET /projects/{project_id}/agent-exchange`, `GET /projects/{project_id}/agent-settings`, `GET /projects/{project_id}/agent-verdicts`, `PUT /projects/{project_id}/agent-settings`, `env:API_HOST`, `env:API_KEYS_FILE`, `env:API_PORT`, `env:ASR_BACKEND` и ещё 73 (роут считается упомянутым, если в карте есть его адрес целиком, с любыми именами параметров, или хвост адреса после двух первых сегментов)
