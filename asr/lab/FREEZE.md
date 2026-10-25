# Снимок лаборатории — 2026-08-26

Три принятых этапа на одном эталонном прогоне
`20260826T174104Z_48a796` (артефакт `art_2026-08-19_211842_54f88c`, проект 1).
Менять молча нельзя ни один.

| Этап | Что принято | Где лежит |
|------|-------------|-----------|
| **1** | Конвейер `01_audio` → `07_verify` без DeepFilterNet | `01_audio/` … `07_verify/`, `run.json` |
| **2** | Прогон Cursor: смысловая разноска спорных слов | только `sense/` |
| **3** | Свой прогон GigaAM и сверка с ним | `giga/`, `reconcile/`, `proof/` |

Этап 2 **не** переписывает `02_asr/words.json`, `04_paint/paint.json`,
`06_transcript/transcript.json`. Этап 3 **не** переписывает ни снимок 01–07,
ни `sense/`. Страница `report-lab` показывает слои поверх этапа 1.

Машиночитаемая копия: `asr/lab/freeze.json`. В прогоне: `freeze.json` (этап 1),
`sense/freeze.json` (этап 2), `proof/freeze.json` (этап 3).

## Этап 1 — конвейер 01–07 (принят)

```powershell
cd D:\Primary_Analitics
$env:PYTHONIOENCODING='utf-8'
venvs\lab\Scripts\python.exe asr\lab_pipeline.py `
  --project-id 1 `
  --artifact-id art_2026-08-19_211842_54f88c `
  --no-voice `
  --report
```

`--no-voice` — стадия `05_voice` (WeSpeaker / Docker) в этом снимке не входила.

## Что считалось

| Стадия | Суть | Параметры |
|--------|------|-----------|
| `01_audio` | копия исходника 16 кГц, **без** DeepFilterNet | `mode=copy_16k` |
| `02_asr` | Parakeet-TDT 0.6B v3, окна | `window_sec=80`, `overlap_sec=8`, `quantization=null`, `device=auto` |
| `03_diar` | Sortformer 4spk v2.1 ONNX | onset 0.64, offset 0.74, pad_onset 0.06, min_duration_on 0.10, min_duration_off 0.15, без сверки с исходником |
| `04_paint` | слово × вероятности | min_prob 0.35, min_margin 0.15 |
| `06_transcript` | реплики без LLM | max_gap 1.2, keep_unassigned true |
| `07_verify` | второй проход Parakeet + Silero VAD | max_speech_duration_s 80 |

Исходник: `audio/input_16k.wav`
sha256 `d9b84c208b383ba5199cf3ba18553deafe073bf9472aec64ae22d333b64bd0e0`, 76852990 байт, 2401.65 с.

## Среда

- Python **3.10.9** (`venvs\lab\Scripts\python.exe`)
- Windows 10.0.26200, GPU NVIDIA GeForce RTX 3090 24 ГБ, драйвер 577.00
- CUDA Toolkit **12.8** (nvcc 12.8.61)
- cuDNN **9.16.0.29** (`nvidia-cudnn-cu12`, `cudnn64_9.dll`)
- onnxruntime-gpu **1.23.2**, сессия на `CUDAExecutionProvider`
- ffmpeg **9.0-full_build** (WinGet Gyan.FFmpeg)

## Библиотеки, которые участвуют в снимке

| Пакет | Версия |
|-------|--------|
| onnxruntime-gpu | 1.23.2 |
| nvidia-cudnn-cu12 | 9.16.0.29 |
| onnx-asr | 0.12.0 |
| sentencepiece | 0.2.1 |
| numpy | 1.26.4 |
| scipy | 1.15.3 |
| librosa | 0.11.0 |
| soundfile | 0.13.1 |
| soxr | 1.0.0 |
| huggingface-hub | 0.36.0 |

Полный `pip freeze` этого venv — в `asr/lab/freeze.json` → `pip_freeze`.
В venv ещё стоят `torch` / `DeepFilterNet` с прошлой установки; **в этом прогоне они не вызывались**.

## Модели (sha256 файлов на диске)

Hugging Face:

- Parakeet ONNX: `istupakov/parakeet-tdt-0.6b-v3-onnx` (revision `8f23f0c`)
- Sortformer: `Scrybl/diar_streaming_sortformer_4spk-v2.1` revision `3be92087de17e4e9063c9cfabc104f9809b2d037`
- Silero VAD: `istupakov/silero-vad-onnx`

| Файл | sha256 | байт |
|------|--------|------|
| encoder-model.onnx | `98a74b21b4cc0017c1e7030319a4a96f4a9506e50f0708f3a516d02a77c96bb1` | 41770866 |
| encoder-model.onnx.data | `9a22d372c51455c34f13405da2520baefb7125bd16981397561423ed32d24f36` | 2435420160 |
| decoder_joint-model.onnx | `e978ddf6688527182c10fde2eb4b83068421648985ef23f7a86be732be8706c1` | 72520893 |
| vocab.txt | `d58544679ea4bc6ac563d1f545eb7d474bd6cfa467f0a6e2c1dc1c7d37e3c35d` | 93939 |
| silero_vad.onnx | `1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3` | 2327524 |
| diar_streaming_sortformer_4spk-v2.1.onnx | `0182539aa7f8ecfc446ec56eb320b6f9131d686f98ca8f40644830af7d4d81c8` | 492243708 |

## Результат эталона

4610 слов, 164 реплики, 4 спикера Sortformer, покраска 4503 / 67 спорных / 40 бесхозных.
Проверка VAD: `warn`, 1 слово вне речи, сходство проходов 0.926.

### Изменение 2026-09-28: порог `07_verify` (решение пользователя)

Провал проверки считается только по словам **на тишине**: самый громкий 50-мс
кадр под словом на 20 дБ тише медианы слов, которые подтвердил VAD (и всё тише
−50 дБFS). Порог прежний — 2% слов. Слова на громком звуке, который VAD не
принял за речь, дают `warn` с участками времени в причине.

Причина — созвон проекта 40 (`art_2026-09-28_153949_f37f3e`): 3 минуты голоса,
искажённого сбоем связи, не услышали ни Silero, ни диаризация. Parakeet
разобрал связный текст, а прежнее правило остановило обработку всей записи
(261 слово вне речи, 5,7%; на тишине из них 6). На 24 прочих прогонах стенда
вердикты по новому правилу не изменились. Остальные параметры этапа 1 те же.

## Этап 2 — прогон Cursor (принят)

Смысловая разноска спорных и бесхозных слов. Текст и таймкоды не меняются,
меняется только `speaker_index`. Пишет исключительно `sense/`.

```powershell
cd D:\Primary_Analitics
venv\Scripts\python.exe asr\lab_sense.py `
  --project-id 1 `
  --artifact-id art_2026-08-19_211842_54f88c `
  --run-id 20260826T174104Z_48a796
```

Среда этапа 2 — основной `venv` (там `cursor-sdk`), не `venvs\lab`.

| Что | Значение |
|-----|----------|
| Python | 3.10.9 (`venv\Scripts\python.exe`) |
| cursor-sdk | 1.0.28 |
| LLM | `LLM_PROVIDER=cursor`, промпт `docs/prompts/lab_sense_speakers.md` |
| Куски | 77 spans, пауза рвёт кусок при gap > 1.5 с, контекст ±14 слов, пачки по 10 |
| Решения | 78 (8 пачек Cursor) |
| Спорных слов в снимке | 107 (67 contested + 40 unassigned) |
| Присвоено оверлеем | 106 |
| Реплик | 94 (в снимке этапа 1 было 164) |

sha256 файлов этого прогона — доказательство, что этап 1 не сдвинулся:

| Файл | sha256 |
|------|--------|
| `04_paint/paint.json` | `11063a9163183cd0937153f479839b0ee8ddf811a110791e068d6ac714239621` |
| `06_transcript/transcript.json` | `f0d27462453c62c144adc3163df467af71da5218ff6cfdc95a2d771961f2de44` |
| `sense/spans.json` | `0e1a600c28454a4911ddf1ba720227d73dc4cd339118b8122c15ec6c787b78ca` |
| `sense/sense.json` | `b9c48b0739dbfde80f12ace9d59c9c1a450316b9331ee0a8a5c3893915e84628` |
| `sense/transcript.json` | `b2cb21419c346d2c9e2de5a4297e58291b4eedff35505efd5caae204157522ba` |

Страница: `http://127.0.0.1:777/artifacts/art_2026-08-19_211842_54f88c/report-lab?run=20260826T174104Z_48a796`

## Этап 3 — сверка с GigaAM (принят)

Свой прогон GigaAM на аудио того же прогона и построчная сверка с ним. Этапы 1 и
2 не переписываются: этап 3 пишет только `giga/`, `reconcile/`, `proof/`.

```powershell
$args = "--project-id 1 --artifact-id art_2026-08-19_211842_54f88c --run-id 20260826T174104Z_48a796"
venv\Scripts\python.exe asr\lab_giga.py $args.Split()
venv\Scripts\python.exe asr\lab_reconcile.py $args.Split()
venv\Scripts\python.exe asr\lab_proof.py $args.Split()
```

### 3a — что услышал GigaAM

| Что | Значение |
|-----|----------|
| Контейнер | `primaryanalitics-gigaam`, `asr_backend=pytorch`, `asr_model=v3_e2e_rnnt` |
| Диаризация | pyannote, `DIARIZATION_MIN_SPEAKERS=4`, `MAX=8`, `num_speakers` не задавали |
| Аудио | `01_audio/clean_16k.wav` того же прогона, загружено как `lab_<run_id>_clean_16k.wav` |
| Время | 127 с на 2400 с записи |
| Реплики | 138, спикеров 6 (`giga_1,2,4,5,7,8`), перекрытий нет |
| Символов | 31 560 (у Parakeet 28 950) |

Таймкоды GigaAM округлены до секунды — в итоговую стенограмму его нарезка **не**
идёт, только мнение о спикере и вариант слова.

### 3b — приговоры по спорным кускам

Спор закрывается только по **однородной** реплике GigaAM: если внутри неё уже
лежат уверенные слова этапа 1 разных лабораторных спикеров, реплика считается
диалогом и судьёй быть не может. Ответы этапа 2 в проверке чистоты не голосуют —
иначе спорный кусок оценивал бы сам себя.

| Порог | Значение |
|-------|----------|
| Карта `giga_* → speaker_*` | доля большинства ≥ 0.75, минимум 5 голосов |
| Чистота реплики | ≥ 0.9 при минимум 5 уверенных словах внутри |
| Запас от границ реплики | ≥ 0.5 с |

Результат на эталоне: 20 подтверждено, 5 конфликтов, 51 без вердикта, 1 открыт.
Карта: `giga_1,giga_8 → speaker_0`; `giga_2,giga_4,giga_5 → speaker_1`;
`giga_7` ни с кем не сошёлся. `speaker_2` и `speaker_3` своего спикера у GigaAM
не получили — pyannote дробит и сливает голоса иначе.

### 3в — вычитка текста

Одно выравнивание на всю запись (без окон: границы реплик у движков не
совпадают). Выбор всегда между двумя услышанными вариантами, свой текст модель
писать не может — `parse_picks` принимает только `lab` или `giga`.

| Что | Значение |
|-----|----------|
| Промпт | `docs/prompts/lab_proof_text.md` |
| LLM | `LLM_PROVIDER=cursor`, температура 0, пачки по 24 |
| Расхождений | 487, из них 58 косметика (регистр, «ё», разбивка токенов) |
| Спрошено | 429 |

Косметикой считается пара, у которой нормализованный текст совпадает: `битрикс` /
`Битрикс`, `еще` / `ещё`, `в 1С` одним токеном против двух.

Итог — `proof/transcript.txt`: спикеры и времена лабораторные, текст там, где
GigaAM услышал лучше, — его. На эталоне: 429 спрошено, у GigaAM взято 119,
своё оставлено 310, изменено 156 слов.

sha256 файлов этапа 3 — доказательство, что этапы 1 и 2 не сдвинулись:

| Файл | sha256 |
|------|--------|
| `04_paint/paint.json` | `11063a9163183cd0937153f479839b0ee8ddf811a110791e068d6ac714239621` |
| `sense/sense.json` | `b9c48b0739dbfde80f12ace9d59c9c1a450316b9331ee0a8a5c3893915e84628` |
| `giga/giga.json` | `7e1df19c429d3cbdf720ef098ae286d2ad0d441d80e9bdccd3043327a0ea4960` |
| `giga/timecodes.txt` | `5327da40ea93a139f503f20f0274fdd2b40d997805be573ab011973823d22286` |
| `giga/plain.txt` | `2c9a8e35ecf08d8f2e7246cb53b3cf2adbb7cdb864b7d2b26d133975567641ba` |
| `reconcile/speakers.json` | `cd013e90d12021dcd09f313d26e3e648d8e5a4a1269f0def45e230aef0ea1004` |
| `reconcile/text.json` | `d089a4011f4a3eb26066a78cb076dabb5e90fa956ee0b52dc5e1883232f99fc8` |
| `proof/proof.json` | `ac3238ea9531916c45262680705cd79f7025be190dd519f3c9aaf47b334deaa3` |
| `proof/transcript.json` | `d8d3ebc197d0245d9a03a9b810ab50d61fa37d517aaa416d943a0f7a15d0b7c8` |
| `proof/transcript.txt` | `958b20c479b43ef436bf8a5e2d08c7ffe764a1f19c1c078992e3a0ea99bb5fcc` |

Задача контейнера: `341f9eb8a24f4fcf8fa507c9d7c12413` (126.7 с).

## Не этап: имена с боя

`asr/lab_hint.py` может подписать `speaker_0…N` именами из
`docker_gigaam_speakers.json`. Это **не** принятый этап: нарезка прода не
копируется. Пишет только `hint/names.json`.
