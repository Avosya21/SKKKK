import importlib.util
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]


def _mod():
    path = ROOT / "tools" / "livekit" / "screen_recorder.py"
    spec = importlib.util.spec_from_file_location("screen_recorder_mod", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_recorder_is_hidden_subscribe_only():
    text = (ROOT / "tools" / "livekit" / "screen_recorder.py").read_text(encoding="utf-8")
    assert 'RECORDER_ID = "primaryanalitics-rec"' in text
    assert '"canPublish": True' not in text
    assert '"canSubscribe": True' in text
    assert '"hidden": True' in text
    assert "CUDA_VISIBLE_DEVICES" in text
    assert "SOURCE_CAMERA" not in text or "camera" in text.lower()


def test_recorder_skips_when_flag_off():
    vc = _mod()
    assert vc.main_disabled("0") is True
    assert vc.main_disabled("1") is False


def test_should_keep_frame_throttles_to_fps():
    vc = _mod()
    assert vc.should_keep_frame(None, 1.0, 1.0) is True
    assert vc.should_keep_frame(1.0, 1.4, 1.0) is False
    assert vc.should_keep_frame(1.0, 2.0, 1.0) is True
    assert vc.should_keep_frame(1.0, 1.2, 5.0) is True


def test_ffmpeg_cmd_is_video_only():
    vc = _mod()
    cmd = vc.ffmpeg_cmd(Path("out.mp4"), 1920, 1080, 1, 5_000_000)
    assert Path(cmd[0]).name.lower().startswith("ffmpeg")
    joined = " ".join(cmd)
    assert "-an" in cmd
    assert "libx264" in cmd
    assert "1920x1080" in joined
    assert "5000000" in cmd
    assert "-crf" not in cmd
    assert "-i" in cmd and "-" in cmd


def test_is_screen_video_pub():
    vc = _mod()
    screen = SimpleNamespace(kind="KIND_VIDEO", source="SOURCE_SCREEN_SHARE")
    cam = SimpleNamespace(kind="KIND_VIDEO", source="SOURCE_CAMERA")
    audio = SimpleNamespace(kind="KIND_AUDIO", source="SOURCE_SCREEN_SHARE")
    assert vc.is_screen_video(screen, screen) is True
    assert vc.is_screen_video(cam, cam) is False
    assert vc.is_screen_video(audio, audio) is False
    proto_screen = SimpleNamespace(kind=2, source=3)
    proto_cam = SimpleNamespace(kind=2, source=1)
    proto_mic = SimpleNamespace(kind=1, source=2)
    assert vc.is_screen_video(proto_screen, proto_screen) is True
    assert vc.is_screen_video(proto_cam, proto_cam) is False
    assert vc.is_screen_video(proto_mic, proto_mic) is False


def test_records_only_screen_video_not_camera():
    text = (ROOT / "tools" / "livekit" / "screen_recorder.py").read_text(encoding="utf-8")
    start = text.split("def start_record")[1].split("def stop_record")[0]
    assert "is_screen_video" in start
    assert "SOURCE_SCREEN_VIDEO" in start or "screen_video_relpath" in start


def test_packed_i420_keeps_exact_frame():
    vc = _mod()
    width, height = 4, 4
    data = bytes(range(vc.i420_size(width, height)))
    packed, out_w, out_h = vc.packed_i420(data, width, height)
    assert (out_w, out_h) == (width, height)
    assert packed == data


def test_packed_i420_infers_1080_when_label_is_540():
    vc = _mod()
    data = bytes(vc.i420_size(1920, 1080))
    packed, width, height = vc.packed_i420(data, 960, 540)
    assert (width, height) == (1920, 1080)
    assert len(packed) == vc.i420_size(1920, 1080)


def test_pack_i420_stride_drops_row_padding():
    vc = _mod()
    width, height, stride = 4, 4, 8
    raw = bytearray(stride * height + (stride // 2) * (height // 2) * 2)
    for row in range(height):
        for col in range(width):
            raw[row * stride + col] = 40 + col
    packed = vc.pack_i420_stride(bytes(raw), width, height)
    assert packed is not None
    assert packed[: width * height] == bytes([40, 41, 42, 43] * height)
    assert len(packed) == vc.i420_size(width, height)


def test_scale_i420_matches_opened_size():
    vc = _mod()
    src = bytes([80] * vc.i420_size(8, 8))
    out = vc.scale_i420(src, 8, 8, 4, 4)
    assert len(out) == vc.i420_size(4, 4)
    assert out[0] == 80
