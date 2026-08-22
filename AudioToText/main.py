import argparse
import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from docx import Document as DocxDocument
from transformers import AutoModel


def extract_audio_from_mp4(video_path: str, wav_path: str) -> None:
    """
    Extract mono 16kHz WAV audio from MP4 using ffmpeg.
    """
    video_path = str(Path(video_path))
    wav_path = str(Path(wav_path))

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        video_path,
        "-ac",
        "1",
        "-ar",
        "16000",
        "-vn",
        wav_path,
    ]
    subprocess.run(cmd, check=True)


def format_time(seconds: float) -> str:
    """
    Format float seconds as HH:MM:SS.mmm.
    """
    total_ms = int(seconds * 1000 + 0.5)
    hours = total_ms // 3_600_000
    total_ms %= 3_600_000
    minutes = total_ms // 60_000
    total_ms %= 60_000
    secs = total_ms // 1000
    ms = total_ms % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"


def load_gigaam_model(
    revision: str = "e2e_rnnt",
) -> Any:
    """
    Load GigaAM on CUDA (float16) when available; CPU (float32) is fallback only.
    """
    import torch

    local = Path("/app/model")
    has_local = local.is_dir() and any(local.iterdir())
    source = str(local) if has_local else "ai-sage/GigaAM-v3"
    kwargs = {"trust_remote_code": True}
    if not has_local:
        kwargs["revision"] = revision
    print(f"GigaAM weights: {source}")
    model = AutoModel.from_pretrained(source, **kwargs)

    device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.float16 if device == "cuda" else torch.float32
    model = model.to(device, dtype=dtype)
    print(f"GigaAM device: {device}, dtype: {dtype}")
    return model


def transcribe_with_timestamps(
    model: Any,
    audio_path: str,
) -> List[Dict[str, Any]]:
    """
    Use model.transcribe_longform to get segments with timestamps.
    """
    utterances = model.transcribe_longform(audio_path)

    segments: List[Dict[str, Any]] = []
    for utt in utterances:
        text = utt.get("transcription", "")
        boundaries = utt.get("boundaries", (0.0, 0.0))
        start, end = float(boundaries[0]), float(boundaries[1])
        segments.append(
            {
                "text": text,
                "start": start,
                "end": end,
                "start_str": format_time(start),
                "end_str": format_time(end),
            }
        )
    return segments


def save_transcription_to_txt(
    segments: List[Dict[str, Any]],
    txt_path: str,
) -> None:
    """
    Save transcription segments with timestamps to a TXT file.
    """
    lines: List[str] = []
    for seg in segments:
        line = f"[{seg['start_str']} - {seg['end_str']}] {seg['text']}"
        lines.append(line)

    Path(txt_path).write_text("\n".join(lines), encoding="utf-8")


def save_transcription_to_docx(
    segments: List[Dict[str, Any]],
    docx_path: str,
) -> None:
    """
    Save transcription segments with timestamps to a DOCX file.
    """
    doc = DocxDocument()
    for seg in segments:
        line = f"[{seg['start_str']} - {seg['end_str']}] {seg['text']}"
        doc.add_paragraph(line)
    doc.save(docx_path)


def save_segments_to_json(
    segments: List[Dict[str, Any]],
    json_path: str,
) -> None:
    """
    Optional helper to save raw segments as JSON.
    """
    Path(json_path).write_text(
        json.dumps(segments, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def process_video(
    video_path: str,
    out_dir: str = "outputs",
    revision: str = "e2e_rnnt",
    save_json: bool = True,
) -> None:
    """
    Full pipeline: MP4 -> WAV -> transcription + timestamps -> TXT + DOCX (+ JSON).
    """
    out_dir_path = Path(out_dir)
    out_dir_path.mkdir(parents=True, exist_ok=True)

    stem = Path(video_path).stem
    wav_path = out_dir_path / f"{stem}.wav"
    txt_path = out_dir_path / f"{stem}.txt"
    docx_path = out_dir_path / f"{stem}.docx"
    json_path = out_dir_path / f"{stem}.json"

    src = Path(video_path)
    if src.suffix.lower() in {".wav", ".flac"}:
        wav_path = src
        print(f"[1/4] Audio already WAV/FLAC: {wav_path}")
    else:
        print(f"[1/4] Extracting audio from {video_path} -> {wav_path}")
        extract_audio_from_mp4(video_path, str(wav_path))

    print(f"[2/4] Loading GigaAM-v3 model")
    model = load_gigaam_model(revision=revision)

    print(f"[3/4] Transcribing with timestamps from {wav_path}")
    segments = transcribe_with_timestamps(model, str(wav_path))

    print(f"[4/4] Saving outputs to {out_dir_path}")
    save_transcription_to_txt(segments, str(txt_path))
    save_transcription_to_docx(segments, str(docx_path))
    if save_json:
        save_segments_to_json(segments, str(json_path))

    ensemble_txt = out_dir_path / f"{stem.replace('_16k', '')}_raw_gigaam.txt"
    text_only = " ".join(s["text"] for s in segments if s.get("text")).strip()
    ensemble_txt.write_text(text_only, encoding="utf-8")

    print(f"TXT saved to:   {txt_path}")
    print(f"DOCX saved to:  {docx_path}")
    if save_json:
        print(f"JSON saved to:  {json_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Transcribe MP4 video with GigaAM-v3 "
            "and save timestamps to TXT/DOCX."
        )
    )
    parser.add_argument(
        "video_path",
        type=str,
        help="Path to input MP4 video file",
    )
    parser.add_argument(
        "--out-dir",
        type=str,
        default="outputs",
        help="Directory to save output files",
    )
    parser.add_argument(
        "--revision",
        type=str,
        default="e2e_rnnt",
        help="GigaAM-v3 revision",
    )
    parser.add_argument(
        "--no-json",
        action="store_true",
        help="Do not save raw segments as JSON",
    )

    args = parser.parse_args()

    process_video(
        video_path=args.video_path,
        out_dir=args.out_dir,
        revision=args.revision,
        save_json=not args.no_json,
    )


if __name__ == "__main__":
    main()
