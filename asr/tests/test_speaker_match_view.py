"""HTML страницы speaker-match: карточки голосов, без таймлайна."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ASR = ROOT / "asr"
if str(ASR) not in sys.path:
    sys.path.insert(0, str(ASR))

import build_speaker_match_view  # noqa: E402


def test_match_html_hides_timeline_and_scrolls_pool(tmp_path: Path):
    payload = {
        "artifact_id": "art_x",
        "project_id": 17,
        "source": "lab",
        "matches": {
            "speaker_0": {
                "diar_label": "speaker_0",
                "profile_id": 26,
                "profile_name": "Елена Соколова",
                "status": "confirmed",
                "clip_start": 1.0,
                "clip_end": 5.0,
                "clip_duration": 4.0,
            }
        },
        "merge_map": {"speaker_0": "speaker_0"},
    }
    match_json = tmp_path / "match.json"
    match_json.write_text(json.dumps(payload), encoding="utf-8")
    out = tmp_path / "speaker_match_test.html"
    build_speaker_match_view.build(
        match_json=match_json,
        out_html=out,
        audio_url="/artifacts/art_x/audio",
        profiles=[
            {"id": 26, "display_name": "Елена Соколова", "scope": "global"},
            {"id": 55, "display_name": "Игорь (Пилот)", "scope": "global"},
        ],
        title="Голоса",
    )
    html = out.read_text(encoding="utf-8")
    assert "id=\"tab-timeline\"" not in html and "tab-timeline" not in html
    assert "Сегменты (" not in html
    assert "class=\"profile-pool\"" in html or "class='profile-pool'" in html or "profile-pool" in html
    assert "База голосов" in html
    assert "База проекта" not in html
    assert "Елена Соколова" in html
    assert "speaker_0" in html
    assert "/projects/17" in html
    assert "Запомнить" in html or "Обновить" in html
