---
name: gpu-orchestrator
description: Sequences GPU work on the 24GB Windows card for SKKKK. Use when implementing or debugging ASR, the local LM Studio model (load by profile, two lanes, idle unload), Ollama embeddings, nvidia-smi VRAM, or the jobs worker.
---

# GPU-оркестратор SKKKK

Одна карта 24 ГБ. Модель (22,4 ГБ) и ASR на GPU вместе не помещаются: на время GPU-шагов ASR модель выгружена (`llm_keeper.asr_gpu_hold`, Р1 по замеру 2026-10-05). Эмбеддинги на CPU.
Устройство — карта §3.2, план и замеры — `docs/plans/LLM-LANES-PLAN.md`.

## Работа на карте

1. **ASR** — последовательно, один GPU-воркер: Parakeet-TDT в `venvs\lab` (ONNX) и диаризация Nemotron 3 в `venvs\nemo-diar` (`DIAR_MODEL=sortformer4` — откат на Sortformer 4spk в `venvs\lab`), затем GigaAM в контейнере `skkkk-gigaam` (сверка и WeSpeaker). Whisper и T-one не стартуют. На шаги `lab_onnx`, `lab_giga`, `lab_voice` модель выгружается (ожидание текущих ответов, метка `data/locks/asr_gpu.hold`), между шагами — снова загружается для вычитки и чата. GigaAM останавливается сразу после дорожек.
2. **LLM** — одна модель `LMSTUDIO_MODEL` в LM Studio, два потока:
   - фоновая полоса — воркер и его процессы ASR (sense, proof, выжимка, сущности, ТЗ, кадры, автопроверки, кнопки-задачи `project_task`);
   - полоса чата — процесс API (ответы чата и черновики MCP строго по очереди, по одному).
   В режиме `cursor` — Cursor API, локальной модели в VRAM нет.
3. **EMBED** — Ollama `bge-m3` с `num_gpu=0`. Можно параллельно с LLM. Нельзя отдавать Ollama GPU.

## Загрузка и выгрузка модели

Только `services/llm_keeper.py` по профилю `tools/lmstudio/qwen-profile.json` (окно 56000, 2 потока, flash-attn, KV q8_0, MTP). Прямой `lms load` в коде портала запрещён (страж `test_llm_lanes.py`): модель поднялась бы не по профилю. Автозагрузка LM Studio (JIT) выключена.

```
venv\Scripts\python.exe scripts\llm_model.py status   # загружена ли, совпадает ли с профилем, JIT
venv\Scripts\python.exe scripts\llm_model.py load     # загрузить по профилю
venv\Scripts\python.exe scripts\llm_model.py unload
```

Загружено ли — список `GET :1234/api/v0/models`, не `/v1/models`. Выгрузка — воркером после `LMSTUDIO_IDLE_UNLOAD_SEC` простоя без задач и чата.

## Двойной слух

Канон: Parakeet — таймлайн, GigaAM — сверка (выбор только lab vs giga). Диаризация — Nemotron 3 до 8 спикеров (откат — Sortformer 4spk). WeSpeaker в том же `skkkk-gigaam`.

Пустой текст у обоих слухов → артефакт «речь не обнаружена», LLM не вызывается.

Детали: `docs/DECISIONS.md` инвариант 1 и «Две полосы локальной модели», `app/app/services/pipeline_lab.py`.
