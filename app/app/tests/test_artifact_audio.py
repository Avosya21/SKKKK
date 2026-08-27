from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from app.app.config import Settings
from app.app.services.artifact_audio import _fix_audio_src, _rebuild_transcript_report, ensure_audio_wav
from app.app.services.storage import artifact_audio_path


def test_fix_audio_src_replaces_player_source():
    html = '<audio controls src="../work/input_16k.wav"></audio>'
    fixed = _fix_audio_src(html, "/artifacts/art_123/audio")
    assert 'src="/artifacts/art_123/audio"' in fixed
    assert "preload=" in fixed
    assert "playsinline" in fixed


def test_fix_audio_src_leaves_other_attrs():
    html = '<audio id="player" controls preload="metadata" src="old.wav"></audio>'
    fixed = _fix_audio_src(html, "/artifacts/x/audio")
    assert 'id="player"' in fixed
    assert 'src="/artifacts/x/audio"' in fixed
    assert fixed.count("preload=") == 1
    assert "playsinline" in fixed


def test_extract_wav_passes_explicit_wav_format(tmp_path, monkeypatch):
    from app.app.services import artifact_audio as aa

    seen: dict[str, list[str]] = {}

    def fake_run(cmd, **_kwargs):
        seen["cmd"] = list(cmd)
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(aa.subprocess, "run", fake_run)
    monkeypatch.setattr(aa, "resolve_ffmpeg_tool", lambda _name: "ffmpeg")
    dst = tmp_path / "input_16k.wav.partial"
    aa._extract_wav(tmp_path / "in.webm", dst)
    assert "-f" in seen["cmd"]
    assert seen["cmd"][seen["cmd"].index("-f") + 1] == "wav"
    assert str(dst) == seen["cmd"][-1]


def test_ensure_audio_wav_writes_atomically(tmp_data, monkeypatch):
    settings = Settings(secret_key="x" * 16, data_dir=tmp_data)
    src = tmp_data / "call.webm"
    src.write_bytes(b"fake-webm")
    artifact = MagicMock(artifact_id="art_wav_atom", project_id=1, file_path=str(src))
    seen: list[str] = []

    def fake_extract(_src, dst, sample_rate=16000):
        seen.append(dst.name)
        dst.write_bytes(b"x" * 20_000)

    monkeypatch.setattr("app.app.services.artifact_audio._extract_wav", fake_extract)
    monkeypatch.setattr(
        "app.app.services.artifact_audio.wav_is_complete",
        lambda wav, source=None: wav.is_file() and wav.stat().st_size >= 1000,
    )

    wav = ensure_audio_wav(settings, artifact)
    assert wav is not None
    assert wav.name == "input_16k.wav"
    assert wav.is_file()
    assert seen == ["input_16k.wav.partial"]
    assert not wav.with_name("input_16k.wav.partial").is_file()


def test_ensure_audio_wav_reextracts_truncated(tmp_data, monkeypatch):
    settings = Settings(secret_key="x" * 16, data_dir=tmp_data)
    src = tmp_data / "call.webm"
    src.write_bytes(b"fake-webm")
    artifact = MagicMock(artifact_id="art_wav_trunc", project_id=1, file_path=str(src))
    wav_path = artifact_audio_path(settings, "art_wav_trunc", 1)
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    wav_path.write_bytes(b"trunc")
    calls = {"n": 0}

    def fake_extract(_src, dst, sample_rate=16000):
        calls["n"] += 1
        dst.write_bytes(b"x" * 20_000)

    monkeypatch.setattr("app.app.services.artifact_audio._extract_wav", fake_extract)
    monkeypatch.setattr(
        "app.app.services.artifact_audio.wav_is_complete",
        lambda wav, source=None: wav.is_file() and wav.stat().st_size >= 1000,
    )

    wav = ensure_audio_wav(settings, artifact)
    assert wav is not None
    assert calls["n"] == 1
    assert wav.stat().st_size >= 1000


def test_rebuild_lab_transcript_skips_legacy_view(tmp_data, monkeypatch):
    from app.app.tests.test_pipeline_lab import _make_run
    from app.app.services.storage import artifact_dir

    settings, run = _make_run(tmp_data, 1, "art_lab_rebuild")
    run.resolve("proof").mkdir(parents=True, exist_ok=True)
    run.resolve("proof/transcript.json").write_text(
        json.dumps(
            {
                "replicas": [
                    {"speaker": "speaker_0", "start": 0.0, "end": 1.0, "text": "привет"},
                ]
            }
        ),
        encoding="utf-8",
    )
    ws = artifact_dir(settings, 1, "art_lab_rebuild")
    (ws / "reports").mkdir(parents=True, exist_ok=True)
    (ws / "reports" / "sortformer_speaker_match.json").write_text(
        json.dumps(
            {
                "source": "lab",
                "merge_map": {"speaker_0": "speaker_0"},
                "matches": {
                    "speaker_0": {
                        "diar_label": "speaker_0",
                        "status": "confirmed",
                        "profile_name": "Елена Соколова",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (ws / "asr").mkdir(parents=True, exist_ok=True)
    (ws / "asr" / "docker_gigaam_speakers.json").write_text(
        json.dumps(
            {
                "turns": [{"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00", "text": "привет"}],
            }
        ),
        encoding="utf-8",
    )
    artifact = MagicMock(
        artifact_id="art_lab_rebuild",
        project_id=1,
        source_type="call",
        original_filename="call.webm",
    )
    final = ws / "parsed_final.txt"
    final.write_text("x\n", encoding="utf-8")

    def _forbid_legacy(*_a, **_k):
        raise AssertionError("lab rebuild must not call build_transcript_view")

    monkeypatch.setattr("app.app.services.artifact_audio.subprocess.run", _forbid_legacy)
    with patch(
        "app.app.services.pipeline_lab.finalize_audio_outputs",
        return_value=(final, "/r"),
    ):
        report = _rebuild_transcript_report(settings, artifact)

    speakers = json.loads((ws / "asr" / "docker_gigaam_speakers.json").read_text(encoding="utf-8"))
    assert speakers["turns"][0]["speaker"] == "Елена Соколова"
    assert report is None or report.name == "transcript.html"