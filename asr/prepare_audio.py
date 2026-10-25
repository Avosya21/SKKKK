"""Готовит аудио для трёх ASR-движков.

Вход: любой видео- или аудиофайл.
Выход: WAV 16 кГц моно (Whisper, GigaAM), WAV 8 кГц моно (T-one), метаданные.

    python prepare_audio.py "D:\\Primary_Analitics\\Test_video\\call.mp4" --out-dir D:\\Primary_Analitics\\asr\\work
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.app.services.ffmpeg_tools import resolve_ffmpeg_tool


def ffprobe_duration(path: Path) -> float:
    ffprobe = resolve_ffmpeg_tool("ffprobe")
    out = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def extract(src: Path, dst: Path, sample_rate: int) -> None:
    ffmpeg = resolve_ffmpeg_tool("ffmpeg")
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(src),
            "-vn", "-ac", "1", "-ar", str(sample_rate),
            "-c:a", "pcm_s16le",
            str(dst),
        ],
        check=True,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="MP4/MKV/WAV -> 16k и 8k моно WAV")
    ap.add_argument("source", help="исходный файл")
    ap.add_argument("--out-dir", default="work", help="куда класть результат")
    args = ap.parse_args()

    src = Path(args.source)
    if not src.exists():
        print(f"ОШИБКА: файл не найден: {src}", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = src.stem

    wav16 = out_dir / f"{stem}_16k.wav"
    wav8 = out_dir / f"{stem}_8k.wav"

    print(f"[1/3] 16 кГц моно -> {wav16}")
    extract(src, wav16, 16000)

    print(f"[2/3] 8 кГц моно  -> {wav8}")
    extract(src, wav8, 8000)

    print("[3/3] длительность")
    duration = ffprobe_duration(wav16)

    meta = {
        "source": str(src),
        "stem": stem,
        "wav_16k": str(wav16),
        "wav_8k": str(wav8),
        "duration_sec": round(duration, 2),
    }
    meta_path = out_dir / f"{stem}_meta.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(meta, ensure_ascii=False, indent=2))
    print(f"\nГотово. Длительность {duration / 60:.1f} мин.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
