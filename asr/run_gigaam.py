"""Движок 2: GigaAM v3 (переработка того, что лежит в AudioToText).

Отличия от AudioToText/main.py:
  - работает нативно на Windows, без Docker;
  - CUDA по умолчанию (`--device auto` → cuda, иначе cpu);
  - модель берётся из ai-sage/GigaAM-v3 или из локальной папки;
  - формат вывода единый с остальными движками;
  - при недоступности transcribe_longform (нужен HF-токен для VAD pyannote)
    честно падает на обычный transcribe, а не молча выдаёт пустой текст.

    python run_gigaam.py --audio work\\call_16k.wav --out-dir out
"""

import argparse
import json
import time
from pathlib import Path

MODEL_ID = "ai-sage/GigaAM-v3"
REVISION = "e2e_rnnt"
ENGINE = "gigaam"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True, help="WAV 16 кГц моно")
    ap.add_argument("--out-dir", default="out")
    ap.add_argument("--model", default=MODEL_ID, help="ID на HF или путь к локальной папке")
    ap.add_argument("--revision", default=REVISION)
    ap.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])
    ap.add_argument("--no-longform", action="store_true", help="не пытаться transcribe_longform")
    args = ap.parse_args()

    import torch
    from transformers import AutoModel
    from transformers.modeling_utils import PreTrainedModel

    # Новые transformers всегда создают модель на meta; GigaAM MelSpectrogram
    # этого не переживает. Убираем meta из контекста инициализации.
    _orig_init_ctx = PreTrainedModel.get_init_context.__func__

    @classmethod
    def _cpu_init_context(cls, dtype, is_quantized, _is_ds_init_called, allow_all_kernels):
        ctx = _orig_init_ctx(cls, dtype, is_quantized, _is_ds_init_called, allow_all_kernels)
        return [c for c in ctx if not (isinstance(c, torch.device) and c.type == "meta")]

    PreTrainedModel.get_init_context = _cpu_init_context

    _orig_finalize = getattr(
        PreTrainedModel._finalize_model_loading,
        "__func__",
        PreTrainedModel._finalize_model_loading,
    )

    def _safe_finalize(cls, model, load_config, loading_info):
        if not hasattr(model, "all_tied_weights_keys"):
            model.all_tied_weights_keys = {}
        try:
            return _orig_finalize(cls, model, load_config, loading_info)
        except TypeError:
            return _orig_finalize(model, load_config, loading_info)

    PreTrainedModel._finalize_model_loading = classmethod(_safe_finalize)

    if args.device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    else:
        device = args.device
    # fp16 ломает cuFFT: n_fft=320 у GigaAM не степень двойки.
    dtype = torch.float32

    audio = Path(args.audio)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[{ENGINE}] модель {args.model} ({args.revision})")
    print(f"[{ENGINE}] устройство {device}")

    load_started = time.time()
    # GigaAM строит MelSpectrogram на CPU; meta-device init из transformers ломает это.
    kwargs = {"trust_remote_code": True, "low_cpu_mem_usage": False}
    # revision имеет смысл только для репозитория HF, для локальной папки его не передаём
    if not Path(args.model).exists():
        kwargs["revision"] = args.revision
    model = AutoModel.from_pretrained(args.model, **kwargs)
    model = model.to(device, dtype=dtype)
    load_sec = time.time() - load_started
    print(f"[{ENGINE}] модель загружена за {load_sec:.1f} с")

    started = time.time()
    segments = []
    mode = "longform"

    if not args.no_longform and hasattr(model, "transcribe_longform"):
        try:
            utterances = model.transcribe_longform(str(audio))
            for utt in utterances:
                boundaries = utt.get("boundaries", (0.0, 0.0))
                segments.append(
                    {
                        "start": float(boundaries[0]),
                        "end": float(boundaries[1]),
                        "text": (utt.get("transcription") or "").strip(),
                    }
                )
        except Exception as exc:  # noqa: BLE001 - нужно увидеть настоящую причину
            print(f"[{ENGINE}] transcribe_longform недоступен: {exc}")
            print(f"[{ENGINE}] причина обычно одна: не принята лицензия pyannote или нет HF_TOKEN")
            segments = []
            mode = "fallback"
    else:
        mode = "fallback"

    if not segments:
        # transcribe() отклоняет файлы длиннее ~25 с. Режем WAV сами,
        # без pyannote/HF (лицензия segmentation-3.0 + Xet CDN недоступны).
        import tempfile
        import wave

        chunk_sec = 20.0
        with wave.open(str(audio), "rb") as wf:
            rate = wf.getframerate()
            nchan = wf.getnchannels()
            width = wf.getsampwidth()
            nframes = wf.getnframes()
            raw = wf.readframes(nframes)
        frames_per_chunk = int(chunk_sec * rate)
        sample_bytes = nchan * width
        total_frames = nframes
        tmp_dir = Path(tempfile.mkdtemp(prefix="gigaam_chunks_"))
        try:
            offset = 0
            idx = 0
            while offset < total_frames:
                end = min(offset + frames_per_chunk, total_frames)
                start_b = offset * sample_bytes
                end_b = end * sample_bytes
                piece = tmp_dir / f"chunk_{idx:04d}.wav"
                with wave.open(str(piece), "wb") as out:
                    out.setnchannels(nchan)
                    out.setsampwidth(width)
                    out.setframerate(rate)
                    out.writeframes(raw[start_b:end_b])
                text_only = model.transcribe(str(piece))
                if isinstance(text_only, (list, tuple)):
                    text_only = " ".join(str(t) for t in text_only)
                text_only = str(text_only).strip()
                if text_only:
                    segments.append(
                        {
                            "start": round(offset / rate, 2),
                            "end": round(end / rate, 2),
                            "text": text_only,
                        }
                    )
                print(f"[{ENGINE}] чанк {idx + 1} {offset / rate:.0f}-{end / rate:.0f}с, символов: {len(text_only)}")
                offset = end
                idx += 1
            mode = "chunk20"
        finally:
            for leftover in tmp_dir.glob("*"):
                leftover.unlink(missing_ok=True)
            tmp_dir.rmdir()

    elapsed = time.time() - started
    text = " ".join(s["text"] for s in segments if s["text"]).strip()

    payload = {
        "engine": ENGINE,
        "model": args.model,
        "revision": args.revision,
        "device": device,
        "mode": mode,
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

    print(f"[{ENGINE}] распознано за {elapsed:.1f} с ({mode}), символов: {len(text)}")
    print(f"[{ENGINE}] {out_dir / f'{stem}_raw_{ENGINE}.txt'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
