# PrimaryAnalitics — «Кто и когда»: стенограмма → база → чат (план внедрения)

Статус: **v4, этапы 1–6 реализованы в коде, ждёт перезапуска сервера и замера на контрольном созвоне** (2026-09-23). pytest: 889 passed. Проверка привязки на существующих сущностях (только чтение): проект 32 — 12/12, проект 18 — 45/45, проект 23 — 60/63. Реплики — чтение канона без таблицы; `speaker_source` track/voice.
Повод: чат проекта не смог сказать, кто и когда принял решение, хотя в созвоне это есть.

## 0. Решения пользователя (2026-09-23)

| # | Вопрос | Решение |
|---|--------|---------|
| U1 | Кто принял решение | Хранить **оба**: `speaker` — кто произнёс реплику; `decided_by` — кто утвердил/согласился |
| U2 | Старые созвоны | **Не трогаем.** Массового перевода нет; код обязан работать со смешанными данными (§5) |
| U3 | Дата загруженной записи | Имя файла → `ffprobe` → дата загрузки (с честной пометкой). Поле даты в форме загрузки — **отдельная задача** после этапа 1 |

### 0.1. Сверка с `DECISIONS.md` и инвариантами карты

| Запись | Как план с ней соотносится |
|--------|---------------------------|
| «Стенограмма созвона — track-first» (`DECISIONS.md:286`) | не меняется: канон — `_final.txt`, leaks из него уже вырезаны; реплики только читают канон |
| «Контракт созвона и провенанс» (`DECISIONS.md:299`): *имя не выдумывается* | `decided_by` проверяется кодом (этап 4, п. 3): имя принимается, только если это говорящий одной из указанных реплик-согласий; иначе отбрасывается |
| там же (`DECISIONS.md:300`): *провенанс — в `value_json`, отдельной таблицы нет* | **соблюдается буквально (решение 2026-09-23):** реплики в БД не копируются, код читает `_final.txt` через `transcript_turns.load_turns` (кэш по mtime); у чанка — только его отрезок времени. Запись уточнена |
| там же (`DECISIONS.md:301`): *`filter_call_payload` только для комнаты* | **решение 2026-09-23:** фильтр и `needs_review` — только комната (`speaker_source: "track"`). Загруженные созвоны получают speaker/время с `speaker_source: "voice"` (опознание по голосу, не доказательство), без совпадения — `source_status: "unbound"`. `unknown` → `speaker: null`, `source_status: "speaker_unknown"`. Чат и контракт показывают источник атрибуции |
| «Время: хранить UTC, показывать в поясе пользователя» (`DECISIONS.md:332`) | `recorded_at` хранится в UTC; дата из имени файла — с точностью «день» (`recorded_at_source=filename`), без выдуманного времени; день для сроков и шапки чата считается в местном поясе; если `recorded_at` появится в API — ISO с `Z` |
| «Итоговая сводка ТЗ — быстрый top-K» (`DECISIONS.md:342`) | окна длиннее прежних фрагментов, а `FAST_CHUNK_TRIM=900` их обрежет → обязательный прогон `scripts/eval_tz.py` на контрольном проекте (этап 3) |
| «Соединение БД не держим на время ответа LLM» (`DECISIONS.md:376`) | реплики читаются из файла; `rebind_call_entities` и `load_turns` LLM не вызывают |
| «Lab-артефакт: HTML/JSON только через `write_prod_view`» (инвариант карты); `asr/lab/FREEZE.md` | `asr/lab` не трогаем; `#t=` в `asr/transcript_view.py` действует на новые/пересобранные отчёты |
| «Тесты не пишут в боевую БД» (инвариант) | eval — на копии БД; новые тесты — на фикстурах `db_engine`/`client` |
| «LLM только через `*_no_think`» (инвариант, `test_llm_no_think_guard.py`) | извлечение идёт через тот же `chat_completion_no_think` |
| Карта не расходится с кодом (`check_map_drift`, тест-сторож манифеста) | новые модули `transcript_turns.py`, `recorded_at.py` → узлы `manifest.yaml` + `check_map_drift --fix` в том же изменении, иначе сторож падает |

## 1. Диагноз (проверено на проекте 32, `art_2026-09-21_165220_866093`)

Канон `_final.txt` содержит всё: `[05:51 - 07:34] Елена Соколова: …`. Дальше по конвейеру теряется:

| # | Где | Что теряется |
|---|-----|--------------|
| D1 | `chunking.chunk_transcript` (`chunking.py:65`) | таймкод срезается; в `chunks` нет полей времени |
| D2 | `chunking.chunk_transcript` (`chunking.py:81`) | новый чанк на каждую смену говорящего: 82 чанка, 39 короче 80 символов; вопрос и «Да, всё правильно» в разных чанках |
| D3 | `chat._answer_chat_inner` (`chat.py:836`) | в LLM идёт `[Спикер] текст` — без файла, даты созвона, времени |
| D4 | `entities.bind_call_source` (`entities.py:638`) | загруженные созвоны **имеют** `proof/transcript.json`, но без `source_span_id` → `_has_span_replicas=False` → решение = `{"text": …}` |
| D5 | `entities.extract_entities_llm` (`entities.py:483`) | LLM получает `timestamp=now()` — время обработки, а не созвона |
| D6 | `EntitiesPayload.decisions: list[str]` | в схеме нет «кто принял» |
| D7 | `_turn_prompt` | `TURN_PROMPT_LIMIT=120` — у длинных комнатных созвонов хвост без реплик |
| D8 | даты | даты созвона в БД нет; `artifact_reference_date` угадывает по имени файла / `upload_date` |
| D9 | `graph.entity_text` (`graph.py:33`) | строка для эмбеддинга и сравнения = **все значения словаря**. Уже сейчас у комнатных созвонов туда попадают `speaker`, `start_sec`, `text_hash`; если добавить привязку всем — одинаковые решения из разных созвонов станут `POTENTIAL_CONFLICT` (`graph.py:178`), а поиск сущностей зашумится |

## 2. Принцип решения

Один слой **реплик** (`transcript_turns.load_turns` — разбор `_final.txt`, в БД не копируется) — единственный источник «кто / когда / что сказал» для созвона.
Все потребители ссылаются на реплики, а не разбирают стенограмму каждый по-своему.

```
_final.txt (+ proof/transcript.json → source_span_id, leaks)
      └─► transcript_turns ──► chunks (окна реплик: start_sec..end_sec)
                          ├─► entities (decision/task/fact → speaker, start_sec, decided_by)
                          ├─► contract (цитата → реплика → speaker, said_at; участники)
                          └─► chat (шапка фрагмента: созвон · дата · таймкод)
artifacts.recorded_at ────► entities (timestamp), chat, project_deadlines, contract
```

Канон текста — `_final.txt` (DECISIONS: «Стенограмма созвона — track-first»). Leaks из него уже вырезаны.
**Кто переписывает `_final.txt`** (карта §5.12–5.13, `--callers`):

| Действие | Цепочка | Переиндексация чанков |
|----------|---------|-----------------------|
| GPU-конвейер | `publish_lab_to_prod` → `run_knowledge_pipeline` | да |
| «Обновить и переиндексировать» | `refresh_artifact_project_index` → `finalize_audio_outputs` → `index_artifact` | да |
| «Проверить голоса и переиндексировать» | `reaudit_voices_and_reindex` → … → `index_artifact` | да |
| «Запомнить», «Слить», подтверждение имён | `rebuild_transcript_report_html` → `apply_confirmed_speaker_names` → `republish_named_transcript` | **нет** (карта §5.12: entities/chunks «—») |
| «Пересобрать выжимку» | `rebuild_artifact_call_summary` → `apply_confirmed_speaker_names` | **нет** |

Реплики читаются из файла и поэтому всегда актуальны. `rebind_call_entities` (дёшево, без Ollama и LLM) вешается на **`apply_confirmed_speaker_names`** (есть `db`) и на `index_artifact`.
Сущности получают новые имена сразу; чанки, как и сейчас, отстают до переиндексации — это видно в `audit_project_index`.
Вывод: связь чанк ↔ реплика и сущность ↔ реплика держится **по времени** (`start_sec..end_sec`), а не по номеру реплики — номера сдвигаются после «Слить».

**Факты о формате `_final.txt`** (проверено по всем 24 созвонам в `data/projects`):
- речь: `[mm:ss - mm:ss] Имя: текст`, после часа — `[hh:mm:ss - hh:mm:ss]` (проект 23, `01:00:50`);
- экран (`pipeline_lab._append_screen_context`): `[mm:ss - mm:ss] Экран (показывал X): …` и `… Речь на экране: X: …`; минуты тут бывают > 59; блоки дописаны **после** речи, не по времени;
- `unknown` — законный говорящий (проект 32: 11 с).

## 3. Кто затронут (карта согласованности)

| Потребитель | Сейчас | После |
|-------------|--------|-------|
| `pipeline_knowledge.run_knowledge_pipeline` | entities → index → summary → graph → tz | как было; извлечение сущностей берёт реплики из `load_turns` |
| `indexing.index_artifact` (вызовы: pipeline, `refresh_artifact_project_index`, `reaudit_voices_and_reindex`, `scripts/refresh_portal_knowledge.py`, тест) | `chunk_transcript(text)` | `load_turns` → окна → затем `rebind_call_entities` |
| `pipeline_lab.apply_confirmed_speaker_names` (из «Запомнить», «Слить», подтверждения, «Пересобрать выжимку») | переписывает `_final.txt`, БД знаний не трогает | + `rebind_call_entities`; чанки — по-прежнему только при переиндексации |
| `indexing.audit_project_index` | «чанки без speaker»; выравнивание `chunk.text[:40] in canon` | + «реплики ≠ _final», «чанк созвона с репликами без start_sec»; выравнивание сохраняется, т. к. строки чанка = строки канона дословно |
| `vector_index.search_vectors` | `text, speaker` | + `start_sec, end_sec` |
| `graph.entity_text` → графовые связи, `retrieval.search_entities`, `knowledge.review_entity` (пересчёт вектора) | все значения словаря | **только смысловые ключи** (D9); провенанс не влияет на вектор и на `same_value` |
| `chat` (`_answer_chat_inner`, `_supplement_context`, `_format_entity_value`) | `[speaker] text`, сырой `value_json` | шапка фрагмента; единый форматтер сущности |
| `tz._collect_project_material` (`tz.py:328`) | сырой `value_json` | единый форматтер |
| `contract._evidence` (`contract.py:234`), реестр участников (`contract.py:525`), `_load_artifacts` | `chunk.speaker`; дата из имени файла | говорящий и время из реплики; участники из реплик; `recorded_at` |
| `project_deadlines.artifact_reference_date` | имя файла → `upload_date` | `recorded_at` (если источник не `upload`) → как было |
| `knowledge.review_entity` (edit) | **заменяет `value_json` целиком** от клиента | переносит ключи провенанса из старого значения, если клиент их не прислал |
| `requirement_review`, `agent_gateway`, `agent_verdicts` | пишут свои ключи в `value_json` | без изменений; rebind не трогает чужие ключи |
| `project_deadlines`, `graph` | `description` / `text` | без изменений |
| `project_mcp_data.mcp_list_entities` | `value_json` как dict | новые поля появляются сами; описать в протоколе MCP |
| `project_purge.purge_artifact` / `purge_project` | chunks, entities, … | без изменений (реплик в БД нет) |
| `pipeline_progress.reconcile_artifact_status` | считает chunks/entities | без изменений |
| отчёт `/artifacts/{id}/report` (`asr/transcript_view.py`) | у реплик есть `data-start` и кнопки «Перейти к», **открытия по `#t=` нет** | (опц., этап 5) переход по `#t=сек` |

## 4. Этапы

Каждый этап выкатывается отдельно. После каждого: `pytest app/app/tests`, `scripts/index_project_map.py`, `scripts/check_map_drift.py`.

### Этап 0. Контрольный созвон и замер «до»
Старые созвоны не переделываем (U2), поэтому приёмка идёт на **свежей обработке**:
1. Тестовый проект-песочница; туда загружается запись проекта 32 (`Клиент Г сдача тикета 2…webm`) и одна из проекта 18 (длинная, 58 мин). Проверить, что поиск дубликатов по `sha256` не блокирует загрузку в другой проект.
2. `docs/eval/chat_questions_provenance.json` — 8–12 вопросов «кто / когда» с ожидаемыми именем и таймкодом + 2 ловушки. `eval_chat.py`: поля `expect_speaker`, `expect_time`, `expect_date`.
3. Прогон на копии БД → цифры «до» записать сюда (§7).

### Этап 1. Дата созвона — D5, D8
- Миграция (`db._MIGRATIONS`): `artifacts.recorded_at DATETIME`, `artifacts.recorded_at_source VARCHAR(16)` (`meeting|filename|media|upload`).
- `services/recorded_at.py::resolve_recorded_at(artifact, path)`:
  комната — `meeting.started_at` (`meeting_analyze.py:398`); загрузка (`upload.create_artifact_record`, `upload.py:176`) — дата из имени (вынести regex из `artifact_reference_date`) → `ffprobe format.tags.creation_time` (рядом с `upload.ffprobe_duration`) → `upload_date` с источником `upload`.
  Записи из браузера (`.webm`) обычно без `creation_time` — тогда честно `upload`.
- `project_deadlines.artifact_reference_date` / `reference_is_explicit`: сначала `recorded_at`, если источник ≠ `upload`; иначе прежняя логика (старые записи с `NULL` работают как раньше).
- `contract._load_artifacts`: `occurred_at` из `recorded_at`; `occurred_at_source` = `recorded_at_source`.
- `extract_entities_llm`: `timestamp=recorded_at` (или прежняя дата материала), не `now()`.
- Тесты: `test_deadline_reference.py`, `test_project_deadlines.py`, `test_contract.py`, новый `test_recorded_at.py` (имя, ffprobe, fallback, NULL у старых).
- **Следом отдельной задачей** (U3): поле «дата созвона» в форме загрузки → `recorded_at_source=user`.

### Этап 2. Реплики — чтение канона, **без таблицы** (решение 2026-09-23, DECISIONS:300)
- Реплики в БД не копируются. `services/transcript_turns.py`:
  - `parse_final_turns(text) -> list[Turn]` — `Turn{index, kind (speech|screen|screen_speech), speaker, start_sec, end_sec, text, line}`; оба формата времени, минуты > 59, строки без метки клеятся к предыдущей реплике, экранные блоки → `kind=screen|screen_speech`;
  - `load_turns(settings, artifact)` — `_final.txt` через `artifact_knowledge_text`-путь, кэш по `(путь, mtime, size)`;
  - `turn_at(turns, sec)` — реплика речи, покрывающая момент (или ближайшая), для якоря по времени;
  - `speaker_source(artifact)` — `track` для комнаты (есть реплики proof с `source_span_id`), иначе `voice`.
- Документы реплик не имеют: все потребители переносят пустой список.
- Миграций, пересборки и чистки при удалении нет — реплики всегда совпадают с каноном.
- Тесты `test_transcript_turns.py`: форматы, > 1 ч, экран, `unknown`, строки без метки, кэш по mtime.

### Этап 3. Чанки-окна — D1, D2
- Миграция `chunks`: `start_sec REAL, end_sec REAL` (у старых — `NULL`). Номеров реплик в чанке не храним: после «Слить» они сдвигаются (§2).
- `chunk_transcript(turns)`: окна подряд идущих реплик до `CHUNK_TOKENS`, перекрытие в 1 реплику. **Строки чанка — дословно строки канона** (`[05:51 - 07:34] Елена Соколова: …`): таймкод виден LLM, выравнивание в `audit` не ломается. Длинная реплика режется по предложениям, у частей тот же таймкод. Экранные реплики — отдельными окнами, не смешиваются с речью.
  `speaker` = имя, если в окне один говорящий, иначе `NULL`.
- `contract._evidence`: для чанка со `start_sec` цитата ищется в репликах его отрезка времени → `speaker`, `said_at = recorded_at + start_sec`; без реплик — прежняя логика. Участники (`contract.py:525`) — из реплик, иначе из чанков. Кэш контракта (`_fingerprint`) правки `value_json` не видит — живёт 30 с, это допустимо.
- `audit_project_index`: новые проверки только для артефактов с репликами.
- `search_vectors` отдаёт новые поля; их получают без изменений чат, `GET /projects/{id}/search` (фронтом не используется), MCP `search_project` (добавить `start_sec`, `end_sec` в ответ), ТЗ fast.
- **ТЗ (DECISIONS 2026-09-18):** `_fast_quota_chunks` берёт первый чанк артефакта, `FAST_CHUNK_TRIM=900` режет длинные окна. Прогнать `scripts/eval_tz.py` на контрольном проекте до и после; при падении — резать по целым строкам реплик, а не посимвольно.
- Тесты: `test_index.py` (переписать 2 теста чанков + окна, экран, выравнивание), `test_contract.py` (смешанный чанк → верный говорящий; старый чанк — как раньше), `test_tz.py`, `test_project_mcp.py`, `index_doctor`.

### Этап 4. Сущности с источником — D4, D6, D7, D9
1. **D9 сначала:** `entity_text` берёт только смысловые ключи (`text`, `description`, `assignee`, `deadline`, `key`, `value`, `name`, `role`, `relation`…); ключи провенанса (`speaker`, `decided_by`, `start_sec`, `end_sec`, `turns`, `source_span_id`, `text_hash`, `recorded_at`, `source_status`, `human_review`…) исключены. Существующие векторы не пересчитываются (U2) — кэш по `entity.id`.
2. Схема: `decisions: list[str | DecisionItem]`, `DecisionItem{text, turns: list[int], decided_by: list[str]}`; `TaskItem` + `turns`. Строки принимаются как раньше. Поправить все места, где решения считаются строками: `normalize_entities_payload` (перенос решения со сроком в task — вместе с `turns`), `_merge_payloads` (дедуп по `text`), `filter_call_payload`, `stub_entities`, `persist_entities`.
3. Промпт `extract_entities.md`: реплики подаются как `T12 [05:51] Павел Пилот: …`; для decision/task/fact — `turns`; для decision — `decided_by: [{name, turn}]`.
   Правило: `decided_by` — кто утвердил или явно согласился, а не кто спросил или предложил; нет явного согласия → `[]`.
   **Проверка кодом (DECISIONS: имя не выдумывается):** запись принимается, только если `turn` существует, это речь (не экран), её говорящий = `name` и реплика не дальше `DECIDED_BY_WINDOW` (≈ 5 реплик) от реплики решения. Иначе запись отбрасывается и пишется в журнал `entities.decided_by.rejected`. `unknown` в `decided_by` не попадает.
4. `_split_for_extraction` режет по репликам; каждая часть идёт со своими пронумерованными репликами → `_turn_prompt` и `TURN_PROMPT_LIMIT` удаляются (D7).
5. `bind_call_source` для всех созвонов берёт реплики из `transcript_turns`: номер от LLM принимается, только если текст совпадает (`_match_replica`) → иначе поиск по тексту → иначе без привязки.
   Пишется: `speaker, speaker_source (track|voice), start_sec, end_sec, source_span_id, recorded_at, decided_by: [{name, start_sec}]` (номер реплики после проверки заменяется временем). Говорящий `unknown` → `speaker: null`, `source_status: "speaker_unknown"`.
   Нет совпадения: **комната** — как сейчас (`needs_review=True`, DECISIONS 2026-09-13); **загруженная запись** — `source_status="unbound"` без `needs_review`, чтобы не завалить очередь проверки и не менять статус контракта. Leaks — как сейчас, из proof.
6. `rebind_call_entities` — без LLM, **на месте** (id, связи, разрешения, `human_review` целы), только для сущностей, у которых уже есть `start_sec` (U2: старые не трогаем). Якорь — `start_sec`, не номер реплики: при слиянии спикеров номера реплик сдвигаются. Обновляет `speaker` и имена в `decided_by` (по времени их реплик). Вызывается из `index_artifact` и `apply_confirmed_speaker_names` (§2).
7. `knowledge.review_entity` (edit): ключи провенанса переносятся из старого `value_json`.
8. Единый форматтер `format_entity_line(entity, artifact)`:
   `[decision] текст — сказал: Елена Соколова; утвердил: Павел Пилот (по голосу) · созвон «…» 21.09.2026 · 05:51`. Пометка «по голосу» — при `speaker_source=voice`.
   Нет данных — эти части просто не выводятся (старые сущности). Используется в `chat` (2 места), `retrieval.search_entities`, `tz._collect_project_material`.
- Тесты: `test_entities.py`, `test_entities_normalize.py`, `test_meeting_provenance.py` (кейсы `external_call_without_span`, `legacy_call_without_proof` — обновить ожидания под `source_status`), `test_entity_vectors.py` (rebind не меняет id и вектор; `entity_text` без провенанса), `test_graph*` (одинаковый текст из разных созвонов ≠ конфликт), `test_tz.py`, `test_project_mcp.py`.

### Этап 5. Чат — D3
- Шапка фрагмента: `Созвон «Клиент Г сдача тикета 2…» · 21.09.2026 · 05:51–07:34 · artifact_id=…`.
  При `recorded_at_source=upload` или `NULL` — `дата загрузки 21.09.2026`, чтобы LLM не выдавал её за дату созвона. Документ — имя и дата загрузки. Старый чанк без времени — шапка без таймкода.
- `sources[]` — `start_sec` лучшего фрагмента; (опц.) ссылка `…/report#t=351` + обработка `#t=` в `asr/transcript_view.py`.
- Выжимки созвонов в `_supplement_context` — с датой.
- `rag_chat.md`: на «кто / когда» — имя, дата созвона и таймкод из шапки; различать «сказал» и «утвердил»; не приписывать решение тому, кто только спросил или предложил; нет согласия в тексте — сказать прямо; дата загрузки ≠ дата созвона.
- Тесты `test_chat.py`: шапка (обе ветки даты, старый чанк), форматтер сущностей.

### Этап 6. Документы и приёмка
- `DECISIONS.md`: уточнить записи :300 и :301 (§0.1); новая запись «Кто и когда в чате»: реплики читаются из канона, chunks — окна реплик со временем, `speaker` ≠ `decided_by`, `speaker_source`, провенанс не входит в `entity_text`, старые созвоны не переводятся.
- `PROJECT-MAP.md`: модуль `transcript_turns`, новые колонки; §1.1 и строки про чат/ТЗ (шапка фрагмента); §5.12 таблица «Таблицы БД по кнопкам» (Запомнить / Слить / Подтвердить → rebind сущностей); §8 конвейер знаний; `manifest.yaml` — узлы `svc_transcript_turns`, `svc_recorded_at`; `check_map_drift --fix`; протокол MCP — поля сущностей и `search_project`.
- `STAGE-05` / `STAGE-07` — критерии (§6); `TESTING.md` — новый eval.
- Повторный `eval_chat.py` на контрольном созвоне.

## 5. Смешанные данные (следствие U2)

| Ситуация | Поведение |
|----------|-----------|
| Старый созвон, никто не трогал | чанки без времени, сущности без провенанса, `recorded_at=NULL`: чат, ТЗ, контракт, сроки работают как сейчас |
| Пользователь сам пересобрал индекс старого созвона | запускается новый код: чанки-окна строятся заново (действие пользователя, не массовый перевод); сущности без `start_sec` rebind не трогает. Пользователь (2026-09-23): старые созвоны трогать не планирует |
| Пользователь сам нажал «переизвлечь сущности» | сущности извлекаются по-новому, с привязкой |
| Проверки `audit_project_index` | новые правила — только для артефактов с репликами; старые не краснеют |

## 6. Критерий готовности

- Реплики не хранятся в БД; `load_turns` совпадает с `_final.txt` после любой кнопки переименования.
- Контрольные созвоны: у всех чанков есть `start_sec`/`end_sec`; `audit_project_index` без проблем по всем проектам (старые не краснеют).
- Контрольные созвоны: ≥ 80 % decision/task с `speaker` + `start_sec`; остальное — `unbound`/`needs_review`, ни одной привязки к несовпадающему тексту.
- Eval «кто / когда»: верные имя и дата ≥ 80 %; «сказал» и «утвердил» не путаются; ловушки — отказ; общий eval чата (`chat_questions_project18.json`) не хуже прежнего.
- Одинаковое решение из двух созвонов → `SEMANTICALLY_RELATED`, не конфликт.
- `scripts/eval_tz.py` на контрольном проекте не хуже прежнего (DECISIONS 2026-09-18).
- Ни одного имени в `decided_by`, которое не совпадает с говорящим реплики-согласия (журнал `entities.decided_by.rejected` разобран).
- `check_map_drift.py` чистый, тест-сторож манифеста зелёный.
- Переименование спикера на контрольном созвоне → новое имя в чате и в сущностях без повторного извлечения.
- `pytest app/app/tests` зелёный.

## 7. Замеры

| Замер | До | После |
|-------|----|-------|
| eval «кто/когда» | — | — |
| eval чата, проект 18 | — | — |

## 8. Риски

| Риск | Мера |
|------|------|
| Таймкоды в тексте чанка ухудшат эмбеддинги | сравнить eval; при потере — эмбеддить без меток, хранить с метками |
| Смешанные окна ломают `contract` | говорящий из реплики (этап 3) |
| Изменение `entity_text` сдвинет старые векторы | старые не пересчитываются; новые считаются по новому правилу, сравнение `same_value` по новому правилу с обеих сторон |
| Экранные блоки идут не по времени | отдельный `kind`, отдельные окна, не источник `decided_by` |
| Кэш реплик устарел или файл читается во время перезаписи | ключ кэша — (mtime, size); пустой/битый разбор не кэшируется |
| LLM возвращает чужие номера реплик | номер принимается только при совпадении текста |
| `.webm` без `creation_time` | `recorded_at_source=upload` + пометка в чате; поле в форме — отдельная задача |
