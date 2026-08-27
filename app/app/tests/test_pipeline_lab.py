from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.app.config import Settings
from app.app.services.pipeline_lab import publish_lab_to_prod, replicas_to_turns
from app.app.services.speaker_profiles import speakers_confirm_status


def _make_run(tmp_data: Path, project_id: int, artifact_id: str):
    from app.app.services.storage import artifact_dir
    from asr.lab.paths import LabRun, new_run_id
    from asr.lab.run import init_run

    settings = Settings(secret_key="x" * 16, data_dir=tmp_data)
    root = artifact_dir(settings, project_id, artifact_id)
    (root / "audio").mkdir(parents=True, exist_ok=True)
    source = root / "audio" / "input_16k.wav"
    source.write_bytes(b"RIFFfake")
    run = LabRun(root, new_run_id())
    init_run(run, artifact_id=artifact_id, project_id=project_id, source_audio=source)
    (run.stage_dir("01_audio") / "clean_16k.wav").write_bytes(b"RIFFclean")
    return settings, run


def test_replicas_to_turns_keep_speaker_n():
    turns = replicas_to_turns(
        [
            {"speaker": "speaker_0", "start": 1.0, "end": 2.0, "text": "привет"},
            {"speaker": "speaker_1", "start": 2.5, "end": 4.0, "text": "здравствуйте"},
        ]
    )
    assert [t.speaker for t in turns] == ["speaker_0", "speaker_1"]
    assert turns[0].text == "привет"


def test_publish_lab_writes_prod_files_and_speaker_n_labels(tmp_data: Path):
    settings, run = _make_run(tmp_data, 1, "art_lab_pub")
    proof = {
        "replicas": [
            {"speaker": "speaker_0", "start": 0.0, "end": 1.5, "text": "добрый день"},
            {"speaker": "speaker_1", "start": 1.6, "end": 3.0, "text": "добрый"},
        ]
    }
    run.resolve("proof").mkdir(parents=True, exist_ok=True)
    run.resolve("proof/transcript.json").write_text(json.dumps(proof), encoding="utf-8")
    match = {
        "source": "lab",
        "merge_map": {"speaker_0": "speaker_0", "speaker_1": "speaker_1"},
        "matches": {
            "speaker_0": {"diar_label": "speaker_0", "status": "new"},
            "speaker_1": {"diar_label": "speaker_1", "status": "new"},
        },
        "display_names": {"speaker_0": "speaker_0", "speaker_1": "speaker_1"},
        "embeddings": {"speaker_0": [1.0], "speaker_1": [0.0]},
    }
    run.resolve("voice").mkdir(parents=True, exist_ok=True)
    run.resolve("voice/match.json").write_text(json.dumps(match), encoding="utf-8")

    artifact = MagicMock(
        artifact_id="art_lab_pub",
        project_id=1,
        source_type="call",
        original_filename="call.webm",
    )
    db = MagicMock()

    with patch("app.app.services.pipeline_lab.finalize_audio_outputs") as fin:
        from app.app.services.storage import artifact_dir

        ws = artifact_dir(settings, 1, "art_lab_pub")
        final = ws / "parsed_final.txt"
        final.write_text("[00:00 - 00:01] speaker_0: добрый день\n", encoding="utf-8")
        fin.return_value = (final, "/artifacts/art_lab_pub/report")
        with patch("app.app.services.speaker_profiles.rebuild_match_report_html"):
            text = publish_lab_to_prod(db, settings, artifact, run.run_id)

    assert "добрый день" in text
    speakers = json.loads((ws / "asr" / "docker_gigaam_speakers.json").read_text(encoding="utf-8"))
    assert speakers["turns"][0]["speaker"] == "speaker_0"
    prod_match = json.loads((ws / "reports" / "sortformer_speaker_match.json").read_text(encoding="utf-8"))
    assert "speaker_0" in prod_match["matches"]
    assert "SPEAKER_00" not in prod_match["matches"]
    assert (ws / "asr" / "transcript.html").is_file()


def test_republish_named_transcript_uses_confirmed_names(tmp_data: Path):
    settings, run = _make_run(tmp_data, 1, "art_lab_names")
    proof = {
        "replicas": [
            {"speaker": "speaker_0", "start": 0.0, "end": 1.5, "text": "добрый день"},
            {"speaker": "speaker_1", "start": 1.6, "end": 3.0, "text": "добрый"},
        ]
    }
    run.resolve("proof").mkdir(parents=True, exist_ok=True)
    run.resolve("proof/transcript.json").write_text(json.dumps(proof), encoding="utf-8")
    match = {
        "source": "lab",
        "merge_map": {"speaker_0": "speaker_0", "speaker_1": "speaker_1"},
        "matches": {
            "speaker_0": {
                "diar_label": "speaker_0",
                "status": "confirmed",
                "profile_name": "Елена Соколова",
            },
            "speaker_1": {
                "diar_label": "speaker_1",
                "status": "confirmed",
                "profile_name": "Игорь (Пилот)",
            },
        },
        "display_names": {"speaker_0": "Елена Соколова", "speaker_1": "Игорь (Пилот)"},
        "embeddings": {"speaker_0": [1.0], "speaker_1": [0.0]},
    }
    from app.app.services.storage import artifact_dir

    ws = artifact_dir(settings, 1, "art_lab_names")
    (ws / "reports").mkdir(parents=True, exist_ok=True)
    (ws / "reports" / "sortformer_speaker_match.json").write_text(
        json.dumps(match), encoding="utf-8"
    )
    (ws / "asr").mkdir(parents=True, exist_ok=True)
    (ws / "asr" / "docker_gigaam_speakers.json").write_text(
        json.dumps(
            {
                "speakers": ["SPEAKER_00"],
                "turns": [{"start": 0.0, "end": 1.5, "speaker": "SPEAKER_00", "text": "мусор"}],
            }
        ),
        encoding="utf-8",
    )

    artifact = MagicMock(
        artifact_id="art_lab_names",
        project_id=1,
        source_type="call",
        original_filename="call.webm",
    )
    final = ws / "parsed_final.txt"
    final.write_text("placeholder\n", encoding="utf-8")

    with patch("app.app.services.pipeline_lab.finalize_audio_outputs", return_value=(final, "/r")):
        from app.app.services.pipeline_lab import republish_named_transcript

        republish_named_transcript(settings, artifact)

    speakers = json.loads((ws / "asr" / "docker_gigaam_speakers.json").read_text(encoding="utf-8"))
    assert [t["speaker"] for t in speakers["turns"]] == ["Елена Соколова", "Игорь (Пилот)"]
    assert "SPEAKER_00" not in {t["speaker"] for t in speakers["turns"]}


def test_speakers_confirm_status_uses_speaker_n(tmp_path, monkeypatch):
    match_path = tmp_path / "sortformer_speaker_match.json"
    match_path.write_text(
        json.dumps(
            {
                "merge_map": {"speaker_0": "speaker_0"},
                "matches": {"speaker_0": {"diar_label": "speaker_0", "status": "new"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "app.app.services.speaker_profiles.match_json_path",
        lambda *_a, **_k: match_path,
    )
    db = MagicMock()
    db.query.return_value.filter.return_value = []
    status = speakers_confirm_status(db, project_id=1, artifact_id="art_x")
    assert status["labels"] == ["speaker_0"]
    assert status["pending"] == ["speaker_0"]
    assert status["ready"] is False


def test_resumable_lab_run_id_resumes_matching_hash(tmp_data: Path):
    from app.app.services.pipeline_lab import resumable_lab_run_id
    from app.app.services.storage import artifact_audio_path

    settings, run = _make_run(tmp_data, 1, "art_lab_resume")
    wav = artifact_audio_path(settings, "art_lab_resume", 1)
    artifact = MagicMock(artifact_id="art_lab_resume", project_id=1)
    assert resumable_lab_run_id(settings, artifact, wav) == run.run_id


def test_resumable_lab_run_id_skips_stale_hash(tmp_data: Path):
    from app.app.services.pipeline_lab import resumable_lab_run_id
    from app.app.services.storage import artifact_audio_path

    settings, run = _make_run(tmp_data, 1, "art_lab_stale")
    wav = artifact_audio_path(settings, "art_lab_stale", 1)
    wav.write_bytes(b"RIFFchanged-source")
    artifact = MagicMock(artifact_id="art_lab_stale", project_id=1)
    assert resumable_lab_run_id(settings, artifact, wav) is None
    assert run.run_json.is_file()


def test_overlay_output_ready_skips_only_complete_files(tmp_data: Path):
    from app.app.services.pipeline_lab import overlay_output_ready

    settings, run = _make_run(tmp_data, 1, "art_lab_skip")
    artifact = MagicMock(artifact_id="art_lab_skip", project_id=1)
    assert overlay_output_ready(settings, artifact, run.run_id, "lab_proof") is False
    proof = run.resolve("proof")
    proof.mkdir(parents=True, exist_ok=True)
    (proof / "diffs.json").write_text('{"asked": 1}\n', encoding="utf-8")
    assert overlay_output_ready(settings, artifact, run.run_id, "lab_proof") is False
    (proof / "transcript.json").write_text('{"replicas": [{"text": "hi"}]}\n', encoding="utf-8")
    assert overlay_output_ready(settings, artifact, run.run_id, "lab_proof") is True
