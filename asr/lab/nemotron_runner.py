"""Nemotron 3 Diarization в отдельном процессе ``venvs\\nemo-diar`` (NeMo + torch).

Лаборатория (``venvs\\lab``) не держит NeMo и torch, поэтому стадия ``03_diar``
вызывает этот файл отдельным процессом и забирает только матрицу вероятностей.

На выходе — ``(T, 8)`` float32 с шагом 80 мс: нативный выход модели (10 мс)
усредняется по 8 кадрам — так же, как это делает ``downsample_preds`` NeMo.
Число кадров совпадает с Sortformer 4spk на той же записи.

Три обхода под нашу машину (Windows, NeMo 3.1.0+a95ea79, Python 3.10.9):

1. Python 3.10.9 не знает ``tarfile.extract(filter=)``, а NeMo распаковывает
   ``.nemo`` с ним — аргумент отбрасывается.
2. Конфиг модели ссылается на классы потерь из более нового NeMo
   (``BCEWithLogitsLoss``, ``ActivityLoss``, ``PhantomLoss``). Для инференса
   потери не нужны: подставляется ``BCELoss``, лишние убираются.
3. ``flex_attention`` компилируется через Triton, которого на Windows нет.
   Маска там — только паддинг, поэтому ``scaled_dot_product_attention`` с той же
   маской даёт тот же результат.

Внутренние параметры кеша спикеров (пороги, boost, обучаемый вектор тишины)
берутся из модели как есть: с ними она обучена.

Точность — bf16, как во всех замерах NVIDIA. fp32 на созвоне проекта 35
(2026-09-25) слил с основным спикером человека с 9.6 с речи, которого bf16
выделил отдельно и который подтверждён на слух.
"""

from __future__ import annotations

import argparse
import json
import sys
import tarfile
import tempfile
import time
from pathlib import Path

# Офлайн-геометрия из карточки модели: chunk, right_context, fifo, update_period, spkcache.
OFFLINE = {"chunk_len": 340, "chunk_right_context": 40, "fifo_len": 40, "spkcache_update_period": 300, "spkcache_len": 264}
POOL = 8  # 10 мс -> 80 мс


def _patch_tarfile() -> None:
    original = tarfile.TarFile.extract

    def extract(self, member, path="", set_attrs=True, *, numeric_owner=False, filter=None):  # noqa: A002
        return original(self, member, path, set_attrs, numeric_owner=numeric_owner)

    tarfile.TarFile.extract = extract


def _patch_attention() -> None:
    import torch.nn.functional as F
    from torch.nn.attention.flex_attention import create_mask

    import nemo.collections.asr.modules.transformer_encoder as encoder

    def dense_mask(mask_mod, B, H, Q_LEN, KV_LEN, device):  # noqa: N803 - имена как у torch
        return create_mask(mask_mod, B, H, Q_LEN, KV_LEN, device)

    def sdpa(q, k, v, block_mask=None, score_mod=None):
        if score_mod is not None:
            raise RuntimeError("score_mod не поддержан обходом SDPA (нужен rope, а не rel_pos)")
        return F.scaled_dot_product_attention(q, k, v, attn_mask=block_mask)

    encoder.create_block_mask = dense_mask
    encoder.flex_attention_compiled = sdpa
    encoder.flex_attention = sdpa


def _compat_config(nemo_path: Path, out_dir: Path) -> Path:
    from omegaconf import OmegaConf

    with tarfile.open(nemo_path) as archive:
        member = next(m for m in archive.getmembers() if m.name.endswith("model_config.yaml"))
        config = OmegaConf.create(archive.extractfile(member).read().decode("utf-8"))
    config.loss = {"_target_": "nemo.collections.asr.losses.bce_loss.BCELoss", "reduction": "mean"}
    for key in ("activity_loss", "phantom_loss"):
        config.pop(key, None)
    path = out_dir / "model_config_compat.yaml"
    OmegaConf.save(config, path)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--wav", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path, help="куда положить .npy (T, 8) 80 мс")
    parser.add_argument("--meta", required=True, type=Path, help="куда положить JSON с параметрами прогона")
    parser.add_argument("--device", default="auto", choices=("auto", "cuda", "cpu"))
    parser.add_argument(
        "--precision",
        default="bf16",
        choices=("bf16", "fp32"),
        help="bf16 — протокол замеров NVIDIA; на CPU всегда fp32",
    )
    args = parser.parse_args()

    _patch_tarfile()
    import numpy as np
    import torch

    if args.device == "cuda" and not torch.cuda.is_available():
        print("CUDA недоступна в venvs\\nemo-diar, а запрошена явно", file=sys.stderr)
        return 5
    device = "cuda" if args.device != "cpu" and torch.cuda.is_available() else "cpu"

    from nemo.collections.asr.models import SortformerEncLabelModel

    _patch_attention()
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        config = _compat_config(args.model, Path(tmp))
        model = SortformerEncLabelModel.restore_from(
            restore_path=str(args.model), map_location=device, strict=False, override_config_path=str(config)
        )
    model.eval()
    modules = model.sortformer_modules
    for name, value in OFFLINE.items():
        setattr(modules, name, value)
    model._check_streaming_parameters()
    loaded = time.monotonic()

    precision = args.precision if device == "cuda" else "fp32"
    autocast = torch.autocast("cuda", dtype=torch.bfloat16, enabled=precision == "bf16")
    with torch.inference_mode(), autocast:
        _, probs = model.diarize(audio=[str(args.wav)], batch_size=1, include_tensor_outputs=True, verbose=False)
    native = probs[0].squeeze(0).float().cpu().numpy().astype(np.float32)
    frames = native.shape[0]
    whole = (frames // POOL) * POOL
    pooled = native[:whole].reshape(-1, POOL, native.shape[1]).mean(axis=1)
    if frames > whole:
        pooled = np.concatenate([pooled, native[whole:].mean(axis=0, keepdims=True)])
    pooled = np.ascontiguousarray(pooled, dtype=np.float32)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    np.save(args.out, pooled)
    import nemo

    meta = {
        "device": device,
        "gpu": torch.cuda.get_device_name(0) if device == "cuda" else None,
        "precision": precision,
        "streaming": OFFLINE,
        "high_resolution": bool(model.high_resolution),
        "native_frame_sec": 0.01,
        "native_frames": int(frames),
        "frame_sec": 0.08,
        "frames": int(pooled.shape[0]),
        "num_speakers": int(pooled.shape[1]),
        "load_sec": round(loaded - started, 2),
        "infer_sec": round(time.monotonic() - loaded, 2),
        "nemo": nemo.__version__,
        "torch": torch.__version__,
    }
    args.meta.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
