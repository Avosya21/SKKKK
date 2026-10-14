import json
from pathlib import Path
from types import SimpleNamespace

from app.app.services import meeting_frames as frames
from app.app.services.meeting_recordings import SOURCE_SCREEN_VIDEO


def test_disabled_extract_writes_nothing(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MEETING_SCREEN_VISION", "0")
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"ftypfake")
    settings = SimpleNamespace()
    artifact = SimpleNamespace(project_id=1, artifact_id="art_x")
    manifest = {
        "tracks": [
            {
                "source": SOURCE_SCREEN_VIDEO,
                "src": str(clip),
                "track_sid": "TR_one",
            }
        ]
    }
    assert frames.extract_meeting_screen_frames(
        settings, artifact, manifest, enabled=False
    ) == []


def test_disabled_extract_logs_skip(tmp_path: Path, caplog):
    artifact = SimpleNamespace(project_id=1, artifact_id="art_log")
    caplog.set_level("INFO", logger="skkkk.meeting_video")
    assert frames.extract_meeting_screen_frames(SimpleNamespace(), artifact, {}, enabled=False) == []
    assert "meeting.video_transcript.skip" in caplog.text
    assert "reason=disabled" in caplog.text


def test_video_transcript_writes_timeline_provenance(tmp_path: Path):
    import json
    from types import SimpleNamespace
    from app.app.services.meeting_frames import _write_video_transcript

    run = SimpleNamespace(resolve=lambda name: tmp_path / name)
    _write_video_transcript(run, [{
        "frame_id": "TR_1_1000", "track_sid": "TR_1", "identity": "member:1",
        "speaker": "Анна", "t_rel": 10.0, "text": "Заказ 42", "text_source": "ocr",
    }, {
        "frame_id": "TR_1_3000", "track_sid": "TR_1", "identity": "member:1",
        "speaker": "Анна", "t_rel": 12.0, "text": "Готово", "text_source": "vision",
    }])
    payload = json.loads((tmp_path / "video_transcript.json").read_text(encoding="utf-8"))
    assert payload["segments"][0]["start"] == 10.0
    assert payload["segments"][0]["end"] == 12.0
    assert payload["segments"][0]["frame_id"] == "TR_1_1000"
    assert "Заказ 42" in (tmp_path / "video_transcript.md").read_text(encoding="utf-8")
    assert not (tmp_path / "frames.json").exists()


def test_vision_off_still_writes_frames_without_captions(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MEETING_SCREEN_VISION", "0")
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"ftypfake")

    def fake_extract(settings, run, track, *, cap, describe, replicas=None):
        assert describe is False
        return [
            {
                "frame_id": "TRone_0",
                "track_sid": "TR_one",
                "identity": "member:1",
                "speaker": "Анна",
                "t_rel": 11.5,
                "ts_in_file": 0.0,
                "sha256": "abc",
                "path": "frames/TRone/0.jpg",
            }
        ]

    class Run:
        def __init__(self, root: Path):
            self.root = root

        def resolve(self, rel: str) -> Path:
            return self.root / rel

    monkeypatch.setattr(frames, "_extract_track", fake_extract)
    monkeypatch.setattr(
        "app.app.services.artifact_lab.latest_run_id",
        lambda *_a, **_k: "run1",
    )
    monkeypatch.setattr(
        "app.app.services.artifact_lab.resolve_run",
        lambda *_a, **_k: Run(tmp_path),
    )
    settings = SimpleNamespace()
    artifact = SimpleNamespace(project_id=1, artifact_id="art_x")
    manifest = {
        "tracks": [
            {
                "source": SOURCE_SCREEN_VIDEO,
                "src": str(clip),
                "track_sid": "TR_one",
                "identity": "member:1",
                "speaker": "Анна",
                "offset_sec": 11.5,
                "duration_sec": 10,
            }
        ]
    }
    rows = frames.extract_meeting_screen_frames(settings, artifact, manifest)
    assert rows[0]["frame_id"] == "TRone_0"
    assert "text" not in rows[0]
    assert (tmp_path / "frames.json").is_file()
    assert (tmp_path / "speech_screen_links.json").is_file()


def test_cluster_onsets_joins_gap_under_8s():
    assert frames.cluster_onsets([1.0, 3.0, 12.0, 13.5], gap_sec=8.0) == [1.0, 12.0]


def test_dedup_keeps_own_speech_over_floor():
    times = frames.select_share_frames(
        30.0,
        scene=[],
        own_onsets=[10.5],
        own_spans=[(10.5, 11.0)],
        other_onsets=[],
        max_n=20,
        floor_step=15.0,
        dedup_sec=2.0,
    )
    near = [t for t in times if 9.0 <= t <= 12.0]
    assert near == [10.5]


def test_cap_drops_floor_before_publisher_speech():
    times = frames.select_share_frames(
        100.0,
        scene=[],
        own_onsets=[10.0, 30.0, 50.0],
        own_spans=[(10.0, 12.0), (30.0, 32.0), (50.0, 52.0)],
        other_onsets=[],
        max_n=5,
    )
    assert times == [0.0, 10.0, 30.0, 50.0, 100.0]


def test_silent_share_gets_edges_and_floor():
    times = frames.select_share_frames(60.0, scene=[], own_onsets=[], own_spans=[], max_n=20)
    assert times[0] == 0.0
    assert times[-1] == 60.0
    assert 15.0 in times
    assert 45.0 in times


def test_bind_own_share_vs_over_others():
    track = {
        "identity": "member:1",
        "speaker": "Анна",
        "offset_sec": 10.0,
        "duration_sec": 20.0,
    }
    grabbed = [
        {"frame_id": "a_0", "t_rel": 10.0},
        {"frame_id": "a_10000", "t_rel": 20.0},
    ]
    replicas = [
        {"source_span_id": "turn:anna", "speaker": "Анна", "identity": "member:1", "start": 12.0, "end": 14.0},
        {"source_span_id": "turn:vasya", "speaker": "Вася", "identity": "member:2", "start": 21.0, "end": 23.0},
    ]
    links = frames.bind_replicas(grabbed, replicas, track)
    by_span = {row["source_span_id"]: row for row in links}
    assert by_span["turn:anna"]["relation"] == frames.RELATION_OWN
    assert by_span["turn:anna"]["frame_id"] == "a_0"
    assert by_span["turn:anna"]["shown_by"] == "member:1"
    assert by_span["turn:vasya"]["relation"] == frames.RELATION_OTHER
    assert by_span["turn:vasya"]["frame_id"] == "a_10000"


def test_frames_json_contract(tmp_path: Path, monkeypatch):
    jpg = tmp_path / "clip.mp4"
    jpg.write_bytes(b"ftypfake")

    def fake_extract(settings, run, track, *, cap, describe, replicas=None):
        assert describe is False
        assert cap == 8
        return [
            {
                "frame_id": "TRone_1000",
                "track_sid": "TR_one",
                "identity": "member:1",
                "speaker": "Анна",
                "t_rel": 12.5,
                "ts_in_file": 1.0,
                "sha256": "abc",
                "path": "frames/TRone/1000.jpg",
            }
        ]

    class Run:
        def __init__(self, root: Path):
            self.root = root

        def resolve(self, rel: str) -> Path:
            return self.root / rel

    monkeypatch.setattr(frames, "_extract_track", fake_extract)
    monkeypatch.setattr(
        "app.app.services.artifact_lab.latest_run_id",
        lambda *_a, **_k: "run1",
    )
    monkeypatch.setattr(
        "app.app.services.artifact_lab.resolve_run",
        lambda *_a, **_k: Run(tmp_path),
    )
    settings = SimpleNamespace()
    artifact = SimpleNamespace(project_id=1, artifact_id="art_x")
    manifest = {
        "tracks": [
            {
                "source": SOURCE_SCREEN_VIDEO,
                "src": str(jpg),
                "track_sid": "TR_one",
                "identity": "member:1",
                "speaker": "Анна",
                "offset_sec": 11.5,
                "duration_sec": 10,
            }
        ]
    }
    rows = frames.extract_meeting_screen_frames(
        settings, artifact, manifest, enabled=True, describe=False, max_n=8
    )
    assert rows[0]["frame_id"] == "TRone_1000"
    assert rows[0]["identity"] == "member:1"
    payload = json.loads((tmp_path / "frames.json").read_text(encoding="utf-8"))
    assert payload[0]["frame_id"] == "TRone_1000"
    links = json.loads((tmp_path / "speech_screen_links.json").read_text(encoding="utf-8"))
    assert links == []


def _screen(path: Path, shade: int, box: tuple[int, int, int, int] | None = None) -> None:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (320, 180), (shade, shade, shade))
    draw = ImageDraw.Draw(img)
    for x in range(0, 320, 40):
        draw.rectangle((x, 20, x + 20, 60), fill=(20, 20, 20))
    if box:
        draw.rectangle(box, fill=(250, 250, 250))
    img.save(path)


def test_repeat_frame_reuses_description(tmp_path: Path, monkeypatch):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"ftypfake")
    pictures = {
        0: dict(shade=200),
        1000: dict(shade=201),
        2000: dict(shade=60, box=(0, 90, 320, 180)),
    }

    def fake_grab(src, ts, dest):
        _screen(dest, **pictures[int(round(ts * 1000))])
        return True

    calls = []

    def fake_text(settings, path, *, describe):
        calls.append(path.name)
        return f"описание {path.name}", "vision"

    class Run:
        root = tmp_path

        def resolve(self, rel: str) -> Path:
            return tmp_path / rel

    monkeypatch.setattr(frames, "scene_times", lambda *_a, **_k: [])
    monkeypatch.setattr(frames, "select_share_frames", lambda *_a, **_k: [0.0, 1.0, 2.0])
    monkeypatch.setattr(frames, "_grab_frame", fake_grab)
    monkeypatch.setattr(frames, "_frame_text", fake_text)
    track = {"src": str(clip), "track_sid": "TR_one", "duration_sec": 3}
    rows = frames._extract_track(SimpleNamespace(), Run(), track, cap=10, describe=True)

    assert calls == ["0.jpg", "2000.jpg"]
    assert rows[1]["repeat_of"] == rows[0]["frame_id"]
    assert rows[1]["text"] == "описание 0.jpg"
    assert rows[1]["text_source"] == "vision"
    assert "repeat_of" not in rows[2]


def test_find_repeat_threshold():
    seen = [{"fingerprint": 0, "frame_id": "a"}]
    assert frames.REPEAT_MAX_BITS == 7
    assert frames.find_repeat(0b111_1111, seen)["frame_id"] == "a"
    assert frames.find_repeat(0b1111_1111, seen) is None
    assert frames.find_repeat(None, seen) is None
