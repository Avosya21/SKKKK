# Лаборатория расшифровки

Канонический конвейер: Parakeet-TDT v3 → Sortformer 4spk v2.1 → покраска слов →
Cursor sense → сверка GigaAM → WeSpeaker. Очистки звука нет — в прогон кладётся
копия исходного 16 кГц.

Снимок стадий 01–07 / sense / proof: `asr/lab/FREEZE.md`. Боевой оркестратор
воркера: `app/app/services/pipeline_lab.py`. Диагностика: `/report-lab`.
Пользователь после загрузки открывает `/report`.

См. `docs/DECISIONS.md`, инвариант 1 и раздел «Лаборатория расшифровки».

## Установка окружения

Отдельный venv нужен из-за `onnxruntime-gpu`: он конфликтует с обычным
`onnxruntime` из `app/requirements.txt` — это один и тот же модуль.

```powershell
cd C:\1C_NEW_SKKKK
py -3.10 -m venv venvs\lab
venvs\lab\Scripts\python.exe -m pip install -U pip
venvs\lab\Scripts\python.exe -m pip install -r asr\lab\requirements.txt
```

Проверка, что ONNX Runtime видит GPU:

```powershell
venvs\lab\Scripts\python.exe -c "import onnxruntime as o; print(o.__version__, o.get_available_providers())"
```

В списке должен быть `CUDAExecutionProvider`. Если его нет, стадии с
`--device cuda` упадут с понятным сообщением, а не уползут молча на CPU.

`onnxruntime-gpu` нужен **cuDNN 9** (`cudnn64_9.dll`). CUDA Toolkit его не
кладёт. Пакет уже в `requirements.txt`:

```powershell
venvs\lab\Scripts\python.exe -m pip install nvidia-cudnn-cu12==9.16.0.29
```

## Модели

Скачиваются при первом запуске в `data/models/`:

| Модель | Куда | Размер |
|--------|------|--------|
| `nemo-parakeet-tdt-0.6b-v3` | `data/models/onnx-asr/nemo-parakeet-tdt-0.6b-v3/` | ~2.4 ГБ |
| Silero VAD | `data/models/onnx-asr/silero-vad/` | ~2 МБ |
| `diar_streaming_sortformer_4spk-v2.1.onnx` | `data/models/onnx-diar/` | ~460 МБ |

Sortformer проверяется по sha256 — битая докачка не пройдёт дальше загрузки.

## Запуск

Полный прогон артефакта:

```powershell
venvs\lab\Scripts\python.exe asr\lab_pipeline.py --project-id 1 --artifact-id art_2026-08-19_211842_54f88c --report
```

Стадия `05_voice` требует контейнер `skkkk-gigaam`. Без Docker:

```powershell
... --no-voice
```

Повторить диаризацию с другими порогами в том же прогоне:

```powershell
... --run-id 20260826T101500Z_ab12cd --only 03_diar --onset 0.55 --offset 0.65 --force
... --run-id 20260826T101500Z_ab12cd --from 04_paint
```

Список прогонов и состояние стадий:

```powershell
... --list
... --run-id 20260826T101500Z_ab12cd --status
```

Страница: `/artifacts/{artifact_id}/report-lab`, конкретный прогон —
`?run=20260826T101500Z_ab12cd`.

## Этап 3 — сверка с GigaAM

Три шага по очереди. Первый нужен контейнер `skkkk-gigaam`, третий — Cursor API.

```powershell
$common = "--project-id 1 --artifact-id art_2026-08-19_211842_54f88c --run-id 20260826T174104Z_48a796"
venv\Scripts\python.exe asr\lab_giga.py $common.Split()        # прогон GigaAM → giga/
venv\Scripts\python.exe asr\lab_reconcile.py $common.Split()   # приговоры по спорным → reconcile/
venv\Scripts\python.exe asr\lab_proof.py $common.Split()       # вычитка текста → proof/
```

`lab_proof.py --dry-run` собирает расхождения без обращения к Cursor,
`--limit N` — проба на первых N расхождениях.

Что этап 3 может и чего не может:

* спорный кусок закрывается только по **однородной** реплике GigaAM: если внутри
  неё уже лежат наши уверенные слова разных спикеров, реплика считается диалогом
  и судьёй быть не может;
* нарезка и таймкоды GigaAM в итог не идут — у него точность до секунды;
* в вычитке выбор всегда между двумя услышанными вариантами, третьего нет;
* итоговая стенограмма — `proof/transcript.txt`, спикеры и времена лабораторные.

## Этап 4 — голоса в ту же БД

Слепки считает тот же WeSpeaker в `skkkk-gigaam`, что и прод. Клипы — прогоны
уверенных слов снимка плюс разноска Cursor, не сырые сегменты Sortformer.
Стадию `05_voice` не перезапускаем.

```powershell
venv\Scripts\python.exe asr\lab_voice.py --project-id 1 --artifact-id art_2026-08-19_211842_54f88c --run-id 20260826T174104Z_48a796
```

`--reuse-embeddings` пересчитывает подсказки из `voice/embed_raw.json` без Docker.

CLI в БД не пишет. На `/artifacts/{id}/report-lab?run=…` карточки **Запомнить** /
**Слить** вызывают `POST /api/artifacts/{id}/lab/{run_id}/speaker-enroll` и
`…/speaker-merge`. Метка в links — `speaker_N`. Файл — `voice/match.json`,
боевой `reports/sortformer_speaker_match.json` не трогается.

## Раскладка прогона

```
data/projects/{pid}/artifacts/{aid}/lab/
├── latest.json
└── runs/20260826T174104Z_48a796/
    ├── run.json
    ├── freeze.json          # этап 1
    ├── report.html
    ├── logs/{stage}.log
    ├── {стадия}/            # 01_audio … 07_verify
    ├── sense/               # этап 2 (Cursor), снимок 01–07 не трогает
    │   ├── spans.json
    │   ├── sense.json
    │   └── transcript.json
    ├── giga/                # этап 3a: свой прогон GigaAM
    │   ├── giga.json        # реплики, спикеры, таймкоды, метаданные задачи
    │   ├── timecodes.txt
    │   ├── plain.txt
    │   ├── api_result.json
    │   └── raw/             # всё, что отдал контейнер
    ├── reconcile/           # этап 3b: приговоры по спорным кускам
    │   ├── speakers.json
    │   └── text.json
    ├── proof/               # этап 3в: вычитка текста
    │   ├── diffs.json
    │   ├── proof.json
    │   ├── transcript.json
    │   └── transcript.txt   # итоговая стенограмма
    └── voice/               # этап 4: слепки WeSpeaker, свой match
        ├── voice.json
        ├── match.json       # не боевой sortformer_speaker_match.json
        └── embed_raw.json
```

`stage.json` каждой стадии содержит sha256 всех входов и выходов, параметры,
метрики и отпечаток. Повторный запуск с теми же входами и параметрами
пропускается; изменился хоть один байт входа или один порог — стадия считается
заново.

## Что делать, если стадия упала

Сообщение об ошибке лежит в `stage.json` (`error`, `traceback`) и в
`logs/{stage}.log`, статус стадии на странице становится красным.

* `нет входа «clean_16k»` — стадия `01_audio` не отработала (рабочая копия 16 кГц).
  Подставлять файл из другого прогона нельзя, запустите стадию заново.
* `исходник изменился с начала прогона` — `input_16k.wav` артефакта
  перезаписали. Нужен новый прогон, старый уже не воспроизводим.
* `проверка не пройдена` на `07_verify` — больше 2% слов попали в тишину. Это
  результат, а не поломка: смотрите блок «Слова вне речи». Тишина — звук под
  словом на 20 дБ тише обычной речи этой записи (и всё тише −50 дБFS). Слова на
  громком звуке, который VAD не принял за речь (голос, искажённый сбоем связи),
  провала не дают: вердикт `warn` и участки времени в причине.
* Hugging Face `ReadTimeout` на `encoder-model.onnx.data` — таймаут по умолчанию
  10 с. Пайплайн ставит `HF_HUB_DOWNLOAD_TIMEOUT=600`. Либо скачайте файлы
  вручную в `data/models/` (см. таблицу выше).
* `cudnn64_9.dll` missing — не установлен `nvidia-cudnn-cu12` в `venvs\lab`.
* `Attempting to broadcast ... 9999 by ...` — старый прогон без нарезки на окна.
  Обновите код и перезапустите `02_asr`.

## Тесты

```powershell
venv\Scripts\python.exe -m pytest asr/tests/test_lab_contract.py asr/tests/test_lab_stages.py asr/tests/test_lab_sense.py asr/tests/test_lab_giga.py asr/tests/test_lab_reconcile.py asr/tests/test_lab_proof.py asr/tests/test_lab_voice.py app/app/tests/test_lab_report.py app/app/tests/test_lab_enroll.py -q -n 0
```

Тесты не требуют ни моделей, ни GPU, ни Docker.
