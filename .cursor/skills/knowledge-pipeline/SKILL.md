---
name: knowledge-pipeline
description: Implements the SKKKK artifact pipeline from upload to TZ and RAG chat. Use when adding processing modules, artifact statuses, entities, graph links, conflict resolution, TZ rebuild, or chat-over-graph.
---

# Конвейер знаний SKKKK

Полная последовательность и статусы — в `docs/PLAN.md` (модули 0–12). Промпты — в `docs/prompts/`.

## Статусы

`uploaded` → `preprocessing` → `asr_running` → `asr_completed` → `transcript_ready` | `text_extracted` → `entities_extracted` → `indexed` → `graph_seeded` → `done` | `error`

Аудио после ASR идёт в `transcript_ready`, затем сущности. Документы/OCR — в `text_extracted`.

## Файлы на диске

Корень артефактов: `C:\1C_NEW_SKKKK\data\projects\{project_id}\`.

Аудио после модуля 2.1 обязано иметь в `parsed\calls\`:

- `{id}_raw_whisper.txt`
- `{id}_raw_tone.txt`
- `{id}_raw_gigaam.txt`
- `{id}_final.txt` — реплики (спикер + текст), не простыня
- `{id}_speakers.json` — интервалы PyAnnote после сглаживания

Оригинал в `raw_inputs` не перезаписывать.

Диаризация — отдельный слой от ASR. Gemma не выдумывает спикеров. Чанки стенограммы режутся по репликам и хранят `speaker`.

## Поведение, которое нельзя «упростить»

- ТЗ не пересобирать на каждый upload (кнопка / каждые 3 новых `graph_seeded` / после ручного resolve).
- Конфликт без явной отмены в тексте → `pending_manual`, не авто-победа «кто новее».
- Сущности с `needs_review=true`, пока человек не принял.
- Чат отвечает только из чанков + графа, иначе «не найдено».
