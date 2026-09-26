# Ревизия планов v4 / v5 относительно кода

> Итоговый план, в который влиты выводы этой ревизии, — [`MEETING-PLAN.md`](MEETING-PLAN.md).

## v5 (`SKKKK-MEETING-ROOM-AND-TRANSCRIPT-PLAN-v5.md`) — итог

v5 закрывает все структурные замечания к v4: этапы A/B/C разделены, native/Egress/Sortformer/provenance вынесены из DoD A, порядок «identity → запись → events → meeting.json → созвон → Повторить» принят, `audio_level` заменён на rank. **Принимать как рабочий план этапа A** с поправками ниже.

### Что уже сделано в коде (рабочая копия, не закоммичено) — v5 этого не знает

Проверено 2026-09-13: `pytest test_meeting_recordings test_meeting_merge test_meeting_checks test_proof_spread test_meeting_lab test_meetings asr/tests/test_lab_proof -n 0` → **83 passed**.

| Этап v5 | Код | Статус |
|---|---|---|
| A0 baseline | `app/app/tests/fixtures/meeting/baseline_*` (meeting.json, events, track.wav, transcript, pytest) | ✔ есть |
| A2 gap pad, `started_epoch` один раз | `GAP_PAD_SEC`, `PcmWavWriter.on_gap`, `upsert_track` («у известной дорожки не трогаем») | ✔ |
| A3 `events.jsonl` | `MeetingEventLog`, `EVENT_SPEAKERS/MUTE/TRACK/PARTICIPANT/QUALITY/GAP`; `voice_cleaner` пишет join/leave/speakers/mute/quality/track/gap | ✔ код; **`voice_cleaner` не перезапущен** — живой процесс пишет старым кодом |
| A4 `meeting.json` | `_enqueue_meeting`: `meeting_started/ended_epoch`, `events`, `duration = meeting_duration_sec` (max), дорожки с `speaker`, `child_id`, `silent`; screen-дорожки со `speaker="external_or_unknown"` | ✔ |
| A8.1 rejected words | `meeting_merge.word_text` | ✔ |
| A8.2 `spread_tokens` | `asr/lab/proof.py` | ✔ |
| A8.3 реплики по полосам | `meeting_merge.merge_track_replicas` | ✔ |
| A8.4 `has_speech` | `meeting_recordings.has_speech`, `pipeline_meeting` пропускает `silent` и `screen` | ✔ |
| A9 checks | `meeting_checks.run_checks`, пороги через env `MEETING_CHECK_OK_SEC` / `MEETING_CHECK_WARN_SEC` / `MEETING_HEAD_LOSS_WARN_SEC` | ✔ |
| A10 leaks | `meeting_merge.find_leaks`, `proof/leaks.json` | ✔ (rank → RMS → UNRESOLVED — сверить с `resolve_leak`) |
| A1 identity гостя | `join_guest` — всё ещё `secrets.token_hex(16)` | ✘ |
| A6 комната (флаги, экран, звук, молчащий мик, качество, PWA, `meeting_layout.js`) | `meeting_room.js` без `TrackPublished`/`setSubscribed(false)`/`LocalAudioSilenceDetected`/`ConnectionQualityChanged` | ✘ |

Следствие: реальный остаток этапа A = **A1 → перезапуск `voice_cleaner` → CONTROL CALL #1 → A6 → Повторить → CONTROL CALL #2**. Порядок v5 (§23) правильный; объём кода — вдвое меньше, чем описано.

**Внимание — параллельная сессия пишет v4, а не v5.** Файлы правились 14:31–14:52 (во время этой ревизии), и в рабочей копии уже лежит объём этапов B/C, который v5 из A исключает: `meeting_overlap.py` (`resolve_same_room`, `assign_one_mic`, `LEAK_BY_DIAR`), `meeting_merge.source_turn_id` / `attach_turn_ids` (provenance §31 v4), screen-дорожки с `speaker="external_or_unknown"` в `meeting.json` (§28 v4), тесты `test_meeting_overlap.py`, `test_meeting_provenance.py`, `test_meeting_screen_audio.py`, `test_meeting_control_call.py`. Пока это чистые функции без GPU-фаз и без миграций схемы — вреда нет, но: (а) они не должны попадать в DoD A и в `PROJECT-MAP` как готовые фичи; (б) `turn_id` без колонки в `Entity` — заготовка, а не provenance; (в) решение «v4 или v5» надо принять до того, как та сессия дойдёт до Sortformer-fallback или Alembic. Комната (`meeting_room.js`, `meetings.py`, `meeting_public.html`) не тронута с утра.

### Поправки к v5

1. **§7 `sample_rate: 16000` → 48000** (та же ошибка, что в v4; код пишет `SAMPLE_RATE = 48000`). `meeting_id` — int. Контракт в коде уже шире, чем в v5: `speaker`, `child_id`, `silent`, `src` — описать его по коду, не по v4.
2. **§8 CONTROL CALL #1 проверяет не то.** Сценарии R1–R8 — это проверки правок комнаты (A6), которые на момент созвона #1 ещё не сделаны; R2–R8 упадут по замыслу. Зато в #1 **нет речевых фаз**, ради которых он и нужен для A8–A10: монолог, наложение (overlap), протечка через колонки ПК, перезаход. Состав #1 = R1 + фазы A–E из `final-plan.md` §4.1. R2–R8 — в CONTROL CALL #2.
3. **§10 A7 «REPEAT после правок UI» — пустой шаг.** «Повторить» гонит ASR по тем же WAV; правки `meeting_room.js` на записанные файлы влиять не могут. Регрессию комнаты ловят живые сессии с двух устройств после каждого шага A6 (Приложение A v4 §5) и созвон #2. Оставить один REPEAT — после A8–A10.
4. **§9 A6 потерял пункт 7 (invite/PWA)** из списка §1 и с ним — открытое решение A/B о хранении токена (v4 Приложение A §6). Без решения делается только манифест PWA и текст тоста; записать это явно.
5. **§9.4 `mic_silent`** — только через `setAttributes`, не через metadata (`_setDenoiseMeta` перезаписывает metadata целиком; парсит её `voice_cleaner.wants_denoise`).
6. **§12** — пороги для `head_loss` отдельные (env `MEETING_HEAD_LOSS_WARN_SEC`, 3 с), не 0.30/0.70; в списке нет `leaks`, `giga_share`, `no_started_epoch`, которые уже есть в `run_checks`.
7. **Операционное, чего нет в v5:** перезапуск `voice_cleaner` до созвона #1 (иначе `events.jsonl` не появится); поднять `?v=` у `meeting_room.js` в шаблонах на каждом шаге A6 (кэш телефонов); `git commit` рабочей копии до A1 — сейчас всё сделанное лежит незакоммиченным.
8. **§16 B1** «изучить, какой модуль применять» — ответ уже есть: `meeting-native-diar.md` ветка C (Sortformer на вырезанном окне через lab-стадию `03_diar`) + WeSpeaker `speaker_profiles`. Сослаться, не исследовать заново.

---

# Ревизия `SKKKK-MEETING-ROOM-AND-TRANSCRIPT-PLAN-v4.md` относительно кода

Дата: 2026-09-13. Проверялся файл `Downloads/SKKKK-MEETING-ROOM-AND-TRANSCRIPT-PLAN-v4.md` (§0–§42 + Приложение A) против текущего кода: `meeting_room.js`, `meetings.py`, `meeting_recordings.py`, `voice_cleaner.py`, `meeting_analyze.py`, `pipeline_meeting.py`, `models/entity.py`, `pytest.ini`, вендорный `livekit-client.umd.js` 2.15.8.

Итог одной строкой: **§9–§25 (комната, запись, расшифровка) совпадают с кодом и с нашими планами и готовы к работе после семи фактических поправок; §7–§8, §26–§31, §33 — новый объём, который в текущем проекте ничем не подкреплён, его надо вынести в отдельные этапы, иначе Definition of Done (§40) не закроется никогда.**

## 1. Что в v4 совпадает с кодом — брать как есть

| § v4 | Проверено по | Статус |
|---|---|---|
| §9 флаги, redraw через rAF, четыре состояния | `_bind` без `TrackPublished`/`TrackSubscribed`; `isMicrophoneEnabled` = `false` без публикации | ✔ |
| §10 отдельные плитки экрана, `is-lead`, порядок по времени публикации, CSS вне media | `chosenVideoPub` вытесняет камеру; `is-screen-lead` только в `_renderTiles`; `grid-row: 1` только в ветке 575.98px | ✔ |
| §11 звук = `setSubscribed(false)` | `attachToElement` в бандле: `t.muted = 0 === n.getAudioTracks().length` — снимает mute при переприсоединении | ✔ |
| §12 identity гостя `guest:` + sha256(hash токена + guest_id) | `join_guest`: `secrets.token_hex(16)` на каждый POST | ✔ |
| §13 молчащий микрофон | `RoomEvent.LocalAudioSilenceDetected` есть в 2.15.8; метр только в лобби | ✔ (одна поправка, см. §2.3) |
| §14 качество связи, пресеты | `ConnectionQualityChanged` не слушается; `videoCaptureDefaults`/`publishDefaults` не заданы | ✔ |
| §15 PWA без второго фронтенда | — | ✔ (одна поправка, §2.6) |
| §16 `meeting_layout.js` + selfcheck, без node | `package.json` в проекте нет | ✔ |
| §17 `events.jsonl` | рекордер подписан только на 4 события; Python SDK отдаёт identity без `audio_level` | ✔ |
| §19 единый ноль, `max(offset + duration)` | `_enqueue_meeting`: `duration = sum(...)` | ✔ |
| §21 `word_text`, §22 `spread_tokens`, §23 реплики по полосам, §24 `has_speech`, §25 биграммы/0.4 с/2 токена | `meeting_merge.word_text` с `or`; `apply_picks` сваливает в голову; `merge_words → final_replicas`; `analyzable_tracks` фильтрует только по размеру файла | ✔ |
| §36 команды `pytest … -m "not gpu and not slow"`, `-n 0` | `pytest.ini` маркеры `gpu`/`slow`, `pytest-xdist` 3.8.0 в requirements | ✔ |

## 2. Фактические поправки (v4 расходится с кодом)

### 2.1. §18 — контракт `meeting.json`

- `sample_rate: 16000` → **48000**. Рекордер пишет 48 кГц (`SAMPLE_RATE = 48000` в `meeting_recordings.py`, так же в `manifest.json`). 16 кГц — это `01_audio` внутри lab-прогона дочернего артефакта, в контракт встречи не попадает.
- `meeting_id: "..."` → **int** (`meetings.id`, `Integer`).
- Не хватает `speaker` и `child_id` у дорожки — они уже есть в текущем `meeting.json` и читаются `pipeline_meeting._prepare_track` / `meeting_lab._track_snapshot`. Контракт должен быть расширением текущего, не заменой.
- `events` — путь относительно `DATA_DIR`, как в `final-plan.md` дыра 2, иначе перенос `data/` ломает лаб.

### 2.2. §20 — пороги

Один набор `OK ≤ 0.30 / WARN 0.30–0.70 / FAIL > 0.70` не подходит всем проверкам: `head_loss` (рекордер входит с опозданием опроса 2 с) даст FAIL на каждом созвоне, `track_frames` при 0.3 с будет краснеть от нормальной добивки. Пороги — **на проверку**, как в `final-plan.md` дыра 7: `track_frames` 0.5 с, `head_loss` 3 с, `speaker_sync` 0.7 с, `track_span` +1 с. Список проверок в v4 неполный: нет `track_span`, `leaks`, `giga_share`, `no_started_epoch`; `clock_integrity` не определена — это либо `track_frames`, либо лишнее. Заголовок «Dither» — это добивка тишиной (gap pad), не дизеринг.

### 2.3. §13 — `mic_silent=1` в participant **metadata** сломает шумодав

`_setDenoiseMeta` пишет `setMetadata(JSON.stringify({denoise: on}))` — **целиком**, а `voice_cleaner.wants_denoise` и `meeting_room.wantsDenoise` парсят этот JSON. Любое второе поле в metadata затирается при переключении шумодава. Использовать **`setAttributes({mic_silent: "1"})`** (participant attributes, отдельный канал в LiveKit 2.x; грант `canUpdateOwnMetadata` в JWT уже покрывает) и слушать `ParticipantAttributesChanged`.

### 2.4. §3 — `CleanedAudio` как отдельный source

Сейчас `skkkk-dfn` публикует `clean-*` треки с `source=SOURCE_MICROPHONE` и отличается только именем трека (`clean-<identity>`) и identity участника. Менять source нельзя без правки `voice_cleaner._pump`, `playCleanTrack`, `cleanSourceId`, `_dropRawIfCleaner` — и незачем: это работает. В модели §3 `CleanedAudio` — не source, а **признак трека по имени**. Оставить как есть.

### 2.5. §28 — имена источников `screen_video` / `screen_audio`

В коде `SOURCE_SCREEN = "screen"`, и рекордер пишет **только аудио** (`track.kind == KIND_AUDIO`); видео экрана не записывается вообще. Файл `{identity}-screen.wav` уже существует для screen-share audio (`safe_track_stem`, тест `test_screen_audio_is_a_separate_file`). Переименование сломает `analyzable_tracks` и тест без выигрыша. Оставить `screen` для аудио; «screen video» появится только с новым компонентом (§4 ниже).

### 2.6. §15 — `Regenerate` / `Revoke` из комнаты

Сейчас это кнопки карточки встречи в `project.html` для admin/manager. В доке комнаты их видят и гости (`meeting_public.html` использует тот же док) — нужен явный флаг роли в `els` и скрытие. И главное: **v4 не фиксирует решение A/B из Приложения A §6** (хранить ли токен). Без него «Copy link» из комнаты сотрудника с другого устройства невозможен — токена в БД нет (`public_token_hash`). Решение должно быть записано в §15 и в `DECISIONS.md`.

### 2.7. §24 — «одинаковый confirmed user → одна группа»

Для гостей понятия «confirmed user» нет. После §12 группа = **identity** (стабильная); для старых записей — по `display_name` (потолок из `final-plan.md` дыра 6). Сформулировать так, иначе исполнитель начнёт искать `user_id` у гостя.

## 3. Что в v4 требуется, но в проекте не существует — новый объём

| § v4 | Что предполагает | Что есть сейчас | Оценка |
|---|---|---|---|
| §7–§8, §37 native iOS (ReplayKit, Broadcast Extension) и Android (MediaProjection), lifecycle-состояния, матрица тестов | нативные клиенты | только web: `meeting_public.html` / `meeting_room.html` + `livekit-client` | Нет ни одной строки нативного кода. §39 сам выводит нативные клиенты в backlog, но §8 требует их тестировать, а §40 — DoD с «iOS/Android native path tested». Противоречие. Для web-комнаты «предусмотреть архитектурно» = ничего не делать: нативный клиент для LiveKit — обычный участник, плитки экрана из §10 его трансляцию покажут автоматически |
| §26 same-room: Sortformer на подозрительном окне + voice profile | стадия диаризации по фрагменту дорожки внутри meeting-пайплайна | lab-стадии работают на целом прогоне (`01_audio → 03_diar`), meeting гонит `--no-voice`; WeSpeaker-профили есть только для сотрудников с enroll | Ветка C из `meeting-native-diar.md`, которую `final-plan.md` §7 сознательно отложил до появления кейса. Реализуемо (мини-прогон на вырезанном WAV), но это GPU-фаза и отдельный этап |
| §27 двое в один микрофон | то же + профили | то же | то же |
| §28 screen audio как отдельная расшифровка `speaker=external` | ASR на `-screen.wav`, псевдо-спикер без имени, участие в leak-детекторе как источник, который всегда выигрывает | `analyzable_tracks` отбрасывает `source != microphone`; `known_speaker.json` требует имя | Полезно (звук чужого видео из колонок иначе ляжет на людей), но это отдельный этап после дыры 5 |
| §29–§30 запись видео экрана, кадры, vision, frame provenance | рекордер видео (LiveKit Egress в Docker или `rtc.VideoStream` в `voice_cleaner`), хранилище кадров, vision-пайплайн | видео не пишется; `voice_cleaner` — CPU и уже занят DFN | Новый компонент. Egress — новый Docker-сервис (правило «новые зависимости — согласовать») |
| §31–§32 `turn_id`, `source_turn_ids[]` в LLM-извлечении, провенанс каждой сущности | схема БД + промпты + `entities.py` | `Entity`: `artifact_id`, `value_json`, `confidence`; `Chunk`: `speaker`, `chunk_index`. Ни `turn_id`, ни ссылки на реплику | Alembic-миграция, правки `docs/prompts/*`, `services/entities.py`, `tz.py`, чат. Это этап знаний, не этап комнаты |
| §33 Meeting Lab как таймлайн со статусами `UNRESOLVED` | новый UI | `meeting_lab.html` — карточки стадий + таблица реплик | Расширение после дыры 7 `final-plan` (секция проверок); таймлайн — следующий шаг |
| §1, §41 аудит Jitsi / Element Call с «подтверждением исходным кодом» | процесс | используем только LiveKit JS SDK; его поведение уже проверено по вендорному бандлу | Лишняя нагрузка. Референс один — `livekit-client` 2.15.8, который лежит в `static/vendor` |

## 4. Порядок работ §38 — где он ломает измеримость

1. **Контрольный созвон один и в конце (шаг 22).** Шаги 9–11 (`events.jsonl`, `meeting.json`, шкала) стоят после всей работы по комнате (1–8). Значит первая запись, на которой можно что-то измерить, появится после ~11 шагов, а все правки расшифровки (12–15) до этого проверяются на встрече без лога событий — дыра 5 `final-plan` там не проверяема вообще. Правильно: **1 → 9 → 10 → 11 → запись контрольного созвона**, дальше комната (2–8) проверяется живыми сессиями с двух устройств без записи, расшифровка (12–15) — кнопкой **Повторить** на той же записи. Второй контрольный созвон — в конце, как приёмка.
2. Шаги 16–20 — объём из §3 этой ревизии. Их надо вынести из таблицы в отдельные этапы, иначе шаг 23 «Full regression» и §40 недостижимы.
3. Нет шага «прогнать тесты дыры 1 `final-plan`» (написаны, не запускались). Это часть шага 0 «Baseline» — записать явно.

## 5. Предлагаемая структура вместо одного плана на 24 шага

| Этап | Содержание | Источник | Критерий закрытия |
|---|---|---|---|
| **A. Комната + запись + расшифровка** | v4 §9–§25 с поправками §2 этой ревизии = `meeting-room-plan.md` + `final-plan.md` | оба плана уже расписаны по шагам, тестам и фазам R1–R8 / A–E | контрольный созвон: критерии обоих планов зелёные |
| **B. Сложная акустика** | v4 §26–§28: same-room, one-mic, screen audio как источник | `meeting-native-diar.md` ветка C + новый план | появление кейса на живых созвонах после этапа A; фазы R11–R13 |
| **C. Экран как источник знаний и провенанс** | v4 §29–§33: запись видео экрана, кадры, vision, `turn_id`, `source_turn_ids`, Meeting Lab-таймлайн | новый план + `DECISIONS.md` (новый Docker-сервис, миграция схемы) | R14 + сквозной вопрос «Кто поставил задачу X?» → реплика → дорожка |
| Backlog | v4 §39 + нативные клиенты §7–§8 | — | — |

§42 («не молчать, показывать `WARN`/`CONFLICT`/`UNKNOWN`») — правильный продуктовый контракт для всех трёх этапов, его стоит перенести в `DECISIONS.md` сейчас.

## 6. Открытые решения, без которых этап A не закрывается

1. **Хранить публичный токен встречи** (Приложение A §6, A или B). Влияет на v4 §15 и критерий 7.
2. **Запись `DECISIONS.md`**: «Протечка — ветка B (сверка дорожек по логу спикеров); Sortformer на native-дорожках — только этап B по кейсу». Сейчас `meeting-native-diar.md` помечен «код не менять, пока не выберем ветку».
