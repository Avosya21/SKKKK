"""Этап 4 лаборатории: клипы из слов и match.json без Docker."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from asr.lab.paths import LabRun, new_run_id  # noqa: E402
from asr.lab.run import init_run  # noqa: E402
from asr.lab.stages.s04_paint import ASSIGNED, CONTESTED  # noqa: E402
from asr.lab.stages.s05_voice import clips_from_runs, word_runs  # noqa: E402
from asr.lab.voice import (  # noqa: E402
    build_match_payload,
    centroids_from_clips,
    match_path,
    overlay_words,
    profile_candidates,
)
from asr.lab_voice import run_voice  # noqa: E402


def _word(index, text, start, end, verdict, speaker, *, sense=False):
    row = {
        "i": index,
        "word": text,
        "start": start,
        "end": end,
        "verdict": verdict,
        "speaker_index": speaker,
        "speaker": None if speaker is None else f"speaker_{speaker}",
    }
    if sense:
        row["sense"] = True
    return row


def test_word_runs_include_sense_joins_overlay_words():
    words = [
        _word(0, "раз", 0.0, 0.4, ASSIGNED, 0),
        _word(1, "спорно", 0.5, 0.9, CONTESTED, 0, sense=True),
        _word(2, "три", 1.0, 1.4, ASSIGNED, 0),
    ]
    frozen = word_runs(words, max_gap=0.6)
    assert len(frozen) == 2
    joined = word_runs(words, max_gap=0.6, include_sense=True)
    assert len(joined) == 1
    assert joined[0]["words"] == 3
    assert joined[0]["text"] == "раз спорно три"


def test_match_payload_uses_lab_speaker_labels():
    clips = {
        "spk0_0": {
            "speaker_index": 0,
            "start": 1.0,
            "end": 5.0,
            "duration": 4.0,
            "words": 8,
            "text": "раз два",
        },
        "spk1_0": {
            "speaker_index": 1,
            "start": 10.0,
            "end": 14.0,
            "duration": 4.0,
            "words": 6,
            "text": "три",
        },
    }
    embeddings = {
        "spk0_0": [1.0, 0.0, 0.0],
        "spk1_0": [0.0, 1.0, 0.0],
    }
    centroids = centroids_from_clips(clips, embeddings)
    payload = build_match_payload(
        run_id="20260826T174104Z_48a796",
        artifact_id="art_test",
        project_id=1,
        clips=clips,
        centroids=centroids,
        candidates=profile_candidates(centroids, [], top=3),
    )
    assert payload["source"] == "lab"
    assert payload["written_to_db"] is False
    assert set(payload["embeddings"]) == {"speaker_0", "speaker_1"}
    assert "SPEAKER_00" not in payload["embeddings"]
    assert payload["matches"]["speaker_0"]["clip_start"] == 1.0
    assert payload["matches"]["speaker_0"]["status"] == "new"


def _make_run(artifact: Path) -> LabRun:
    run = LabRun(artifact, new_run_id())
    init_run(
        run,
        artifact_id="art_test",
        project_id=1,
        source_audio=artifact / "audio" / "input_16k.wav",
    )
    return run


def test_run_voice_reuse_embeddings_writes_lab_match(tmp_path: Path):
    artifact = tmp_path / "artifacts" / "art_test"
    (artifact / "audio").mkdir(parents=True)
    (artifact / "audio" / "input_16k.wav").write_bytes(b"RIFFfake")
    run = _make_run(artifact)
    paint = run.stage_dir("04_paint")
    paint.mkdir(parents=True, exist_ok=True)
    words = [
        _word(0, "длинный", 0.0, 8.0, ASSIGNED, 0),
        _word(1, "второй", 20.0, 28.0, ASSIGNED, 1),
    ]
    (paint / "paint.json").write_text(
        json.dumps({"words": words, "word_count": 2}, ensure_ascii=False),
        encoding="utf-8",
    )
    sense = run.resolve("sense")
    sense.mkdir(parents=True, exist_ok=True)
    (sense / "sense.json").write_text(
        json.dumps({"decisions": [{"word_from": 0, "word_to": 0, "speaker_index": 0}]}),
        encoding="utf-8",
    )
    overlay, sense_applied = overlay_words(run)
    assert sense_applied is True
    assert overlay[0].get("sense") is True
    runs = word_runs(overlay, max_gap=0.6, include_sense=True)
    clips = clips_from_runs(
        runs, edge_trim=0.25, min_duration=2.0, max_duration=12.0, per_speaker=3
    )
    assert set(clips) == {"spk0_0", "spk1_0"}

    raw = {
        "speakers": {
            key: {
                "embedding": [
                    1.0 if row["speaker_index"] == 0 else 0.0,
                    1.0 if row["speaker_index"] == 1 else 0.0,
                    0.0,
                ]
            }
            for key, row in clips.items()
        }
    }
    run.resolve("voice").mkdir(parents=True, exist_ok=True)
    run.resolve("voice/embed_raw.json").write_text(json.dumps(raw), encoding="utf-8")

    summary = run_voice(run, reuse_embeddings=True)
    assert summary["written_to_db"] is False
    assert summary["speakers"] == ["speaker_0", "speaker_1"]

    payload = json.loads(match_path(run).read_text(encoding="utf-8"))
    assert payload["source"] == "lab"
    assert "speaker_0" in payload["embeddings"]
    assert "SPEAKER_00" not in payload["embeddings"]
    assert payload["written_to_db"] is False
    assert run.resolve("voice/voice.json").is_file()
