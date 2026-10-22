"""Шаг 2. Векторы EmbeddingGemma 2 для фрагментов и кадров.

    ...\\python.exe embed.py chunks              # тексты фрагментов (приставка Document)
    ...\\python.exe embed.py frames              # кадры: картинка и описание отдельно
    опции: --device auto|cpu|cuda  --threads 4  --dim 768|512|256|128

Результат в out/: gemma_chunks_<dim>.npy (+ ids), gemma_frames_img_<dim>.npy, gemma_frames_txt_<dim>.npy.
"""
from __future__ import annotations

import argparse
import time

import numpy as np

from common import OUT, load_model, read_jsonl


def embed_chunks(args) -> None:
    rows = read_jsonl(OUT / "chunks.jsonl")
    model = load_model(args.device, modalities="text", threads=args.threads)
    t0 = time.time()
    vecs = model.encode(
        [r["text"] for r in rows],
        prompt_name="Document",
        batch_size=args.batch,
        truncate_dim=args.dim,
        normalize_embeddings=True,
        show_progress_bar=True,
    )
    np.save(OUT / f"gemma_chunks_{args.dim}.npy", vecs.astype(np.float32))
    np.save(OUT / "gemma_chunk_ids.npy", np.array([r["id"] for r in rows], dtype=np.int64))
    print(f"[embed] chunks {vecs.shape} за {time.time() - t0:.0f} с")


def embed_frames(args) -> None:
    from PIL import Image

    rows = read_jsonl(OUT / "frames.jsonl")
    model = load_model(args.device, modalities="image", threads=args.threads)
    t0 = time.time()
    imgs = []
    for r in rows:
        with Image.open(r["jpg"]) as im:
            imgs.append(im.convert("RGB"))
    img_vecs = model.encode(
        [{"image": im} for im in imgs],
        batch_size=max(1, args.batch // 8),
        truncate_dim=args.dim,
        normalize_embeddings=True,
        show_progress_bar=True,
    )
    txt_vecs = model.encode(
        [r["text"] or " " for r in rows],
        prompt_name="Document",
        batch_size=args.batch,
        truncate_dim=args.dim,
        normalize_embeddings=True,
        show_progress_bar=True,
    )
    np.save(OUT / f"gemma_frames_img_{args.dim}.npy", np.asarray(img_vecs, dtype=np.float32))
    np.save(OUT / f"gemma_frames_txt_{args.dim}.npy", np.asarray(txt_vecs, dtype=np.float32))
    print(f"[embed] frames {len(rows)} за {time.time() - t0:.0f} с")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["chunks", "frames"])
    ap.add_argument("--device", default="auto")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--dim", type=int, default=768, choices=[768, 512, 256, 128])
    args = ap.parse_args()
    {"chunks": embed_chunks, "frames": embed_frames}[args.what](args)


if __name__ == "__main__":
    main()
