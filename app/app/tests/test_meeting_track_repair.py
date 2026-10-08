"""Ремонт реплик дорожки встречи: Parakeet на тихом микрофоне молчал или галлюцинировал, вычитка подгоняла верный текст
GigaAM под неверные слова. Времена и форма ошибок — как на настоящей встрече; имена и тексты реплик вымышлены."""
from __future__ import annotations

from app.app.services.meeting_track_repair import (
    replica_from_turn,
    repair_track_replicas,
    suspect_reason,
)


def _word(i, text, start, end):
    return {"i": i, "word": text, "final": text, "start": start, "end": end}


# Пять реплик из разных мест пятиминутной записи «вылились» в три слова Parakeet длиной 1,4 с в конце
COLLAPSED = {
    "speaker": "speaker_0", "start": 316.56, "end": 317.92,
    "text": "Всё готово. Что дальше? Ага. Да, слышу вас. Пришли ссылку, пожалуйста. До встречи, коллеги.",
    "words": [_word(0, "Всё готово. Что дальше? Ага.", 316.56, 316.96), _word(1, "Да, слышу вас. Пришли ссылку,", 316.96, 317.44),
              _word(2, "пожалуйста. До встречи, коллеги.", 317.44, 317.92)],
}
COLLAPSED_GIGA = [
    {"start": 3.0, "end": 23.0, "text": "Всё готово."}, {"start": 25.0, "end": 32.0, "text": "Что дальше? Ага."},
    {"start": 178.0, "end": 184.0, "text": "Да, слышу вас."}, {"start": 286.0, "end": 287.0, "text": "Пришли ссылку, пожалуйста."},
    {"start": 316.0, "end": 317.0, "text": "До встречи, коллеги."},
]

# Английские слова из шума с вырожденными временами («do» 9 с, «a» 11 с, «people,» 20 с), в них же вписан русский текст
DEGENERATE = {
    "speaker": "speaker_0", "start": 232.48, "end": 275.56,
    "text": "So if you do a lot of people, you can see that we have to есть просто решить, что дальше делаем",
    "words": [_word(0, "So", 232.48, 234.56), _word(1, "if", 234.56, 234.64), _word(2, "you", 234.64, 234.96),
              _word(3, "do", 234.96, 244.0), _word(4, "a", 244.0, 255.12), _word(5, "lot", 255.12, 255.12),
              _word(6, "of", 255.12, 255.12), _word(7, "people,", 255.12, 275.28)],
}
DEGENERATE_OK = {"speaker": "speaker_0", "start": 309.92, "end": 314.92, "text": "и всё. О'кей. Давай.",
                 "words": [_word(0, "и", 309.92, 310.2), _word(1, "всё.", 310.2, 310.6), _word(2, "О'кей.", 313.0, 313.6),
                           _word(3, "Давай.", 314.24, 314.9)]}
DEGENERATE_GIGA = [{"start": 178.0, "end": 180.0, "text": "Алло."},
                   {"start": 233.0, "end": 235.0, "text": "То есть просто решить, что дальше делаем, и всё."},
                   {"start": 309.0, "end": 315.0, "text": "О'кей. Давай."}]


def test_collapsed_replica_is_detected_and_normal_ones_are_not():
    assert suspect_reason(COLLAPSED) == "collapsed"
    assert suspect_reason(DEGENERATE) == "degenerate"
    assert suspect_reason(DEGENERATE_OK) is None
    assert suspect_reason({"start": 1.0, "end": 1.3, "text": "Да.", "words": [_word(0, "Да.", 1.0, 1.3)]}) is None  # короткое — норма
    long_ok = {"start": 0.0, "end": 20.0, "text": "слово " * 40, "words": [_word(i, "слово", i * 0.5, i * 0.5 + 0.4) for i in range(40)]}
    assert suspect_reason(long_ok) is None  # медленная длинная речь с паузами не подозрительна


def test_long_normal_replica_with_pauses_is_not_dropped():
    """Длинная реплика ведущего: 309 с, 496 слов, два слова по 7 с перед паузами — это норма, а не вырождение."""
    words = [_word(i, "слово", i * 0.62, i * 0.62 + 0.4) for i in range(496)]
    words[300] = _word(300, "Хорошо?", 230.0, 237.4)  # пауза после слова
    words[400] = _word(400, "беседу.", 296.8, 303.9)
    long_turn = {"speaker": "speaker_0", "start": 5.28, "end": 314.48, "text": "слово " * 496, "words": words}
    assert suspect_reason(long_turn) is None
    out, audit = repair_track_replicas([long_turn], [{"start": 5.0, "end": 55.0, "text": "Нужно выдать доступы"}])
    assert out == [long_turn] and audit == []  # не заменяем точную разметку по словам грубой по репликам


def test_five_utterances_get_their_own_times_instead_of_a_1_4_second_lump():
    out, audit = repair_track_replicas([COLLAPSED], COLLAPSED_GIGA)
    assert [r["text"] for r in out] == [t["text"] for t in COLLAPSED_GIGA]
    assert [round(r["start"]) for r in out] == [3, 25, 178, 286, 316]
    assert audit[0]["action"] == "dropped" and audit[0]["reason"] == "collapsed"
    assert sum(1 for a in audit if a["action"] == "added_from_giga") == 5
    assert all(w["proof"] == "giga_anchor" for r in out for w in r["words"])


def test_english_hallucination_dropped_and_missing_russian_restored():
    out, audit = repair_track_replicas([DEGENERATE, DEGENERATE_OK], DEGENERATE_GIGA)
    texts = [r["text"] for r in out]
    assert not any("people" in t or "So if" in t for t in texts)  # английской галлюцинации нет
    assert texts == ["Алло.", "То есть просто решить, что дальше делаем, и всё.", "и всё. О'кей. Давай."]
    assert [a["reason"] for a in audit if a["action"] == "dropped"] == ["degenerate"]


def test_track_without_parakeet_words_is_not_lost():
    out, audit = repair_track_replicas([], [{"start": 271.0, "end": 273.0, "text": "Кто отвечает за выгрузку?"}])
    assert len(out) == 1 and out[0]["text"] == "Кто отвечает за выгрузку?" and (out[0]["start"], out[0]["end"]) == (271.0, 273.0)
    assert audit[0]["action"] == "added_from_giga"


def test_without_second_hearing_replicas_are_returned_untouched():
    out, audit = repair_track_replicas([COLLAPSED], [])
    assert out == [COLLAPSED] and audit == []  # проверить нечем — не удаляем


def test_covered_giga_turn_is_not_duplicated():
    good = {"speaker": "speaker_0", "start": 25.0, "end": 31.0, "text": "Что дальше? Ага.",
            "words": [_word(0, "Что", 25.0, 25.4), _word(1, "дальше?", 25.4, 26.0), _word(2, "Ага.", 30.0, 30.6)]}
    out, audit = repair_track_replicas([good], [{"start": 25.0, "end": 32.0, "text": "Что дальше? Ага."}])
    assert out == [good] and audit == []


def test_replica_from_turn_is_one_indivisible_block_with_natural_duration():
    rep = replica_from_turn({"start": 10.0, "end": 14.0, "text": "раз два три четыре"})
    assert [w["word"] for w in rep["words"]] == ["раз два три четыре"]  # один блок: не рассыпается при слиянии с чужими словами
    assert rep["words"][0]["start"] == 10.0 and rep["end"] == 13.0  # 0,5 с × 4 слова + 1 с, а не грубые 4 с
    coarse = replica_from_turn({"start": 3.0, "end": 23.0, "text": "Всё готово."})
    assert coarse["end"] == 5.0  # грубый отрезок GigaAM 3–23 с не растягивает реплику на 20 с
    zero = replica_from_turn({"start": 5.0, "end": 5.0, "text": "Ага."})
    assert zero["end"] > zero["start"]


def test_repaired_words_merge_into_correct_meeting_clock_positions():
    from app.app.services.meeting_merge import merge_track_replicas, shift_words, words_from_replicas

    out, _ = repair_track_replicas([COLLAPSED], COLLAPSED_GIGA)
    oleg = shift_words(words_from_replicas(out), 6.624, "Олег")
    anna = shift_words([_word(0, "Привет", 20.0, 20.5), _word(1, "всем", 20.5, 21.0)], 7.335, "Anna")
    merged = merge_track_replicas(oleg, anna)
    speakers_in_order = [(r["speaker"], round(r["start"])) for r in merged]
    assert speakers_in_order[0][0] == "Олег" and ("Anna", 27) in speakers_in_order
    assert [r["text"] for r in merged if r["speaker"] == "Олег"] == [t["text"] for t in COLLAPSED_GIGA]  # пять целых реплик, без дробления
    # чужие слова внутри интервала реплики не рассыпают её
    busy = shift_words([_word(i, f"м{i}", 8.0 + i * 0.4, 8.3 + i * 0.4) for i in range(60)], 0.0, "Anna")
    again = merge_track_replicas(shift_words(words_from_replicas(out), 0.0, "Олег"), busy)
    assert "Всё готово." in [r["text"] for r in again if r["speaker"] == "Олег"]


# ───────────── защита от повторения: сверка звука с текстом и проверки на странице встречи ─────────────

def _write_wav(path, segments, total_sec=30, rate=16000):
    """Тишина с тональными вставками segments=[(start, end)] (сек)."""
    import math
    import struct
    import wave

    frames = bytearray()
    for i in range(total_sec * rate):
        t = i / rate
        loud = any(a <= t < b for a, b in segments)
        frames += struct.pack("<h", int(12000 * math.sin(2 * math.pi * 300 * t)) if loud else 0)
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(rate)
        fh.writeframes(bytes(frames))


def test_active_windows_finds_sound_with_offset_and_ignores_silence(tmp_path):
    from app.app.services.meeting_track_repair import active_windows

    wav = tmp_path / "t.wav"
    _write_wav(wav, [(2.0, 4.0), (10.0, 11.0)])
    windows = active_windows(wav, offset_sec=7.0)
    assert [round(a) for a, _ in windows] == [9, 17] and round(sum(b - a for a, b in windows)) == 3
    assert active_windows(tmp_path / "нет.wav") == []


def test_track_coverage_flags_sound_without_text():
    from app.app.services.meeting_track_repair import track_coverage

    windows = [(9.0, 30.0)]  # у Олега на микрофоне звук 9–30 с
    lost = track_coverage("Олег", windows, [{"speaker": "Олег", "start": 322.0, "end": 323.5}, {"speaker": "Anna", "start": 9.0, "end": 30.0}])
    assert lost["active_sec"] == 21.0 and lost["uncovered_sec"] == 21.0  # реплика Анны не считается
    ok = track_coverage("Олег", windows, [{"speaker": "Олег", "start": 9.6, "end": 29.6}])
    assert ok["uncovered_sec"] == 0.0


def test_meeting_checks_catch_collapsed_and_uncovered():
    from app.app.services.meeting_checks import STATUS_FAIL, STATUS_NO_DATA, STATUS_PASS, STATUS_WARN, check_replica_density, check_track_coverage

    collapsed = {"speaker": "Олег", "start": 322.1, "end": 323.5, "text": COLLAPSED["text"]}
    assert check_replica_density([collapsed])["status"] == STATUS_FAIL
    normal = {"speaker": "Anna", "start": 0.0, "end": 20.0, "text": "обычная речь " * 10}
    assert check_replica_density([normal])["status"] == STATUS_PASS
    assert check_replica_density([])["status"] == STATUS_NO_DATA
    assert check_track_coverage([{"speaker": "Олег", "active_sec": 40.0, "uncovered_sec": 27.0}])["status"] == STATUS_WARN
    assert check_track_coverage([{"speaker": "Игорь", "active_sec": 2.0, "uncovered_sec": 2.0}])["status"] == STATUS_PASS  # шум < 3 с
    assert check_track_coverage([{"speaker": "Анна", "active_sec": 270.0, "uncovered_sec": 4.0}])["status"] == STATUS_PASS
    assert check_track_coverage(None)["status"] == STATUS_NO_DATA


def test_run_checks_includes_new_checks_and_stays_backward_compatible():
    from app.app.services.meeting_checks import run_checks

    old_style = run_checks({"tracks": []}, [], [], [])
    ids = {row["id"] for row in old_style}
    assert {"replica_density", "track_coverage"} <= ids
    new_style = run_checks({"tracks": []}, [], [], [], replicas=[{"speaker": "К", "start": 1.0, "end": 2.0, "text": "х" * 90}],
                           coverage=[{"speaker": "К", "active_sec": 30.0, "uncovered_sec": 20.0}])
    status = {row["id"]: row["status"] for row in new_style}
    assert status["replica_density"] == "FAIL" and status["track_coverage"] == "WARN"


def test_transcribe_track_keeps_speech_heard_only_by_the_second_ear(tmp_path, monkeypatch):
    """Дорожка без слов Parakeet не отбрасывается, если GigaAM услышал речь."""
    from types import SimpleNamespace

    from app.app.services import pipeline_meeting as pm

    written = {}
    monkeypatch.setattr(pm, "_lab_env", lambda settings, child: {})
    monkeypatch.setattr(pm, "_run_cmd", lambda *a, **k: None)
    monkeypatch.setattr(pm, "latest_run_id", lambda settings, child: "run1")
    monkeypatch.setattr(pm, "_track_word_count", lambda settings, child, run_id: 0)  # Parakeet ничего не нашёл
    monkeypatch.setattr("app.app.services.docker_runtime.ensure_gigaam_ready", lambda url: None)
    monkeypatch.setattr(pm, "_load_track_replicas", lambda settings, child, run_id: [])
    monkeypatch.setattr(pm, "_load_giga_turns", lambda settings, child, run_id: [{"start": 271.0, "end": 273.0, "text": "Кто отвечает за выгрузку?"}])
    monkeypatch.setattr(pm, "_write_track_repairs", lambda settings, child, run_id, repairs: written.setdefault("repairs", repairs))
    stops = []
    monkeypatch.setattr("app.app.services.docker_runtime.stop_gigaam", lambda: stops.append(1))
    settings = SimpleNamespace(gigaam_url="http://x", lmstudio_keep_during_asr=False)
    parent = SimpleNamespace(project_id=1, artifact_id="art_parent")
    row = {"child_id": "art_parent__guest-x", "identity": "guest:x", "speaker": "Игорь", "wav16": str(tmp_path / "a.wav"), "offset_sec": 7.614}
    words, _wav, had_words = pm._transcribe_track(settings, parent, row)
    assert stops == [1]  # GigaAM освобождает VRAM сразу после своего шага: дальше вычитка моделью
    assert had_words is True and " ".join(w["word"] for w in words) == "Кто отвечает за выгрузку?"
    assert round(words[0]["start"], 1) == 278.6 and all(w["speaker"] == "Игорь" for w in words)  # 271 + смещение 7,614
    assert written["repairs"][0]["action"] == "added_from_giga"
