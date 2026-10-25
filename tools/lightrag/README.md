# LightRAG — граф знаний PrimaryAnalitics

[LightRAG](https://github.com/hkuds/lightrag) держит **структурированный граф** по документации проекта: кнопки → API → сервисы → БД, пайплайны, спикеры. Дополняет `scripts/search_project_map.py` (sqlite-vec) связями entity–relation.

## Быстрый старт

```powershell
cd D:\Primary_Analitics\tools\lightrag
Copy-Item .env.example .env
# при необходимости поправить LLM/embedding в .env

docker compose up -d
# WebUI: http://127.0.0.1:9621
```

Перед **первой индексацией** на хосте нужны:

```powershell
ollama pull bge-m3          # embeddings (если ещё нет)
ollama pull qwen2.5:3b      # LLM для графа (CPU, ~2 GB)
```

Альтернатива LLM — LM Studio Gemma на `:1234` (см. комментарии в `.env.example`).

Проверка:

```powershell
venv\Scripts\python.exe scripts\lightrag_doctor.py
```

## Синхронизация и индекс

Из корня проекта:

```powershell
venv\Scripts\python.exe scripts\lightrag_sync_docs.py
venv\Scripts\python.exe scripts\lightrag_index.py --upload --wait
```

После правок `docs/PROJECT-MAP.md` или кода — повторить обе команды.

## Запросы (CLI)

```powershell
venv\Scripts\python.exe scripts\lightrag_query.py "что вызывает Пересчитать спикеров"
venv\Scripts\python.exe scripts\lightrag_query.py "таблицы БД для speaker_profiles" --mode global
```

Режимы: `local`, `global`, `hybrid` (по умолчанию), `naive`, `mix`.

## Файлы

| Путь | Назначение |
|------|------------|
| `data/inputs/` | Копии docs для scan (генерирует sync) |
| `data/rag_storage/` | Граф + вектора (persist) |
| `.env` | Секреты LightRAG (не в git) |

## API

- Swagger: http://127.0.0.1:9621/docs
- Заголовок: `X-API-Key: <LIGHTRAG_API_KEY>`

Не путать с prod RAG чата проекта (`sqlite-vec` + `entities`) — LightRAG только для **карты разработки**.
