# Тестирование SKKKK

## Принцип

Тестируем то, что ломается: права доступа, состояния очереди, границы окон, изоляцию проектов, поведение при сбое модели. Не тестируем формулировки, которые выдаёт LLM, — для них проверки эвристические и регрессионные, а не точное сравнение строк.

## Уровни

| Уровень | Что покрывает | Скорость | Маркер |
|---|---|---|---|
| Модульные | Чанкинг, определение типов, нарезка окон, правила конфликтов | Секунды | нет |
| API | Роли, статусы, загрузка, изоляция проектов | Секунды | нет |
| Интеграционные без GPU | Конвейер при `DRY_RUN=1`, очередь, ошибки | Секунды | нет |
| Интеграционные с GPU | Двойной слух (Parakeet + GigaAM + диаризация Nemotron 3 / Sortformer), сущности, ТЗ, чат | Минуты | `gpu` |
| Нагрузочные | Пакет аудио, большой граф, длинный документ | Десятки минут | `slow` |

## Запуск

```powershell
cd C:\1C_NEW_SKKKK
venv\Scripts\activate

pytest app -m "not gpu and not slow" -q     # обычный цикл: параллельно (pytest-xdist, -n auto)
pytest app -m gpu -n 0 -q                   # GPU — без параллелизма (одна видеокарта)
pytest app -m slow -n 0 -q                  # длинные тесты — последовательно
pytest app -q                               # приёмка целиком
pytest app -n 0 -q                          # весь suite последовательно (отладка)
```

### Ускорение (Trail of Bits / PyPI Warehouse)

| Приём | Где | Зачем |
|---|---|---|
| **pytest-xdist** `-n auto` | `pytest.ini` → `addopts` | параллельный прогон на всех ядрах |
| **testpaths** | `pytest.ini` | быстрый collection, без лишних каталогов |
| **Фиксированные bcrypt-хеши** | `app/app/tests/conftest.py` | не считать salt при каждом старте pytest |
| **loadscope** | `pytest.ini` | тесты одного модуля — на одном worker |
| **COVERAGE_CORE=sysmon** | env (Python 3.12+) | быстрее coverage.py при `pytest --cov` |

Изоляция БД уже совместима с xdist: каждый тест получает свой `tmp_path` и SQLite-файл.

При coverage с xdist понадобится `sitecustomize.py` + `coverage process_startup()` — см. [документацию coverage](https://coverage.readthedocs.io/en/latest/subprocess.html).

## Структура

```
app\app\tests\
├── conftest.py
├── fixtures\
│   ├── short_call.wav          ~3 минуты русской речи
│   ├── long_call.wav           60+ минут (не в git, путь через переменную окружения)
│   ├── silence.wav
│   ├── text.pdf  scan.pdf  mixed.pdf  table.docx  screenshot.png  note.txt
│   └── qa_regression.yaml      эталонные вопросы и ожидаемые факты
├── test_auth.py  test_projects.py
├── test_upload.py  test_queue.py
├── test_audio_prep.py  test_asr_ensemble.py  test_merge.py
├── test_documents.py  test_normalization.py
├── test_entities.py  test_index.py
├── test_graph.py  test_notes.py  test_conflicts.py
├── test_tz.py  test_chat.py
├── test_security.py  test_resilience.py  test_backup.py
└── test_migration.py           (этап 10)
```

Большие аудиофайлы в git не хранятся. Путь задаётся переменной окружения, при её отсутствии тесты помечаются как пропущенные.

## Фикстуры

`conftest.py` предоставляет: временную БД, временный `DATA_DIR`, `anon_client` (без логина), `cookie_auth_client` (после `/auth/login` в cookie `skkkk_session`), токены трёх ролей и `auth_headers` (Bearer), клиента API, фабрику проектов и артефактов, поддельные ответы LM Studio и Ollama. База личных паролей (`credentials_db`) в каждом тесте — временный файл (`_isolated_credentials_db`), боевая `data/credentials.db` недоступна; по умолчанию в тестах `PERSONAL_PASSWORD=off` (conftest), включается `monkeypatch.setenv("PERSONAL_PASSWORD", "required")` + `get_settings.cache_clear()`; компании — временный `Companies.txt` (`ACME | Исполнитель`, `Заказчик Тест | Заказчик`), фото — `DATA_DIR/people`. Смоук против боевого сервера: `SMOKE_PERSONAL_PW` — личный пароль смоук-логина, его пароль и карточку один раз заводят в браузере (иначе код 77). Login также кладёт тот же JWT в HttpOnly cookie; тесты, которым нужен только Bearer, делают `client.cookies.clear()`.

## Проверка ответов LLM

Точное сравнение текста бессмысленно. Используются:

1. **Структурные проверки** — валидность JSON, наличие всех разделов ТЗ, наличие ссылок на источники в ответе чата.
2. **Проверки на выдумки** — доля пятиграмм финальной стенограммы, отсутствующих во всех трёх черновиках, не выше порога; числа и даты исходника присутствуют в нормализованном тексте.
3. **Регрессия по эталонам** — `qa_regression.yaml`: набор вопросов и фактов, которые обязаны присутствовать в ответе. Прогоняется после каждого изменения промптов.
4. **Отказ от ответа** — на вопрос без данных ответ обязан содержать признание незнания.
5. **«Кто и когда» в чате** — `scripts/eval_chat.py --questions docs/eval/chat_questions_provenance.json` на проекте с заново загруженным созвоном (имя + таймкод в ответе, ловушки — отказ).
6. **Автопроверка моделью** — `scripts/eval_auto_review.py --project N [--scope conflicts]`: теневой прогон на копии БД (боевая очередь должна быть пуста, модель загружена), скорость и CSV из 60 вердиктов; человек ставит «да/нет», `--score файл.csv` считает ложные подтверждения (≤ 5 %), ложные отклонения (≤ 2 %) и долю «не уверена» (≤ 25 %). `live` включается только после этого. Тесты без модели: `test_auto_review.py`, `test_review_requirements.py`, `test_review_conflicts.py`, `test_speaker_auto.py` (LLM подменяется, проверяется и то, что соединение БД не держится на время ответа).

## Обязательные проверки безопасности

Прогоняются на каждом этапе начиная с первого, а не только на восьмом:

- без сессии: `/login` и `GET /health` публичны; HTML-оболочки 302 на `/login`; `/health/ready` и API — 401;
- `test_perimeter.py` и `test_user_scenarios.py` фиксируют периметр и 30 пользовательских потоков;
- пользователь не получает данные чужого проекта в поиске, графе, чате, скачивании;
- эскалация роли через подбор пароля невозможна;
- файлы недоступны в обход прав;
- пользовательское имя файла не влияет на путь на диске.

## Что считается непройденным этапом

Этап не закрыт, если:

- хотя бы один тест соответствующего этапа красный;
- пункт «Критерий готовности» не отмечен;
- ручная проверка из таблицы этапа даёт результат, отличный от ожидаемого.

Обходные пути в духе «пока пропустим тест ансамбля» недопустимы: именно эти тесты защищают инварианты из `DECISIONS.md`.

## Базовый прогон 2026-09-18 (пункт плана 0.2)

- Полный набор: **633 passed** (2–3 мин).
- Каждый файл отдельно (`-n0`): **84 файла, все проходят, сумма 633**.

## Что нашли и исправили при прогоне

- **Файлы падали в одиночку** (`ModuleNotFoundError: No module named 'app.app'`): у `app/app/tests` есть `__init__.py`, режим импорта `prepend` добавлял в `sys.path` папку `app/`, и `app` указывал на внутренний пакет. В полном прогоне порядок импортов это скрывал. Исправлено: `--import-mode=importlib` в `pytest.ini`.
- **Тест затирал боевой `.env`**: `test_llm_settings.py::test_resolve_llm_provider_cli` подменял `bootstrap.ENV_PATH`, но не `llm_settings.ENV_PATH`, и переписывал `LLM_PROVIDER` в реальном `.env`. Исправлено: тест работает во временной папке.

## Таблица: файл, секунды, итог (по одному)

| Файл | с | Итог |
|---|---|---|
| test_acme_challenge.py | 4 | 2 passed |
| test_artifact_actions.py | 5 | 4 passed |
| test_artifact_audio.py | 6 | 6 passed |
| test_artifact_download.py | 6 | 5 passed |
| test_artifact_purge.py | 18 | 6 passed |
| test_artifact_reports.py | 3 | 3 passed |
| test_audio_playback.py | 16 | 8 passed |
| test_auth.py | 14 | 7 passed |
| test_bootstrap.py | 15 | 10 passed |
| test_call_summary_prompt.py | 3 | 4 passed |
| test_chat.py | 27 | 13 passed |
| test_docker_runtime.py | 3 | 13 passed |
| test_documents.py | 2 | 8 passed |
| test_edge_runtime.py | 2 | 12 passed |
| test_entities.py | 2 | 2 passed |
| test_entities_normalize.py | 3 | 9 passed |
| test_entity_vectors.py | 15 | 8 passed |
| test_external_base_url.py | 2 | 2 passed |
| test_ffmpeg_tools.py | 2 | 1 passed |
| test_gpu_phase.py | 3 | 10 passed |
| test_graph.py | 5 | 4 passed |
| test_health.py | 10 | 3 passed |
| test_image_vision.py | 3 | 6 passed |
| test_index.py | 5 | 7 passed |
| test_lab_enroll.py | 10 | 5 passed |
| test_lab_errors.py | 2 | 5 passed |
| test_lab_report.py | 24 | 12 passed |
| test_livekit_env.py | 2 | 4 passed |
| test_llm_labels.py | 2 | 5 passed |
| test_llm_provider.py | 2 | 16 passed |
| test_llm_settings.py | 3 | 8 passed |
| test_login_allowlist.py | 6 | 6 passed |
| test_login_personal.py | 17 | 17 passed |
| test_personal_password.py | 17 | 17 passed |
| test_personal_password_console.py | 8 | 8 passed |
| test_user_profile.py | 19 | 19 passed |
| test_avatars.py | 13 | 13 passed |
| test_login_profile.py | 16 | 16 passed |
| test_user_profile_console.py | 6 | 6 passed |
| test_meeting_avatars.py | 5 | 5 passed |
| test_voice_link.py | 6 | 6 passed |
| test_media_cleanup.py | 4 | 8 passed |
| test_meeting_checks.py | 3 | 7 passed |
| test_meeting_control_call.py | 3 | 1 passed |
| test_meeting_frames.py | 2 | 10 passed |
| test_meeting_lab.py | 18 | 8 passed |
| test_meeting_merge.py | 4 | 14 passed |
| test_meeting_overlap.py | 4 | 7 passed |
| test_meeting_provenance.py | 20 | 10 passed |
| test_meeting_recordings.py | 4 | 23 passed |
| test_meeting_screen_audio.py | 3 | 1 passed |
| test_meeting_screen_video.py | 6 | 3 passed |
| test_meetings.py | 71 | 39 passed |
| test_ollama_client.py | 3 | 3 passed |
| test_ollama_runtime.py | 2 | 5 passed |
| test_perimeter.py | 15 | 8 passed |
| test_pipeline_lab.py | 4 | 10 passed |
| test_pipeline_progress.py | 2 | 3 passed |
| test_pipeline_resume.py | 3 | 1 passed |
| test_plain_text.py | 2 | 8 passed |
| test_preflight.py | 3 | 16 passed |
| test_project_access.py | 25 | 12 passed |
| test_project_deadlines.py | 13 | 8 passed |
| test_project_mcp.py | 9 | 5 passed |
| test_project_members.py | 25 | 8 passed |
| test_project_tasks.py | 15 | 6 passed |
| test_projects.py | 33 | 12 passed |
| test_proof_spread.py | 4 | 5 passed |
| test_queue.py | 16 | 6 passed |
| test_report_embed.py | 3 | 3 passed |
| test_resumable_upload.py | 13 | 4 passed |
| test_retrieval.py | 5 | 2 passed |
| test_screen_recorder.py | 2 | 10 passed |
| test_session_cookie.py | 9 | 5 passed |
| test_session_role.py | 7 | 1 passed |
| test_single_instance.py | 3 | 4 passed |
| test_speaker_recalculate.py | 7 | 6 passed |
| test_speakers.py | 3 | 4 passed |
| test_startup.py | 4 | 10 passed |
| test_status.py | 4 | 2 passed |
| test_summarize_document.py | 3 | 7 passed |
| test_summary.py | 3 | 3 passed |
| test_tls_cert.py | 5 | 11 passed |
| test_transcript_export.py | 11 | 8 passed |
| test_tz.py | 8 | 14 passed |
| test_tz_counting.py | 3 | 5 passed |
| test_universal_speakers.py | 6 | 3 passed |
| test_upload.py | 24 | 7 passed |
| test_user_activity.py | 19 | 7 passed |
| test_user_identity.py | 11 | 4 passed |
| test_user_scenarios.py | 79 | 31 passed |
| test_voice_cleaner.py | 9 | 11 passed |
| test_voice_gate_pipeline.py | 3 | 10 passed |

## Индекс проекта (21.09.2026)

- `app/app/tests/test_project_index.py` (29 тестов, Ollama не нужна): нарезка (функции, методы, код уровня модуля, JS, шаблоны, Markdown-находки, CSS), маскирование секретов и запрет индексации `.env`/`*.db`, пересоздание схемы старой версии, снятие устаревшей блокировки, индекс реального репозитория во временную БД (символы, вызовы против `grep`, роуты с конкретными id, таблицы, окружение и константы, связка интерфейса, тесты), протокол MCP-сервера, доступ к `/admin/map` только админу, дрейф карты и манифеста, корректность наборов вопросов.
- Тест `test_project_map_and_manifest_do_not_drift_from_code` падает, если добавлен файл кода без записи в карте, в манифесте есть мёртвый узел или битое ребро, либо реестр модулей устарел. Лечение: `venv\Scripts\python.exe scripts\check_map_drift.py --fix`.
- Качество поиска меряется отдельно (нужна собранная индексом БД и Ollama): `venv\Scripts\python.exe scripts\eval_index.py --show-misses` (набор для настройки) и `--holdout` (контрольный: веса на нём не подбирались). Порог recall@5 для `qa_all.py --evals` — 0.85. Только точный поиск без Ollama: `--no-vector`.
- Диагностика окружения индекса: `venv\Scripts\python.exe scripts\index_doctor.py`.

## Граф зависимостей (03.10.2026)

- `app/app/tests/test_project_graph.py` (14 тестов, Ollama не нужна): граф собирается из индекса реального репозитория во временную БД — у **каждого** файла и роута есть узел, висячих рёбер и дублей нет, на месте все слои (символы и вызовы, импорты, покрытие тестами, история, таблицы БД, роуты с правами, элементы интерфейса, рёбра манифеста), окрестность узла, список сирот (функция без вызовов, без тестов, не роут и не под декоратором), циклы импортов сходятся с таблицей `imports`, выгрузки json/graphml/dot и свод по подсистемам валидны, атлас содержит все разделы и не затирает рукописную шапку. Синтетические случаи: локальный вызов против внешнего (`--with-external`), `from . import x` в цикле, связка кнопка → JS → API.
- `app/app/tests/test_admin_map_graph.py`: страница `/admin/map/graph` и её эндпоинты — аноним получает редирект и 401, не-admin — 403, admin — HTML с графом; `/summary` и `/data` отдают узлы и рёбра, фильтры `group`/`kinds`/`limit` работают, `limit` вне диапазона — 422.
- Тест-сторож от расхождения карты с кодом для графа тот же `test_project_map_and_manifest_do_not_drift_from_code` плюс проверки покрытия в `test_project_graph.py`.

## Ремонт дорожек встречи (21.09.2026)

- `app/app/tests/test_meeting_track_repair.py` (14 тестов): форма ошибок реальной встречи (имена и тексты вымышлены) — схлопнутая реплика, английская галлюцинация, дорожка без слов Parakeet, нормальная длинная реплика ведущего (не должна отбрасываться), сверка звука с текстом на синтетическом WAV, новые проверки `replica_density` и `track_coverage`, интеграция `_transcribe_track` с подменёнными шагами.

## Таблицы документов (25.09.2026)

- `app/app/tests/test_document_tables.py` (23 теста, в т.ч. HTML-таблицы и выгрузка Google Sheets, проверки «ужатия» по значимым символам, выдуманной метки и потери чисел; LLM и Ollama не нужны; `.doc` — только если есть LibreOffice): формат таблиц `table_md`, DOCX в порядке документа (таблица под разделом, объединения, вложенные, `|` и переносы в ячейке, колонтитулы, нумерованные заголовки), `.doc` через LibreOffice, Excel (объединённая подпись и двухуровневая шапка, форматы чисел/дат/процентов, скрытый лист, пустые отформатированные ячейки), CSV `;` в cp1251, PDF-таблица с рамками, нормализация (таблицы в LLM не уходят, потерянная метка, «ужатый» ответ, длинный текст без заголовков режется), нарезка (300 строк: шапка в каждом фрагменте, строки целые; широкая таблица записями), FTS (основы слов, числа с разрядами, находка ниже порога близости, чистка при переиндексации).
- Реальные файлы: `venv\Scripts\python.exe scripts\reprocess_documents.py --all` — отчёт без изменений: новое извлечение против текущего `_clean` (доля текста, число таблиц, числа таблиц, пропавшие из `_clean`, табличные фрагменты в индексе).
- Качество ответов чата на вопросах по таблицам — `scripts\eval_chat.py` на копии базы с наборами `docs/eval/chat_questions_tables_p23.json` (смета Клиент Б) и `chat_questions_tables_p34.json` (смета Клиент А): `--project 23 --questions …`.

## Смоук на живом сервере

`venv\Scripts\python scripts\smoke
un_all.py` — роли, presence, песочничный поток, комната. Настройки через `SMOKE_BASE`, `SMOKE_PROJECT_ID`, `SMOKE_ADMIN_PW`, `SMOKE_MANAGER_PW`. Только песочница (проект 29). Зависимость: `app/requirements-dev.txt`.

## Проверка комнаты и анализа созвона без людей (20.09.2026)

- `venv\Scripts\python.exe scripts\qa_room.py [--outage 170]`: два гостя в Chromium с фейковыми устройствами, зеркало по фронтальной/тыловой камере, выбор микрофона и динамика, обрывы сети (5 с и длинный), возврат в комнату. Создаёт встречу в песочнице (проект 29) кодом приложения, пароли не нужны.
- `venv\Scripts\python.exe scripts\qa_analysis.py [--meeting ID]`: два «участника» публикуют синтезированную русскую речь (`scripts/qa_data_speech_*.wav`) в живую комнату, рекордеры пишут дорожки, встреча завершается, запускается анализ, проверяется транскрипт. Нужны запущенные сервер, воркер, LiveKit, рекордеры, LM Studio и Docker Desktop (GigaAM).
- `venv\Scripts\python.exe scripts\qa_room_ui.py [--shots папка]`: телефон (Pixel 7) вертикально и горизонтально, с показом экрана и без — все плитки видны целиком и не наезжают, в горизонтали экран слева, участники лентой справа; пульт на ПК и телефоне — подсказки у всех кнопок, микрофон/звук/камера/шумодав/«Спикер-Сетка» переключаются и возвращаются, «Участники» и меню динамика закрываются по Esc, «Скопировать ссылку», «Развернуть окно»; говорящий не шлёт `setAttributes` на каждый кадр; камера, погашенная системой (имитация livekit-client и `devicechange`), возвращается сама, выключенная человеком — нет; после переподключения нет устаревшего «нет связи».
- `venv\Scripts\python.exe scripts\qa_meeting_lifecycle.py [--linger 150]`: жизненный цикл на живом сервере — создание (вход до запуска 409, комнаты LiveKit нет), удаление без запуска, запуск и запись, повторный вход с тем же `guest_id` без дублей, пульс (чужой identity — 403), отзыв ссылки (гость выведен, встреча идёт) и новая ссылка, удалить идущую — 409, завершение (комната закрыта, дорожки закрыты за секунды, гость видит «завершена», комната не воскресает, вход 409), удаление записанной встречи и временного проекта с идущей встречей (комната закрыта); через `--linger` секунд запись удалённых встреч не вернулась на диск.
- `venv\Scripts\python.exe scripts\qa_media_path.py [--report отчёт.json]` (~2,5 мин, живой сервер, песочница): гость А подаёт в фейковый микрофон речь с шумом (гул + клавиатура, первые 3 с — только шум) и камеру, гость Б слушает. Сервер: микрофон А (Opus/RED), слои камеры, `clean-<identity>` у `skkkk-dfn`, `skkkk-rec` в комнате, сырая WAV растёт ~96 000 Б/с. У Б: для А играет ровно одна дорожка — очищенная; паузы (10-й перцентиль полусекунд) заглушены ≥ 6 дБ сверх шумодава браузера, речь (90-й перцентиль) ±6 дБ, задержка `clean-*` ограничена (медиана ≤ 550 мс); потери < 2 %, маскировка < 5 %; камера ≥ 320 px, ≥ 10 к/с, замирания ≤ 5 % времени. «Шумодав» у А выкл/вкл — Б переходит на сырой и обратно без тишины и двойного звука, clean-дорожка у А одна и не публикуется заново; 4 переключения подряд — голос А у Б ни на тик не звучит дважды, на странице Б один `<audio>`; шумодав выкинут из комнаты — Б сразу на сыром, шумодав возвращается, запись дорожки идёт дальше; экран одним слоем, 1–8 к/с. Выравнивание к исходнику — одной прямой по окнам (фейковый микрофон Chrome уходит по темпу ~1,5 %); точная задержка — синтетическим слушателем на `venvs/voice`, качество речи — `test_live_filter_matches_whole_recording`.
- В `qa_room.py` проверки индикатора связи на время прогона пропускают в индикатор только события сценария: настоящие оценки LiveKit (на сервере камера гостя оценивается «good/poor» попеременно — браузер ходит на собственный публичный адрес) раньше давали ложные провалы.
- Перед запуском не перезапускайте сервер во время прогона; после прогона встречи в песочнице остаются (каталоги записей нужно чистить вручную).

## Мобильные устройства (20.09.2026)

Инструменты лежат в `tools/android/` (в git не попадают: `platform-tools`, `jdk`, `sdk`, `avd` в `.gitignore`):
- `platform-tools/adb.exe` (или `sdk/platform-tools`): связь с Android; телефон подключается по USB с «Отладкой по USB».
- Эмулятор: Android 14 (Google APIs, Chrome 113), виртуальный Pixel 7 `pixel7`, ускорение WHPX (`emulator -accel-check` → usable). Камеры: фронтальная эмулированная, тыловая «виртуальная сцена».
- Playwright WebKit (`playwright install webkit`): движок Safari для эмуляции iPhone и iPad (без WebRTC на Windows).

Скрипты (запуск `venv\Scripts\python.exe scripts\<имя>`):
- `qa_mobile_webkit.py [--shots папка]`: iPhone 15 (вертикально и горизонтально), iPad Pro 11 (WebKit), Pixel 7 и Galaxy S9+ (Chromium): вёрстка входа и комнаты, размеры кнопок, вход без разрешений, JS-ошибки. Эмуляция, не настоящие устройства.
- `qa_mobile_android.py [--boot] [--outage 30] [--shots папка]`: настоящий Chrome на Android (эмулятор или телефон по USB) плюс «компьютер» в одной встрече: вход, зеркало у фронтальной и без зеркала у тыловой камеры, переключение камеры, динамик, Wake Lock, обрыв сети телефона и возврат. `--boot` сам запускает эмулятор (~1 мин). Код 77: нет устройства.

Ручной запуск эмулятора: `tools\android\sdk\emulator\emulator.exe -avd pixel7 -no-window -no-audio -no-snapshot -gpu swiftshader_indirect -camera-back virtualscene -camera-front emulated` (с переменными `JAVA_HOME=tools\android\jdk`, `ANDROID_SDK_ROOT=tools\android\sdk`, `ANDROID_AVD_HOME=tools\android\avd`). Остановить: `adb emu kill`.
Ограничения: звука в эмуляторе нет (микрофон не проверяется), Safari на iOS настоящий можно проверить только на iPhone или в облаке (BrowserStack).
