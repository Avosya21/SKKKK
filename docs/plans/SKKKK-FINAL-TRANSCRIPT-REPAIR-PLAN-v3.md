# SKKKK — План полного закрытия дыр в конвейере созвона

**Дата:** 2026-09-13  
**Версия:** 3.0 — полный ремонт meeting pipeline  
**Основание:** `final-plan.md` (rev.2) + актуальные `docs/PLAN.md`, `docs/DECISIONS.md`, `docs/PROJECT-MAP.md`, `docs/TESTING.md`, `docs/OPERATIONS.md`, `docs/AUDIT-REPAIR-LOG.md`.

## 0. Назначение

Цель этого плана — довести конвейер созвона до состояния, в котором итоговая лента является проверяемым источником:

> **кто → что сказал → в какой момент → из какой дорожки это получено → почему слово/фраза оставлены или удалены → на основании каких данных эта информация попала в сущности, решения, задачи, граф и ТЗ.**

План закрывает не только девять уже найденных дыр, но и четыре класса ограничений, которые предыдущая ревизия сознательно оставляла открытыми:

1. сетевой джиттер и несоответствие времени записи;
2. два человека в одной комнате на разных микрофонах;
3. два человека, говорящие в один микрофон;
4. речь/голос из screen-share.

Дополнительно вводится сквозная **provenance-проверка**: исправленная стенограмма не должна превращаться в «непроверяемое знание» при следующем этапе обработки.

---

# 1. Что считается конечным результатом

## 1.1. Каноническая лента

Для каждого фрагмента должны быть доступны:

```json
{
  "speaker_id": "guest:...",
  "speaker_name": "Имя",
  "start": 12.340,
  "end": 13.870,
  "text": "Я номер один",
  "track_identity": "guest:...",
  "proof": "lab|giga|reconciled",
  "source": "microphone|screen",
  "confidence": 0.0,
  "removal": null
}
```

Поле `confidence` является диагностическим и не используется как разрешение на произвольное удаление текста.

Каждый удалённый фрагмент должен иметь отдельную запись причины:

```json
{
  "text": "...",
  "start": 41.2,
  "end": 42.7,
  "reason": "leak",
  "from_track": "guest:...",
  "to_track": "guest:...",
  "decision_by": "rank|rms|diarization|manual"
}
```

## 1.2. Главный инвариант

После публикации должно быть возможно ответить на вопросы:

- откуда взялась эта фраза;
- почему она принадлежит этому человеку;
- какие исходные аудиоданные её подтверждают;
- была ли она заменена GigaAM;
- была ли она удалена как протечка;
- каким был исходный timestamp;
- какой объект `entity/task/decision` создан на основании этой фразы.

## 1.3. Acceptance

Финальная приёмка состоит из двух уровней:

**Уровень A — технический:** все автоматические тесты и проверки зелёные.

**Уровень B — контрольный созвон:** один и тот же созвон повторно прогоняется после каждого основного этапа, а результаты измеряются цифрами.

Пока новую живую запись провести нельзя: уровень B заменяет синтетический пакет `app/app/tests/test_meeting_control_call.py` (фазы A–I без GPU). Живые p50/p95 `speaker_sync` остаются отложенными до следующего реального созвона.

---

# 2. Что не меняется

Нельзя возвращать старый production-путь.

Канон остаётся:

```text
LiveKit tracks
→ Parakeet-TDT
→ Sortformer только там, где он предусмотрен текущим canonical lab path
→ GigaAM second-hearing
→ reconcile
→ proof
→ speaker/track attribution
→ leak/overlap processing
→ final transcript
→ entities/index/graph/TZ/chat
```

Whisper, T-one, `merge_ensemble.py`, старый `build_transcript_view.py` в боевой путь не возвращаются.

Один GPU worker сохраняется.

В `lmstudio` режиме ASR и локальная LLM не находятся в VRAM одновременно.

Оригинальные файлы и исходные дорожки не изменяются.

Все новые данные пишутся в рамках существующей структуры `data/meetings/...` и `artifacts/...`.

---

# 3. Архитектурный принцип ремонта

Вместо одной большой функции исправляем конвейер слоями:

```text
01 Recording integrity
        ↓
02 Meeting event log
        ↓
03 Track timeline normalization
        ↓
04 ASR / proof
        ↓
05 Speaker attribution
        ↓
06 Leak detection
        ↓
07 Overlap-aware replica building
        ↓
08 Quality checks
        ↓
09 Published transcript
        ↓
10 Provenance / entities / graph / TZ
```

Нельзя переносить ошибки между слоями.

Каждый слой должен:

1. принимать явно определённый формат;
2. возвращать проверяемый результат;
3. писать диагностические данные;
4. иметь unit-test;
5. иметь живую проверку на контрольном созвоне.

---

# 4. Единая временная шкала

## 4.1. Ноль встречи

Единый ноль:

```text
meetings.started_at
        =
meeting.json.meeting_started_epoch
        =
0.0 секунды смешанного WAV
        =
t_rel событий
```

`voice_cleaner` и FastAPI находятся на одной машине, поэтому используется одна epoch-шкала.

## 4.2. Дорожка

Для каждой дорожки фиксируются:

```text
identity
source
started_epoch
ended_epoch
frames
sample_rate
duration_sec
offset_sec
```

`offset_sec` считается от первого реально записанного аудиосэмпла.

`joined_at` используется только как fallback для старых данных.

## 4.3. Математика

Для каждого слова:

```text
t_rel = word.start + offset_sec
```

Для события:

```text
t_rel = event.t - meeting_started_epoch
```

Для проверки продолжительности:

```text
meeting_duration =
max(track.offset_sec + track.duration_sec)
```

Нельзя использовать сумму длительностей дорожек как длительность встречи.

---

# 5. Этап 0 — baseline и заморозка поведения

## Цель

Зафиксировать текущее поведение до изменения кода.

## Действия

1. Запустить:

```powershell
cd C:\1C_NEW_SKKKK
venv\Scripts\python.exe -m pytest app -m "not gpu and not slow" -q
```

2. Сохранить результаты.
3. Сохранить текущий HTML meeting lab.
4. Сохранить текущие `meeting.json`, `manifest.json`, `proof/transcript.json`.
5. Зафиксировать контрольный созвон или имеющийся артефакт тестовой встречи как baseline.

## Новые требования

Создать:

```text
tests/fixtures/meeting/
    baseline_meeting.json
    baseline_events.jsonl
    baseline_track.wav
    baseline_transcript.json
```

Ничего в baseline не изменять.

## Готово

Есть воспроизводимый baseline и все тесты доработки начинают выполняться только после него.

---

# 6. Этап 1 — целостная запись дорожек

**Закрывает:** дыра 1 + часть дыры 7 + фундамент для 2/5/6.

## 6.1. `PcmWavWriter`

Файл:

```text
app/app/services/meeting_recordings.py
```

Добавить:

```python
GAP_PAD_SEC = 0.1
```

При поступлении нового кадра:

1. определить elapsed time от предыдущего кадра;
2. если gap > `GAP_PAD_SEC`:
   - вставить нулевые PCM-сэмплы;
   - записать `gap` event;
3. записать реальные кадры.

## 6.2. Запрет перезаписи начала

`upsert_track()` обязан:

- один раз сохранить `started_epoch`;
- не менять его при republish;
- сохранять фактическое количество frames.

## 6.3. Тесты

Добавить:

```text
test_wav_writer_pads_mute_gap
test_wav_writer_does_not_pad_normal_frames
test_manifest_keeps_track_start
test_writer_reports_gap
```

Проверяется:

```text
duration ≈ frames / sample_rate
```

## 6.4. Ограничение

Этот механизм не доказывает, что каждый кадр пришёл в момент захвата. Он сохраняет серверное время получения/обработки.

Поэтому этап 2 добавляет независимый event log, а этап 8 вводит `track_frames` и `clock_integrity`.

---

# 7. Этап 2 — журнал событий LiveKit

**Закрывает:** дыра 2 и даёт данные для 5/7.

## 7.1. Новый файл

```text
data/meetings/{room}/events.jsonl
```

## 7.2. Новый сервис

В:

```text
app/app/services/meeting_recordings.py
```

создать:

```python
class MeetingEventLog:
    append(kind, **fields)

def read_events(path) -> list[dict]
```

Каждая строка:

```json
{
  "t": 1750000000.123,
  "kind": "speakers",
  "active": ["member:28", "guest:c5"]
}
```

Открывать файл в append-mode на каждую запись.

Битая строка не должна ломать чтение.

## 7.3. События

Записывать:

### Speakers

```json
{
  "kind": "speakers",
  "active": [...]
}
```

Сохранять только при изменении кортежа.

### Mute

```json
{
  "kind": "mute",
  "identity": "...",
  "muted": true
}
```

### Track

```json
{
  "kind": "track",
  "identity": "...",
  "event": "start",
  "frames": 12345
}
```

и аналогично `end`.

### Participant

```json
{
  "kind": "participant",
  "identity": "...",
  "event": "join"
}
```

### Connection quality

```json
{
  "kind": "quality",
  "identity": "...",
  "value": "poor"
}
```

### Gap

```json
{
  "kind": "gap",
  "identity": "...",
  "sec": 11.4
}
```

## 7.4. Скрытый участник

`skkkk-dfn` исключается из пользовательских событий.

## 7.5. Тесты

```text
test_event_log_append
test_event_log_recovers_from_bad_line
test_speakers_changed
test_mute_events
test_track_start_end
test_gap_event
```

---

# 8. Этап 3 — единый контракт `meeting.json`

**Закрывает:** дыра 9.

Функция:

```text
services/meeting_analyze.py::_enqueue_meeting
```

должна создавать:

```json
{
  "meeting_id": "...",
  "room_name": "...",
  "meeting_started_epoch": 1750000000.0,
  "meeting_ended_epoch": 1750000200.0,
  "events": "data/meetings/room/events.jsonl",
  "duration_sec": 200.0,
  "tracks": [
    {
      "identity": "...",
      "speaker": "...",
      "source": "microphone",
      "child_id": "...",
      "started_epoch": 1750000001.0,
      "offset_sec": 1.0,
      "frames": 160000,
      "sample_rate": 16000,
      "duration_sec": 10.0,
      "silent": false
    }
  ]
}
```

Все downstream readers читают именно этот контракт.

Больше не допускается скрытый поход за критически важными значениями в другие места БД.

Для старых `meeting.json` разрешено graceful degradation:

```text
нет поля → check = none
```

но новые созвоны обязаны иметь полный контракт.

## Тест

Проверить JSON schema-поля и совместимость старого JSON.

---

# 9. Этап 4 — исправление текста GigaAM

**Закрывает:** дыра 3.

## 9.1. `word_text`

Файл:

```text
services/meeting_merge.py
```

Правило:

```python
if "final" in row:
    return row["final"] or ""
```

То есть:

```text
final: ""
```

означает сознательно удалённое слово.

Fallback допустим только если:

```text
final отсутствует
```

или:

```text
final is None
```

## 9.2. `meeting_lab`

Использовать одну функцию `word_text`, а не собственную альтернативную логику.

## 9.3. Тесты

Обязательно:

```text
final=""      -> ""
final=None    -> original
no final      -> original
```

---

# 10. Этап 5 — сохранение таймкодов при proof

**Закрывает:** дыра 4.

## 10.1. Проблема

Если GigaAM выигрывает текстовое расхождение, нельзя складывать всю новую фразу в первое слово.

## 10.2. Новая функция

Создать:

```python
spread_tokens(tokens, slots) -> list[str]
```

Алгоритм:

```text
j-th token
    ↓
slot = indexes[j * N // M]
```

где:

```text
N = количество исходных слов
M = количество токенов GigaAM
```

## 10.3. Правила

- timestamp всегда берётся от Parakeet;
- GigaAM не генерирует новые timestamps;
- порядок токенов сохраняется;
- каждый token должен попасть ровно в один slot;
- пустые хвостовые slots получают `final=""`;
- вставки и удаления не изменять.

## 10.4. Диагностика

Добавить:

```text
proof_distribution = {
    tokens,
    slots,
    strategy: "uniform",
    source: "giga"
}
```

## 10.5. Тесты

Минимум:

```text
5 -> 5
2 -> 5
7 -> 3
0 -> N
```

Проверять сохранение всех токенов и порядок.

## 10.6. Второй уровень

Если baseline покажет систематическую ошибку распределения, добавить второй алгоритм:

```text
spread_tokens_by_char_length()
```

но не менять первый вариант до замера.

---

# 11. Этап 6 — построение реплик по человеку

**Закрывает:** дыра 8.

## Проблема

Нельзя строить реплики после глобальной пословной сортировки:

```text
A1 B1 A2 B2
```

потому что это искусственно дробит фразы.

## Новая функция

```python
merge_track_replicas(groups)
```

Для каждого человека:

```text
group
→ replicas_from_words(group)
```

Граница реплики:

```text
gap > REPLICA_MAX_GAP_SEC
```

После этого:

```text
all replicas
→ sort(start, speaker)
```

## Важно

Пословная timeline остаётся и используется для:

- proof;
- leak detection;
- diagnostics;
- audit.

Меняется только построение человекочитаемых реплик.

## Тест

Сценарий:

```text
A: один два три       10.0–11.5
B: раз два             10.4–11.0
```

Ожидается:

```text
A: один два три
B: раз два
```

а не:

```text
A: один
B: раз
A: два
B: два
A: три
```

---

# 12. Этап 7 — удаление пустых дорожек и объединение одного человека

**Закрывает:** дыра 6.

## 12.1. `has_speech`

На стадии анализа WAV определить наличие речи.

Первый вариант:

```text
RMS
1-секундные окна
SPEECH_MIN_SEC = 2
SPEECH_RMS_MIN = configurable
```

Тихая дорожка:

```text
silent=true
```

и не отправляется на ASR.

## 12.2. Объединение

Все child tracks с одинаковым подтверждённым человеком объединяются в один `group`.

Оригинальные child runs не удаляются.

То есть:

```text
raw track A
raw track B
    ↓
group "Игорь"
```

## 12.3. Безопасность

Для `member:` использовать стабильный `user_id`.

Для `guest:` не объединять два одинаковых отображаемых имени автоматически, если нет идентичного session identity.

## 12.4. Тесты

```text
silence -> skipped
speech -> processed
same member on 2 tracks -> 1 group
same guest display_name but different identity -> 2 groups
```

---

# 13. Этап 8 — автоматическая проверка шкалы

**Закрывает:** дыра 7 и превращает ошибки времени в измеримую диагностику.

Создать:

```text
app/app/services/meeting_checks.py
```

API:

```python
run_checks(
    meeting,
    events,
    tape,
    leaks
) -> list[Check]
```

## 13.1. `track_span`

OK:

```text
offset + duration <= meeting duration + 1 sec
```

FAIL:

```text
дорожка вылезла за встречу
```

## 13.2. `track_frames`

Сравнить:

```text
track end - track start
```

и:

```text
frames / sample_rate
```

Пороги:

```text
OK      <= 0.30 sec
WARN    0.30–0.70 sec
FAIL    > 0.70 sec
```

## 13.3. `gaps`

Каждый `gap` обязан иметь:

```text
mute
```

в диапазоне ±1 сек.

Иначе:

```text
WARN
```

## 13.4. `leaks`

Количество записей `leaks.json`.

Важно:

```text
leaks > 0
```

само по себе не обязательно ошибка — это означает, что механизм обнаружил реальную протечку.

Поэтому статус:

```text
INFO/WARN = обнаружено и обработано
FAIL = leak-обработка не оставила trace
```

## 13.5. `giga_share`

Выброс относительно медианы остальных дорожек.

Не считать сам по себе ошибкой.

## 13.6. `no_started_epoch`

Новые записи без `started_epoch`:

```text
FAIL
```

Старые:

```text
NONE
```

## 13.7. `head_loss`

Проверить:

```text
min(started_epoch) - meeting_started_epoch
```

Целевой порог:

```text
OK <= 3 сек
WARN > 3 сек
```

## 13.8. `speaker_sync`

**Единый acceptance threshold:**

```text
OK    <= 0.30 сек
WARN  0.30–0.70 сек
FAIL  > 0.70 сек
```

Мерить не только median, но и:

```text
median
p95
max
```

Финальная таблица показывает все три.

---

# 14. Этап 9 — реальная защита от микрофонной протечки

**Закрывает:** дыра 5.

## 14.1. Основной алгоритм

На вход:

```text
groups = tracks after proof
events.jsonl
wavs
```

Функция:

```python
find_leaks(groups)
```

строит индекс биграмм:

```text
track B:
(token1, token2) -> positions
```

Затем ищет совпадение у A с:

```text
abs(start_a - start_b) <= 0.4 sec
```

и продолжает цепочку.

Минимум:

```text
LEAK_MIN_TOKENS = 2
```

## 14.2. Кто настоящий автор

Первый уровень:

```text
active speaker rank
```

Второй:

```text
RMS
```

Третий:

```text
native diarization fallback
```

если предыдущие два дали:

```text
нет данных
или
ничью
```

## 14.3. Удаление

Удалять слова только из проигравшей дорожки.

Всегда писать:

```text
proof/leaks.json
```

пример:

```json
{
  "speaker": "guest:B",
  "from_speaker": "guest:A",
  "start": 34.2,
  "end": 35.7,
  "text": "один два три",
  "by": "rank"
}
```

## 14.4. Нельзя

Нельзя молча выкидывать текст.

Нельзя удалять совпадение только по одному слову.

Нельзя проверять полосы одного подтверждённого человека между собой.

---

# 15. Этап 10 — отдельное решение для двух людей в одной комнате

Это больше не оставляется за бортом.

## 15.1. Проблема

Если A и B находятся физически рядом, оба микрофона могут содержать голос обоих.

LiveKit `active_speakers` показывает, кто громче на своей дорожке, но это не является доказательством личности.

## 15.2. Триггер

Не запускать тяжёлый дополнительный pipeline всегда.

Запускать только когда:

```text
leak candidate
AND
rank ambiguous
AND
RMS ambiguous
```

или:

```text
одинаковая фраза одновременно на двух дорожках
```

## 15.3. Fallback

Использовать существующий voice pipeline/WeSpeaker и, при наличии в текущем репозитории нужного native diarization-модуля, сегментацию только спорного участка.

Правило:

```text
native diarization only on suspect window
```

а не на весь созвон.

## 15.4. Результат

Для каждого спорного overlap:

```text
author = A
secondary = B
confidence = ...
method = diarization
```

Если и fallback не уверен:

```text
status = unresolved
```

Текст не удаляется автоматически.

Он помечается в лаб как:

```text
CONFLICT — требует ручной проверки
```

Это безопаснее, чем уверенная ложь.

---

# 16. Этап 11 — два человека в один микрофон

Это отдельный класс, который дорожки пользователя физически не различают.

## Триггер

На одной track:

```text
voice embedding changes sharply
AND
есть overlap/быстрая смена speaker characteristics
```

## Fallback

Запуск локальной diarization только на suspect intervals.

Источники:

```text
mixed microphone track
→ speech segments
→ speaker clusters
→ mapping to known speaker profiles
```

## Правило безопасности

Если невозможно уверенно связать cluster с именем:

```text
speaker = unknown
```

а не назначать имя предыдущего участника.

## Acceptance

Добавить отдельную фазу теста:

```text
один микрофон
A → B → A
```

Ожидается:

```text
A
B
A
```

или `unknown` на спорном участке.

Нельзя принимать:

```text
A
A
A
```

как зелёный результат.

---

# 17. Этап 12 — экранный звук

## 17.1. Что сохраняем

Если LiveKit отдаёт:

```text
source=screen
```

дорожка сохраняется независимо от того, попадает она в основной transcript или нет.

## 17.2. Атрибуция

В первую очередь:

```text
screen source
→ publisher identity
```

То есть известно:

> этот аудиопоток был опубликован пользователем X.

Но это не доказывает, что голос на нём принадлежит X.

## 17.3. Два режима

### Режим A — речь самого демонстрирующего

Можно использовать как источник контекста демонстрации.

### Режим B — видео/чужая запись внутри screen-share

Помечать:

```text
source=screen
speaker=external_or_unknown
```

и не присваивать автоматически человеческое имя.

## 17.4. Дополнительная обработка

Экранный звук идёт в отдельный transcript channel:

```text
screen_transcript
```

и может попадать в knowledge index с:

```text
source_type = screen_audio
```

но не смешивается с пользовательской speech attribution без доказательства.

---

# 18. Этап 13 — защита от сетевого джиттера

## Проблема

SDK не даёт нам идеальную физическую метку момента записи каждого PCM-фрейма.

Поэтому нельзя обещать математическую точность исходного микрофонного capture-time.

## Что делаем

1. сохраняем server receive timing;
2. сохраняем `frames`;
3. сохраняем `started_epoch`;
4. сохраняем connection quality;
5. строим `track_frames`;
6. сохраняем `gap`;
7. сравниваем с длиной встречи.

## Дополнительный контроль

Добавить:

```text
clock_integrity
```

с отчётом:

```text
expected_duration
actual_duration
frame_duration
difference
```

## Правило

Если отличие стабильно выше порога:

```text
meeting = degraded
```

и не считается идеальным.

Это не скрытая ошибка.

---

# 19. Этап 14 — provenance от transcript до знаний

Это обязательная часть плана.

## Проблема

Исправленная лента может быть идеальной, но потом:

```text
entity
decision
task
```

теряют точную ссылку на исходную фразу.

## Что добавить

Каждая сущность, решение и задача из созвона должны иметь:

```json
{
  "artifact_id": "...",
  "source_type": "call",
  "speaker": "...",
  "start_sec": 123.4,
  "end_sec": 131.2,
  "source_span_id": "...",
  "text_hash": "..."
}
```

## Как строить

Перед extraction каждую реплику снабжать стабильным ID:

```text
turn:{artifact_id}:{speaker}:{start_ms}:{ordinal}
```

LLM получает эти IDs вместе с текстом.

Ответ extraction обязан возвращать:

```text
source_turn_ids[]
```

При записи в DB эти IDs превращаются в ссылки.

## Запрет

Нельзя принимать entity/decision/task без source reference для call-derived данных.

## Тест

Создать вопрос:

> Кто поставил задачу X?

И проверить цепочку:

```text
chat answer
→ entity/task
→ turn_id
→ timestamp
→ transcript
→ source audio
```

---

# 20. Этап 15 — проверка source truth после knowledge pipeline

После полного pipeline добавить интеграционный тест:

```text
meeting transcript
    ↓
entities
    ↓
decision/task
    ↓
current_tz
    ↓
RAG answer
```

Проверяем:

1. speaker не меняется;
2. timestamp не исчезает;
3. source span существует;
4. RAG ответ содержит source;
5. ответ не ссылается на удалённый leak;
6. удалённая реплика не становится entity.

---

# 21. Этап 16 — человекочитаемый Meeting Lab

В лаборатории должна появиться единая карта:

```text
Timeline
├── audio
├── speakers
├── words
├── replicas
├── leaks
├── overlaps
├── events
└── quality checks
```

## Для каждого спорного участка

Показать:

```text
время
speaker
text
track
proof
leak?
event
confidence
```

## Цвета

```text
green  = confirmed
yellow = warning
red    = fail/conflict
gray   = no data
```

## Для leak

Показывать:

```text
A: [исходное]
B: [удалено]
Причина: microphone leak
Метод: rank
```

## Для unresolved

Кнопка:

```text
Проверить участок
```

Не нужно заставлять пользователя пересобирать весь созвон.

---

# 22. Этап 17 — контрольный созвон

После этапов 1–8 записать один canonical meeting.

## Состав

3 человека, ~3 минуты.

### A — clock

По очереди:

```text
Я номер один
Я номер два
Я номер три
```

два круга.

### B — overlap

Два человека одновременно считают.

### C — mute

Третий mute 30 секунд.

### D — reconnect

Один участник выходит и входит снова.

### E — goodbye

Все заканчивают созвон.

### F — отдельный leak test

Один участник включает вывод чужого голоса через колонки так, чтобы его микрофон действительно записал эту речь.

### G — same-room test

Два участника физически рядом и говорят одновременно.

### H — one-mic test

Два человека по очереди говорят в один микрофон.

### I — screen-share test

Запустить видео с человеческой речью через screen-share audio.

---

# 23. Acceptance criteria

## Критерий 1 — шкала

```text
median speaker_sync <= 0.30 s
p95 speaker_sync <= 0.50 s
max <= 0.70 s
```

Если нет данных — не зелёный.

## Критерий 2 — протечки

Все реальные leak cases должны иметь:

```text
leak record
reason
source track
target track
start/end
decision method
```

Ни один удалённый фрагмент не должен исчезать без trace.

## Критерий 3 — rejected words

Ни один:

```text
final=""
```

не должен возвращаться в опубликованную ленту.

## Критерий 4 — proof timing

Количество токенов GigaAM:

```text
input token count == distributed token count
```

Никаких потерянных токенов.

## Критерий 5 — overlap

Каждый участник сохраняет связную реплику.

## Критерий 6 — reconnect

Повторный вход:

```text
один speaker group
```

без повторной сущности и без лишнего пустого ASR.

## Критерий 7 — same-room

Если система уверена:

```text
правильная attribution
```

если нет:

```text
CONFLICT / UNKNOWN
```

но не ложная подпись.

## Критерий 8 — one-mic

Минимум:

```text
A → B → A
```

не может автоматически стать:

```text
A → A → A
```

## Критерий 9 — screen-share

Screen audio не приписывается человеку автоматически без доказательства.

## Критерий 10 — provenance

100% call-derived entities/tasks/decisions имеют source turn.

## Критерий 11 — knowledge integrity

Удалённый leak не появляется:

```text
entities
graph
TZ
chat
```

как факт.

---

# 24. Этап 18 — тестовый пакет

Добавить/обновить:

```text
app/app/tests/test_meeting_recordings.py
app/app/tests/test_meetings.py
app/app/tests/test_meeting_merge.py
app/app/tests/test_proof_spread.py
app/app/tests/test_meeting_checks.py
app/app/tests/test_meeting_lab.py
app/app/tests/test_meeting_provenance.py
app/app/tests/test_meeting_overlap.py
app/app/tests/test_meeting_screen_audio.py
```

## Обязательные тестовые категории

### Unit

- gap padding;
- event log;
- offset;
- `word_text`;
- token spreading;
- replica grouping;
- leak detection;
- leak resolution;
- check thresholds;
- group merging;
- source span IDs.

### Integration no-GPU

- meeting.json;
- event replay;
- pipeline data contracts;
- provenance;
- old meeting compatibility.

### GPU

```powershell
pytest app -m gpu -n 0 -q
```

### Full

```powershell
pytest app -q
```

---

# 25. Этап 19 — порядок реализации

| Шаг | Работа | Причина порядка |
|---|---|---|
| 0 | baseline | знать исходную точку |
| 1 | writer/gap | исправить фундамент времени |
| 2 | events.jsonl | получить наблюдаемость |
| 3 | meeting.json | открыть данные downstream |
| 4 | rejected words | убрать явную порчу текста |
| 5 | proof timing | восстановить временную корректность |
| 6 | replicas per track | исправить overlap presentation |
| 7 | silent/duplicate tracks | очистить группы |
| 8 | meeting checks | включить измеряемость |
| 9 | leak detector | убрать дубли |
| 10 | same-room fallback | обработать трудный acoustic case |
| 11 | one-mic diarization fallback | обработать multi-speaker single track |
| 12 | screen audio | не терять audio source |
| 13 | jitter diagnostics | не скрывать timing uncertainty |
| 14 | provenance | сохранить источник знания |
| 15 | knowledge integrity | проверить downstream |
| 16 | Meeting Lab | сделать спорные места проверяемыми |
| 17 | контрольный созвон | измерить всё на одном источнике |
| 18 | full test suite | регрессия |
| 19 | финальная документация | синхронизировать архитектуру |

---

# 26. Правило “не принимать зелёным то, что не доказано”

Новая система статусов:

```text
PASS
WARN
FAIL
NO_DATA
UNRESOLVED
```

`NO_DATA` никогда автоматически не считается `PASS`.

`UNRESOLVED` не превращается автоматически в имя.

`WARN` нельзя прятать из UI.

`FAIL` запрещает финальный acceptance.

---

# 27. Что менять в DECISIONS.md

После реализации необходимо явно зафиксировать:

1. production meeting pipeline использует native track-first attribution;
2. leak detection работает до формирования финальных реплик;
3. удалённые фрагменты сохраняются в `leaks.json`;
4. подозрительные overlap могут запускать локальный diarization fallback только на спорном участке;
5. unresolved speaker не получает выдуманного имени;
6. screen-share audio является отдельным source channel;
7. call-derived knowledge обязан иметь source turn provenance.

Это важно, потому что `DECISIONS.md` является документом зафиксированных решений и не должен содержать скрытый конфликт с новым поведением.

---

# 28. Что менять в PROJECT-MAP.md

Обновить:

- граф meeting pipeline;
- `meeting_recordings.py`;
- `meeting_analyze.py`;
- `meeting_merge.py`;
- `meeting_checks.py`;
- `meeting_lab.py`;
- provenance path;
- `events.jsonl`;
- `leaks.json`;
- same-room fallback;
- screen audio branch.

После изменений:

```powershell
python scripts/index_project_map.py
```

Проверить, что карта отражает фактический code path.

---

# 29. Что НЕ делать

Не возвращать:

```text
Whisper
T-one
merge_ensemble
глобальную LLM-склейку всего созвона
```

Не использовать LLM для угадывания speaker identity там, где есть технические признаки.

Не удалять слова без audit record.

Не использовать `display_name` guest как единственный identity key.

Не считать отсутствие события доказательством отсутствия речи.

Не считать `active_speakers` доказательством личности.

Не считать `screen publisher` доказательством того, кто говорит внутри видео.

Не принимать `NO_DATA` за зелёный результат.

---

# 30. Финальный Definition of Done

Работа считается законченной только когда одновременно выполнено всё:

```text
[ ] baseline сохранён
[ ] writer tests зелёные
[ ] events.jsonl появляется на новом созвоне
[ ] meeting.json полный
[ ] timing checks зелёные
[ ] rejected words не возвращаются
[ ] proof timestamps не схлопываются
[ ] overlap не дробит реплики
[ ] silent tracks не идут в ASR
[ ] duplicate track одного человека объединяется
[ ] leak detection работает
[ ] каждый leak имеет audit record
[ ] same-room case либо атрибутирован, либо UNRESOLVED
[ ] one-mic case либо разделён, либо UNRESOLVED
[ ] screen audio сохранён отдельным source
[ ] jitter диагностируется
[ ] provenance существует от entity/task/decision до turn
[ ] leak не попадает в knowledge layer
[ ] Meeting Lab показывает проверки
[ ] контрольный созвон прошёл
[ ] full pytest зелёный
[ ] PROJECT-MAP обновлён
[ ] DECISIONS обновлён
[ ] тестовый baseline сохранён как regression
```

## 31. Итог

После выполнения этого плана система не должна обещать физически невозможное «идеальное распознавание любого голоса».

Правильная цель:

> **каждая опубликованная фраза либо технически подтверждена, либо явно помечена как предупреждение/конфликт; ни одна исправленная или удалённая часть не исчезает бесследно; а любой факт, пришедший из созвона, можно проследить обратно до конкретного времени и источника.**

Это и есть критерий надёжной стенограммы для SKKKK, а не просто высокий процент WER.
