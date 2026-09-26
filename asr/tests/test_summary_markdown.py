from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "asr"))

from transcript_view import _summary_to_html, parse_md_table, render_transcript_html  # noqa: E402


def test_parse_md_table_who_what_when():
    rows = [
        "| Кто | Что | Срок |",
        "|-----|-----|------|",
        "| Елена Соколова | Отправить файл | Завтра |",
        "| Игорь (Пилот) | Список форм | — |",
    ]
    parsed = parse_md_table(rows)
    assert parsed is not None
    header, body = parsed
    assert header == ["Кто", "Что", "Срок"]
    assert body[0] == ["Елена Соколова", "Отправить файл", "Завтра"]
    assert body[1][2] == "—"


def test_summary_to_html_renders_table_without_separator():
    md = """## Кто / Что

| Кто | Что | Срок |
| Елена Соколова | Выставить счёт | — |
"""
    html = _summary_to_html(md)
    assert "<table class=\"summary-table\">" in html
    assert "<td>Елена Соколова</td>" in html


def test_summary_to_html_renders_table_not_pipes():
    md = """## Кто / Что

| Кто | Что | Срок |
|-----|-----|------|
| Елена Соколова | Выставить счёт | — |
"""
    html = _summary_to_html(md)
    assert "<table class=\"summary-table\">" in html
    assert "<th scope=\"col\">Кто</th>" in html
    assert "<td>Елена Соколова</td>" in html
    assert "<p>|" not in html


def test_unknown_speaker_is_not_a_filter_chip():
    from speakers import Turn

    html = render_transcript_html(
        title="t",
        audio_url="/a",
        turns=[
            Turn(start=0.0, end=1.0, speaker="Елена Соколова", text="привет"),
            Turn(start=1.0, end=2.0, speaker="unknown", text="Хорошо."),
        ],
        artifact_id="art_t",
        project_id=1,
        registry=[
            {
                "primary": "speaker_0",
                "labels": ["speaker_0"],
                "display_name": "Елена Соколова",
                "input_value": "Елена Соколова",
                "status": "confirmed",
            }
        ],
    )
    assert 'data-filter="unknown"' not in html
    assert "Хорошо." in html
    assert "Елена Соколова" in html
    assert "toLowerCase() === \"unknown\"" in html


def test_render_transcript_embeds_html_table():
    html = render_transcript_html(
        title="test",
        audio_url="/a",
        turns=[],
        summary_md="| Кто | Что |\n|-----|-----|\n| А | Б |",
        artifact_id="art_t",
        project_id=1,
    )
    assert "<table class=\"summary-table\">" in html
    assert "summary-table-wrap" in html
    assert "<td>А</td>" in html
    assert 'role="tablist"' in html
    assert "Итоговое резюме созвона" in html
    assert "Краткое резюме созвона" in html
    assert "Пересобрать выжимку" in html


def test_render_transcript_brief_tab_and_placeholder():
    html = render_transcript_html(
        title="test",
        audio_url="/a",
        turns=[],
        summary_md="## Итог\n\n- договорённость",
        brief_md="",
        artifact_id="art_t",
        project_id=1,
    )
    assert "Итоговое резюме созвона" in html
    assert "Краткое резюме созвона" in html
    assert "договорённость" in html
    assert "Краткое резюме ещё не собрано" in html
    assert 'id="panel-summary-brief"' in html
    assert 'hidden' in html


def test_render_transcript_shows_brief_text():
    html = render_transcript_html(
        title="test",
        audio_url="/a",
        turns=[],
        summary_md="полное",
        brief_md="суть созвона одним абзацем",
    )
    assert "суть созвона одним абзацем" in html
    assert "полное" in html


def test_summary_to_html_inline_and_numbered():
    html = _summary_to_html(
        "## Тема\n\n- **жирный** и *курсив* и `код`\n1. Первый\n2. Второй"
    )
    assert "<strong>жирный</strong>" in html
    assert "<em>курсив</em>" in html
    assert "<code>код</code>" in html
    assert "<ol>" in html
    assert "<li>Первый</li>" in html
    assert "<li>Второй</li>" in html
