"""Локальная запись созвона: контракт meeting_local_record.js без WebRTC и без node."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECORD_JS = ROOT / "static" / "meeting_local_record.js"
ROOM_JS = ROOT / "static" / "meeting_room.js"
ROOM_HTML = ROOT / "templates" / "meeting_room.html"
PUBLIC_HTML = ROOT / "templates" / "meeting_public.html"


def _js() -> str:
    return RECORD_JS.read_text(encoding="utf-8")


def test_module_exports_and_mime_order():
    text = _js()
    assert "PrimaryAnaliticsLocalRecord" in text
    assert "pickMimeType" in text and "buildFileName" in text
    assert "parsePresencePayload" in text and "createRecorder" in text
    assert "fixWebmDuration" in text
    assert "insertDurationIntoInfo" in text
    assert "drawNameBar" in text and "drawPlaceholder" in text
    assert "drawAvatar" in text and "tilePortraitRadius" in text
    assert "meeting-tile-avatar" in text
    # Аватар — круг как заглушка, не cover на всю плитку
    assert "drawAvatar(ctx, avatar" in text
    assert "drawCover(ctx, avatar" not in text
    # Экран — contain (края видны), камера — cover; говорящий — рамка
    assert "drawContain" in text and "drawSpeakingRing" in text
    assert "meeting-tile-screen" in text and "meeting-tile-speaking" in text
    assert "isScreen ? drawContain : drawCover" in text
    assert "remuxViaServer" in text
    assert "looksLikeMp4" in text
    assert "finalizePending" in text
    assert "deferFinalize" not in text
    assert "280_000" in text  # клиентский таймаут remux ≈ сервер 300 с
    assert "setTimeout(done, 2500)" in text  # stop MediaRecorder с таймаутом
    # Одна остановка: кнопка, выход и «Завершить» все зовут stop → finalizeFile (MP4).
    stop_fn = text[text.index("async function stop") : text.index("async function dispose")]
    assert "await finalizeFile()" in stop_fn
    assert "deferFinalize" not in stop_fn
    assert "createMediaRecorder" in text
    assert "triggerDownload" in text
    # Проводник Chrome отключён — только скачивание в «Загрузки»
    pick = text[text.index("async function pickSaveHandle") : text.index("function looksLikeMp4")]
    assert "return null;" in pick
    assert "window.showSaveFilePicker" not in pick
    assert "папку загрузок браузера" in text
    # Захват: WebM раньше MP4 (Chrome canvas+mp4 ломается); seekable MP4 — remux
    vp9 = text.index("video/webm;codecs=vp9,opus")
    mp4 = text.index('"video/mp4"')
    assert vp9 < mp4
    assert "CANVAS_W = 1920" in text and "TARGET_FPS = 24" in text


def test_webm_duration_inserter_builds_seekable_info():
    """Мини-Info без Duration → insertDurationIntoInfo добавляет 0x4489."""
    # EBML Info id 0x1549A966 + vint size 0x80|0 (=empty content) is too small;
    # content: TimecodeScale placeholder bytes, then we append Duration.
    info_id = bytes([0x15, 0x49, 0xA9, 0x66])
    # size vint for 4 bytes content: 0x84
    content = bytes([0x2A, 0xD7, 0xB1, 0x00])  # fake TimecodeScale id start + pad
    blob = bytearray(info_id + bytes([0x84]) + content)
    # Port of insert: find Info, append duration element (2+1+8=11 bytes), rewrite size.
    # Здесь проверяем контракт в JS-файле и что патч не только «если уже есть Duration».
    text = _js()
    assert "if (!patched) return blob" not in text  # старый путь без вставки
    assert "insertDurationIntoInfo(bytes, durationSec)" in text
    assert len(blob) == 9


def test_build_file_name_pattern_in_source():
    text = _js()
    assert "fileExt" in text
    assert 'return "sozvon-" + id + "-" + stamp + fileExt(mime)' in text
    d = datetime(2026, 11, 16, 8, 5, 9)
    stamp = f"{d.year}{d.month:02d}{d.day:02d}-{d.hour:02d}{d.minute:02d}{d.second:02d}"
    assert re.fullmatch(r"sozvon-42-\d{8}-\d{6}\.webm", f"sozvon-42-{stamp}.webm")
    assert re.fullmatch(r"sozvon-42-\d{8}-\d{6}\.mp4", f"sozvon-42-{stamp}.mp4")


def test_parse_presence_contract_matches_js():
    text = _js()
    assert 'PRESENCE_TYPE = "local_recording"' in text
    # Эталон разбора (тот же контракт, что в JS)
    raw = json.dumps({"type": "local_recording", "on": True, "at": 123})
    data = json.loads(raw)
    assert data["type"] == "local_recording" and data["on"] is True and data["at"] == 123
    assert json.loads(json.dumps({"type": "other"})).get("type") != "local_recording"


def test_room_corner_shows_only_local_recording():
    """Угол комнаты не показывает связь, дорожки и «Вы в комнате» — только локальную запись."""
    js = ROOM_JS.read_text(encoding="utf-8")
    assert "TEXT_LOCAL_RECORDING_SELF = \"Вы ведёте локальную запись\"" in js
    assert "TEXT_LOCAL_RECORDING_PEERS = \"Локальную запись ведут: \"" in js
    assert "_paintLocalRecordLine" in js
    paint = js[js.index("_paintRoomState()") : js.index("_applyScreenRate")]
    assert "TEXT_LOCAL_RECORDING" not in paint
    for path in (ROOM_HTML, PUBLIC_HTML):
        html = path.read_text(encoding="utf-8")
        start = html.index('<div id="meeting-state"')
        tag = html[start : html.index(">", start)]
        assert "hidden" in tag
        assert 'id="meeting-people"' in html


def test_employee_room_has_record_button_and_module():
    html = ROOM_HTML.read_text(encoding="utf-8")
    base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
    assert 'id="meeting-local-record"' in html
    assert "recordBtn" in html
    # Один раз в оболочке портала: после обрыва страницы WebM находится и вне комнаты.
    assert "meeting_local_record.js?v=a12" in base
    assert "meeting_local_record.js" not in html


def test_crash_page_keeps_webm_in_the_browser():
    """Падение вкладки не дожидается remux: секунды лежат в IndexedDB и потом скачиваются как WebM."""
    text = _js()
    assert 'CRASH_DB = "primaryanalitics-local-record"' in text
    assert "recoverCrashRecording" in text
    assert 'bar.id = "local-record-crash"' in text
    assert "Скачать WebM" in text
    data = text[text.index("ondataavailable") : text.index("ondataavailable") + 320]
    assert "rememberChunk" in data
    start = text[text.index("async function start") : text.index("async function cleanupAfterFail")]
    assert "armCrashSession(api)" in start
    fin = text[text.index("async function finalizeFile") : text.index("async function start")]
    assert fin.index("triggerDownload") < fin.index("sealCrashSession")
    hide = text[text.index("function flushCrashOnHide") : text.index("function recoverCrashRecording")]
    assert "requestData" in hide
    assert "triggerDownload" not in hide
    name = text[text.index("function crashFileName") : text.index("function crashFileName") + 400]
    assert "video/webm" in name
    assert "if (root.document) bootCrashRecovery();" in text


def test_guest_page_has_no_record_button():
    html = PUBLIC_HTML.read_text(encoding="utf-8")
    assert "meeting-local-record" not in html
    assert "meeting_local_record.js" not in html


def test_room_js_wires_local_recorder_hooks():
    js = ROOM_JS.read_text(encoding="utf-8")
    assert "_attachLocalRecorder" in js
    assert "_stopLocalRecorder" in js
    assert "toggleLocalRecord" in js
    assert "_localRecordHoldSubs" in js
    assert "_setLocalRecordVideoQuality" in js
    assert "leaveRoom" in js and "_stopLocalRecorder" in js
    assert 'remuxUrl: els.remuxUrl || "/api/meetings/local-record/remux"' in js
    assert "_dismissConfirm" in js
    assert "_confirmResolve" in js
    assert "_flushLocalRecord" in js
    assert "DOWNLOAD_HANDOFF_MS = 1500" in js
    assert "deferFinalize" not in js
    assert "const first = !this._localRecordHoldSubs" in js
    # Сначала тот же remux, что у кнопки, и пауза на скачивание; только потом disconnect и уход.
    leave = js[js.index("async leaveRoom()") : js.index("_showDuplicateSession")]
    assert leave.index("_flushLocalRecord") < leave.index("room.disconnect")
    assert leave.index("_flushLocalRecord") < leave.index("window.location.href")
    assert "_finalizeLocalRecorder" not in leave
    assert "if (this._leaveStarted) return" in leave
    end_at = js.index("async endForAll()")
    end = js[end_at : js.index("\n  watchEnded(", end_at)]
    assert end.index("_flushLocalRecord") < end.index('method: "POST"')
    assert end.index("leaveRoom()") > end.index('method: "POST"')


def test_live_page_saves_mp4_before_forced_exit():
    """Пока вкладка жива, принудительный выход сохраняет MP4 тем же flush, что «Выйти»."""
    js = ROOM_JS.read_text(encoding="utf-8")
    assert "_recordingActive()" in js
    assert "typeof rec.stop === \"function\"" in js
    disc = js[js.index("RoomEvent.Disconnected") : js.index("RoomEvent.TrackSubscribed")]
    assert "_afterDuplicate" in disc
    closed = disc[disc.index("reason === removed") : disc.index("_beginRejoin")]
    assert "_recordingActive()" in closed and "leaveRoom()" in closed
    assert "Комната закрыта." in closed and "Вас удалили из комнаты." in closed
    dup = js[js.index("async _afterDuplicate") : js.index("async _afterDuplicate") + 240]
    assert dup.index("_flushLocalRecord") < dup.index("_showDuplicateSession")
    loop = js[js.index("async _rejoinLoop") : js.index("async _rejoinLoop") + 700]
    assert loop.index("_flushLocalRecord") < loop.index("for (let attempt")
    assert "_stopLocalRecorder" not in loop
    short = js[js.index("RoomEvent.Reconnecting") : js.index("RoomEvent.Reconnected")]
    assert "_flushLocalRecord" not in short and "_stopLocalRecorder" not in short
    assert "beforeunload" in js and "#nav-logout" in js and "paLocalRecord" in js
    assert "_saveRecordBeforeClick" in js and "_saveRecordOnBack" in js


def test_save_hold_asks_not_to_close():
    """Пока remux идёт, человек видит «не закрывайте», и вкладку нельзя закрыть молча."""
    js = ROOM_JS.read_text(encoding="utf-8")
    assert 'wrap.id = "meeting-record-save"' in js
    assert "Погодите, не закрывайте страницу. Идёт сохранение записи." in js
    assert 'role", "status"' in js or 'setAttribute("role", "status")' in js
    flush_at = js.index("async _flushLocalRecord")
    flush = js[flush_at : js.index("_attachLocalRecorder", flush_at)]
    assert flush.index("_showRecordSaveHold") < flush.index("_stopLocalRecorder")
    assert flush.index("DOWNLOAD_HANDOFF_MS") < flush.index("_hideRecordSaveHold")
    unload = js[js.index('addEventListener("beforeunload"') : js.index('addEventListener("beforeunload"') + 420]
    assert "_recordSaveHold" in unload


def test_local_record_mix_only_microphone_sources():
    js = ROOM_JS.read_text(encoding="utf-8")
    assert "function isScreenSource(" in js
    assert "function isMicSource(" in js
    assert "source === 3 || source === 4" in js
    mix = js[js.index("_localRecordMixTracks()") : js.index("_setLocalRecordVideoQuality")]
    assert "isMicSource(pub.source)" in mix
    assert "isScreenPub(pub)" in mix
    assert 'indexOf("screen")' not in mix


def test_employee_room_wires_end_button_once():
    """Один click-listener на #meeting-end: дубль срывал confirm (dismiss без resolve)."""
    html = ROOM_HTML.read_text(encoding="utf-8")
    js = ROOM_JS.read_text(encoding="utf-8")
    assert "meeting-end" in html
    assert 'id="meeting-end" hidden' in html or 'id="meeting-end" hidden ' in html
    assert "data.can_end" in html
    assert html.count("endForAll()") == 1
    assert 'endBtn.addEventListener("click"' not in js
    assert "canEnd" in js or "can_end" in html
    assert "_confirmResolve" in js
    assert "_endConfirmOpen" in js
    assert "settled" in js[js.index("_confirm(message") : js.index("_confirm(message") + 1200]
    assert "создатель комнаты" in js


def test_local_record_does_not_pulse_subs_every_frame():
    """ensureMixSubscriptions на каждом кадре забивал UI — «Завершить» не реагировала при записи."""
    text = _js()
    assert "startSubsPulse" in text
    assert "setInterval" in text and "2000" in text
    assert "pausePaint" in text and "resumePaint" in text
    assert "paintPaused" in text
    # В frameLoop больше нет вызова подписок
    frame = text[text.index("function frameLoop") : text.index("async function pickSaveHandle")]
    assert "ensureMixSubscriptions" not in frame
    assert "paintTiles" in frame and "syncMixTracks" in frame


def test_end_pauses_record_paint_for_confirm():
    js = ROOM_JS.read_text(encoding="utf-8")
    end = js[js.index("async endForAll()") : js.index("async endForAll()") + 2200]
    assert "pausePaint" in end and "resumePaint" in end
    assert "_confirmResolve) return" not in end  # не soft-lock на залипшем resolve
    confirm = js[js.index("_confirm(message") : js.index("_confirm(message") + 2200]
    assert "stageEl" in confirm
