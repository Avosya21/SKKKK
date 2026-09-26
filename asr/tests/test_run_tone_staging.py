from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


def test_run_tone_stages_docker_input_before_request(tmp_path, monkeypatch):
    audio = tmp_path / "audio"
    audio.mkdir()
    wav8 = audio / "input_8k.wav"
    wav8.write_bytes(b"artifact-tone-8k")

    docker_work = tmp_path / "docker_work"
    monkeypatch.setattr("docker_stage.DOCKER_WORK", docker_work)
    monkeypatch.setattr("run_pipeline.WORK", audio)
    monkeypatch.setattr("run_pipeline.OUT", tmp_path / "asr")
    (tmp_path / "asr").mkdir()
    monkeypatch.setattr("run_pipeline.TONE", "http://127.0.0.1:18103")

    calls: list[str] = []

    def _fake_stage(path: Path) -> Path:
        calls.append(str(path))
        return stage_tone_copy(path, docker_work)

    def stage_tone_copy(source: Path, work: Path) -> Path:
        work.mkdir(parents=True, exist_ok=True)
        dest = work / "input_8k.wav"
        dest.write_bytes(source.read_bytes())
        return dest

    captured: dict = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"text":"hello tone","segments":[]}'

    def _fake_urlopen(req, timeout=3600):
        captured["payload"] = req.data.decode("utf-8")
        return _Resp()

    monkeypatch.setattr("run_pipeline.stage_tone_docker_input", _fake_stage)
    monkeypatch.setattr("run_pipeline.urllib.request.urlopen", _fake_urlopen)

    from run_pipeline import run_tone

    result = run_tone()
    assert result["ok"] is True
    assert calls == [str(wav8)]
    assert "/data/input_8k.wav" in captured["payload"]
