# ASR Docker

Три сервиса в Docker Desktop со статусом **Running**.

```powershell
# один раз: клон GigaAMGUI, если ещё нет
git clone --depth 1 https://github.com/dubr1k/GigaAMGUI.git C:\1C_NEW_SKKKK\vendor\GigaAMGUI

cd C:\1C_NEW_SKKKK\asr
docker compose --env-file ..\.env up -d --build
```

| Контейнер | Откуда | Порт | Проверка |
|---|---|---|---|
| `skkkk-whisper` | `hwdsl2/whisper-server:cuda` | 18101 | http://127.0.0.1:18101/docs |
| `skkkk-gigaam` | [dubr1k/GigaAMGUI](https://github.com/dubr1k/GigaAMGUI) REST `api.py` | 18102 | http://127.0.0.1:18102/health |
| `skkkk-tone` | `tinkoffcreditsystems/t-one:0.1.0` + REST | 18103 | http://127.0.0.1:18103/health |

Whisper: `POST /v1/audio/transcriptions`, CUDA, язык `ru`. Веса `large-v3` с ModelScope лежат в `asr/models/faster-whisper-large-v3` и монтируются в контейнер (`WHISPER_LOCAL_ONLY`), без Hugging Face CDN. Диаризация Whisper включена (`WHISPER_DIARIZATION=true`, число спикеров не фиксируем: на записи может быть от 2 до 15). Локальный sherpa-onnx, модели с GitHub. GigaAM: `enable_diarization=true`, backend `pyannote`, диапазон 2–15 (`DIARIZATION_MIN_SPEAKERS` / `DIARIZATION_MAX_SPEAKERS`). Число участников заранее неизвестно, поэтому `num_speakers` не задаём. Нужен `HF_TOKEN` и эмбеддинги голоса `pyannote/wespeaker-voxceleb-resnet34-LM` (публичная, CC-BY-4.0). Веса pyannote из‑за таймаута Hugging Face CDN лежат в `data/gigaam/models/huggingface/hub` (скачаны с ModelScope); пайплайн грузится из локального `data/gigaam/models/pyannote/diarization_config.yaml`.
GigaAM: `POST /api/v1/transcribe` (нужен `X-API-Key` = `GIGAAM_API_KEY` из `.env`), backend `pytorch`, модель `v3_e2e_rnnt`. Внутри контейнера порт 8000, на хосте **18102** — хостовый 8000 занят MCP 1С.
T-one: официальный Linux-образ, `POST /transcribe`.

Перед расшифровкой: `lms unload --all`. Клиент: `asr/client.py`.

Просмотр стенограммы с плеером (клик по реплике, фильтр, переименование):

```powershell
python asr\build_transcript_view.py --serve
# http://127.0.0.1:18104/out/transcript.html  (если порт занят — 18105 и дальше)
```

Сервер отдаёт WAV с HTTP Range, иначе клик по таймкоду не перематывает плеер.
