"""Шаг 4. Поиск по кадрам демонстрации экрана: картинка против описания vision-модели.

Разметки для кадров нет, поэтому смотрим глазами: на каждый запрос три колонки по 5 кадров —
по самой картинке (Gemma), по тексту описания (Gemma) и по их сумме.

    ...\\python.exe search_frames.py "схема авторизации" "таблица остатков" --project 18

Результат: out/frames_<время>.html (миниатюры ссылаются на jpg портала, файлы не копируются).
"""
from __future__ import annotations

import argparse
import html
from datetime import datetime
from pathlib import Path

import numpy as np

from common import OUT, load_model, read_jsonl


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("queries", nargs="+")
    ap.add_argument("--project", type=int, default=None)
    ap.add_argument("--dim", type=int, default=768)
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--device", default="auto")
    args = ap.parse_args()

    frames = read_jsonl(OUT / "frames.jsonl")
    img = np.load(OUT / f"gemma_frames_img_{args.dim}.npy")
    txt = np.load(OUT / f"gemma_frames_txt_{args.dim}.npy")
    keep = [i for i, f in enumerate(frames) if args.project is None or f["project_id"] == args.project]

    model = load_model(args.device, modalities="text")
    qv = model.encode(args.queries, prompt_name="SearchQuery", truncate_dim=args.dim, normalize_embeddings=True)

    parts = ["<!doctype html><meta charset=utf-8><title>Кадры</title>",
             "<style>body{font:14px sans-serif;margin:16px}td{vertical-align:top;width:33%}"
             "img{max-width:100%;border:1px solid #ccc}p{margin:2px 0 14px;color:#555}</style>"]
    for qi, q in enumerate(args.queries):
        s_img, s_txt = img[keep] @ qv[qi], txt[keep] @ qv[qi]
        cols = {"Картинка": s_img, "Описание": s_txt, "Сумма": s_img + s_txt}
        parts.append(f"<h2>{html.escape(q)}</h2><table><tr>" + "".join(f"<th>{c}</th>" for c in cols) + "</tr><tr>")
        for scores in cols.values():
            parts.append("<td>")
            for j in np.argsort(-scores)[: args.top]:
                f = frames[keep[j]]
                uri = Path(f["jpg"]).as_uri()
                parts.append(f"<img src='{uri}'><p>{scores[j]:.3f} · пр.{f['project_id']} · "
                             f"{html.escape(f['frame_key'])} · {html.escape(f['text'][:160])}</p>")
            parts.append("</td>")
        parts.append("</tr></table>")
    out = OUT / f"frames_{datetime.now():%Y%m%d_%H%M%S}.html"
    out.write_text("\n".join(parts), encoding="utf-8")
    print(f"[frames] {out}")


if __name__ == "__main__":
    main()
