"""Собрать docker_whisper.txt с метками SPEAKER_xx из verbose_json."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def format_text(data: dict) -> tuple[str, list[str], int]:
    segs = data.get("segments") or []
    speakers = sorted(
        {s.get("speaker") for s in segs if isinstance(s, dict) and s.get("speaker")}
    )
    if not speakers:
        text = data.get("text") or ""
        return text, [], len(segs)
    lines: list[str] = []
    prev: str | None = None
    buf: list[str] = []
    for seg in segs:
        if not isinstance(seg, dict):
            continue
        speaker = str(seg.get("speaker") or "SPEAKER_00")
        chunk = (seg.get("text") or "").strip()
        if not chunk:
            continue
        if prev is None:
            prev = speaker
        if speaker != prev:
            lines.append(f"[{prev}] {' '.join(buf)}")
            buf = [chunk]
            prev = speaker
        else:
            buf.append(chunk)
    if prev is not None and buf:
        lines.append(f"[{prev}] {' '.join(buf)}")
    return "\n".join(lines), speakers, len(segs)


def main() -> int:
    out = Path(__file__).resolve().parent / "out"
    src = out / "docker_whisper.json"
    data = json.loads(src.read_text(encoding="utf-8"))
    text, speakers, nseg = format_text(data)
    (out / "docker_whisper.txt").write_text(text, encoding="utf-8")
    elapsed = float(sys.argv[1]) if len(sys.argv) > 1 else None
    summary = {
        "whisper": {
            "ok": True,
            "diarization": bool(speakers),
            "speakers": speakers,
            "segments": nseg,
            "chars": len(text),
            "elapsed_sec": elapsed,
            "preview": text[:400].replace("\n", " | "),
        }
    }
    (out / "docker_whisper_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("speakers", speakers)
    print("segments", nseg)
    print("chars", len(text))
    print("lines", text.count("\n") + (1 if text else 0))
    print(text[:600])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
