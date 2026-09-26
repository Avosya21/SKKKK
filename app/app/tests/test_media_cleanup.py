from __future__ import annotations

from pathlib import Path

import pytest

from app.app.services.media_cleanup import (
    is_video_container,
    maybe_discard_video_source,
    reconcile_artifact_source_path,
    verify_extracted_wav,
)


def test_is_video_container():
    assert is_video_container(Path("call.mp4"))
    assert is_video_container(Path("call.MKV"))
    assert not is_video_container(Path("call.wav"))
    assert not is_video_container(Path("notes.pdf"))


def test_verify_extracted_wav_missing(tmp_path: Path):
    ok, reason = verify_extracted_wav(tmp_path / "missing.wav")
    assert not ok
    assert reason == "wav missing"


def test_verify_extracted_wav_too_small(tmp_path: Path):
    wav = tmp_path / "tiny.wav"
    wav.write_bytes(b"x" * 100)
    ok, reason = verify_extracted_wav(wav)
    assert not ok
    assert "too small" in reason


def test_verify_extracted_wav_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    wav = tmp_path / "ok.wav"
    wav.write_bytes(b"x" * 20_000)

    monkeypatch.setattr(
        "app.app.services.media_cleanup.ffprobe_duration",
        lambda _p: 42.0,
    )
    ok, reason = verify_extracted_wav(wav)
    assert ok
    assert "42.0s" in reason


def test_maybe_discard_video_source_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    video = tmp_path / "call.mp4"
    video.write_bytes(b"fake video")
    wav = tmp_path / "input_16k.wav"
    wav.write_bytes(b"x" * 20_000)

    monkeypatch.setattr(
        "app.app.services.media_cleanup.ffprobe_duration",
        lambda _p: 60.0,
    )
    assert maybe_discard_video_source(video, wav, artifact_id="art_test")
    assert not video.is_file()
    assert wav.is_file()


def test_maybe_discard_video_source_keeps_wav_if_bad_extract(tmp_path: Path):
    video = tmp_path / "call.mp4"
    video.write_bytes(b"fake video")
    wav = tmp_path / "input_16k.wav"
    wav.write_bytes(b"x" * 100)

    assert not maybe_discard_video_source(video, wav)
    assert video.is_file()


def test_maybe_discard_skips_pure_audio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    audio = tmp_path / "call.wav"
    audio.write_bytes(b"fake wav")
    wav = tmp_path / "input_16k.wav"
    wav.write_bytes(b"x" * 20_000)
    monkeypatch.setattr(
        "app.app.services.media_cleanup.ffprobe_duration",
        lambda _p: 10.0,
    )
    assert not maybe_discard_video_source(audio, wav)
    assert audio.is_file()


def test_reconcile_artifact_source_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    video = tmp_path / "call.mp4"
    wav = tmp_path / "artifacts" / "input_16k.wav"
    wav.parent.mkdir(parents=True)
    wav.write_bytes(b"x" * 20_000)
    monkeypatch.setattr(
        "app.app.services.media_cleanup.ffprobe_duration",
        lambda _p: 30.0,
    )
    assert reconcile_artifact_source_path(str(video), wav) == str(wav)
    video.write_bytes(b"still here")
    assert reconcile_artifact_source_path(str(video), wav) is None
