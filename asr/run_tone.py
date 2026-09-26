"""Движок 3: T-one (t-tech/T-one), телефонный русский, 8 кГц.

Два пути запуска, выбирается автоматически:

  1. Пакет `tone` от разработчиков (StreamingCTCPipeline). Официально
     поддерживаются Linux и macOS, на Windows может не установиться.
  2. Запасной путь для Windows: sherpa-onnx с конвертированной ONNX-моделью
     T-one (OnlineRecognizer.from_t_one_ctc).

    python run_tone.py --audio work\\call_8k.wav --out-dir out
    python run_tone.py --audio work\\call_8k.wav --backend sherpa --model-dir models\\t-one
"""

import argparse
import json
import time
import wave
from pathlib import Path

ENGINE = "tone"
SAMPLE_RATE = 8000


def read_wav_mono(path: Path):
    """Читает 16-битный моно WAV в numpy float32 [-1, 1]."""
    import numpy as np

    with wave.open(str(path), "rb") as wf:
        if wf.getnchannels() != 1:
            raise ValueError(f"Ожидается моно, получено каналов: {wf.getnchannels()}")
        if wf.getsampwidth() != 2:
            raise ValueError("Ожидается 16-битный PCM")
        rate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())

    data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    return data, rate


def run_tone_package(audio: Path):
    from tone import StreamingCTCPipeline, read_audio

    pipeline = StreamingCTCPipeline.from_hugging_face()
    data = read_audio(str(audio))
    phrases = pipeline.forward_offline(data)

    segments = []
    for ph in phrases:
        start = getattr(ph, "start_time", None)
        end = getattr(ph, "end_time", None)
        text = getattr(ph, "text", None)
        if text is None and isinstance(ph, dict):
            start, end, text = ph.get("start_time"), ph.get("end_time"), ph.get("text")
        segments.append({"start": start, "end": end, "text": (text or "").strip()})
    return segments, "tone-package"


def run_sherpa(audio: Path, model_dir: Path):
    import numpy as np
    import sherpa_onnx

    model = model_dir / "model.onnx"
    tokens = model_dir / "tokens.txt"
    if not model.is_file() or not tokens.is_file():
        raise FileNotFoundError(
            f"Не найдены {model} и {tokens}. Скачайте sherpa-onnx-streaming-t-one-russian "
            "из релизов k2-fsa/sherpa-onnx (asr-models) и распакуйте в эту папку."
        )

    recognizer = sherpa_onnx.OnlineRecognizer.from_t_one_ctc(
        model=str(model), tokens=str(tokens), debug=False
    )
    data, rate = read_wav_mono(audio)
    if rate != SAMPLE_RATE:
        raise ValueError(f"T-one ожидает {SAMPLE_RATE} Гц, у файла {rate} Гц")

    stream = recognizer.create_stream()
    stream.accept_waveform(rate, np.zeros(int(0.3 * rate), dtype=np.float32))
    stream.accept_waveform(rate, data)
    stream.accept_waveform(rate, np.zeros(int(0.66 * rate), dtype=np.float32))
    stream.input_finished()

    while recognizer.is_ready(stream):
        recognizer.decode_stream(stream)

    text = recognizer.get_result(stream)
    return [{"start": None, "end": None, "text": (text or "").strip()}], "sherpa-onnx"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True, help="WAV 8 кГц моно")
    ap.add_argument("--out-dir", default="out")
    ap.add_argument("--backend", default="auto", choices=["auto", "tone", "sherpa"])
    ap.add_argument("--model-dir", default="models/t-one", help="папка с model.onnx и tokens.txt")
    args = ap.parse_args()

    audio = Path(args.audio)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    started = time.time()
    segments, backend = None, None
    errors = []

    if args.backend in ("auto", "tone"):
        try:
            print(f"[{ENGINE}] пробую пакет tone")
            segments, backend = run_tone_package(audio)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"tone: {exc}")
            print(f"[{ENGINE}] пакет tone не сработал: {exc}")

    if segments is None and args.backend in ("auto", "sherpa"):
        print(f"[{ENGINE}] пробую sherpa-onnx")
        segments, backend = run_sherpa(audio, Path(args.model_dir))

    if segments is None:
        print(f"[{ENGINE}] ОШИБКА: ни один бэкенд не сработал: {'; '.join(errors)}")
        return 1

    elapsed = time.time() - started
    text = " ".join(s["text"] for s in segments if s["text"]).strip()

    payload = {
        "engine": ENGINE,
        "model": "t-tech/T-one",
        "backend": backend,
        "audio": str(audio),
        "text": text,
        "segments": segments,
        "transcribe_sec": round(elapsed, 2),
        "chars": len(text),
    }

    stem = audio.stem.replace("_8k", "")
    (out_dir / f"{stem}_raw_{ENGINE}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / f"{stem}_raw_{ENGINE}.txt").write_text(text, encoding="utf-8")

    print(f"[{ENGINE}] распознано за {elapsed:.1f} с ({backend}), символов: {len(text)}")
    print(f"[{ENGINE}] {out_dir / f'{stem}_raw_{ENGINE}.txt'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
