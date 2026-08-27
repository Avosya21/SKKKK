---
name: knowledge-pipeline
description: Implements the SKKKK artifact pipeline from upload to TZ and RAG chat. Use when adding processing modules, artifact statuses, entities, graph links, conflict resolution, TZ rebuild, or chat-over-graph.
---

# Конвейер знаний SKKKK

Полная последовательность и статусы — в `docs/PLAN.md` (модули 0–12). Промпты — в `docs/prompts/`.

## Статусы

`uploaded` → `preprocessing` → `asr_running` → `speakers_pending` → (confirm) → `speakers_confirmed` → `transcript_ready` | `text_extracted` → `entities_extracted` → `indexed` → `graph_seeded` → `done` | `error`

Аудио: лабораторная расшифровка сразу после загрузки, пауза на имена, затем knowledge. Документы/OCR — в `text_extracted`.

## Файлы на диске

Корень артефактов: `C:\1C_NEW_SKKKK\data\projects\{project_id}\`.

Аудио после lab-публикации в `parsed\calls\`:

- `{id}_final.txt` — реплики (спикер + текст)
- `{id}_speakers.json` — `docker_gigaam_speakers.json` (`speaker_N`; имена после «Запомнить», не только после confirm)
- `{id}_call_summary.md` — протокол встречи (после индекса)

Оригинал в `raw_inputs` не перезаписывать.

Диаризация — Sortformer + WeSpeaker, не выдумка LLM. Чанки стенограммы режутся по репликам и хранят `speaker`.

Кнопки `/report` и `/projects/{id}`: что вызывает что — `docs/PROJECT-MAP.md` §5.13. Lab HTML пишет `write_prod_view`, не `build_transcript_view.py`.

Дедлайны: `GET /projects/{id}/deadlines` и `POST /projects/{id}/reextract-entities` живут в `routers/knowledge.py`. Reextract читает lab `_final.txt`, пишет `entities`, **не** вызывает reindex. ТЗ (`rebuild_tz`) для аудио берёт `_final.txt`, не Whisper/T-one. Чат ищет по **чанкам** после knowledge; без индекса — «не найдено».

## Поведение, которое нельзя «упростить»

- Confirm спикеров **не** запускает Whisper/T-one/merge: только имена + knowledge.
- Enroll имена сразу в `docker_gigaam_speakers.json` / HTML; confirm не обязателен для отображения имён.
- ТЗ не пересобирать на каждый upload (кнопка / каждые 3 новых `graph_seeded` / после ручного resolve).
- Конфликт без явной отмены в тексте → `pending_manual`, не авто-победа «кто новее».
- Сущности с `needs_review=true`, пока человек не принял.
- Чат отвечает только из чанков + графа, иначе «не найдено».

Векторный индекс проекта: после knowledge / reindex — `venv\Scripts\python.exe scripts\inspect_project_index.py --project 18`. Сверяет число чанков и sqlite-vec, спикеров и что текст чанков из lab `_final.txt`, не из Whisper.
