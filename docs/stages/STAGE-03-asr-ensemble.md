# Этап 3. Три параллельных ASR и склейка LLM

## Цель

Аудио превращается в финальную стенограмму через **ансамбль из трёх движков** (Whisper, T-one, GigaAM), работающих одновременно, с последующей склейкой через **LLM** (`lmstudio` → Gemma; `cursor` → Cursor API). Это ядро системы, самый рискованный этап.

## Предусловия

Этап 2 зелёный. Этап 0: для **lmstudio** — `lms unload/load`, JIT выключен; для **cursor** — `CURSOR_API_KEY`, preflight `llm` зелёный.

## Ключевые архитектурные решения этапа

**Три процесса, а не три потока.** faster-whisper тянет CTranslate2 со своей сборкой CUDA/cuDNN, GigaAM и T-one — PyTorch со своей. В одном процессе Windows это конфликт DLL и падение при инициализации второй модели. Каждый движок запускается как отдельный процесс, все три — одновременно. Дополнительная выгода: выход процесса гарантированно возвращает VRAM, чего `torch.cuda.empty_cache()` на Windows не обеспечивает.

**Склейка по окнам.** Каждое окно 120–180 с склеивается отдельным запросом LLM. В режиме lmstudio — ещё лимит VRAM рядом с Gemma; в cursor — запросы в API без локальной модели.

## Шаги

1. **Подготовка аудио** `services/audio.py`:
   - FFmpeg → мастер WAV 16 кГц моно;
   - VAD по мастеру: границы речи, паузы длиннее 2 секунд вырезаются;
   - нарезка на окна 120–180 секунд **строго по границам тишины**, не по фиксированному времени, чтобы окно не рвало слово;
   - для каждого окна два файла: 16 кГц (Whisper, GigaAM) и 8 кГц (T-one), полученные ресемплом из одного и того же мастер-куска;
   - запись окон в `transcript_windows`: индекс, начало, конец;
   - результат в `tmp/audio_clean/`.

2. **Освобождение GPU** `workers/gpu_phase.py` (**только lmstudio**):
   - `lms unload --all`;
   - ожидание падения занятости по `nvidia-smi`;
   - в режиме **cursor** шаг пропускается (`asr/run_pipeline.py` помечает `gemma_unload` ok).

3. **Три процесса ASR** (прототипы уже лежат в `asr/`, установка — [../../asr/SETUP.md](../../asr/SETUP.md)):
   - `run_whisper.py` — `bond005/whisper-large-v3-ru-podlodka`, 16 кГц;
   - `run_tone.py` — `t-tech/T-one` на 8 кГц (пакет `tone`, на Windows запасной путь через sherpa-onnx);
   - `run_gigaam.py` — `ai-sage/GigaAM-v3`, ревизия `e2e_rnnt`, 16 кГц;
   - у каждого движка своё виртуальное окружение (`venvs\whisper`, `venvs\gigaam`, `venvs\tone`);
   - у каждого одинаковый контракт: на вход путь к окну (или к списку окон), на выход JSON `{window_index, text, engine, duration_sec}` в stdout или файл;
   - запуск всех трёх одновременно, ожидание всех, сбор результатов;
   - падение одного движка не отменяет остальных: пишется `asr_runs` со статусом `error` для этого движка;
   - полные тексты по движкам сохраняются в `parsed/calls/{id}_raw_whisper.txt`, `_raw_tone.txt`, `_raw_gigaam.txt`.

4. **Статус `asr_completed`**, все процессы завершены, VRAM свободна.

5. **Загрузка LLM для склейки** (**lmstudio only**): `lms load google/gemma-4-31b-qat`. В **cursor** — шаг `gemma_load` пропускается.

6. **Склейка по окнам** (`asr/merge_ensemble.py` → `asr/llm_client.py`): промпт `docs/prompts/merge_transcript.md`. LLM восстанавливает слова ASR, не перефразирует, не размечает спикеров.
7. Сшить окна. Наложить интервалы PyAnnote (GigaAM diarize) на текст: сглаживание коротких островков, overlay по таймкодам (`asr/speakers.py`). Match Sortformer → WeSpeaker, voice turn audit. Результаты окон сшиваются в `parsed/calls/{id}_final.txt` **репликами**, плюс `{id}_speakers.json`. Статус `transcript_ready`.

7.1. **Индексация** (модуль 5): чанки с именами спикеров → sqlite-vec через Ollama `bge-m3`.

7.2. **Итоговая выжимка**: после индекса, LLM по тексту стенограммы (`asr/summarize_call.py`, `docs/prompts/call_summary.md`). Map/reduce на длинных созвонах.

7. **Граничный случай «тишина»**: если все три ASR пустые — LLM не вызывается.

7.2. **Защита от галлюцинаций на паузе** (`asr/hallucination_guard.py`, `asr/silence_energy.py`):
   - до Gemma — схлопнуть петли ASR, не слать окно если речь по диаризации∩энергии почти нулевая и жив один движок;
   - после Gemma и при навеске на спикеров — бюджет символов ∝ секундам речи, выкинуть реплики с невозможной плотностью;
   - таймлайн WAV не сжимать (плеер = исходник). См. [DECISIONS.md](../DECISIONS.md).

7.3. **Привязка к времени**: UI и `_final.txt` собираются через `asr/timing_align.py` — часы Whisper words, спикеры Sortformer/PyAnnote, текст Gemma на сетке Whisper. Не пропорциональный carve окна по диаризации.

8. **Telegram** — реальная отправка уведомления загрузившему.

## Что появляется на диске

```
data\projects\{id}\parsed\calls\
    art_..._raw_whisper.txt
    art_..._raw_tone.txt
    art_..._raw_gigaam.txt
    art_..._final.txt
    art_..._speakers.json
    art_..._speaker_map.json     (после ручной подписи имён, может отсутствовать)
    art_..._call_summary.md      (итоговая выжимка, только из текста финала)
data\projects\{id}\tmp\audio_clean\   (пусто после завершения)
```

Три raw обязательны. Финал — реплики с таймкодами/именами, не простыня без спикеров.

## Полезные проекты на GitHub

Конкретные модели ансамбля уже зафиксированы в [DECISIONS.md](../DECISIONS.md) (Whisper `bond005/whisper-large-v3-ru-podlodka`, GigaAM v3, T-one, GigaAMGUI-контейнер) — их не менять без пользователя. Ниже — проекты, которые помогают с обвязкой вокруг уже выбранных моделей.

| Проект | Что даёт | Как используем |
|---|---|---|
| [pyannote/pyannote-audio](https://github.com/pyannote/pyannote-audio) | Сам движок диаризации `speaker-diarization-3.1` и модель эмбеддингов WeSpeaker, уже используемые в `data/gigaam/dump_pyannote.py` | Первоисточник API `Pipeline.instantiate`, параметров кластеризации (`threshold`, `min_cluster_size`) и `pipeline._embedding` — сверяться при апгрейде версии пакета |
| [m-bain/whisperX](https://github.com/m-bain/whisperX) | Готовый паттерн выравнивания «слово → спикер» по пересечению таймкодов и обвязка VAD-препроцессинга | Мы разошлись с ним архитектурно (три ASR + голосовая ресегментация вместо одного Whisper + пересечение по словам), но `assign_word_speakers` — хороший образец для юнит-тестов `overlay_text` в `asr/speakers.py` |
| [SYSTRAN/faster-whisper](https://github.com/SYSTRAN/faster-whisper) | CTranslate2-бэкенд Whisper — основа для запасного хостового пути `run_whisper.py`, если контейнер `skkkk-whisper` недоступен | Смотреть их обработку `vad_filter` и `word_timestamps=True` для нарезки окон по границам тишины |
| [snakers4/silero-vad](https://github.com/snakers4/silero-vad) | Лёгкая VAD-модель (ONNX, CPU) для нарезки пауз > 2 с и границ окон 120–180 с | Кандидат для `services/audio.py`, если текущей VAD-логики (WebRTC VAD / встроенный VAD движков) не хватит по точности на телефонных записях |
| [dubr1k/GigaAMGUI](https://github.com/dubr1k/GigaAMGUI) | Уже канон для контейнера `skkkk-gigaam` (см. DECISIONS.md) | Источник `api.py` и `Dockerfile` — смотреть при обновлении версии GigaAM или добавлении новых эндпоинтов (например, будущего `/summarize`) |

## Проверки

Тестовый набор: короткий чистый файл (2–3 минуты), длинный реальный созвон (60+ минут), файл с тишиной, телефонная запись плохого качества.

| Проверка | Действие | Ожидание |
|---|---|---|
| Полный ансамбль | Обработать короткий wav | Три raw непустые, `_final.txt` репликами, есть `_speakers.json` |
| Параллельность | Во время ASR смотреть диспетчер задач | Три процесса Python работают **одновременно**, не по очереди |
| VRAM в фазе ASR | `nvidia-smi` во время ASR | Gemma не в памяти, занято примерно 6–8 ГБ |
| Возврат Gemma | После `asr_completed` | `lms ps` показывает загруженную модель, занято ~19–20 ГБ |
| Окна | Проверить `transcript_windows` | Окна покрывают всю длительность, без разрывов и наложений |
| Длинный файл | Созвон на час | Дошёл до `transcript_ready`, ошибок контекста нет |
| Падение движка | Временно переименовать `run_tone.py` | Whisper и GigaAM отработали, в `asr_runs` у T-one `error`, финал собран из двух с пометкой |
| Тишина | Файл без речи | Статус `done`, Gemma не вызывалась |
| Галлюцинация на паузе | Окно с тишиной + loop Whisper в raw | В `docker_merged_windows.json` у окна `skipped`, в финале нет абзаца за 2–6 с |
| Защита от чата | Отправить запрос в `/chat` во время ASR | Запрос ждёт, VRAM не растёт, обработка не падает |
| Качество | Сравнить `_final.txt` с тремя raw глазами на 5 минутах | Финал не короче лучшего из raw, нет выдуманных абзацев |
| Итоговая выжимка | `python asr/summarize_call.py`, открыть `transcript.html` в браузере, скриншот верха страницы | Блок «Итоговая выжимка созвона» сразу под заголовком, заголовки/списки/`**жирный**` отрендерены как HTML, не как сырой markdown или LaTeX |

Скриншот-проверка обязательна для этого этапа, не факультативна: `python asr/build_transcript_view.py --serve`, открыть `http://127.0.0.1:18104/out/transcript.html`, сделать скриншот всей страницы. Сверить визуально: цвета спикеров стабильны при перезагрузке страницы (не перемешиваются), реплики короче ~0.6 с не режут слово посередине, клик по реплике двигает плеер.

Проверка выдумок отдельно: в финале не должно быть **пересказанных** фраз и фактов, которых нет ни в одном черновике даже в искажённом виде. Восстановленное слово («синхронизация» вместо «синхранизацыя») — не выдумка. Пересказ своими словами — выдумка.

## Тесты

`asr/tests/test_hallucination_guard.py` и `asr/tests/test_silence_energy.py` (без GPU)

- `test_collapse_adjacent_sentence_repeat`
- `test_loop_detector_catches_whisper_spin`
- `test_density_impossible_on_screenshot_case`
- `test_prepare_skips_single_engine_on_quiet_window`
- `test_apply_merged_windows_drops_silence_novel`
- `test_sanitize_drops_zero_duration_sentence`
- `test_rms_speech_sec_silence_vs_tone`
- `test_effective_speech_takes_min_of_diar_and_energy`

`app/app/tests/test_speakers.py` (без GPU; прототип сейчас `asr/tests/test_speakers.py`)

- `test_parse_and_canonical`
- `test_smooth_aba_island`
- `test_overlay_assigns_by_overlap`
- `test_rename_merge_and_talk_time`

Резегментация по голосу (`data/gigaam/dump_pyannote.py::resegment_by_voice`, скользящее окно + гистерезис поверх центроидов PyAnnote-кластеров) живёт внутри контейнера `skkkk-gigaam` (torch + pyannote), автотестом без GPU не покрыта. Проверяется вручную по логу `voice resegmentation: N raw clusters -> M voice-timed turns` и глазами по стенограмме: один голос не должен менять номер спикера каждые пару секунд.

`app/app/tests/test_audio_prep.py` (без GPU)

- `test_master_wav_is_16k_mono`
- `test_tone_input_is_8k_mono`
- `test_windows_cover_full_duration_without_gaps`
- `test_windows_split_on_silence_not_midword` — на синтетическом файле с известными паузами.
- `test_long_pauses_removed`

`app/app/tests/test_asr_ensemble.py` (маркер `gpu`)

- `test_three_engines_produce_three_files`
- `test_engines_run_in_parallel` — суммарное время меньше суммы одиночных прогонов.
- `test_engine_failure_records_error_and_keeps_others`
- `test_vram_released_before_asr` — по `nvidia-smi` перед стартом ASR занято меньше порога.
- `test_gemma_reloaded_after_asr`
- `test_silent_audio_skips_llm`

`app/app/tests/test_summary.py` (без GPU, чистая логика; прототип сейчас — тесты для `asr/summarize_call.py:_chunk_text`)

- `test_chunk_text_splits_on_turn_boundaries_not_mid_turn`
- `test_single_chunk_skips_map_pass`
- `test_empty_transcript_raises`

`app/app/tests/test_merge.py` (маркер `gpu`, плюс часть на моках)

- `test_merge_prompt_contains_all_three_variants`
- `test_merge_marks_missing_engine` — при двух движках в промпте явная пометка о недоступном варианте.
- `test_merge_is_windowed` — на часовом входе в LM Studio ушло больше одного запроса, каждый в пределах лимита токенов.
- `test_final_transcript_has_no_unseen_ngrams` — эвристика: доля пятиграмм финала, отсутствующих во всех трёх черновиках, ниже порога.
- `test_merge_timeout_retries_then_errors_window` — обрыв одного окна не теряет остальные.

```powershell
pytest app -m "not gpu" -q      # быстрые
pytest app -m gpu -q            # на реальном железе, идут минуты
```

## Критерий готовности

- [ ] Для каждого аудио на диске три raw от разных движков, `_final.txt` репликами и `_speakers.json`
- [ ] Три процесса ASR подтверждённо работают одновременно
- [ ] Во время ASR Gemma выгружена, после ASR возвращена
- [ ] Часовой созвон проходит целиком, без обрезки контекста
- [ ] Падение одного движка не роняет обработку и фиксируется в `asr_runs`
- [ ] Финал не содержит выдуманных фрагментов сверх порога
- [ ] Telegram-уведомление приходит
- [ ] Финал размечен репликами по PyAnnote, а не догадками Gemma; на тестовой записи HTML-просмотр открывается и клик ставит плеер
- [x] Итоговая выжимка созвона строится из текста финала (`asr/summarize_call.py`, промпт `docs/prompts/call_summary.md`) и отображается в начале HTML-страницы

## Типичные отказы

| Симптом | Причина | Что делать |
|---|---|---|
| Второй движок падает при старте | Конфликт CUDA/cuDNN в одном процессе | Убедиться, что движки в отдельных процессах |
| CUDA out of memory в фазе ASR | Gemma не выгрузилась или JIT поднял её | Проверить ожидание освобождения VRAM и настройку JIT |
| Финал обрезан на середине | Склейка не по окнам | Проверить, что окна реально формируются |
| Финал «причёсан» до неузнаваемости | Промпт даёт слишком много свободы | Ужесточить формулировку про запрет додумывания |
| T-one сильно хуже прочих | Подан не 8 кГц | Проверить ресемпл |
| Всё очень медленно | Модели грузятся заново на каждое окно | Процессы ASR держат модель на весь файл; окна — только для склейки Gemma |
