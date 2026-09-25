# SKKKK — Запись экрана: полный план (C1–C4)

Дата: 2026-09-13. Приложение к [`MEETING-PLAN.md`](MEETING-PLAN.md) §7 этап C.  
Источники: карта §5.13 / §7 `meeting_*`, `meeting_recordings.py`, `voice_cleaner.py`, `meeting_analyze.py`, `DECISIONS.md`.

**Цель.** После «Завершить» в той же куче, что голоса (`data/meetings/{room}/`), лежит видео каждого шаринга. Файл привязан к **тому человеку, который нажал «Экран»**, по той же `identity`, что его микрофон. Дальше — кадры, OCR/vision-расшифровка содержимого экрана, provenance и таймлайн. Чтобы это не разъехалось на второй встрече.

C1 **не ждёт этап B**. B3 — это звук чужого видео (`-screen.wav` → ASR), другой файл и другая семантика. C2–C4 начинаются только когда C1 прошёл живую приёмку.

---

## 1. Что уже есть, чего нет

| Есть | Нет |
|---|---|
| В комнате экран **видно** (плитка `meeting-tile-screen-{sid}`, `contentHint: detail`, 720p/5 fps) | Видео экрана **на диск** |
| `voice_cleaner` пишет **звук** шаринга: `{identity}-screen.wav`, `source: "screen"` | Процесс-рекордер, mp4, `source: "screen_video"` |
| `is_human_identity("skkkk-rec")` уже ложно (тест есть) | Сам `skkkk-rec`, pid, лог, `ensure_*` |
| `meeting.json` умеет дорожки с `identity / speaker / source / offset_sec` | Строка видео + `track_sid` / `fps` / имя **издателя** |
| Purge комнаты сносит весь каталог | Явная проверка, что mp4 уходит вместе с WAV |
| Лаб показывает `source` дорожки | Бейдж «экран» на видео, путь к mp4, кадры |

Камера **не пишется**. Только `TrackSource.SCREEN_SHARE` (видео). Камера остаётся живой плиткой.

---

## 2. Зафиксированные решения

Записать в `DECISIONS.md` вместе с кодом C0. Здесь — канон, чтобы шаги не спорили.

1. **Рекордер — второй Python-процесс**, не LiveKit Egress и не Redis. Файл: `tools/livekit/screen_recorder.py`. Участник LiveKit: `skkkk-rec` (hidden, `CUDA_VISIBLE_DEVICES=-1`). Тот же паттерн, что `voice_cleaner`: ListRooms → join → subscribe. `voice_cleaner.py` **не трогаем** (DFN + сырые WAV; перезапуск посреди встречи и так режет архив).
2. **Кучка одна.** Видео: `data/meetings/{room_name}/tracks/`. Учёт: тот же `manifest.json`. Часы: тот же `events.jsonl`. Связь со встречей: `meetings.room_name`.
3. **Издатель = владелец файла, не «кто говорит в картинке».** `identity` видео = `participant.identity` того, кто опубликовал screen-track (`member:{id}` / `guest:{hex}`). Имя в `meeting.json` — `MeetingParticipant.display_name` этой identity. Звук шаринга (`source: "screen"`) по-прежнему `speaker="external_or_unknown"`: в колонках вкладки может говорить кто угодно.
4. **Один шаринг — один файл.** Ключ манифеста для видео: `(identity, source, track_sid)`. Стоп и снова «Экран» → новый `track_sid` → новый mp4, обе строки остаются. Два человека шарят сразу → два файла, две identity.
5. **1 кадр/с** (`MEETING_SCREEN_FPS`, дефолт 1), холст **1920×1080**, битрейт **5 Мбит/с** (`MEETING_SCREEN_BITRATE`). Нужно, чтобы vision читал текст формы. В комнате по-прежнему 5 fps — рекордер прореживает и скейлит в холст.
6. **Без новых пакетов**, пока не упрёмся. Кадры: LiveKit `rtc.VideoStream` → ffmpeg stdin (Gyan.FFmpeg уже в PATH). `av` / PyAV — только если сырой кадр иначе не собрать; тогда спросить.
7. **Флаг.** `MEETING_SCREEN_RECORD` (дефолт как запись вообще: вкл, если `MEETING_RECORD≠0`). `0` — процесс жив, файлов нет. OCR кадров выполняется CPU и сохраняется в `video_transcript.json`/`.md`; `MEETING_SCREEN_VISION=1` включает дополнительное vision-обогащение. Голос MP4 не содержит: речь screen share остаётся в отдельном `-screen.wav`.
8. **Чистка** — человеком, не планировщиком (`DECISIONS`: ретеншн сырой медиа вручную). `remove_room_recordings` / `purge_meeting` сносят mp4 вместе с WAV.

---

## 3. Контракт данных (железно)

### 3.1. Файл

```text
data/meetings/{room}/tracks/{stem}.mp4
stem = safe_track_stem(identity, "screen_video") + "-" + safe_sid(track_sid)
пример: member-28-screen_video-TR_abc12.mp4
         guest-2d48…-screen_video-TR_def34.mp4
```

`safe_sid`: только `[A-Za-z0-9_-]`, обрезка до 16 символов после нормализации. Столкновения с `{identity}-screen.wav` нет: другой `source` и другое расширение.

Контейнер: mp4, yuv420p, libx264, preset `fast`, `-tune stillimage`, `-b:v 5M`, каждый кадр keyframe, fps = `MEETING_SCREEN_FPS`, холст 1920×1080. Звук в этот файл **не** muxить (звук шаринга уже в `-screen.wav`).

### 3.2. Строка `manifest.json`

```json
{
  "identity": "member:28",
  "source": "screen_video",
  "track_sid": "TR_xxx",
  "path": "tracks/member-28-screen_video-TR_xxx.mp4",
  "started_epoch": 1726234567.123,
  "ended_epoch": 1726234890.001,
  "fps": 1,
  "frames": 323,
  "duration_sec": 323.0,
  "width": 1280,
  "height": 720
}
```

`upsert` аудио **не менять**: ключ `(identity, source)` как сейчас. Для `screen_video` — отдельная функция `upsert_screen_video(..., track_sid=)`. Повторный `track_subscribed` того же sid продолжает тот же файл (как DFN переключает mic). Другой sid — новая строка.

`started_epoch` — стенные часы **первого принятого кадра**, не момент subscribe (иначе дыра до первого keyframe съедет относительно WAV).

### 3.3. События `events.jsonl`

Те же типы, что у микрофона:

- `track` / `start` — `identity`, `source: "screen_video"`, `track_sid`
- `track` / `end` — плюс `frames`, `duration_sec`
- `gap` — **не** пишем (видео не добиваем чёрными кадрами; пауза = нет файла на этом интервале)

Шкала встречи: `offset_sec = max(0, started_epoch − meeting_started_epoch)` — тот же `_track_offset_sec`, что у WAV.

### 3.4. `meeting.json` после «Проанализировать»

В `tracks[]` видео-строка рядом с голосами:

| Поле | Значение |
|---|---|
| `identity` | издатель шаринга |
| `speaker` | display_name издателя (**не** `external_or_unknown`) |
| `source` | `screen_video` |
| `src` | абсолютный путь к mp4 |
| `track_sid`, `fps`, `frames`, `width`, `height` | из манифеста |
| `offset_sec`, `duration_sec`, `started_epoch` | как у WAV |
| `silent` | нет / не считать через `has_speech` (это WAV-хелпер) |
| `child_id` | `track_child_id(art, identity + ":" + track_sid)` — не схлопывать с mic-child |

`analyzable_tracks` / `pipeline_meeting` **игнорируют** `screen_video` (как сейчас `source == screen` для ASR). Длительность встречи `max(offset+duration)` **включает** видео: шаринг без речи не должен обрезать шкалу.

`screen_tracks()` остаётся только про `source == "screen"` (аудио). Новый `screen_video_tracks()`.

### 3.5. Что нельзя сломать

Рабочий путь §2.2 `MEETING-PLAN`: голоса, DFN, «Проанализировать», proof, knowledge. Регрессия — тот же набор pytest плюс новые файлы ниже. Ожидания старых тестов (`source: screen` → `external_or_unknown`, stem `member-28-screen`) не менять.

---

## 4. Привязка к спикеру — правила, не эвристика

```text
кто нажал «Экран» → LiveKit publication.participant.identity
                 → та же identity, что mic WAV
                 → MeetingParticipant.display_name → speaker
```

| Случай | Как |
|---|---|
| Сотрудник шарит | `member:{user_id}`, имя из логина / карточки участника |
| Гость шарит | `guest:{hex}` после A1 (стабильный `guest_id` в localStorage) |
| Гость перезашёл и снова шарит | та же identity, **новый** `track_sid`, второй mp4 |
| Двое шарят сразу | два файла, два speaker |
| Издатель молчит, на экране чужой ролик | видео всё равно на издателе; звук ролика — `-screen.wav` / `external_or_unknown` |
| Издатель говорит в свой мик, водя курсором | речь в его mic WAV; видео на нём же; связь в знаниях — через время (`t_rel` кадра ∩ `start_sec` реплики), не через «картинка сказала» |
| Рекордер не успел join | нет строки `screen_video`, в лаб `NO_DATA`, имя не выдумываем |
| identity `skkkk-*` | не пишем (`is_human_identity`) |

Имя **не** берём из vision, из Sortformer, из того, кто «сейчас говорит» в `events.speakers`.

---

## 5. Этапы

Каждый шаг: где / что / как / тест / готово. Код — после красного теста. Новый pip — только с вопросом.

### C0. Контракт в `meeting_recordings` (без LiveKit)

**Где.** `app/app/services/meeting_recordings.py`, тесты `test_meeting_recordings.py`.

**Что.** Константа `SOURCE_SCREEN_VIDEO = "screen_video"`. `safe_track_stem(member:28, screen_video) == "member-28-screen_video"`. `upsert_screen_video` / `set_screen_video_frames` по `(identity, track_sid)`. `remove_room_recordings` уже сносит каталог — тест кладёт dummy mp4 и проверяет отсутствие.

**Как.** Не расширять старый `upsert_track` новым смыслом ключа: аудио останется `(identity, source)`.

**Тест.** stem; две сессии одного человека → две строки; тот же sid → одна строка, обновлённый path; purge удаляет mp4; `is_human_identity("skkkk-rec")` уже зелёный.

**Готово.** pytest зелёный; в `.env.example` группа Meeting: `MEETING_SCREEN_RECORD`, `MEETING_SCREEN_FPS=1`, `MEETING_SCREEN_MAX_W=1920`, `MEETING_SCREEN_HEIGHT=1080`, `MEETING_SCREEN_BITRATE=5M`.

### C1. Процесс `skkkk-rec`

#### C1.1 Скелет процесса

**Где.** `tools/livekit/screen_recorder.py`. Поднять: `edge_runtime.ensure_screen_recorder()` после LiveKit, рядом с `ensure_voice_cleaner`. Pid: `data/locks/screen_recorder.pid`. Лог: `data/logs/screen_recorder.log`. JWT: `sub=skkkk-rec`, `hidden`, `canSubscribe`, **без** `canPublish`.

**Что.** ListRooms `skkkk-*` раз в 2 с → join → на `track_subscribed` KIND_VIDEO + source SCREEN → запись. Камеру и аудио пропускать. Unpublish/unsubscribe → закрыть ffmpeg, `track end`.

**Как.** Скопировать у `voice_cleaner` только опрос комнат и join, не DFN и не WAV. Общий код часов/манифеста — вызовы `meeting_recordings`, не копипаста JSON.

**Тест.** `test_screen_recorder.py` (importlib, как `test_voice_cleaner`): id `skkkk-rec`; join hidden subscribe-only; `canPublish` нет; start_record только SCREEN video; камера в тексте функции не пишется; `MEETING_SCREEN_RECORD=0` → return 0.

**Готово.** `main` пишет `edge.screen status=started|already|off`. В комнате плитки людей без «запись экрана» в ростере (`isHidden`: `identity` начинается с `skkkk-`, не только `skkkk-dfn`). Denoise-логика `isCleaner` остаётся только про DFN.

#### C1.2 Кадры → mp4

**Где.** Тот же процесс: `_pump_video(track) → ffmpeg`.

**Что.** `rtc.VideoStream(track, format=I420)`; каждый кадр: если с прошлого записанного прошло `< 1/fps` — drop; иначе упаковать I420 (без stride/хвоста буфера) и записать ровно `w*h*3/2` байт в stdin. Подпись `width`/`height` у кадра LiveKit может врать — размер брать из длины буфера (`1920×1080` в буфере при ярлыке `960×540`). Первый кадр фиксирует `started_epoch` и открывает ffmpeg (`-f rawvideo -pix_fmt yuv420p -s WxH -r fps -i -`). Смена WxH дальше — `scale_i420` в тот же размер, не сырой хвост в stdin (иначе ffmpeg режет лишние байты на зелёные кадры). Filter `scale='min(W,iw)':-2` остаётся.

**Как.** ffmpeg как subprocess, не библиотека. Код закрытия: stdin close → wait(timeout) → kill. Падение ffmpeg → событие `track end` с тем, что успели, плюс строка в лог. Не поднимать GPU.

**Тест.** Юнит на «прореживание»: 5 fps вход, fps=1 → пишем каждый 5-й (по штампу, не по счётчику). Юнит на `safe_sid`. Живой прогон — C1.6. Без фейкового LiveKit в pytest.

**Готово.** На фикстуре из сырых кадров (если понадобится) длина файла ≈ `frames/fps`.

#### C1.3 Потолок перезапуска

**Где.** `docs/OPERATIONS.md`, тот же абзац, что про `voice_cleaner`.

**Что.** Перезапуск `skkkk-rec` посреди шаринга открывает **новый** mp4 (новый процесс не знает старый stdin). Старый файл остаётся, если успели закрыть; иначе — обрезанный moov. Код менять только между встречами.

**Тест.** Строковый: в OPERATIONS есть «screen_recorder» и запрет mid-call. В коде — `wave.open(..., "wb")` нет (это видео).

#### C1.4 Analyze / meeting.json

**Где.** `meeting_analyze._enqueue_meeting`, `_track_contract`.

**Что.** После mic + screen-audio дописать `screen_video_tracks`. Speaker = `_speaker_name(...)`. Не звать `has_speech` на mp4. `duration_sec` = `frames/fps` или `ended_epoch − started_epoch`. Jobs ASR **не** создавать на эти строки (фильтр `source != screen_video`, как уже для `screen`).

**Тест.** `test_meeting_screen_audio.py` или новый `test_meeting_screen_video.py`: фиктивный mp4 в манифесте → в `meeting.json` есть строка, `speaker` = имя участника, в списке jobs её нет; duration встречи растёт, если видео длиннее голоса.

**Готово.** Старые тесты screen-audio зелёные.

#### C1.5 Лаб и карточка

**Где.** `meeting_lab.py`, `meeting_lab.html`.

**Что.** У дорожки `source=screen_video` бейдж «экран», имя издателя, длительность, ссылка на mp4 (admin). Нет ASR-стадий — не рисовать пустой Parakeet. Проверка шкалы: `started_epoch` у видео считается; отсутствие видео при живом шаринге в `events.jsonl` → `WARN` (есть `track start` screen_video и нет файла).

**Тест.** `test_meeting_lab.py`: снимок с `screen_video` не падает; бейдж/source в payload.

#### C1.6 Живая приёмка R14 (без этого C1 не закрыт)

Протокол (один шаринг, два человека, десктоп Chrome):

1. Встреча → оба в комнате → A шарит окно 1С/Excel/PDF ≥ 40 с, двигает содержимое.
2. Стоп шаринга → B шарит ≥ 20 с.
3. A шарит ещё раз (вторая сессия).
4. Завершить.

Проверки на диске, не «на глаз только»:

- в `tracks/` ровно **3** mp4, stems содержат identity A, B, A;
- `manifest.json`: три `screen_video`, разные `track_sid`, у каждого `started_epoch` / `frames` / `duration_sec`;
- `events.jsonl`: три пары start/end, identity совпадают с манифестом;
- длительность каждого файла ±2 с от `(end−start)` в логе;
- после «Проанализировать» в `meeting.json` `speaker` = имена A и B, не `external_or_unknown`;
- mic WAV на месте, DFN жив, стенограмма как раньше;
- в комнате нет плитки «запись экрана» / `skkkk-rec`.

Телефон: шаринг с Android, если матрица §7.3 позволяет; iPhone — только если `canShareScreen()`. Нет шаринга → в матрице «не умеет», не дыра рекордера.

**Готово C1.** R14 пройден + pytest + `edge.screen=already` после рестарта портала (процесс не плодится).

---

### C2. Кадры → vision

**Где.** `app/app/services/meeting_frames.py`, промпт `docs/prompts/screen_frame.md`, флаг `MEETING_SCREEN_VISION`.

**Что.** На каждый `screen_video`: отбор меток на шкале файла (не все 1 fps). Ярусы: края шаринга → кластер начал реплик **издателя** (дыра < 8 с) → `ffmpeg select='gt(scene,0.15)'` → кластер реплик других, пока шаринг жив → пол каждые 15 с тишины издателя. Дедуп < 2 с (остаётся высший ярус). Потолок `MEETING_FRAMES_MAX` (60) **на `track_sid`**: режем снизу (пол → чужая речь → сцена → речь издателя). jpg+vision только на уцелевших. Выход: `lab/runs/{run_id}/frames/{track_sid}/{ts_ms}.jpg` + `frames.json` + `speech_screen_links.json` `{source_span_id, frame_id, shown_by, spoken_by, relation: own_share|over_others_share}`. Связка без LLM: кадр `k` держит `[t_k, t_{k+1})`. Имя на видео = издатель, не говорящий. Vision — `services/llm.py` + `docs/prompts/screen_frame.md`. Чанк `source_type=screen_frame` — C3.

**Как.** Кадры, `speech_screen_links.json` и video transcript пишутся на merge всегда. OCR выполняется без GPU; `MEETING_SCREEN_VISION` включает дополнительное описание через существующий LLM adapter. Не во время комнаты. `pipeline_meeting` передаёт merge-реплики в отбор. Тихий шаринг всё равно получает края и пол.

**Тест.** кластер 8 с; дедуп 2 с; кап 60 режет пол раньше речи издателя; `own_share` vs `over_others_share`; тихий шаринг → края+пол; без флага jpg+json есть, `.md` нет.

**Готово.** Прогон на mp4 с R14: кадры есть, md не пустые, стоимость только при флаге.

---

### C3. Провенанс (довести заготовку A0)

Уже есть: `source_span_id` на репликах, `value_json`, фильтр knowledge.

Довести по `MEETING-PLAN` §7 C3 плюс кадр:

1. LLM возвращает `source_span_ids`; несуществующие → fallback A0.
2. Чат: «(Имя, 12:34, созвон)» + якорь `/report#t=`.
3. ТЗ: сноска «источник: созвон N, Имя, 12:34».
4. Факт с экрана: `frame_id` / `t_rel` / `identity` издателя в том же `value_json`. Не подставлять «кто говорил в этот момент», если реплики нет.
5. Протечка по-прежнему не становится знанием.

**Тест.** `test_meeting_provenance.py`, `test_chat.py`, `test_tz.py`. Сквозной: «Что было на экране у Даши в 01:12?» → чанк `screen_frame` → её identity → mp4.

---

### C4. Лаб-таймлайн

**Где.** `meeting_lab.py::_timeline_snapshot`, CSS-grid в `meeting_lab.html`, без JS-библиотек.

**Что.** Одна шкала (`t=0` = `meeting_started_epoch`): полосы mic (речь/тишина/gap), `screen_video` (сегменты файлов), кадры точками, реплики, протечки, события join/mute. Клик — карточка C1.5 / кадр.

**Тест.** Снимок содержит сегмент видео с `offset_sec`; пустая встреча без экрана не падает.

---

## 6. Связь с B3 (звук экрана)

Не смешивать в одном шаге.

| | Файл | `source` | `speaker` | Куда |
|---|---|---|---|---|
| Микрофон | `{id}.wav` | `microphone` | имя участника | ASR, реплики |
| Звук шаринга | `{id}-screen.wav` | `screen` | `external_or_unknown` | B3: ASR + протечки `LEAK_BY_SCREEN` |
| Видео шаринга | `{id}-screen_video-{sid}.mp4` | `screen_video` | имя **издателя** | C1–C4 |

B3 может идти своим чередом. C1 от него не зависит.

---

## 7. Операции и отказ

- Старт: `main all` после `:7880` поднимает `voice_cleaner`, затем `screen_recorder`. Нет `venvs/voice` → комната жива, видео нет, в логе `edge.screen=skip` (как DFN).
- Рестарт портала **не** убивает `skkkk-rec` (сейчас так же с DFN: `already`). После смены кода C1 — снять pid и поднять заново, **между** встречами.
- Нет ffmpeg в PATH → процесс пишет ошибку и не врёт манифестом без файла.
- Диск: 1 fps 1080p @ 5 Мбит/с ≈ 37 МБ/мин. Не автоудалять. Кнопка purge как сейчас.
- Рекордер не публикует медиа — петли и «кто это в плитке» нет.

---

## 8. Тесты (сводка)

Новые: `test_meeting_recordings.py` (дописать), `test_screen_recorder.py`, `test_meeting_screen_video.py`, дописать `test_meeting_lab.py` / `test_meeting_analyze` / `test_meeting_provenance.py`.

В регрессию §2.2 добавить:

```text
test_screen_recorder.py test_meeting_screen_video.py
```

Живые: R14 (C1), затем R14+vision (C2), сквозной вопрос про кадр (C3). Матрица устройств — факт, не декларация.

---

## 9. Порядок работ

```text
C0 контракт + env + тесты манифеста
→ C1.1 процесс + скрыть skkkk-rec в UI + edge_runtime
→ C1.2 ffmpeg pump
→ C1.3 OPERATIONS
→ C1.4 meeting.json / не в ASR
→ C1.5 лаб
→ перезапуск rec (не mid-call) → R14
→ C2 кадры/vision (флаг)
→ C3 провенанс чата/ТЗ/кадра
→ C4 таймлайн
→ карта §5/§7/§8, manifest.yaml (svc_screen_recorder, svc_meeting_frames),
  DECISIONS, index_project_map
```

Карту и `index_project_map.py` — сразу после C1.1 (новый процесс = новый узел), не откладывать на конец C.

---

## 10. Definition of Done

```text
[ ] C0: screen_video в манифесте; purge сносит mp4; аудио-контракт не сломан
[ ] C1: skkkk-rec пишет mp4 в tracks/; identity = издатель; speaker = его имя;
        один sid — один файл; две сессии — два файла; камера не пишется;
        ASR jobs только на mic; R14 зелёный
[ ] C2: кадры по смене сцены, потолок, vision за флагом, frame_id в чанке
[ ] C3: чат/ТЗ цитируют реплику; факт с экрана → frame_id + identity издателя
[ ] C4: таймлайн со слоем экрана
[ ] OPERATIONS: второй процесс, потолок mid-call
[ ] карта + manifest.yaml + index
[ ] регрессия §2.2 + новые тесты зелёные
```

---

## 11. Чего не делаем

Egress, Redis, Docker для записи; писать видео в `voice_cleaner`; mux звука в mp4; запись камеры; клиентский MediaRecorder «на всякий случай» (два архива разъедутся); угадывать спикера по картинке; Sortformer на экране; автоудаление mp4; нативный iOS/Android capture; новые npm/vitest; новый pip без вопроса.
