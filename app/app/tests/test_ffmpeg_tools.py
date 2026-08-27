from app.app.services.ffmpeg_tools import resolve_ffmpeg_tool


def test_resolve_ffmpeg_from_known_windows_path():
    ffmpeg = resolve_ffmpeg_tool("ffmpeg")
    ffprobe = resolve_ffmpeg_tool("ffprobe")
    assert ffmpeg.lower().endswith("ffmpeg.exe")
    assert ffprobe.lower().endswith("ffprobe.exe")
