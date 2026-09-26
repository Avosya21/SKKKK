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


def _lab_workspace(tmp_path: Path, diar: dict, matches: dict) -> Path:
    ws = tmp_path / "art_y"
    diar_dir = ws / "lab" / "runs" / "run_1" / "03_diar"
    diar_dir.mkdir(parents=True)
    (diar_dir / "diar.json").write_text(json.dumps(diar), encoding="utf-8")
    match_json = ws / "reports" / "sortformer_speaker_match.json"
    match_json.parent.mkdir(parents=True)
    payload = {"artifact_id": "art_y", "project_id": 3, "source": "lab", "run_id": "run_1",
               "matches": matches, "merge_map": {k: k for k in matches}}
    match_json.write_text(json.dumps(payload), encoding="utf-8")
    return match_json


def _render(match_json: Path) -> str:
    out = match_json.parent / "page.html"
    build_speaker_match_view.build(match_json=match_json, out_html=out, audio_url="/a", profiles=[])
    return out.read_text(encoding="utf-8")


def test_nemotron_page_shows_model_speech_and_low_speech(tmp_path: Path):
    matches = {
        "speaker_0": {"status": "confirmed", "profile_id": 1, "profile_name": "Иван", "clip_start": 1, "clip_end": 13, "clip_duration": 12.0},
        "speaker_6": {"status": "new", "suggested_profile_name": "Яна", "suggested_score": 0.67,
                      "clip_start": 3440, "clip_end": 3445, "clip_duration": 4.9},
    }
    diar = {"model_key": "nemotron3", "max_speakers": 8,
            "speakers": [{"speaker": "speaker_0", "seconds": 600.0}, {"speaker": "speaker_6", "seconds": 2.0}]}
    html = _render(_lab_workspace(tmp_path, diar, matches))
    assert "Голоса созвона" in html and "Sortformer + запоминание" not in html
    assert "Nemotron 3 Diarization" in html and "Найдено голосов: <strong>2</strong>" in html
    assert "Заняты все" not in html
    assert "Говорил</span> 10 мин 00 с" in html and "&lt;1%" in html
    assert "мало речи" in html
    assert "Похож на <strong>Яна</strong> (67%) — в поле не подставлено" in html
    assert 'value="Яна"' not in html  # слабая подсказка не попадает в поле имени


def test_sortformer_at_ceiling_warns_about_merged_voices(tmp_path: Path):
    matches = {f"speaker_{i}": {"status": "new", "clip_duration": 12.0} for i in range(4)}
    diar = {"model": "diar_streaming_sortformer_4spk-v2.1.onnx", "max_speakers": 4,
            "speakers": [{"speaker": f"speaker_{i}", "seconds": 300.0} for i in range(4)]}
    html = _render(_lab_workspace(tmp_path, diar, matches))
    assert "Sortformer 4spk v2.1" in html
    assert "Заняты все 4 канала Sortformer" in html


def test_page_without_lab_run_still_renders(tmp_path: Path):
    match_json = tmp_path / "reports" / "m.json"
    match_json.parent.mkdir()
    match_json.write_text(json.dumps({"artifact_id": "a", "matches": {"speaker_0": {"status": "new"}}}), encoding="utf-8")
    html = _render(match_json)
    assert "Кто говорит" not in html and "speaker_0" in html
