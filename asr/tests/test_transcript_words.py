"""Word-level точечная подсказка экрана: слово -> сегмент на его время."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "asr"))
sys.path.insert(0, str(ROOT))

from transcript_view import render_transcript_html  # noqa: E402
from app.app.services.pipeline_lab import replicas_to_turn_words  # noqa: E402


def _render(*, overlap: bool = True):
    turns = [
        {"start": 60.0, "end": 90.0, "speaker": "SPEAKER_00", "text": "один два три четыре пять",
         "words": [
             {"word": "один", "final": "один", "start": 60.0, "end": 61.0},
             {"word": "два", "final": "два", "start": 61.0, "end": 62.0},
             {"word": "три", "final": "три", "start": 75.0, "end": 76.0},
             {"word": "четыре", "final": "четыре", "start": 76.0, "end": 77.0},
             {"word": "пять", "final": "пять", "start": 89.0, "end": 90.0},
         ]},
    ]
    replicas = [dict(turns[0], words=turns[0]["words"])]
    words = replicas_to_turn_words(replicas)
    segs = [{"start": 50.0, "end": 70.0, "text": "документ А", "identity": "m1"},
            {"start": 70.0, "end": 85.0, "text": "таблица Б", "identity": "m2"},
            {"start": 85.0, "end": None and 95.0 or 95.0, "text": "график В", "identity": "m3"}]
    from asr.speakers import Turn
    html_turns = [Turn(t["start"], t["end"], t["speaker"], t["text"]) for t in turns]
    return render_transcript_html(
        title="t",
        audio_url="/a",
        turns=html_turns,
        artifact_id="art_x",
        project_id=1,
        screen_segments=segs,
        turn_words=words,
    )


def test_words_rendered_as_spans_with_time():
    html = _render()
    assert '<span class="w" data-i="0"' in html
    assert 'data-start="60.000"' in html
    assert 'data-end="61.000"' in html
    assert "один" in html
    assert 'id="screen-modal-wrap"' in html


def test_screen_dot_button_per_block():
    html = _render()
    assert 'class="screen-dot"' in html
    assert "▣" in html
    assert html.count('class="screen-dot"') == 3


def test_screen_dot_button_before_group_words():
    html = _render()
    seg0 = html.index('data-seg="0"')
    first_word = html.index(">один<")
    assert seg0 < first_word


def test_standalone_button_for_empty_screen_window():
    turns = [
        {"start": 0.0, "end": 100.0, "speaker": "S", "text": "один два три",
         "words": [
             {"word": "один", "final": "один", "start": 5.0, "end": 6.0},
             {"word": "два", "final": "два", "start": 60.0, "end": 61.0},
             {"word": "три", "final": "три", "start": 62.0, "end": 63.0},
         ]},
    ]
    replicas = [dict(turns[0], words=turns[0]["words"])]
    words = replicas_to_turn_words(replicas)
    segs = [{"start": 4.0, "end": 6.0, "text": "а", "identity": "m1"},
            {"start": 10.0, "end": 12.0, "text": "б", "identity": "m2"},
            {"start": 58.0, "end": 63.0, "text": "в", "identity": "m3"}]
    from asr.speakers import Turn
    html = render_transcript_html(
        title="t",
        audio_url="/a",
        turns=[Turn(t["start"], t["end"], t["speaker"], t["text"]) for t in turns],
        artifact_id="art_x",
        project_id=1,
        screen_segments=segs,
        turn_words=words,
    )
    assert html.count('class="screen-dot"') == 3
    hole = html.index('data-seg="1"')
    two = html.index(">два<")
    assert hole < two
    assert 'title="Кадр экрана 00:10"' in html


def test_payload_has_words_and_screen():
    html = _render()
    assert '"words": [' in html
    assert '"screen": [' in html
    assert "renderScreenModal" in html
    assert "closeScreenModal" in html


def test_screen_text_in_collapsible_block():
    html = _render()
    assert 'class="sm-fold"' in html
    assert '<details class="sm-fold">' in html
    assert "Текст описания" in html
    assert 'class="sm-fold-arrow"' in html


def test_fallback_no_words_render_plain_text():
    turns = [{"start": 0.0, "end": 10.0, "speaker": "S", "text": "обычный текст"}]
    from asr.speakers import Turn
    html = render_transcript_html(
        title="t",
        audio_url="/a",
        turns=[Turn(t["start"], t["end"], t["speaker"], t["text"]) for t in turns],
        artifact_id="art_x",
        project_id=1,
        screen_segments=[],
    )
    assert "обычный текст" in html
    assert 'class="w"' not in html