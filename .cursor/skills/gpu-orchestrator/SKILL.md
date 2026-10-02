---
name: gpu-orchestrator
description: Sequences GPU phases on the 24GB Windows card for SKKKK. Use when implementing or debugging ASR, LM Studio Gemma load/unload, Ollama embeddings, nvidia-smi VRAM, or the jobs worker.
---

# GPU-оркестратор SKKKK

Одна карта 24 ГБ. Две GPU-фазы взаимоисключающие. Эмбеддинги на CPU.

## Фазы

1. **ASR** — последовательно, один GPU-воркер: Parakeet-TDT в `venvs\lab` (ONNX) и диаризация Nemotron 3 в `venvs\nemo-diar` (`DIAR_MODEL=sortformer4` — откат на Sortformer 4spk в `venvs\lab`), затем GigaAM в контейнере `skkkk-gigaam` (сверка и WeSpeaker). Whisper и T-one не стартуют. В режиме lmstudio LM Studio должен быть unload.
2. **LLM** — sense, proof, выжимка, сущности, ТЗ, чат. В режиме `lmstudio`: `LMSTUDIO_MODEL` (`qwen/qwen3.8-27b`) через LM Studio. В режиме `cursor`: Cursor API, локальная LLM в VRAM нет.
3. **EMBED** — Ollama `bge-m3` с `num_gpu=0`. Можно параллельно с LLM. Нельзя отдавать Ollama GPU.

## Переключение (lmstudio)

```
lms unload --all
# дождаться падения занятости по nvidia-smi
# ... lab_pipeline (Parakeet + Nemotron 3) → lab_giga → lab_voice ...
# процессы завершились -> VRAM освобождена
lms load qwen/qwen3.8-27b
# sense/proof если ещё не сделаны в cursor-режиме; иначе сущности / ТЗ / чат
```

В режиме **cursor** load/unload Gemma пропускаются; sense и proof идут через Cursor API сразу после ONNX.

Чат и rebuild ТЗ ставятся в `jobs` с `phase=llm` и ждут, если сейчас `asr_running`.

## Двойной слух

Канон: Parakeet — таймлайн, GigaAM — сверка (выбор только lab vs giga). Диаризация — Nemotron 3 до 8 спикеров (откат — Sortformer 4spk). WeSpeaker в том же `skkkk-gigaam`.

Пустой текст у обоих слухов → артефакт «речь не обнаружена», LLM не вызывается.

Детали: `docs/DECISIONS.md` инвариант 1, `app/app/services/pipeline_lab.py`.
