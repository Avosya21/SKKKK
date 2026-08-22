# SKKKK

Корпоративный портал файлов проекта и локальная память знаний (три параллельных ASR + Gemma в LM Studio + BGE-M3 в Ollama).

Всё, что относится к этому проекту, хранится **только** здесь: `C:\1C_NEW_SKKKK`.

## Запуск

Одна команда: она готовит `.env`, проверяет все системы и поднимает портал вместе с
GPU-воркером. При первом запуске пароли ролей печатаются в консоль.

```powershell
cd C:\1C_NEW_SKKKK
.\venv\Scripts\python.exe -m app.app.main
```

Портал: `http://127.0.0.1:1111`. Только проверка систем, без запуска — `main check`.
Подробности и остальные режимы: [docs/OPERATIONS.md](docs/OPERATIONS.md).

## С чего читать

1. [docs/PLAN.md](docs/PLAN.md) — единый пошаговый план
2. [docs/AGENT1C-BRIDGE.md](docs/AGENT1C-BRIDGE.md) — порты и клиент LLM, уже проверенные в Agent1C
3. [docs/PLAN-REVIEW.md](docs/PLAN-REVIEW.md) — ревизия плана и внесённые исправления
4. [docs/TESTING.md](docs/TESTING.md) — стратегия тестирования
5. [docs/DECISIONS.md](docs/DECISIONS.md) — что нельзя менять без явного решения
6. [AGENTS.md](AGENTS.md) — как агенту работать в репозитории

## Зафиксировано

- Три ASR параллельно **в Docker** (GPU): Whisper, T-one, GigaAM; склейка через `google/gemma-4-31b-qat` в LM Studio
- Эмбеддинги: Ollama `bge-m3` на CPU
- Windows 11, одна GPU 24 ГБ, оркестратор фаз ASR ↔ LLM

## Текущий этап

Этапы 0–9 реализованы. Полный цикл проверен от входа до пересборки ТЗ.

Проверка стенда до установки зависимостей:

```powershell
powershell -ExecutionPolicy Bypass -File C:\1C_NEW_SKKKK\scripts\check_env.ps1
```
