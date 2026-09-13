# SKKKK — Созвон: единый план до production-ready (комната → запись → расшифровка → знания)

Дата: 2026-09-13. Версия 8 — доработка v7: каждый шаг расписан «где / что / как / тест / готово»; сверено с кодом и `docs/PROJECT-MAP.md` (§5.13, §7 строки `meeting_*`, `manifest.yaml` узлы `svc_meeting_*`, `ui_meeting_*`). **Единственная точка входа.** Детальные приложения: [`final-plan.md`](final-plan.md) (расшифровка, дыры 1–9), [`meeting-room-plan.md`](meeting-room-plan.md) (комната, дыры 1–8, цитаты встречи), [`plan-review.md`](plan-review.md) (сверка черновиков v4/v5 с кодом), [`meeting-native-diar.md`](meeting-native-diar.md) (ветки диаризации).

Источники истины: [`docs/PLAN.md`](../PLAN.md), [`docs/DECISIONS.md`](../DECISIONS.md), [`docs/PROJECT-MAP.md`](../PROJECT-MAP.md), [`docs/TESTING.md`](../TESTING.md).

---

## 1. Ради чего

SKKKK превращает встречи с заказчиком в знания проекта: стенограмма → сущности, задачи, решения → граф → ТЗ → ответы чата с источником. Созвон в комнате SKKKK — единственный вход, где мы контролируем всё: кто говорит (одна дорожка на человека), когда (единые часы), что (двойной слух Parakeet + GigaAM). Внешняя запись такого не даёт — там «кто говорит» приходится угадывать диаризацией.

Встреча (`skkkk-<комната>`, 13.09) показала цену дефектов: из 15 минут по делу — полторы. Флаги микрофона врали, экран прятался под закреплением, «Звук выкл.» не выключал звук, гость перезашёл и стал вторым человеком (две дорожки), голос из колонок ПК попал в микрофон ПК и лёг в стенограмму под чужим именем. Каждая беда в комнате — мусор в записи; мусор в записи — ложная сущность в графе и ложный пункт ТЗ.

## 2. Куда идём

```text
Комната (правдивый UI, стабильная identity)
→ Запись (дорожка на человека, единые часы, лог событий)
→ Расшифровка (Parakeet + GigaAM + proof по дорожке; реплики по полосам; протечки — в audit)
→ Атрибуция (имя из participant mapping; спорное → WARN/CONFLICT/UNKNOWN, не догадка)
→ Знания (сущность/задача/решение → реплика → дорожка → секунда)
```

**Продуктовый контракт** (в `DECISIONS.md`, шаг A6): каждая опубликованная существенная фраза имеет проверяемый источник (дорожка), говорящего и время. Если данных недостаточно, система показывает `WARN` / `CONFLICT` / `UNKNOWN`, а не придумывает имя. `NO_DATA` ≠ `PASS`.

### 2.1. Что означает «готовый продукт»

Полный Definition of Done (§11) наступает после **A + B + C**. Этапы B и C — не исследования на будущее, а обязательная часть результата; выделены отдельно из-за объёма и потому что им нужны живые записи кейсов, которых до этапа A нет.

| Уровень | Требование |
|---|---|
| Room | Участник без повторных попыток видит правду о микрофоне/камере/сети; экран не теряется под закреплением |
| Mobile Web | iPhone Safari и Android Chrome — явная матрица «умеет/не умеет» (§7.3), не декларация |
| Recording | Каждый участник — одна identity, единая шкала от `meetings.started_at`, `events.jsonl` |
| Transcript | Нет воскресших слов с `final=""`, свалившихся таймстемпов GigaAM, раздробленных реплик |
| Attribution | Спикер — по техническому источнику; недостаток доказательств → `WARN/CONFLICT/UNKNOWN` |
| Screen | Экран — отдельный media source (плитка, дорожка `-screen.wav`, в этапе C — видео/кадры) |
| Knowledge | Факт из созвона прослеживается к реплике (`source_span_id`) и дорожке; факт с экрана — к кадру |
| Reliability | Тесты, два контрольных созвона, «Повторить» на той же записи, baseline-фикстуры |
| Operations | Обычный созвон не требует разработчика: `main` поднимает LiveKit и `voice_cleaner`, кнопка «Проанализировать» делает всё остальное |

### 2.2. Принцип совместимости — что нельзя сломать

Рабочий путь сегодня (по карте §5.13 и блоку «Встречи»): **Создать встречу → Начать → ссылка `/m/{token}` → Войти (гость/сотрудник) → мик/кам/экран/шумодав/звук → Завершить → `manifest.json` + WAV → Проанализировать → `process_meeting_artifact` → `speakers_pending` → Подтвердить → knowledge (сущности, индекс, выжимка, ТЗ, чат)**. Плюс путь **внешнего аудио**: Загрузить → `process_lab_artifact` → тот же confirm → knowledge.

Каждый шаг ниже сохраняет оба пути. Регрессионный набор, который гоняется после каждого шага (`venv\Scripts\python.exe -m pytest … -n 0 -q`):

```text
app/app/tests/test_meetings.py test_meeting_recordings.py test_meeting_merge.py test_meeting_checks.py
test_meeting_lab.py test_meeting_overlap.py test_meeting_provenance.py test_meeting_screen_audio.py
test_meeting_control_call.py test_proof_spread.py test_voice_cleaner.py test_livekit_env.py
test_entity_vectors.py test_pipeline_lab.py test_chat.py test_tz.py test_artifact_reports.py
asr/tests/test_lab_proof.py asr/tests/test_lab_stages.py
```

Полный прогон `pytest app -m "not gpu and not slow" -q` — перед каждым контрольным созвоном и в конце этапа.

### 2.3. Приёмка важнее числа тестов

`pytest` зелёный — необходимое, не достаточное. Готово = pytest + selfcheck в браузере + матрица устройств + CONTROL CALL #1 + «Повторить» на той же записи + CONTROL CALL #2 + сквозная проверка знаний (§7.4).

## 3. Инварианты (не обсуждаются)

- Двойной слух Parakeet + GigaAM, без Whisper/T-one/склейки окон. Один GPU-воркер, очередь `jobs`, без Celery.
- Native-дорожка = один человек; Sortformer на ней не запускается по умолчанию — только на подозрительном окне (`DECISIONS.md`, «Созвон в комнате»). Не больше 4 спикеров в окне.
- Оригинальные WAV не перезаписываются; производное — `lab/runs/{run_id}/`, `proof/`, `leaks.json`.
- LiveKit — транспорт; чистка — серверный `skkkk-dfn` (`tools/livekit/voice_cleaner.py`); клиентский DFN3 выключен.
- `LLM_PROVIDER=cursor`; Gemma в VRAM не грузится. Vision — тоже Cursor API.
- Ожидания тестов не подгоняются под падение. Осознанные исключения перечислены поимённо (A4 шаги 2, 4, 6).
- Бизнес-пороги — env-переменные с дефолтами в коде (`MEETING_*`), группа «Meeting» в `.env.example`.

## 4. Где стоим (сверено с кодом 13.09, 15:00)

`pytest test_meeting_* test_proof_spread test_meetings asr/tests/test_lab_proof -n 0` → зелёный. Всё в рабочей копии, **не закоммичено**. Карта §7 уже описывает новые модули (`meeting_checks.py`, `meeting_overlap.py`, `events.jsonl`).

| Звено | Готово | Не готово / риск |
|---|---|---|
| Запись | `PcmWavWriter` добивает паузы (`GAP_PAD_SEC` 0.1), `on_gap` → событие; `started_epoch` один раз (`upsert_track`); `MeetingEventLog` → `events.jsonl` (join/leave, speakers по рангу, mute, quality, track start/end, gap); `has_speech`/`window_rms` | живой `voice_cleaner` работает на старом коде — **не перезапущен** |
| Контракт | `meeting.json`: `meeting_started/ended_epoch`, `events` (путь от `DATA_DIR`), `duration = max(offset+duration)`, дорожки `identity/speaker/source/src/started_epoch/offset_sec/frames/sample_rate(48000)/duration_sec/silent/child_id`; screen-дорожки как `speaker="external_or_unknown"` | — |
| Расшифровка | `word_text` (final авторитетен), `spread_tokens`, `merge_track_replicas`, пропуск `silent`/`screen`, `find_leaks` (биграммы, 0.4 с, ранг из `speakers`), `refine_leaks` → `leaks.json`, `run_checks` (env-пороги), `attach_turn_ids` → `source_span_id` на репликах | **RMS не подключён**: `pipeline_meeting` зовёт `refine_leaks(…, events=events)` без `rms` → цепочка на деле «ранг → UNRESOLVED». Нет живой записи с `events.jsonl` |
| Комната | — | identity гостя случайная; флаги не перерисовываются на `TrackPublished`; экран вытесняет камеру и прячется под pin; «Звук» = `muted`, который SDK снимает при reattach; нет «микрофон молчит», качества связи, PWA/ссылки |
| Лаб встречи | дорожки, I/O стадий, итог на шкале | нет секции проверок/протечек/silent |
| Заготовки B | `meeting_overlap.resolve_same_room` (ранг → RMS с `MEETING_RMS_TIE_RATIO` → `diar` callable → UNRESOLVED), `assign_one_mic` | `diar` никем не передаётся; screen-аудио не расшифровывается |
| Заготовки C | `entities.py`: `load_meeting_proof`, `_turn_prompt` (реплики с `source_span_id` в промпт), `bind_call_source`, `filter_call_payload`; промпт `extract_entities.md` дополнен | **Регрессия knowledge**: `filter_call_payload` выбрасывает decision/task/fact, чей текст не является подстрокой реплики, и обнуляет `summary`. LLM перефразирует → почти всё выпадает; у **внешних** call-артефактов без `source_span_id` и у legacy без lab-прогона (`replicas == []`) выпадает **всё**. Чинится в **A0**, до любого запуска knowledge |

## 5. Этап A — комната, запись, расшифровка

Порядок: сначала то, без чего запись контрольного созвона неполна (guard knowledge, identity, перезапуск рекордера), потом **одна** запись, потом всё проверяется на ней «Повторить» и живыми сессиями с двух устройств.

```text
A0 guard knowledge + commit → A1 identity гостя → A2 перезапуск voice_cleaner → CONTROL CALL #1
→ A3 сверка расшифровки на записи #1 (RMS, пороги, Повторить) → A4 комната (6 шагов)
→ A5 лаб: проверки/протечки → CONTROL CALL #2 → A6 карта/документы/решения
```

### A0. Guard регрессии knowledge и фиксация сделанного

**Где.** `app/app/services/entities.py::persist_entities`, `filter_call_payload`, `bind_call_source`, `_match_replica`.

**Что.** Фильтр по источнику применяется только когда есть чем фильтровать, и не удаляет то, что может быть перефразом:

1. `persist_entities`: фильтр включается, если `artifact.source_type == "call"` **и** `replicas` непустые **и** хотя бы у одной реплики есть `source_span_id` (то есть артефакт — встреча из комнаты). Внешние call-артефакты и legacy идут как раньше.
2. `_match_replica`: сначала подстрока, затем `_text_overlap(text, replica_text, min_ratio=0.35)` (функция уже есть в модуле); окно — 3 соседние реплики, чтобы перефраз через две фразы тоже находился.
3. `summary` не обнуляется никогда — это выжимка, у неё источник — весь артефакт.
4. Не найденный источник у decision/task/fact → **не удалять**, а `needs_review=True` и `source_span_id=null` (по контракту §2 — это `WARN`, не выброс). Удаляется только совпадение с `leaks` (`_leak_covers`).

**Тесты** (`test_meeting_provenance.py`, дописать): перефраз «сделать выгрузку до пятницы» ↔ реплика «выгрузку надо к пятнице сделать» → сущность сохранена с `source_span_id`; внешний call без span-id → payload не тронут; legacy без proof → payload не тронут; текст протечки → удалён; summary не пустеет.

**Затем.** `git add -A app asr tools docs scripts && git commit` — «meeting: events.jsonl, meeting.json contract, per-track replicas, leaks, checks, provenance draft». Без коммита следующий шаг нельзя откатить.

**Готово, когда** тесты A0 зелёные, коммит есть, `refresh_portal_knowledge.py --project N` не теряет сущности встречи относительно `structured/{id}_entities.json` до правки (сравнить число tasks/decisions).

### A1. Одна identity на гостя

**Где.** `services/meetings.py::join_guest`, `routers/meetings.py::GuestJoinIn`, `templates/meeting_public.html` (форма лобби и вызов `join`).

**Сейчас.** `identity = "guest:" + secrets.token_hex(16)` на каждый POST → перезаход = новый человек, вторая дорожка `guest-…wav`, вторая полоса в лаб.

**Что.**
1. Клиент: в лобби перед `join` — `let gid = localStorage.getItem("skkkk-guest-" + token); if (!gid) { gid = crypto.randomUUID(); localStorage.setItem(...) }`; тело `POST /api/meetings/public/{token}/join` → `{display_name, guest_id}`.
2. `GuestJoinIn.guest_id: str | None = None`; валидация в роутере: `len ≤ 64`, `re.fullmatch(r"[A-Za-z0-9-]+")`, иначе 400.
3. `join_guest(db, settings, meeting, display_name, guest_id=None)`: `identity = "guest:" + hashlib.sha256(f"{meeting.public_token_hash}:{guest_id}".encode()).hexdigest()[:32]` при `guest_id`, иначе прежний `token_hex` (старый клиент/кэш). Константы `GUEST_PREFIX`, `GUEST_ID_MAX_LEN`, `GUEST_ID_RE` — в `meetings.py`.
4. `MeetingParticipant` — `select` по `(meeting_id, identity)`; есть → обновить `display_name`, `joined_at`; нет → создать. Не плодить строки.
5. LiveKit при входе с той же identity сам закрывает старую сессию (`DUPLICATE_IDENTITY`) — призрак в списке исчезает. Рекордер (`start_record`) находит writer по `(identity, source)` и продолжает тот же файл, пауза добивается тишиной. Потолок: перезапуск `voice_cleaner` посреди встречи откроет файл заново (`wave.open(..., "wb")`) — записать в `OPERATIONS.md`.

**Тесты** (`test_meetings.py`): два `join` с одним `guest_id` → одна identity, одна строка `meeting_participants`; разные `guest_id` → разные identity; `guest_id="a b/../"` → 400; без `guest_id` → как раньше; `test_join_returns_livekit_when_configured` не меняется.

**Карта.** §5.13/блок «Встречи», строка «Войти во встречу»: `{display_name, guest_id}`; identity = `guest:` + sha256(hash токена + `guest_id` из `localStorage`); перезаход — та же identity. `manifest.yaml`: `svc_meetings.title` дополнить; `ui_meeting_public.title` — «guest_id в localStorage».

**Готово, когда** pytest зелёный и на двух устройствах: перезагрузка страницы гостя → в списке один человек, в `manifest.json` одна дорожка.

### A2. Перезапуск рекордера и проверка наблюдаемости

`scripts/SKKKK-1-Остановить.bat` → запуск `main` (он поднимает LiveKit и `voice_cleaner` после `:7880`, карта §3 «Voice cleaner»). Проверка: `data/logs/voice_cleaner.log` содержит `voice_cleaner: record …`; при входе участника появляется `data/meetings/{room}/events.jsonl` с `participant join`, `track start`; при mute — `mute`; при разговоре — `speakers`. Без этого созвон #1 не проводить.

### CONTROL CALL #1 — запись, на которой меряется расшифровка

Три устройства: ПК (сотрудник, колонки, без гарнитуры), телефон A (гость, гарнитура, VPN/мобильная сеть), телефон B (гость, в одной комнате с ПК). Комната ещё не починена — это нормально: созвон #1 проверяет **запись и речь**, не UI. Скрипт фаз распечатать; фазы объявлять голосом («фаза S2») — это метки в стенограмме.

| Фаза | Что делают | Что измеряет |
|---|---|---|
| R1 | A входит, перезагружает страницу, входит снова с тем же именем | одна identity, одна дорожка, `leave`+`join` одной identity в `events.jsonl` |
| S1 монолог | каждый по 30 с, остальные молчат | `word_text`, `spread_tokens`, `speaker_sync` |
| S2 наложение | ПК и A говорят одновременно 20 с (читают разные тексты) | `merge_track_replicas`: реплики каждого связные, не нарезаны по чужим словам |
| S3 протечка | ПК со включённым звуком; A говорит 30 с — его голос из колонок ПК идёт в микрофон ПК | `find_leaks`: протечка с source=A, target=ПК; удалена из ПК; есть в `leaks.json` |
| S4 пауза | A выключает мик на 60 с, потом говорит | `gap`/`mute` в событиях, `track_frames`, добивка тишиной |
| S5 сеть | A включает режим полёта на 5 с, возвращается | `quality`, reconnect не ломает `offset_sec` |
| S6 два микрофона рядом | B и ПК в одной комнате: B говорит 30 с, ПК молчит | материал для этапа B1 (что попало в микрофон ПК); `find_leaks` разводит рангом или даёт `CONFLICT` |

Сохранить как **контрольную запись A**: WAV, `manifest.json`, `meeting.json`, `events.jsonl`, снимок `/report-meeting-lab` (HTML) в `app/app/tests/fixtures/meeting/control1/` (WAV — обрезанные до 10 с фрагменты, полные — в `data/`). Дальше расшифровка проверяется на ней «Повторить»; новых записей ради ASR-правок не делать.

### A3. Сверка расшифровки на записи #1

**Что подключить до «Повторить».**
1. **RMS в цепочку протечек**: `pipeline_meeting.process_meeting_artifact` передаёт `refine_leaks(groups, leaks, events=events, rms=rms_of)`, где `rms_of(name, start, end)` = `window_rms(track_wav16[name], start - offset, end - offset)`; карта имён → (wav16, offset) собирается в цикле дорожек (уже есть `mix_inputs`). `_as_rms_map` в `meeting_overlap` уже умеет callable.
2. **`speaker_sync` по рангу**: `run_checks` сравнивает начало реплики с ближайшим событием `speakers`, где спикер первый в списке; окно `SPEAKER_SYNC_BEFORE_SEC`/`AFTER_SEC` — уже в коде.

**Проверка.** «Повторить» → `/report-meeting-lab`: все проверки не `FAIL`/`NO_DATA`; S2 — по одной-двум репликам на человека, не десять обрывков; S3 — в `leaks.json` с `by=rank|rms`, текст отсутствует у ПК; `_final.txt` читается как разговор; S6 — либо разведено, либо `CONFLICT` (не молча приписано). Что упало — чинится в `meeting_merge` / `proof.py` / `meeting_checks` с тестом на воспроизведение (фикстура из записи #1) и снова «Повторить».

**Пороги (env, группа Meeting в `.env.example`).** `MEETING_CHECK_OK_SEC` 0.30, `MEETING_CHECK_WARN_SEC` 0.70, `MEETING_HEAD_LOSS_WARN_SEC` 3.0, `MEETING_GAP_MUTE_WINDOW_SEC` 1.0, `MEETING_RMS_TIE_RATIO` 1.15, `MEETING_SPEECH_MIN_SEC` 2.0, `MEETING_SPEECH_RMS_MIN` 0.01. Пороги — на проверку, не один на всё.

**Готово, когда** `run_checks` на записи #1 без `FAIL`, S2/S3 подтверждены глазами, фикстуры записи #1 закреплены тестами.

### A4. Комната — `static/meeting_room.js`, `static/app.css`, `meeting_public.html`, `meeting_room.html`

Общее для всех шагов:
- `?v=` у `meeting_room.js` в **обоих** шаблонах поднимать на каждом шаге (телефоны держат кэш).
- Чистые функции без DOM — в новый `static/meeting_layout.js` (подключается вторым `<script>` в обоих шаблонах, экспорт через `window.SKKKKLayout`): `tileOrder(people, screens, publishedAt)`, `flagState(pub, attrs)`, `soundPlan(soundOn, pubs)`, `qualityBadge(quality)`. Самопроверка `static/meeting_layout.selfcheck.js` — `console.assert`, запускается из консоли (`import('/static/meeting_layout.selfcheck.js')`) или `agent-browser`-сценарием; node/vitest не заводим.
- После каждого шага: строковые тесты `test_meetings.py`, selfcheck, живая проверка с двух устройств (ПК + телефон).
- Тексты UI — в существующем словаре строк `meeting_room.js` (там, где сейчас `mediaErrorText` и подсказки), не в разметке.

| # | Шаг | Где / что сделать | Тест |
|---|---|---|---|
| 1 | **Флаги мик/кам** | `_bind`: `const redraw = () => { if (!this._raf) this._raf = requestAnimationFrame(() => { this._raf = 0; this._renderTiles(); this._fillRoster(); }); }`; подписать на `TrackPublished`, `TrackUnpublished`, `TrackSubscribed` (после `_attachTrack`), `TrackUnsubscribed`, `TrackStreamStateChanged`, `ParticipantNameChanged`, `ParticipantAttributesChanged`, `ConnectionQualityChanged`. `flagState(pub, attrs)`: нет публикации → `none` (серый чип «нет микрофона»), `pub.isMuted` → `off`, `attrs.mic_silent === "1"` → `silent`, иначе `on`. `_fillTile`/`_fillRoster` читают `flagState`, а не `isMicrophoneEnabled` | `RoomEvent.TrackPublished` и `redraw` в `_bind`; `flagState` в `meeting_layout.js` с четырьмя значениями; selfcheck: publication=null → `none` |
| 2 | **Звук = отписка** | `toggleSound`: `this._soundOn = !this._soundOn; soundPlan(this._soundOn, remoteAudioPubs).forEach(({pub, subscribed}) => pub.setSubscribed(subscribed))`; `remoteAudioPubs` = все аудиопубликации всех удалённых участников, включая `clean-*` от `skkkk-dfn`. `_attachTrack`: `if (track.kind === "audio" && !this._soundOn) return;`. `TrackPublished` при `!_soundOn` и `kind === audio` → `pub.setSubscribed(false)`. Включение → `setSubscribed(true)`; `TrackSubscribed` сам присоединит. `_applyOutputMute` удалить. Тост при выключении: «Звук выключен: вы никого не слышите». `_dropRawIfCleaner` — проверять `pub.isSubscribed`, не `pub.track` | `setSubscribed(false)` в пути `toggleSound`; guard в `_attachTrack`; **осознанно** снимается assert `node.muted = !this._soundOn` (он и есть дыра); selfcheck `soundPlan(false, pubs)` → все `subscribed:false` |
| 3 | **Микрофон молчит** | `_bind`: `RoomEvent.LocalAudioSilenceDetected` → `_onMicSilent()`; `RoomEvent.MediaDevicesError` → тот же тост с `mediaErrorText`. После `setMicrophoneEnabled(true)` и после `_restartMic`: `const t = localMicTrack(room).mediaStreamTrack; t.onmute = t.onended = () => this._onMicSilent()`. `_onMicSilent`: тост «Микрофон молчит. Проверьте устройство» + кнопка «Перезапустить» → `_restartMic(this._dfn3On)`; `room.localParticipant.setAttributes({mic_silent: "1"})`; при первом ненулевом уровне метра — `setAttributes({mic_silent: ""})`. **Не** metadata: её целиком переписывает `_setDenoiseMeta`, а `voice_cleaner.wants_denoise` её парсит. Полоска уровня на своей плитке — `_startMeter(new MediaStream([t]))` (метр из лобби) | `LocalAudioSilenceDetected`, `MediaDevicesError`, `setAttributes` и `mic_silent` в js; `ParticipantAttributesChanged` → `redraw` |
| 4 | **Экран — отдельная плитка** | Модель плиток: `tileOrder(people, screens, publishedAt)` → `[{kind:"screen", id:"meeting-tile-screen-"+sid, personId, lead}, …, {kind:"person", id}]`: экраны по убыванию `publishedAt[sid]` (заполняется в `TrackPublished`/`LocalTrackPublished` через `Date.now()`), первый — `lead:true`; люди — в порядке входа. `_renderTiles`: `_ensureTile` для каждой записи, DOM-порядок через `tilesEl.append(node)` по списку (узлы не пересоздаются — `<video>` не перезапускается); класс `meeting-tile-screen`, подпись «Экран — Имя»; `chosenVideoPub` возвращает **только** `Source.Camera`. Своя трансляция — тоже плитка. Новый экран → `this._pinLocked = false; this._spotlightId = leadId`; уход lead → следующий экран, иначе сетка. Кнопка «Экран сверху» в `#meeting-more-tray` возвращает lead после ручного pin. CSS: одно правило вне media `.meeting-tiles .meeting-tile-screen.is-lead { grid-column: 1 / -1; grid-row: 1; }`; `.is-screen-lead` остаётся только для пропорций рядов на телефоне. `setScreenShareEnabled(true, {contentHint: "detail"})`; `publishDefaults.screenShareEncoding = ScreenSharePresets.h720fps5` | `meeting-tile-screen-` и `tileOrder` в js; `.meeting-tile-screen.is-lead` **вне** media в css; **осознанно** переезжает assert `".meeting-tiles.is-screen-lead .meeting-tile-screen" in phone` из `test_phone_meeting_grid_fits_all_tiles`; selfcheck: два экрана → свежий `lead` |
| 5 | **Качество и пресеты** | `ConnectionQualityChanged` → `qualityBadge(q)`: `excellent/good` → `""`, `poor` → «плохая связь», `lost` → «нет связи»; чип на плитке + у себя строка статуса «Связь плохая: видео снижено». `Room` опции: `videoCaptureDefaults.resolution = isPhoneClient() ? VideoPresets.h360 : VideoPresets.h540`; `publishDefaults: {simulcast: true, videoSimulcastLayers: [VideoPresets.h180, VideoPresets.h360], dtx: true, red: true}`; `adaptiveStream`/`dynacast` остаются `true` | `ConnectionQualityChanged`, `videoSimulcastLayers`, `VideoPresets.h360` в js; `test_join_returns_livekit_when_configured` не трогать |
| 6 | **PWA + ссылка** | `static/meeting.webmanifest` (`name`, `display: standalone`, `start_url` = текущий URL комнаты, иконка из `static/`), роут в `pages.py` отдаёт его с `application/manifest+json`; `<link rel="manifest">` в обоих шаблонах; на телефоне подсказка «Добавить на экран» один раз (`localStorage`). Service worker не нужен. Ссылка из комнаты сотрудника — по решению §8.1: **B** → `Meeting.public_token` (Alembic `add_column`, шифрование `Fernet`, если `cryptography` в venv через `python-jose`; иначе — решение пользователя), `member_payload` отдаёт `invite_url` admin/manager, дока показывает тост со ссылкой при входе и по кнопке, карточка встречи в `project.html` показывает ссылку всегда; **A** → только текст тоста «Ссылка гостя есть во вкладке, где встречу создали, или через «Новая ссылка»». Кнопки «Новая ссылка»/«Отозвать» в комнате не добавляем — они в карточке | манифест отдаётся 200; `rel="manifest"` в обоих шаблонах; при B — `GET /projects/{id}/meetings` отдаёт `public_url` admin/manager, **осознанно** переписывается `test_create_meeting_returns_public_link_once`, `public_token: null` для user остаётся |

Группировка дорожек — `speaker_group_key(identity)` (уже по identity); после A1 это и есть «один человек — одна группа». Для записей до A1 — двойные дорожки одного гостя остаются двумя группами (потолок, задокументировать в лаб).

### A5. Лаб встречи — проверки, протечки, тишина

**Где.** `services/meeting_lab.py::build_meeting_lab`, `templates/meeting_lab.html`.

**Что.**
1. `_checks_snapshot(meeting, events, replicas, leaks)` → `run_checks(...)` → список `{id, status, title, detail, value, threshold}`; в шаблон блоком «Проверки шкалы» над дорожками, статус — цветной бейдж (`PASS` зелёный, `WARN` жёлтый, `FAIL` красный, `NO_DATA`/`UNRESOLVED` серый с подписью «нет данных ≠ пройдено»).
2. `_leaks_snapshot` из `proof/leaks.json` → таблица «Протечки»: время (`format_meeting_clock`), откуда → куда, текст, метод (`rank`/`rms`/`diarization`/`unresolved`), статус `CONFLICT`. Ссылка на файл через `meeting_lab_file_url`.
3. У дорожки — бейдж `silent` («тишина, ASR пропущен») и `source=screen`; ссылка на `events.jsonl` рядом с `manifest.json`.
4. Спорная точка (`CONFLICT`/`UNRESOLVED`) в итоговой таблице подсвечена: время, спикер, дорожка, текст, причина, статус.

**Тест** (`test_meeting_lab.py`): снимок содержит `checks` с `id`/`status`; при отсутствии `events.jsonl` — `NO_DATA`, не `PASS`; протечка из фикстуры — в `leaks`; страница admin-only остаётся.

### CONTROL CALL #2 — приёмка

Те же три устройства. R1 + S1–S6 повторяются, плюс комната:

| Фаза | Что делают | Ожидание |
|---|---|---|
| R2 | A выключает/включает мик; ПК — камеру | флаг у остальных ≤ 1 с, при входе сразу верный, «нет микрофона» ≠ «выключен» |
| R3 | A входит с микрофоном, занятым другим приложением | у A тост ≤ 5 с и «Перезапустить»; у остальных чип «молчит» |
| R4 | ПК «Звук выкл.»; через минуту A перезаходит, B щёлкает шумодав | колонки ПК молчат всё время; в DevTools у аудиопубликаций `isSubscribed === false`; в записи ПК протечки S3 **нет** (сравнить с записью #1) |
| R5 | B закрепляет A, потом ПК запускает экран | у всех экран сверху на всю ширину, pin снят, камера ПК в своей плитке |
| R6 | A запускает второй экран; ПК останавливает свой | сверху — экран A; при остановке ничего не прыгает |
| R7 | A режим полёта 5 с | чип «плохая связь» → исчезает |
| R8 | сотрудник открывает комнату с другого браузера, «Скопировать ссылку» | ссылка (по §8.1) |

Готово, когда все фазы отмечены, «Повторить» на записи #2 даёт проверки без `FAIL`, и в стенограмме #2 нет протечки S3 (её убрала отписка звука, а не только детектор).

### A6. Карта, документы, решения — вместе с кодом

| Шаг | `PROJECT-MAP.md` | `manifest.yaml` |
|---|---|---|
| A0 | §7 `entities.py`: «для встреч — привязка к `source_span_id`, перефраз через `_text_overlap`, без источника → `needs_review`» | `svc_entities.title` |
| A1 | блок «Встречи», строка «Войти во встречу»; §5.13 «Гостевая страница» — `guest_id` в `localStorage` | `svc_meetings.title`, `ui_meeting_public.title` |
| A3 | §7 `pipeline_meeting.py`: «`refine_leaks` с RMS окон дорожек» | `svc_pipeline_meeting.title` |
| A4.1–3 | «Гостевая страница»: события перерисовки; `#meeting-sound` = отписка (`setSubscribed(false)`), не `muted`; тост «Микрофон молчит», атрибут `mic_silent` | `ui_btn_meeting_sound.title`; новый node `ui_meeting_mic_silent_toast` (edge `ui_meeting_dock → contains`) |
| A4.4 | новый абзац «Плитки экрана»: плитка на публикацию, последняя — `is-lead`, приоритет над закреплением, «Экран сверху» в `#meeting-more-tray` | node `ui_meeting_screen_tile`; edge `ui_meeting_dock → ui_meeting_screen_tile: shares_screen` |
| A4.5 | чип качества; пресеты публикации | `ui_meeting_dock.title` |
| A4.6 | манифест PWA; при B — «Скопировать ссылку» у сотрудника всегда, `public_url` в списке | node `static_meeting_webmanifest`; при B — `router_meetings.title` |
| A4 общ. | §7: `static/meeting_layout.js` (чистые функции раскладки/звука/флагов) + selfcheck | node `ui_meeting_layout_rules` |
| A5 | §5 таблица `/report-meeting-lab`: «проверки шкалы, протечки, silent» | `ui_page_meeting_lab.title`, `svc_meeting_lab.title` |
| все | `TESTING.md`: selfcheck, регрессионный набор §2.2, протокол контрольных созвонов; `OPERATIONS.md`: перезапуск `voice_cleaner`, потолок «перезапуск посреди встречи», env `MEETING_*`; `DECISIONS.md`: контракт §2, ветка B протечек (сверка дорожек по логу спикеров; Sortformer — только на окне), решение §8.1, «provenance — в `value_json`, без новой таблицы» | — |

После правок — `venv\Scripts\python.exe scripts\index_project_map.py`.

### Definition of Done A

```text
[ ] A0: guard knowledge, тесты перефраза/legacy, коммит; сущности встречи не потеряны
[ ] A1: identity гостя — pytest + живая проверка
[ ] A2: voice_cleaner перезапущен, events.jsonl пишется
[ ] контрольная запись #1 сохранена (WAV, manifest, meeting.json, events, лаб, фикстуры)
[ ] A3: RMS подключён; Повторить на #1 — проверки без FAIL/NO_DATA, S2 связные, S3 в leaks.json, S6 разведено или CONFLICT
[ ] A4 шаги 1–6: строковые тесты + selfcheck + живая проверка каждого; ?v= поднят
[ ] A5: лаб показывает проверки, протечки, silent, CONFLICT
[ ] контрольный созвон #2: R1–R8 + S1–S6 зелёные; протечки S3 в записи #2 нет
[ ] A6: карта, manifest.yaml, TESTING, OPERATIONS, DECISIONS; index_project_map
[ ] pytest app -m "not gpu and not slow" зелёный; регрессионный набор §2.2 зелёный
```

## 6. Этап B — сложная акустика (обязателен; старт — записи S6 из созвонов #1/#2)

Цель: там, где ранг и RMS не разводят, не угадывать, а доказывать или честно писать `CONFLICT`/`UNKNOWN`.

### B0. Ground truth

Из записей #1/#2 (фаза S6) и первых рабочих созвонов собрать окна, где `leaks.json` дал `unresolved`/`CONFLICT`. Для каждого — вручную отметить правду в `app/app/tests/fixtures/meeting/acoustic/{case}.json` (`start`, `end`, `truth`). Минимум 5 окон. Замерить частоту: доля `CONFLICT` от всех протечек. Если < 5 % и все на ПК с колонками — B1 сводится к RMS-порогу, Sortformer не нужен; иначе — B1.

### B1. Двое в одной комнате, два микрофона — Sortformer на подозрительном окне

**Где.** Новый `services/meeting_diar_window.py`; вызов из `pipeline_meeting` через параметр `diar=` в `refine_leaks` (заготовка уже принимает callable).

**Как.**
1. `diar_window(name_a, name_b, start, end) -> str`: для обеих дорожек вырезать `[start-1, end+1]` из wav16 (`ffmpeg -ss -to`, уже используется в `mix_track_wavs`) в `lab/runs/{run_id}/diar_windows/{ordinal}/{name}.wav`.
2. Прогнать Sortformer на **каждом** окне отдельно: `venvs\lab\Scripts\python.exe asr/lab_pipeline.py --project-id … --artifact-id {child} --only 03_diar --no-voice` на временном child-артефакте **без** `known_speaker.json` (иначе `s03_diar` пишет одного `speaker_0`). Результат: число говорящих в окне у каждой дорожки и доля времени каждого.
3. Решение: если в окне дорожки X два голоса, а в дорожке Y один, и доминирующий голос Y совпадает по времени с «вторым» голосом X → победитель Y, метод `diarization`. Если у обеих по одному голосу или по два — WeSpeaker: `asr/lab/voice.docker_embed` по клипам окна, сравнить с центроидами дорожек (`centroids_from_clips` по остальным репликам того же человека) и с `speaker_profiles` проекта (`load_profiles`) — cosine выше `MEETING_DIAR_COSINE_MIN` (0.6) → победитель; иначе `""` → `UNRESOLVED`/`CONFLICT`.
4. GPU: одна фаза на артефакт — все окна собираются, Sortformer запускается одним процессом после ASR всех дорожек (в `lmstudio`-режиме — внутри уже открытой ASR-фазы, Gemma выгружена). Бюджет: `MEETING_DIAR_MAX_WINDOWS` (20) и `MEETING_DIAR_MAX_SEC` (300); сверх — `UNRESOLVED`.

**Тесты.** `test_meeting_overlap.py`: `resolve_same_room` с `diar` → `LEAK_BY_DIAR`; `test_meeting_diar_window.py`: вырезка окна даёт нужную длину; правило «два голоса против одного»; бюджет окон; fake-runner без GPU. GPU-тест (`-m gpu`) на фикстуре B0.

**Карта.** §7 новый модуль; §8 конвейер встречи: «… → `find_leaks` → ранг → RMS → Sortformer-окно → `CONFLICT`»; `manifest.yaml` node `svc_meeting_diar_window`, edge `svc_pipeline_meeting → svc_meeting_diar_window`; `DECISIONS.md` — «Sortformer на native-дорожке только окном, бюджет env».

### B2. Двое в один микрофон

**Триггер.** В окне одной дорожки Sortformer (из B1) видит два голоса без кандидата на другой дорожке. `assign_one_mic(words, segments, profiles)` уже есть: слова окна получают `speaker` из профиля (`speaker_profiles` → имя) или `unknown` + `CONFLICT`. Подключить: сегменты — из `03_diar` окна, `profiles` — `{cluster: name}` по cosine с `load_profiles`. В `_final.txt` такие слова публикуются как `[неизвестно]` — имя предыдущего не продолжается. В лаб — `CONFLICT` с кнопкой «Назначить» (существующий enroll/merge из `/report-speaker-match`).

**Тест.** `assign_one_mic` без профиля → `unknown`; с профилем → имя; публикация не подставляет соседа.

### B3. Звук чужого видео из screen share

Сейчас `-screen.wav` пишется, попадает в `meeting.json` как `speaker="external_or_unknown"`, ASR пропущен. Что сделать: включить ASR по screen-дорожке (`work` в `pipeline_meeting` не фильтрует `SOURCE_SCREEN`, если `MEETING_SCREEN_ASR=1`), результат — отдельная группа `external_or_unknown`, которая подаётся в `find_leaks` как источник, **всегда** выигрывающий (`LEAK_BY_SCREEN`): текст, совпавший со screen-аудио, снимается с людей. В `_final.txt` screen-реплики публикуются с меткой «[экран]». В knowledge такие реплики — `source_type=call_screen_audio`, decision/task из них не создаются без ручного подтверждения.

**Тест** (`test_meeting_screen_audio.py` дописать): screen-группа побеждает в протечке; реплики экрана помечены; сущности из них `needs_review`.

### Definition of Done B

```text
[ ] B0: ≥ 5 размеченных окон, частота CONFLICT измерена
[ ] B1: Sortformer-окно + WeSpeaker; LEAK_BY_DIAR в leaks.json; бюджет env; GPU-тест
[ ] B2: one-mic → имя по профилю или [неизвестно]; лаб предлагает «Назначить»
[ ] B3: screen-аудио — источник протечек, реплики «[экран]», knowledge needs_review
[ ] живая приёмка R11 (двое рядом, раздельные мики), R12 (A→B→A в один мик), R13 (речь через screen share)
[ ] карта §7/§8, manifest.yaml, DECISIONS, index_project_map
```

## 7. Этап C — экран как источник и провенанс знаний (обязателен)

### C1. Запись экрана

**Выбор рекордера.** Два пути: (а) LiveKit Egress — отдельный Docker-сервис + Redis, track egress пишет `.mp4`/`.ivf` каждой видеопубликации; (б) `rtc.VideoStream` в отдельном Python-процессе `tools/livekit/screen_recorder.py` (participant `skkkk-rec`, `CUDA_VISIBLE_DEVICES=-1`), кадры → ffmpeg stdin → `data/meetings/{room}/tracks/{identity}-screen.mp4`, кадр раз в `MEETING_SCREEN_FPS` (1). Рекомендация — **(б)**: нет новой инфраструктуры (Redis), тот же паттерн, что `voice_cleaner`, 1 fps достаточно для текста и кадров; согласовать с пользователем (новый процесс, `install_meeting_voice.py` ставит `av`/ffmpeg уже есть).

**Контракт.** `manifest.json` дорожка `{identity, source: "screen_video", track_sid, path, started_epoch, frames, fps}`; `meeting.json` — та же строка + `offset_sec`. `remove_room_recordings` и `purge_meeting` удаляют mp4. `voice_cleaner` не трогается.

**Тесты.** `test_meeting_recordings.py`: контракт screen_video; purge удаляет. Живая: R14 — шарят 1С/Excel/PDF → mp4 есть, длительность сходится с `events.jsonl` `track start/end`.

### C2. Кадры → vision

`services/meeting_frames.py`: по `meeting.json` для каждой screen_video — кадры на смене содержимого (ffmpeg `select='gt(scene,0.3)'`, максимум `MEETING_FRAMES_MAX` 60) → `lab/runs/{run_id}/frames/{track_sid}/{ts_ms}.jpg`; `frames.json` `{frame_id, track_sid, t_rel, sha256}`. Vision — существующий провайдер `services/llm.py` (Cursor API, режим vision уже используется для документов) с промптом `docs/prompts/screen_frame.md`: «что на экране: система, форма, цифры, текст»; результат `frames/{frame_id}.md`. В knowledge кадры идут как чанки артефакта с `source_type=screen_frame`, `frame_id`, `t_rel`. Стоимость: только при `MEETING_SCREEN_VISION=1`.

**Тесты.** выбор кадров ограничен `MEETING_FRAMES_MAX`; `frames.json` контракт; чанк кадра несёт `frame_id`; без флага — ничего не запускается.

### C3. Провенанс знаний — довести заготовку

Уже есть (после A0): `source_span_id` на репликах, реплики в промпте, привязка сущности к реплике в `value_json` (`speaker`, `start_sec`, `end_sec`, `source_span_id`, `text_hash`). Решение: **провенанс хранится в `value_json`, без новой таблицы** — `Entity.value_json` уже JSON, чат и ТЗ его читают; миграция не нужна (записать в `DECISIONS.md`).

Довести:
1. **LLM отдаёт источник сам.** `TaskItem.source_span_ids: list[str] = []`, decisions — как `{text, source_span_ids}` (или параллельный `decision_sources`), facts — `fact_sources: dict[key, list[str]]`. Промпт `extract_entities.md`: «для каждого decision/task/fact укажи `source_span_ids` из списка». `bind_call_source` сначала берёт id от LLM (проверив, что они существуют), потом fallback A0 (подстрока → `_text_overlap`).
2. **Чат цитирует реплику.** `chat._format_entity_value` / `_supplement_context` добавляют «(Имя, 12:34, созвон {файл})» из `value_json`; в ответе — ссылка `/artifacts/{id}/report#t={start_sec}` (якорь по времени уже есть у `/report`? если нет — добавить `id="t-{start_ms}"` на реплики в `write_prod_view`).
3. **ТЗ.** `rebuild_tz.md` получает в контексте decision/task со `speaker`/временем; в `current_tz.md` — сноска «источник: созвон N, Имя, 12:34».
4. **Кадры.** Факт из `screen_frame` несёт `frame_id`/`t_rel` тем же способом (`value_json`).
5. **Протечка не становится знанием.** Уже фильтруется `_leak_covers`; закрепить тестом на записи #1 (текст S3 отсутствует в `entities` ПК).

**Тесты.** `test_meeting_provenance.py`: LLM-id принимаются, несуществующие отбрасываются с fallback; `test_chat.py`: ответ содержит имя/время; `test_tz.py`: сноска источника; сквозной: фикстура записи #1 → «Кто поставил задачу X?» → задача → `source_span_id` → реплика → дорожка (`test_meeting_control_call.py` дописать).

### C4. Лаб-таймлайн

`meeting_lab.py::_timeline_snapshot`: одна шкала (0 = `meeting_started_epoch`) с полосами: события (`join/leave/mute/quality`), дорожки (речь/тишина/gap), экраны (`screen_video` + кадры), реплики, протечки (`by`/`CONFLICT`), проверки. Рендер — CSS-grid по `t_rel` без JS-библиотек. Клик по точке — та карточка, что в A5.

### Definition of Done C

```text
[ ] C1: screen_video пишется отдельным процессом; контракт в manifest/meeting.json; purge удаляет; R14
[ ] C2: кадры по смене содержимого; vision через Cursor; чанки с frame_id; за флагом
[ ] C3: LLM отдаёт source_span_ids; чат и ТЗ цитируют реплику/кадр; протечка не в knowledge (тест на записи #1)
[ ] C4: таймлайн в лаб
[ ] сквозной вопрос «Кто поставил задачу X?» → задача → реплика → спикер → секунда → дорожка — проходит на записи #2
[ ] карта §5/§7/§8, manifest.yaml (svc_screen_recorder, svc_meeting_frames, edges), DECISIONS, OPERATIONS (второй процесс), index_project_map
```

### 7.3. Матрица устройств (проверяется живыми созвонами, фиксируется в `TESTING.md`)

| Устройство | Вход | Мик | Камера | Принять экран | Отдать экран |
|---|---|---|---|---|---|
| Desktop Chrome/Edge | ✔ | ✔ | ✔ | ✔ | ✔ `getDisplayMedia` |
| iPhone Safari | ✔ | ✔ | ✔ | ✔ | только если `canShareScreen()` истинно на этой iOS (определяется в рантайме, не обещается) |
| Android Chrome | ✔ | ✔ | ✔ | ✔ | ✔ вкладка/экран, где поддержано |
| Native iOS/Android | вне плана; их `ScreenShare`-публикация отобразится плиткой без правок | | | | |

### 7.4. Референсы с открытым кодом — когда и что смотреть

Референс доказывает, что подход реален; готовность доказывает только код SKKKK + тест + живой созвон. В этапе A референс один — вендорный `static/vendor/livekit/livekit-client.umd.js` 2.15.8 (поведение `attachToElement`, `setSubscribed`, `LocalAudioSilenceDetected` проверяется по нему). Внешние проекты — только для B/C:

| Наша работа | Референс | Что именно смотреть |
|---|---|---|
| A4.4 плитки экрана, stage/pin | LiveKit Meet (`github.com/livekit-examples/meet`), Element Call (`github.com/element-hq/element-call`) | модель «экран — отдельный трек/плитка», поведение pin при новом экране |
| A4.5 / C1 качество экрана | Element Call (screen-share quality: разрешение, fps, simulcast) | `contentHint`, слои, деградация «экран держит detail, камера — fps» |
| C1 запись экрана | LiveKit `client-sdk-python` (`rtc.VideoStream`), LiveKit Egress | контракт кадров/трека; что дешевле для одного хоста |
| Будущие native-клиенты (вне плана) | LiveKit Swift (`LKSampleHandler`, `Docs/ios-screen-sharing.md`), Android (`MediaProjection`), Jitsi issues | lifecycle screen share: фон, блокировка, revoke, reconnect — на будущее |

## 8. Решения, нужные от пользователя

1. **Хранить ли публичный токен встречи** (A4 шаг 6). A — как есть (hash-only, ссылка один раз); B — `Meeting.public_token` шифрованно, `public_url` admin/manager, ссылка в комнате и карточке. Рекомендация — **B**: токен уже живёт в мессенджерах, отзыв/перевыпуск есть, встречи короткоживущие. Без решения шаг 6 делается в объёме манифеста PWA и текста тоста.
2. **Второй Python-процесс для записи экрана** (C1, вариант б) вместо Egress+Redis. Рекомендация — процесс.
3. **Параллельная сессия, пишущая v4.** A0 чинит её главный риск (фильтр knowledge). Дальше либо остановить её до закрытия A, либо принять, что B/C-заготовки растут параллельно, но не входят в DoD A и не трогают `Entity`-схему.
4. Записать в `DECISIONS.md`: контракт §2; ветка B протечек; «provenance в `value_json`»; «Sortformer на native-дорожке — только окном».

## 9. Чего не делаем

Нативные iOS/Android клиенты (ReplayKit, MediaProjection); TURN через 443/TLS (VPN остаётся на ICE/TCP 7881 — отдельная работа по `livekit_env.build_livekit_yaml`); лечение задержки сети (только показываем и снижаем битрейт телефона); клиентский DFN3; node/vitest; Qdrant/Neo4j; автоматическое «понять, что двое в одной комнате» без протечки; отдельный фронтенд ради «приложения».

## 10. Порядок всего проекта

```text
A0 guard knowledge + commit → A1 identity → A2 restart cleaner → CONTROL CALL #1
→ A3 RMS + Повторить #1 → A4.1…A4.6 (живые сессии) → A5 лаб → CONTROL CALL #2 → A6 docs
→ B0 ground truth (из S6) → B1 Sortformer-окно → B2 one-mic → B3 screen audio → приёмка R11–R13
→ C1 screen recorder → C2 frames/vision → C3 provenance → C4 timeline → приёмка R14 + сквозной вопрос
```

## 11. Полный Definition of Done продукта

```text
[ ] рабочий путь §2.2 (встреча и внешнее аудио) сохранён; регрессионный набор зелёный
[ ] DoD A, DoD B, DoD C отмечены
[ ] комната проходит R1–R8; матрица устройств §7.3 заполнена по факту
[ ] контрольная запись #1 обработана трижды: baseline → после A3 → после B; разница объяснена в лаб
[ ] CONTROL CALL #2 зелёный; протечка S3 исчезла из записи, не из отчёта
[ ] сомнительное — CONFLICT/UNKNOWN в лаб и «[неизвестно]»/«[экран]» в стенограмме, никогда — чужое имя
[ ] сущность/задача/решение из созвона → source_span_id → реплика → дорожка; из экрана → frame_id
[ ] чат отвечает «Кто поставил задачу X?» с именем, временем и ссылкой на реплику
[ ] удалённая протечка отсутствует в entities/chunks
[ ] PROJECT-MAP, manifest.yaml, DECISIONS, OPERATIONS, TESTING актуальны; index_project_map прогнан
[ ] baseline-фикстуры записей #1/#2 в репозитории
[ ] обычный созвон не требует разработчика
```

Финальный результат — не «исправили девять багов», а: **SKKKK — готовый к эксплуатации продукт созвонов и памяти проекта: что работало — работает; известные дефекты устранены; мобильный и screen-share сценарии проверены на устройствах; запись и расшифровка воспроизводимы на одной и той же записи; сомнительные места показаны, а не скрыты; каждое знание проекта имеет проверяемый источник.**
