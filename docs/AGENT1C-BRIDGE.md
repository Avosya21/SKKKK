# Что берём из Agent1C в PrimaryAnalitics

Источник: `C:\Agent1C`. Это уже работающий локальный стенд: Gemma в LM Studio, FastAPI Web UI, клиент OpenAI. Код 1С-агента (QERA, MCP Serena, BSL Atlas) в PrimaryAnalitics **не копируем** — копируем способ поднять локальный сервер и разговаривать с моделями.

## Адреса и порты этой машины

| Сервис | Адрес | Кто занимает | Для PrimaryAnalitics |
|---|---|---|---|
| Web UI + API | `http://127.0.0.1:1111` | Agent1C сейчас, PrimaryAnalitics в этом проекте | **Да. Единственный порт приложения.** |
| LM Studio (Gemma) | `http://127.0.0.1:1234/v1` | LM Studio | **Да. Тот же endpoint.** |
| MCP 1С / BSL Atlas | `http://127.0.0.1:8000/mcp/` | Agent1C | **Нет. Порт 8000 не трогать.** |
| Serena MCP | `http://127.0.0.1:9121/mcp` | Agent1C | Нет |
| Ollama | `http://127.0.0.1:11434` | локальный Ollama (`bge-m3`) | **Да, только эмбеддинги на CPU** |

В `C:\Agent1C\run.ps1` в сообщении указан порт 999 — это устаревшая строка. Канон: `config.py` → `WEB_HOST = "127.0.0.1"`, `WEB_PORT = 1111`.

Uvicorn PrimaryAnalitics слушает `127.0.0.1:1111`, как Agent1C: один процесс FastAPI отдаёт и страницы, и API. Не поднимать второй сервер на 8000.

## Модель и клиент LLM (проверено в бою)

```python
from openai import AsyncOpenAI

llm = AsyncOpenAI(
    base_url="http://127.0.0.1:1234/v1",
    api_key="lm-studio",
)
# model="google/gemma-4-31b-qat"
```

Идентификатор модели в LM Studio — **`google/gemma-4-31b-qat`**, не короткое `gemma-4-31b-qat`. Запросы с неверным именем молча уезжают не туда или падают.

Контекст, с которым Agent1C уже живёт: `n_ctx = 32768`, резерв ответа ~4096 токенов, рабочий бюджет промпта ~22 000 символов. Для склейки стенограмм это подтверждает решение резать аудио на окна, а не пихать три полных расшифровки в один запрос.

## Что переиспользуем из кода и библиотек

Готовое окружение: `C:\Agent1C\.venv` (`openai`, `fastapi`, `uvicorn`, `pydantic`, `aiohttp`). Для портала PrimaryAnalitics — **своё** `D:\Primary_Analitics\venv`, с теми же пакетами. ASR-движки остаются в отдельных `venvs\whisper|gigaam|tone` и в это окружение не ставятся.

Паттерн запуска: `run.ps1` → activate venv → `python main.py` → uvicorn поднимает FastAPI. Для PrimaryAnalitics тот же приём (`scripts/run.ps1` на этапе 1).

Паттерн Web UI: FastAPI раздаёт HTML с одной страницы, стриминг через `StreamingResponse`, статус `/api/status`. Для PrimaryAnalitics — Jinja + те же приёмы статуса и polling, без копирования 1С-чата.

## Чего не трогаем

- MCP-серверы 1С, Serena, OData.
- Порт 8000.
- Логику QERA / FSM / memory L0–L3 Agent1C. У PrimaryAnalitics свой конвейер знаний.
