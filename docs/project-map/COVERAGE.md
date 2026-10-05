# Охват карты проекта

Сгенерировано `venv\Scripts\python.exe scripts\projectmap.py report` (2026-10-05 18:52). Не править руками.

**Что это.** Для каждого слоя карты инструмент `projectmap` отдельно находит, что есть в проекте, и считает, сколько из этого
есть в графе зависимостей (`data/project_map_graph.json`). Это измеримая полнота карты: цели заданы в `.projectmap/config.yaml`,
осознанные исключения (сторонний код, данные, архив) — там же, с причинами. Строка ✗ — охват ниже цели.
Подробности и план закрытия пробелов — `docs/plans/PROJECTMAP-PLAN.md`.

| Слой | Охват | Ниже цели |
|---|---|---|
| Структура | 47% | 2 |
| Интерфейсы | 89% | 1 |
| Данные | 100% | 0 |
| Настройки | 100% | 0 |
| Тесты | 71% | 0 |
| История | 98% | 0 |
| Смысл | 48% | 2 |
| Метрики | 0% | 1 |
| Среда | 8% | 2 |

| Слой | Проба | Есть в проекте | В графе | Охват | Цель | |
|---|---|---|---|---|---|---|
| Структура | Файлы проекта в графе | 720 | 677 | 94% | 97% | ✗ |
| Структура | Функции в скриптах оболочки в графе | 26 | 0 | 0% | 90% | ✗ |
| Интерфейсы | Элементы интерфейса связаны с обработчиком или API | 406 | 362 | 89% | 90% | ✗ |
| Данные | Таблицы с известным читателем или писателем | 33 | 33 | 100% | 100% | ✓ |
| Настройки | Переменные окружения с известным читателем в коде | 136 | 136 | 100% | 100% | ✓ |
| Тесты | Файлы кода с определениями, которые покрывает хотя бы один тест | 332 | 236 | 71% | 70% | ✓ |
| История | Коммиты проекта в графе | 169 | 165 | 98% | 95% | ✓ |
| Смысл | Ссылки документов на файлы ведут на существующее | 799 | 761 | 95% | 97% | ✗ |
| Смысл | Ссылки документ → файл есть в графе | 761 | 0 | 0% | 90% | ✗ |
| Метрики | Файлы с метриками (группа, fan-in/out) | 703 | 0 | 0% | 95% | ✗ |
| Среда | Сервисы compose-файлов в графе | 5 | 0 | 0% | 90% | ✗ |
| Среда | Запуски «скрипт → скрипт» в графе | 115 | 18 | 16% | 70% | ✗ |

**Примеры пропусков**

- Файлы проекта в графе: `.claude/launch.json`, `.gitignore`, `.mcp.json`, `AUDIT_REPORT.md`, `Companies.example.txt`, `INSTALL.bat`, `INSTALL.md`, `Login_up.example.txt` и ещё 35 (осознанно исключено конфигом: 308)
- Функции в скриптах оболочки в графе: `run_server.ps1::Get-LmsPath`, `scripts/bootstrap.ps1::Confirm-Step`, `scripts/bootstrap.ps1::Find-Python310`, `scripts/bootstrap.ps1::Have`, `scripts/bootstrap.ps1::Refresh-Path`, `scripts/bootstrap.ps1::Say`, `scripts/bootstrap.ps1::Winget-Install`, `scripts/check_env.ps1::Add-Check` и ещё 18
- Сервисы compose-файлов в графе: `asr/docker-compose.yml::gigaam`, `asr/docker-compose.yml::tone`, `asr/docker-compose.yml::whisper`, `tools/lightrag/docker-compose.yml::lightrag`, `tools/livekit/docker-compose.yml::livekit`
- Запуски «скрипт → скрипт» в графе: `app/app/config.py → tools/livekit/voice_cleaner.py`, `app/app/models/speaker_profile.py → app/app/services/voice_link.py`, `app/app/preflight.py → scripts/install_meeting_voice.py`, `app/app/routers/admin_map.py → scripts/index_project_map.py`, `app/app/routers/admin_map_graph.py → scripts/export_project_graph.py`, `app/app/routers/admin_map_graph.py → scripts/index_project_map.py`, `app/app/routers/people.py → scripts/user_profile.py`, `app/app/routers/project_people.py → scripts/people_voice.py` и ещё 89
- Ссылки документов на файлы ведут на существующее: `AUDIT_REPORT.md → app/app/services/transcript_view.py`, `AUDIT_REPORT.md → app/app/static/chat.js`, `AUDIT_REPORT.md → app/app/tests/test_transcript_view.py`, `AUDIT_REPORT.md → app/app/timeutil.py`, `AUDIT_REPORT.md → scripts/db_orphans.py`, `docs/AGENT1C-BRIDGE.md → scripts/run.ps1`, `docs/DECISIONS.md → asr/call_summary.md`, `docs/DECISIONS.md → asr/docker_gigaam_speakers.json` и ещё 30 (пропуск = битая ссылка)
- Ссылки документ → файл есть в графе: `AGENTS.md → .cursor/rules`, `AGENTS.md → .cursor/skills`, `AGENTS.md → app/app/changelog.json`, `AGENTS.md → app/app/tests/test_map_coverage.py`, `AGENTS.md → docs/DECISIONS.md`, `AGENTS.md → docs/PLAN.md`, `AGENTS.md → docs/PROJECT-MAP.md`, `AGENTS.md → docs/TESTING.md` и ещё 192
- Элементы интерфейса связаны с обработчиком или API: `ui:au-users`, `ui:bg-panel`, `ui:changelog-title`, `ui:changelogModal`, `ui:checks-title`, `ui:cy`, `ui:deadlines-table`, `ui:delete-artifact-title` и ещё 36
- Файлы кода с определениями, которые покрывает хотя бы один тест: `app/app/cursor_compat.py`, `app/app/models/project_mcp_token.py`, `app/app/models/project_member.py`, `app/app/routers/acme.py`, `app/app/schemas/artifact.py`, `app/app/schemas/auth.py`, `app/app/schemas/project.py`, `app/app/services/acme_challenge.py` и ещё 88
- Коммиты проекта в графе: `593cadc8`, `88c8629e`, `e5b3913f`, `eb174f51` (коммиты вне разобранных файлов в граф не попадают)
- Файлы с метриками (группа, fan-in/out): `.cursor/rules/00-project-home.mdc`, `.cursor/rules/01-nonnegotiables.mdc`, `.cursor/rules/02-stack.mdc`, `.cursor/rules/03-python.mdc`, `.cursor/rules/04-fastapi-backend.mdc`, `.cursor/rules/05-python-tests.mdc`, `.cursor/rules/06-frontend-ui.mdc`, `.cursor/rules/07-project-map.mdc` и ещё 192
