from __future__ import annotations

from pathlib import Path

import pytest

from docker_stage import stage_docker_work_file, stage_tone_docker_input


def test_stage_docker_work_file_copies_artifact_audio(tmp_path, monkeypatch):
    artifact_audio = tmp_path / "artifact" / "audio"
    artifact_audio.mkdir(parents=True)
    src = artifact_audio / "input_8k.wav"
    src.write_bytes(b"artifact-8k-wav")

    docker_work = tmp_path / "docker_work"
    monkeypatch.setattr("docker_stage.DOCKER_WORK", docker_work)

    staged = stage_tone_docker_input(src)
    assert staged == docker_work / "input_8k.wav"
    assert staged.read_bytes() == b"artifact-8k-wav"


def test_assert_docker_work_matches_rejects_stale(tmp_path, monkeypatch):
    docker_work = tmp_path / "docker_work"
    docker_work.mkdir()
    stale = docker_work / "input_8k.wav"
    stale.write_bytes(b"stale")

    src_dir = tmp_path / "artifact"
    src_dir.mkdir()
    src = src_dir / "input_8k.wav"
    src.write_bytes(b"fresh")

    monkeypatch.setattr("docker_stage.DOCKER_WORK", docker_work)
    from docker_stage import assert_docker_work_matches

    with pytest.raises(RuntimeError, match="out of sync"):
        assert_docker_work_matches(src, "input_8k.wav")


def test_stage_docker_work_file_missing_raises(tmp_path, monkeypatch):
    monkeypatch.setattr("docker_stage.DOCKER_WORK", tmp_path / "docker_work")
    with pytest.raises(FileNotFoundError):
        stage_docker_work_file(tmp_path / "missing.wav", "input_8k.wav")
