# Поиск рабочих CUDA / ASR-проектов в PyCharm 2025.2

Дата обхода: 2026-08-18. Источник списка проектов: `C:\Users\rene_\AppData\Roaming\JetBrains\PyCharm2025.2\options\recentProjects.xml` (сборка `PY-252.23892.515`). Проекты PyCharm **не изменялись**.

**Короткий вывод.** В PyCharm нет готового CUDA-пайплайна Whisper / GigaAM / T-one. То, что пользователь вспоминал как «CUDA encoder», это проект `C:\PythonProject_ENCODER`: аппаратный **видеокодек NVENC (hevc_nvenc)** через FFmpeg, плюс CUDA-чат на Llama 3.1. Рабочий GigaAM лежит уже в SKKKK (`AudioToText/` и `asr/run_gigaam.py`) и **должен работать на GPU**. Старые CPU-флаги в `AudioToText/` — наследие чужой слабой машины, не дизайн. Для ансамбля SKKKK нужно продолжать `asr/run_*.py`, а не копировать PyCharm-проекты.

## Что просмотрено

Стандартные корни **отсутствуют**:

- `C:\Users\rene_\PyCharmProjects`
- `C:\Users\rene_\PycharmProjects`
- `C:\Users\rene_\IdeaProjects`
- `C:\Users\rene_\.PyCharm*`
- `C:\Coursor projects` (есть только след в истории консоли PyCharm: `cd "c:\Coursor projects\1c_analitik"`)

Единственная конфигурация JetBrains: `PyCharm2025.2` (Roaming + Local). Других `recentProjects.xml` нет.

Все доверенные проекты PyCharm 2025.2:

| Путь | Последний кадр / суть | ASR CUDA? |
|---|---|---|
| `C:\PythonProject_ENCODER` | `encoder.py` + `AI new.py` | Нет ASR. Есть NVENC-видео и Llama на CUDA |
| `C:\PythonProject_SORA` | LoRA/SFT Llama, `torch.cuda.amp` | Нет |
| `C:\AI_local` | `ai_run.py`, gpt-oss-20b | Нет |
| `C:\AI local\project` | в recents, каталог сейчас не найден | — |
| `C:\1c_analitik` | аналитика 1С, venv с `torch 2.12.0+cpu` | Нет |
| `C:\1c_analitik\py new clear` | веб-оркестратор 1С | Нет |
| `C:\Users\rene_\1c-ai-agent` | агент 1С | Нет |
| `C:\Agent1C` | текущий lastOpenedProject | Нет (только типы OpenAI whisper в зависимостях) |
| `C:\1C` | база 1С, не Python | Нет |
| `C:\1C_NEW_SKKKK` | SKKKK, не открывался как проект PyCharm | ASR-скрипты есть, GPU-venv нет |

Desktop / Downloads / Documents: отдельных ASR-проектов нет.

## 1. `C:\PythonProject_ENCODER` — то, что называется encoder

Интерпретатор проекта в `.idea\misc.xml`: системный **Python 3.10.9**

`C:\Users\rene_\AppData\Local\Programs\Python\Python310\python.exe`

В таблице SDK PyCharm также зарегистрирован venv `C:\PythonProject_ENCODER\.venv`, но в нём только pip/setuptools. Реальный CUDA-стек стоит **в системном Python 3.10**:

- `torch==2.2.1+cu121`
- `torch.version.cuda == 12.1`
- `transformers` установлен там же

Запуск: Run Configuration `Python.encoder` (coverage suite `PythonProject_ENCODER$encoder`, 2025-09). Скрипт интерактивный, не CLI.

### Что делает `encoder.py`

Это **не speech encoder**. Это пакетное перекодирование видео:

1. Проверка NVENC: `ffmpeg -encoders`, ищется `hevc_nvenc`.
2. Жёсткий путь: `C:\ffmpeg-8.0-full_build\bin\ffmpeg.exe` (файл на диске есть).
3. Кодирование `hevc_nvenc` preset `slow`, аудио `libmp3lame`.

Это подтверждает, что на этой машине NVIDIA-кодирование через FFmpeg уже работало. К Whisper / GigaAM / T-one отношения не имеет.

### Что делает `AI new.py` / `tetst.py`

Локальный чат 1С: `Meta-Llama-3.1-8B-Instruct` из ModelScope,

`C:\Users\rene_\.cache\modelscope\hub\models\LLM-Research\Meta-Llama-3___1-8B-Instruct`

Загрузка: `AutoModelForCausalLM.from_pretrained(..., device_map="auto", torch_dtype=bfloat16|float16)`. Это рабочий CUDA LLM, не ASR.

## 2. Другие PyCharm-проекты с CUDA (тоже не ASR)

**`C:\PythonProject_SORA`** — дообучение Llama (`AI подготовка сета.py`): `DEVICE = "cuda" if torch.cuda.is_available() else "cpu"`, `torch.cuda.amp`. SDK в misc.xml тоже «Python 3.10» (системный). venv проекта почти пустой.

**`C:\AI_local\ai_run.py`** — инференс `gpt-oss-20b`, печать `torch.cuda.is_available()`, `get_device_name`, `empty_cache()`. Путь модели в скрипте: `C:/AI local/gpt-oss-20b` (каталог с пробелом). CUDA-стек тот же системный 3.10 + cu121.

**`C:\1c_analitik\.venv`** — `torch 2.12.0+cpu` (`cuda = None`). Не использовать для ASR.

## 3. Настоящий ASR на этой машине — уже в SKKKK, не в PyCharm

### `C:\1C_NEW_SKKKK\AudioToText`

Исходный Docker-вариант GigaAM v3 `e2e_rnnt` (ConformerEncoder 16 слоёв, 16 кГц). Dockerfile базируется на `pytorch/pytorch:2.3.0-cuda11.8-cudnn8-runtime`. GigaAM **должен идти на GPU**:

- `main.py` выбирает CUDA + float16, если `torch.cuda.is_available()`, иначе CPU + float32;
- `docker-compose.yaml` резервирует NVIDIA GPU (`device_ids: ["0"]`, `capabilities: ["gpu"]`);
- модель ожидается в `/app/model/` внутри контейнера;
- локально в `AudioToText\model\` есть только `config.json`, `modeling_gigaam.py`, `tokenizer.model` — **весов нет**.

CPU-флаги (`CUDA_VISIBLE_DEVICES=""`, `model.to("cpu")`, пустой `devices: []`) были устаревшими и сняты. Канон конвейера SKKKK — `asr/run_gigaam.py` (тоже CUDA по умолчанию), см. `docs/DECISIONS.md`.

### `C:\1C_NEW_SKKKK\asr`

Скрипты ансамбля уже написаны:

| Движок | Скрипт | Модель | Вход |
|---|---|---|---|
| Whisper | `run_whisper.py` | `bond005/whisper-large-v3-ru-podlodka` | 16 кГц |
| GigaAM | `run_gigaam.py` | `ai-sage/GigaAM-v3`, revision `e2e_rnnt` | 16 кГц |
| T-one | `run_tone.py` | сначала пакет `tone`, иначе sherpa-onnx | 8 кГц |

`engines.json` указывает на `C:\1C_NEW_SKKKK\venvs\{whisper,gigaam,tone}` — этих venv на диске **нет**. `prepare_audio.py` уже прогонялся: есть `asr\work\input_meta.json` (источник `input.webm`, 853.63 с).

T-one Windows-fallback: в `asr\models\t-one\` скачан только `tokens.txt`. Нет `model.onnx`. Кэш HF `models--csukuangfj--sherpa-onnx-streaming-t-one-russian-2025-09-08` содержит лишь `refs/main`.

Кэши HF для Whisper Podlodka и GigaAM-v3 тоже неполные (config / modeling, без шардов весов).

## 4. Что брать в SKKKK, а что нет

### Брать

1. **Факт, что CUDA на этой карте уже работала** через системный `torch 2.2.1+cu121`. Для трёх ASR-venv ставить PyTorch с тем же индексом `cu121` (как в `asr/SETUP.md`), но на **Python 3.11**, не подключая системный 3.10.
2. **FFmpeg full build** `C:\ffmpeg-8.0-full_build\bin\` — проверенный бинарник с NVENC. Для ASR нужен только decode/resample в WAV (уже в `prepare_audio.py`). Имеет смысл добавить этот `bin` в PATH воркера, если `ffmpeg` в PATH иногда пропадает. **Не** копировать `hevc_nvenc`-пайплайн в транскрипцию.
3. **Паттерн загрузки GPU из рабочих LLM-скриптов** (без копирования файлов): `torch.cuda.is_available()` → `device_map="auto"` / `.to("cuda")` + fp16/bf16. Именно так уже сделано в `asr/run_whisper.py` и `asr/run_gigaam.py`.
4. **Логика GigaAM** из `AudioToText\main.py` уже перенесена в `run_gigaam.py`: GPU (CUDA + float16), путь модели HF, fallback с `transcribe_longform` на `transcribe`. CPU только если CUDA недоступна.

### Не брать

- `encoder.py` — видеокодек, не speech-to-text.
- `AudioToText` как runtime конвейера SKKKK: Docker-пути `/app/model/`, нет нативных Windows-venv. Сам GigaAM там GPU, но канон — `asr/run_gigaam.py`.
- Системный Python 3.10 и его site-packages в SKKKK (инвариант: Python 3.11 + отдельные venv на движок).
- `C:\1c_analitik\.venv` (`torch+cpu`).
- Пустые `.venv` PyCharm-проектов ENCODER / SORA / AI_local.
- Официальный pip-пакет с именем `tone` с PyPI без проверки: на Windows это ловушка («Deal with tone»). В `run_tone.py` уже есть запасной sherpa-onnx.

### T-one / Whisper / GigaAM — конкретные дыры, которые PyCharm не закрывает

Нужно дособрать в `C:\1C_NEW_SKKKK`, а не искать в старых проектах:

1. Создать `venvs\whisper`, `venvs\gigaam`, `venvs\tone` по `asr/SETUP.md`.
2. Докачать веса:
   - Whisper: `bond005/whisper-large-v3-ru-podlodka`
   - GigaAM: `ai-sage/GigaAM-v3` revision `e2e_rnnt`
   - T-one: `csukuangfj/sherpa-onnx-streaming-t-one-russian-2025-09-08` → `asr\models\t-one\model.onnx` рядом с уже лежащим `tokens.txt`
3. Перед прогоном: `lms unload --all`, три процесса одновременно, Gemma после ASR.

Whisper в SKKKK идёт через `transformers.pipeline` на CUDA, не через faster-whisper/CTranslate2. Это сознательно: Podlodka — HF-модель. Конфликт CTranslate2/PyTorch из плана остаётся аргументом за **три процесса / три venv**, даже если faster-whisper в этом дереве не установлен.

## 5. Как в PyCharm запускали CUDA (для справки)

Общий рабочий рецепт, который уже доказан на этой машине:

```
интерпретатор: C:\Users\rene_\AppData\Local\Programs\Python\Python310\python.exe
torch:         2.2.1+cu121
запуск:        PyCharm Run (не терминальный activate .venv проекта)
проверка:      torch.cuda.is_available() / NVENC в ffmpeg -encoders
```

Для SKKKK тот же GPU, другой Python и три изолированных venv. Копировать файлы из `C:\PythonProject_ENCODER` в `app/` не нужно.
