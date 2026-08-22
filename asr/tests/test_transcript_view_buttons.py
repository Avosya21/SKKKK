"""Кнопки действий на странице стенограммы."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "asr"))

from transcript_view import render_transcript_html  # noqa: E402


def test_transcript_registry_toolbar_has_three_buttons():
    html = render_transcript_html(
        title="test",
        audio_url="/artifacts/art_test/audio",
        turns=[],
        artifact_id="art_test",
        project_id=1,
        registry=[
            {
                "primary": "SPEAKER_00",
                "labels": ["SPEAKER_00"],
                "display_name": "SPEAKER_00",
                "input_value": "",
                "status": "new",
            }
        ],
    )
    assert 'id="recalc"' in html
    assert 'id="rebuild-summary"' in html
    assert 'id="reindex"' in html
    assert 'id="download-transcript"' in html
    assert 'id="download-summary"' in html
    assert "export-panel" in html
    assert html.index("export-panel") < html.index("<h1>")
    assert "/download/transcript.txt" in html
    assert "/download/summary.docx" in html
    assert "/rebuild-summary" in html
    assert "/reindex" in html
    assert "registry-collapse" in html
    assert "registry-summary" in html
    assert "(развернуть)" in html
    assert 'call_summary_rebuild: ["#rebuild-summary"]' in html
