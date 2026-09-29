"""Чистые проверки шкалы созвона. Без БД и без LiveKit."""

from __future__ import annotations

import os
from statistics import median

STATUS_PASS = "PASS"
STATUS_WARN = "WARN"
STATUS_FAIL = "FAIL"
STATUS_NO_DATA = "NO_DATA"
STATUS_UNRESOLVED = "UNRESOLVED"

CHECK_OK_SEC_ENV = "MEETING_CHECK_OK_SEC"
CHECK_WARN_SEC_ENV = "MEETING_CHECK_WARN_SEC"
HEAD_LOSS_WARN_ENV = "MEETING_HEAD_LOSS_WARN_SEC"
GAP_MUTE_WINDOW_ENV = "MEETING_GAP_MUTE_WINDOW_SEC"
OK_SEC_DEFAULT = 0.30
WARN_SEC_DEFAULT = 0.70
HEAD_LOSS_WARN_DEFAULT = 3.0
GAP_MUTE_WINDOW_DEFAULT = 1.0
TRACK_SPAN_SLACK_SEC = 1.0
SPEAKER_SYNC_BEFORE_SEC = 0.3
SPEAKER_SYNC_AFTER_SEC = 1.0
CLOCK_DEGRADED = "degraded"


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def check_ok_sec() -> float:
    return _env_float(CHECK_OK_SEC_ENV, OK_SEC_DEFAULT)


def check_warn_sec() -> float:
    return _env_float(CHECK_WARN_SEC_ENV, WARN_SEC_DEFAULT)


def head_loss_warn_sec() -> float:
    return _env_float(HEAD_LOSS_WARN_ENV, HEAD_LOSS_WARN_DEFAULT)


def gap_mute_window_sec() -> float:
    return _env_float(GAP_MUTE_WINDOW_ENV, GAP_MUTE_WINDOW_DEFAULT)


def _check(check_id: str, status: str, title: str, detail: str, **extra) -> dict:
    row = {"id": check_id, "status": status, "title": title, "detail": detail}
    row.update(extra)
    return row


def _level(delta: float, ok_sec: float, warn_sec: float) -> str:
    if delta <= ok_sec:
        return STATUS_PASS
    if delta <= warn_sec:
        return STATUS_WARN
    return STATUS_FAIL


REPLICA_MAX_CHARS_PER_SEC = 25.0  # обычная речь ~10–15 знаков/с: больше — текст не мог быть сказан за это время
REPLICA_MIN_CHARS = 20
REPLICA_MIN_SEC = 0.5
COVERAGE_WARN_SEC = 3.0  # столько секунд звука на микрофоне без единой реплики — уже подозрительно
COVERAGE_WARN_SHARE = 0.25


def check_replica_density(replicas: list[dict]) -> dict:
    """Реплика с невозможной плотностью текста — признак «схлопнутой» расшифровки (на реальной встрече — 5 реплик в 1,4 с)."""
    if not replicas:
        return _check("replica_density", STATUS_NO_DATA, "Плотность реплик", "нет реплик")
    bad = []
    for row in replicas:
        text = str(row.get("text") or "")
        span = max(float(row.get("end") or 0.0) - float(row.get("start") or 0.0), REPLICA_MIN_SEC)
        if len(text) >= REPLICA_MIN_CHARS and len(text) / span > REPLICA_MAX_CHARS_PER_SEC:
            bad.append(f"{row.get('speaker')} {float(row.get('start') or 0.0):.0f}с: {len(text)} знаков за {span:.1f} с")
    if bad:
        return _check("replica_density", STATUS_FAIL, "Плотность реплик", "схлопнутый текст: " + "; ".join(bad[:5]), count=len(bad))
    return _check("replica_density", STATUS_PASS, "Плотность реплик", f"{len(replicas)} реплик в норме")


def check_track_coverage(coverage: list[dict] | None) -> dict:
    """Звук на микрофоне участника есть, а текста от него нет: речь потеряна или приписана другому."""
    if not coverage:
        return _check("track_coverage", STATUS_NO_DATA, "Звук без текста", "сверка не выполнялась (старая обработка)")
    flagged = []
    for row in coverage:
        active = float(row.get("active_sec") or 0.0)
        uncovered = float(row.get("uncovered_sec") or 0.0)
        if uncovered >= COVERAGE_WARN_SEC and active > 0 and uncovered / active >= COVERAGE_WARN_SHARE:
            flagged.append(f"{row.get('speaker')}: {uncovered:.0f} из {active:.0f} с звука без текста")
    if flagged:
        return _check("track_coverage", STATUS_WARN, "Звук без текста", "; ".join(flagged), speakers=len(flagged))
    return _check("track_coverage", STATUS_PASS, "Звук без текста", "весь заметный звук покрыт текстом")


def run_checks(
    meeting: dict,
    events: list[dict] | None = None,
    tape: list[dict] | None = None,
    leaks: list[dict] | None = None,
    replicas: list[dict] | None = None,
    coverage: list[dict] | None = None,
) -> list[dict]:
    rows = list(meeting.get("tracks") or [])
    log = list(events or [])
    words = list(tape or [])
    leak_rows = list(leaks or [])
    ok_sec = check_ok_sec()
    warn_sec = check_warn_sec()
    return [
        check_track_span(meeting, rows),
        check_track_frames(rows),
        check_gaps(log),
        check_leaks(leak_rows),
        check_giga_share(words),
        check_started_epoch(meeting, rows),
        check_head_loss(meeting, rows),
        check_speaker_sync(meeting, log, words, ok_sec, warn_sec),
        check_clock_integrity(meeting, rows, ok_sec, warn_sec),
        check_replica_density(list(replicas or [])),
        check_track_coverage(coverage),
    ]


def check_track_span(meeting: dict, tracks: list[dict]) -> dict:
    duration = float(meeting.get("duration_sec") or 0.0)
    if not tracks or duration <= 0:
        return _check("track_span", STATUS_NO_DATA, "Границы дорожек", "нет длительности встречи")
    overflow = []
    for row in tracks:
        span = float(row.get("offset_sec") or 0.0) + float(row.get("duration_sec") or 0.0)
        if span > duration + TRACK_SPAN_SLACK_SEC:
            overflow.append(str(row.get("identity") or "?"))
    if overflow:
        return _check("track_span", STATUS_FAIL, "Границы дорожек", "вылезли: " + ", ".join(overflow))
    return _check("track_span", STATUS_PASS, "Границы дорожек", "внутри встречи")


def check_track_frames(tracks: list[dict]) -> dict:
    deltas = []
    for row in tracks:
        frames = row.get("frames")
        rate = row.get("sample_rate")
        started = row.get("started_epoch")
        ended = row.get("ended_epoch")
        if frames is None or not rate or started is None or ended is None:
            continue
        wall = float(ended) - float(started)
        pcm = float(frames) / float(rate)
        deltas.append(abs(wall - pcm))
    if not deltas:
        return _check("track_frames", STATUS_NO_DATA, "Кадры дорожки", "нет started/ended/frames")
    worst = max(deltas)
    return _check(
        "track_frames",
        _level(worst, check_ok_sec(), check_warn_sec()),
        "Кадры дорожки",
        f"max |wall-pcm|={worst:.3f}s",
        max_delta=round(worst, 3),
    )


def check_gaps(events: list[dict]) -> dict:
    gaps = [row for row in events if row.get("kind") == "gap"]
    mutes = [row for row in events if row.get("kind") == "mute"]
    if not gaps:
        return _check("gaps", STATUS_NO_DATA if not events else STATUS_PASS, "Паузы записи", "нет gap")
    window = gap_mute_window_sec()
    orphan = 0
    for gap in gaps:
        when = float(gap.get("t") or 0.0)
        identity = str(gap.get("identity") or "")
        matched = False
        for mute in mutes:
            if str(mute.get("identity") or "") != identity:
                continue
            if abs(float(mute.get("t") or 0.0) - when) <= window:
                matched = True
                break
        if not matched:
            orphan += 1
    status = STATUS_WARN if orphan else STATUS_PASS
    return _check("gaps", status, "Паузы записи", f"gap={len(gaps)}, без mute={orphan}")


def check_leaks(leaks: list[dict]) -> dict:
    if not leaks:
        return _check("leaks", STATUS_PASS, "Протечки", "нет записей")
    missing = [row for row in leaks if not row.get("text") or row.get("start") is None]
    if missing:
        return _check("leaks", STATUS_FAIL, "Протечки", "есть снятие без trace")
    return _check("leaks", STATUS_WARN, "Протечки", f"обработано {len(leaks)}")


def check_giga_share(words: list[dict]) -> dict:
    by_speaker: dict[str, list[int]] = {}
    for row in words:
        speaker = str(row.get("speaker") or "")
        by_speaker.setdefault(speaker, []).append(1 if row.get("proof") else 0)
    if len(by_speaker) < 2:
        return _check("giga_share", STATUS_NO_DATA, "Доля GigaAM", "мало дорожек")
    shares = []
    for speaker, flags in by_speaker.items():
        shares.append((speaker, sum(flags) / len(flags) if flags else 0.0))
    mid = median(item[1] for item in shares)
    outliers = [name for name, share in shares if mid > 0 and share > mid * 2]
    detail = ", ".join(f"{name}={share:.2f}" for name, share in shares)
    status = STATUS_WARN if outliers else STATUS_PASS
    return _check("giga_share", status, "Доля GigaAM", detail)


def check_started_epoch(meeting: dict, tracks: list[dict]) -> dict:
    if meeting.get("meeting_started_epoch") is None and not tracks:
        return _check("no_started_epoch", STATUS_NO_DATA, "started_epoch", "старый контракт")
    missing = [str(row.get("identity") or "?") for row in tracks if row.get("started_epoch") is None]
    if missing and meeting.get("meeting_started_epoch") is None:
        return _check("no_started_epoch", STATUS_NO_DATA, "started_epoch", "старая запись")
    if missing:
        return _check("no_started_epoch", STATUS_FAIL, "started_epoch", "нет у: " + ", ".join(missing))
    return _check("no_started_epoch", STATUS_PASS, "started_epoch", "есть у всех дорожек")


def check_head_loss(meeting: dict, tracks: list[dict]) -> dict:
    started = meeting.get("meeting_started_epoch")
    epochs = [float(row["started_epoch"]) for row in tracks if row.get("started_epoch") is not None]
    if started is None or not epochs:
        return _check("head_loss", STATUS_NO_DATA, "Потеря начала", "нет epoch")
    delay = min(epochs) - float(started)
    status = STATUS_PASS if delay <= head_loss_warn_sec() else STATUS_WARN
    return _check("head_loss", status, "Потеря начала", f"{delay:.3f}s")


def event_rel_sec(event: dict, meeting: dict) -> float:
    if event.get("t_rel") is not None:
        return float(event["t_rel"])
    started = meeting.get("meeting_started_epoch")
    when = float(event.get("t") or 0.0)
    if started is None:
        return when
    return when - float(started)


def check_speaker_sync(
    meeting: dict,
    events: list[dict],
    words: list[dict],
    ok_sec: float,
    warn_sec: float,
) -> dict:
    speakers_log = [row for row in events if row.get("kind") == "speakers"]
    if not speakers_log or not words:
        return _check("speaker_sync", STATUS_NO_DATA, "Синхрон спикера", "нет событий или слов")
    deltas = []
    seen: set[tuple[str, float]] = set()
    for row in words:
        speaker = str(row.get("speaker") or "")
        start = float(row.get("start") or 0.0)
        key = (speaker, round(start, 3))
        if key in seen:
            continue
        seen.add(key)
        match = None
        for event in speakers_log:
            active = [str(name) for name in (event.get("active") or [])]
            if speaker not in active:
                continue
            event_t = event_rel_sec(event, meeting)
            if start + SPEAKER_SYNC_BEFORE_SEC < event_t:
                continue
            if event_t > start + SPEAKER_SYNC_AFTER_SEC:
                continue
            match = abs(start - event_t)
            break
        if match is not None:
            deltas.append(match)
    if not deltas:
        return _check("speaker_sync", STATUS_NO_DATA, "Синхрон спикера", "нет пар replica/speakers")
    values = sorted(deltas)
    p95_index = max(0, int(round(0.95 * (len(values) - 1))))
    stats = {
        "median": round(median(values), 3),
        "p95": round(values[p95_index], 3),
        "max": round(values[-1], 3),
    }
    status = _level(stats["median"], ok_sec, warn_sec)
    if stats["max"] > warn_sec:
        status = STATUS_FAIL
    elif stats["p95"] > ok_sec and status == STATUS_PASS:
        status = STATUS_WARN
    detail = f"median={stats['median']} p95={stats['p95']} max={stats['max']}"
    return _check("speaker_sync", status, "Синхрон спикера", detail, **stats)


def check_clock_integrity(meeting: dict, tracks: list[dict], ok_sec: float, warn_sec: float) -> dict:
    started = meeting.get("meeting_started_epoch")
    ended = meeting.get("meeting_ended_epoch")
    if started is None or ended is None:
        return _check("clock_integrity", STATUS_NO_DATA, "Целостность часов", "нет границ встречи")
    expected = float(ended) - float(started)
    actual = float(meeting.get("duration_sec") or 0.0)
    frame_spans = []
    for row in tracks:
        frames = row.get("frames")
        rate = row.get("sample_rate")
        offset = float(row.get("offset_sec") or 0.0)
        if frames and rate:
            frame_spans.append(offset + float(frames) / float(rate))
    frame_duration = max(frame_spans) if frame_spans else 0.0
    difference = abs(expected - actual)
    status = _level(difference, ok_sec, warn_sec)
    degraded = status != STATUS_PASS
    return _check(
        "clock_integrity",
        status,
        "Целостность часов",
        f"expected={expected:.3f} actual={actual:.3f} frames={frame_duration:.3f} diff={difference:.3f}",
        expected_duration=round(expected, 3),
        actual_duration=round(actual, 3),
        frame_duration=round(frame_duration, 3),
        difference=round(difference, 3),
        meeting=CLOCK_DEGRADED if degraded else STATUS_PASS,
    )
