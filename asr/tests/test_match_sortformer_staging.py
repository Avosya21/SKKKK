from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def test_docker_embed_stages_audio_before_exec(tmp_path, monkeypatch):
    audio = tmp_path / "input_16k.wav"
    audio.write_bytes(b"wav-bytes")
    staged = tmp_path / "staged" / "input_16k.wav"
    staged.parent.mkdir()
    staged.write_bytes(b"wav-bytes")

    monkeypatch.setattr(
        "match_sortformer_speakers.JOB_HOST",
        tmp_path / "embed_job.json",
    )
    monkeypatch.setattr(
        "match_sortformer_speakers.RESULT_HOST",
        tmp_path / "embed_result.json",
    )
    (tmp_path / "embed_result.json").write_text(
        '{"speakers": {"SPEAKER_00": {"embedding": [0.1]}}}',
        encoding="utf-8",
    )

    with patch("docker_stage.stage_gigaam_docker_input", return_value=staged) as stage_mock:
        with patch("match_sortformer_speakers.subprocess.run") as run_mock:
            run_mock.return_value = MagicMock(returncode=0)
            from match_sortformer_speakers import docker_embed

            out = docker_embed(audio, {"SPEAKER_00": {"start": 0.0, "end": 3.0, "duration": 3.0}})

    stage_mock.assert_called_once_with(audio)
    job = (tmp_path / "embed_job.json").read_text(encoding="utf-8")
    assert '"/work/input_16k.wav"' in job
    assert out["SPEAKER_00"]["embedding"] == [0.1]
