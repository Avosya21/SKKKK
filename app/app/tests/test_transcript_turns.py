import json
from types import SimpleNamespace

from app.app.services.transcript_turns import (
    KIND_SCREEN,
    KIND_SCREEN_SPEECH,
    format_timecode,
    parse_final_turns,
    turn_at,
    turns_in_range,
)

FINAL = (
    "[00:10 - 00:20] Елена Соколова: Предлагаю вынести длину в реквизит.\n"
    "продолжение той же реплики\n"
    "[00:21 - 00:24] Павел Пилот: Да, согласен.\n"
    "[00:25 - 00:30] unknown: угу\n"
    "[01:00:50 - 01:01:10] Елена Соколова: Поздняя реплика.\n"
    "[75:10 - 75:20] Экран (показывал Елена Соколова): таблица\n"
    "[75:10 - 75:20] Речь на экране: Елена Соколова: вот таблица\n"
)


def test_parse_formats_kinds_and_continuation():
    turns = parse_final_turns(FINAL)
    assert [t.index for t in turns] == list(range(6))
    first = turns[0]
    assert first.speaker == "Елена Соколова" and first.start_sec == 10.0 and first.end_sec == 20.0
    assert first.text.endswith("продолжение той же реплики")
    assert turns[2].speaker == "unknown" and turns[2].known_speaker is None
    assert turns[3].start_sec == 3650.0  # hh:mm:ss
    assert turns[4].kind == KIND_SCREEN and turns[4].start_sec == 75 * 60 + 10  # минуты > 59
    assert turns[5].kind == KIND_SCREEN_SPEECH and not turns[5].is_speech


def test_turn_at_and_range_use_speech_only():
    turns = parse_final_turns(FINAL)
    assert turn_at(turns, 22.0).speaker == "Павел Пилот"
    assert turn_at(turns, 75 * 60 + 15).speaker == "Елена Соколова"  # ближайшая речь, не экран
    assert [t.index for t in turns_in_range(turns, 15.0, 26.0)] == [0, 1, 2]


def test_old_format_without_timecodes():
    turns = parse_final_turns("Спикер №1: привет\nСпикер №2: пока")
    assert [t.speaker for t in turns] == ["Спикер №1", "Спикер №2"]
    assert all(t.start_sec is None for t in turns)


def test_match_turn_ignores_tiny_turns_and_picks_best():
    """Регрессия (проект 32): однобуквенная реплика «С» «содержалась» в любом тексте и забирала все решения."""
    from app.app.services.entities import _match_turn

    turns = parse_final_turns(
        "[15:38 - 15:54] Елена Соколова: В течение этой недели разработчик займется заказ-заявкой и поручением экспедитору.\n"
        "[15:54 - 15:55] Дмитрий Николаев: С\n"
        "[16:12 - 16:14] Дмитрий Николаев: Да-да, конечно, хорошо.\n"
        "[18:08 - 18:11] Елена Соколова: Хорошо, запись в резюме завтра тогда отправлю.\n"
    )
    hit = _match_turn("Разработчики займутся заказ-заявкой и поручением экспедитору", turns, [])
    assert (hit.speaker, hit.start_sec) == ("Елена Соколова", 938.0)
    assert _match_turn("Отправить запись созвона в резюме", turns, []).start_sec == 1088.0
    assert _match_turn("Согласовать бюджет проекта с директором", turns, []) is None
    assert _match_turn("С", turns, [1]) is None


def test_format_timecode():
    assert format_timecode(351) == "05:51"
    assert format_timecode(3650) == "01:00:50"
    assert format_timecode(None) == ""


def test_load_turns_reads_canon_and_follows_file_changes(tmp_path, monkeypatch):
    from app.app.services import transcript_turns

    final = tmp_path / "art_x_final.txt"
    final.write_text("[00:01 - 00:02] Анна: раз\n", encoding="utf-8")
    monkeypatch.setattr(transcript_turns, "final_path_for", lambda _s, _a: final)
    art = SimpleNamespace(file_type="audio", transcription_text=None)
    assert transcript_turns.load_turns(None, art)[0].speaker == "Анна"
    final.write_text("[00:01 - 00:02] Анна Петрова: раз\n", encoding="utf-8")
    assert transcript_turns.load_turns(None, art)[0].speaker == "Анна Петрова"
    assert transcript_turns.load_turns(None, SimpleNamespace(file_type="docx")) == []


def test_entity_text_ignores_provenance():
    from app.app.services.graph import entity_text

    a = json.dumps({"text": "Решение", "speaker": "Анна", "start_sec": 1.0, "speaker_source": "voice"})
    b = json.dumps({"text": "Решение", "speaker": "Борис", "start_sec": 99.0, "decided_by": [{"name": "Борис"}]})
    assert entity_text(a) == entity_text(b) == "Решение"
    task = json.dumps({"description": "Сделать", "assignee": "Анна", "deadline": "пятница", "speaker": "Анна"})
    assert entity_text(task) == "Сделать Анна пятница"


def test_format_entity_line_who_and_when():
    from app.app.services.retrieval import format_entity_line

    art = SimpleNamespace(
        file_type="audio",
        original_filename="сдача.webm",
        artifact_id="art_1",
        recorded_at=None,
        recorded_at_source=None,
        upload_date=None,
    )
    value = {
        "text": "длину в реквизит строки",
        "speaker": "Елена Соколова",
        "speaker_source": "voice",
        "start_sec": 351.0,
        "decided_by": [{"name": "Павел Пилот", "start_sec": 360.0}],
    }
    line = format_entity_line("decision", json.dumps(value, ensure_ascii=False), art)
    assert line.startswith("[decision] длину в реквизит строки — сказал: Елена Соколова (по голосу)")
    assert "утвердил: Павел Пилот" in line
    assert "Созвон «сдача.webm»" in line and "05:51" in line
    old = format_entity_line("decision", json.dumps({"text": "старое"}, ensure_ascii=False), art)
    assert old == "[decision] старое · Созвон «сдача.webm»"


def test_chunk_header_marks_upload_date_as_not_call_date():
    from datetime import datetime

    from app.app.services.retrieval import chunk_header

    art = SimpleNamespace(
        file_type="audio",
        original_filename="звонок.webm",
        artifact_id="art_2",
        recorded_at=datetime(2026, 9, 21, 13, 52),
        recorded_at_source="upload",
        upload_date=datetime(2026, 9, 21, 13, 52),
    )
    header = chunk_header(art, {"start_sec": 122.0, "end_sec": 258.0})
    assert header == "[Созвон «звонок.webm» · дата загрузки 21.09.2026 (дата созвона неизвестна) · 02:02–04:18 · artifact_id=art_2]"
    art.recorded_at_source = "meeting"
    assert "созвон 21.09.2026" in chunk_header(art, {"start_sec": None})


def test_review_edit_keeps_provenance():
    from app.app.routers.knowledge import _keep_provenance

    old = json.dumps({"text": "старое", "speaker": "Анна", "start_sec": 5.0, "decided_by": [{"name": "Борис"}]})
    merged = json.loads(_keep_provenance(old, json.dumps({"text": "новое"})))
    assert merged == {"text": "новое", "speaker": "Анна", "start_sec": 5.0, "decided_by": [{"name": "Борис"}]}
