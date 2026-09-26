"""Движок 1: Whisper large-v3 Russian Podlodka (bond005).

https://huggingface.co/bond005/whisper-large-v3-ru-podlodka

Вход: WAV 16 кГц моно. Выход: JSON и TXT единого формата.

    python run_whisper.py --audio work\\call_16k.wav --out-dir out
"""

import argparse
import json
import time
from pathlib import Path

MODEL_ID = "bond005/whisper-large-v3-ru-podlodka"
ENGINE = "whisper"


def _write_outputs(out_dir: Path, audio: Path, payload: dict, text: str) -> Path:
    stem = audio.stem.replace("_16k", "")
    (out_dir / f"{stem}_raw_{ENGINE}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    txt_path = out_dir / f"{stem}_raw_{ENGINE}.txt"
    txt_path.write_text(text, encoding="utf-8")
    return txt_path


def run_sherpa(args) -> int:
    import numpy as np
    import sherpa_onnx
    import wave

    audio = Path(args.audio)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    model_dir = Path(args.model)
    encoder = model_dir / "large-v3-encoder.int8.onnx"
    decoder = model_dir / "large-v3-decoder.int8.onnx"
    tokens = model_dir / "large-v3-tokens.txt"
    if not encoder.is_file() or not decoder.is_file() or not tokens.is_file():
        raise FileNotFoundError(
            f"Нужны {encoder.name}, {decoder.name}, {tokens.name} в {model_dir}"
        )

    provider = "cpu"
    if args.device in ("auto", "cuda"):
        provider = "cuda"

    print(f"[{ENGINE}] sherpa-onnx {model_dir}")
    print(f"[{ENGINE}] provider {provider}")

    load_started = time.time()
    try:
        recognizer = sherpa_onnx.OfflineRecognizer.from_whisper(
            encoder=str(encoder),
            decoder=str(decoder),
            tokens=str(tokens),
            language="ru",
            task="transcribe",
            num_threads=4,
            provider=provider,
        )
    except Exception as exc:  # noqa: BLE001
        if provider == "cuda":
            print(f"[{ENGINE}] CUDA provider недоступен ({exc}); CPU")
            provider = "cpu"
            recognizer = sherpa_onnx.OfflineRecognizer.from_whisper(
                encoder=str(encoder),
                decoder=str(decoder),
                tokens=str(tokens),
                language="ru",
                task="transcribe",
                num_threads=4,
                provider="cpu",
            )
        else:
            raise
    load_sec = time.time() - load_started
    print(f"[{ENGINE}] модель загружена за {load_sec:.1f} с")

    with wave.open(str(audio), "rb") as wf:
        rate = wf.getframerate()
        nchan = wf.getnchannels()
        frames = wf.readframes(wf.getnframes())
        sampwidth = wf.getsampwidth()
    if sampwidth != 2:
        raise ValueError("Ожидается 16-битный PCM")
    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if nchan > 1:
        samples = samples.reshape(-1, nchan).mean(axis=1)

    started = time.time()
    # Официальный Whisper ONNX в sherpa принимает только ≤30 с.
    chunk_sec = 29.0
    hop = int(chunk_sec * rate)
    parts = []
    segments = []
    n_chunks = (len(samples) + hop - 1) // hop
    for idx, offset in enumerate(range(0, len(samples), hop)):
        piece = samples[offset : offset + hop]
        stream = recognizer.create_stream()
        stream.accept_waveform(rate, piece)
        recognizer.decode_stream(stream)
        chunk_text = (stream.result.text or "").strip()
        start = offset / rate
        end = min(offset + hop, len(samples)) / rate
        if chunk_text:
            parts.append(chunk_text)
            segments.append(
                {"start": round(start, 2), "end": round(end, 2), "text": chunk_text}
            )
        print(
            f"[{ENGINE}] чанк {idx + 1}/{n_chunks} {start:.0f}-{end:.0f}с, символов: {len(chunk_text)}"
        )
    elapsed = time.time() - started
    text = " ".join(parts).strip()

    payload = {
        "engine": ENGINE,
        "model": str(model_dir),
        "backend": "sherpa-onnx",
        "device": provider,
        "audio": str(audio),
        "text": text,
        "segments": segments,
        "load_sec": round(load_sec, 2),
        "transcribe_sec": round(elapsed, 2),
        "chars": len(text),
    }
    txt_path = _write_outputs(out_dir, audio, payload, text)
    print(f"[{ENGINE}] распознано за {elapsed:.1f} с, символов: {len(text)}")
    print(f"[{ENGINE}] {txt_path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True, help="WAV 16 кГц моно")
    ap.add_argument("--out-dir", default="out")
    ap.add_argument("--model", default=MODEL_ID)
    ap.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])
    ap.add_argument("--chunk-length", type=int, default=30)
    ap.add_argument("--batch-size", type=int, default=8)
    ap.add_argument(
        "--backend",
        default="transformers",
        choices=["transformers", "sherpa"],
        help="transformers (HF/Podlodka) или sherpa-onnx (локальный Whisper ONNX)",
    )
    args = ap.parse_args()

    if args.backend == "sherpa":
        return run_sherpa(args)

    import torch
    from transformers import pipeline

    if args.device == "auto":
        device = "cuda:0" if torch.cuda.is_available() else "cpu"
    else:
        device = "cuda:0" if args.device == "cuda" else "cpu"
    dtype = torch.float16 if device.startswith("cuda") else torch.float32

    audio = Path(args.audio)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[{ENGINE}] модель {args.model}")
    print(f"[{ENGINE}] устройство {device}, dtype {dtype}")

    load_started = time.time()
    asr = pipeline(
        "automatic-speech-recognition",
        model=args.model,
        device=device,
        torch_dtype=dtype,
        chunk_length_s=args.chunk_length,
    )
    load_sec = time.time() - load_started
    print(f"[{ENGINE}] модель загружена за {load_sec:.1f} с")

    started = time.time()
    result = asr(
        str(audio),
        batch_size=args.batch_size,
        return_timestamps=True,
        generate_kwargs={"language": "ru", "task": "transcribe"},
    )
    elapsed = time.time() - started

    segments = []
    for chunk in result.get("chunks", []) or []:
        ts = chunk.get("timestamp") or (None, None)
        segments.append(
            {
                "start": ts[0],
                "end": ts[1],
                "text": (chunk.get("text") or "").strip(),
            }
        )

    text = (result.get("text") or "").strip()

    payload = {
        "engine": ENGINE,
        "model": args.model,
        "device": device,
        "audio": str(audio),
        "text": text,
        "segments": segments,
        "load_sec": round(load_sec, 2),
        "transcribe_sec": round(elapsed, 2),
        "chars": len(text),
    }

    stem = audio.stem.replace("_16k", "")
    (out_dir / f"{stem}_raw_{ENGINE}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / f"{stem}_raw_{ENGINE}.txt").write_text(text, encoding="utf-8")

    print(f"[{ENGINE}] распознано за {elapsed:.1f} с, символов: {len(text)}")
    print(f"[{ENGINE}] {out_dir / f'{stem}_raw_{ENGINE}.txt'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
