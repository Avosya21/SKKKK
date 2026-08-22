---
name: gpu-orchestrator
description: Sequences GPU phases on the 24GB Windows card for SKKKK. Use when implementing or debugging ASR, LM Studio Gemma load/unload, Ollama embeddings, nvidia-smi VRAM, or the jobs worker.
---

# GPU-оркестратор SKKKK

Одна карта 24 ГБ. Две GPU-фазы взаимоисключающие. Эмбеддинги на CPU.

## Фазы

1. **ASR** — в VRAM только Whisper + T-one + GigaAM, **три отдельных процесса**, запущенных одновременно. LM Studio должен быть unload, JIT-автозагрузка выключена.
2. **LLM** — в VRAM только `google/gemma-4-31b-qat` через LM Studio (`http://127.0.0.1:1234/v1`). Процессы ASR завершены.
3. **EMBED** — Ollama `bge-m3` с `num_gpu=0`. Можно параллельно с LLM. Нельзя отдавать Ollama GPU.

## Переключение

```
lms unload --all
# дождаться падения занятости по nvidia-smi, а не фиксированной паузы
# ... три процесса ASR одновременно, три raw txt ...
# процессы завершились -> VRAM освобождена гарантированно
lms load google/gemma-4-31b-qat
# склейка по окнам / нормализация / сущности / ТЗ / чат
```

Чат и rebuild ТЗ ставятся в `jobs` с `phase=llm` и ждут, если сейчас `asr_running`.

## Ансамбль

Всегда три движка. Упал один — записать `asr_runs` со статусом error, при двух живых всё равно звать Gemma с пометкой о недоступном варианте. Не удалять третий движок из пайплайна.

Промпт склейки: `docs/prompts/merge_transcript.md`.

Аудио: мастер 16 кГц моно → окна 120–180 с по границам тишины/реплик → из каждого окна 16 кГц (Whisper, GigaAM) и 8 кГц (T-one). Перед Gemma — защита от галлюцинаций на паузе (петли ASR, энергия WAV ∩ диаризация, бюджет символов). Склейка Gemma идёт окно за окном, затем сшивка в `_final.txt`. Таймлайн исходного файла не сжимаем.

Все три черновика пустые → Gemma не вызывается, артефакт закрывается с пометкой «речь не обнаружена».

Детали и тесты: `docs/stages/STAGE-03-asr-ensemble.md`.
