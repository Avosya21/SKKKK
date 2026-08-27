from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from seed_artifact_asr import ensure_artifact_asr_inputs


def test_ensure_artifact_asr_inputs_does_not_copy_global_asr_out(tmp_path):
    """Пересборка transcript view не должна тянуть stale из asr/out."""
    root = tmp_path
    project_id = 99
    artifact_id = "art_test_isolation"

    ws = root / "data" / "projects" / str(project_id) / "artifacts" / artifact_id
    (ws / "audio").mkdir(parents=True)
    (ws / "audio" / "input_16k.wav").write_bytes(b"wav")

    parsed = root / "data" / "projects" / str(project_id) / "parsed" / "calls"
    parsed.mkdir(parents=True)
    (parsed / f"{artifact_id}_final.txt").write_text(
        "[00:00 - 00:05] Спикер №1: demo text\n",
        encoding="utf-8",
    )

    global_out = root / "asr" / "out"
    global_out.mkdir(parents=True)
    (global_out / "docker_merged_windows.json").write_text('{"stale": true}', encoding="utf-8")
    (global_out / "docker_whisper.json").write_text('{"from": "wrong-job"}', encoding="utf-8")

    with patch("seed_artifact_asr._ffprobe_duration", return_value=5.0):
        asr_out = ensure_artifact_asr_inputs(
            root=root,
            project_id=project_id,
            artifact_id=artifact_id,
            ws=ws,
            match_json=None,
        )

    assert (asr_out / "docker_merged_windows.json").is_file()
    assert "stale" not in (asr_out / "docker_merged_windows.json").read_text(encoding="utf-8")
    assert not (asr_out / "docker_whisper.json").is_file()
