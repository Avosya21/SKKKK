"""Шаг 3. Сравнение поиска: BGE-M3 (как в портале) против EmbeddingGemma 2.

Эталон — out/gold/questions_gold.json (собирается gold_tools.py build из out/gold/spec_*.json):
правильные фрагменты найдены по цитатам памяти решений и шаблонам, без обеих моделей;
кадры размечены глазами по листам миниатюр. Категории вопросов:

  fact   — точный факт, число, срок из документа или переписки
  para   — перефраз: в вопросе нет ключевых слов ответа
  evol   — тема менялась во времени, ответ в нескольких материалах
  speech — разговорная речь созвона (ASR, жаргон, оговорки)
  term   — термины, аббревиатуры, английские слова
  trap   — ответа в проекте нет: проверяем, отличает ли модель «нет ответа» по уверенности
  frame  — кадр демонстрации экрана

    ...\\python.exe evaluate.py --dim 768            # текст + ловушки + кадры
    ...\\python.exe evaluate.py --dim 256 --no-frames
    ...\\python.exe evaluate.py --bge-frames         # + BGE-M3 по описаниям кадров (1 341 запрос в Ollama портала)

Результат в out/: eval_<время>.md / .json, side_by_side_<время>.md, blind_<время>.md + blind_key_<время>.json.
"""
from __future__ import annotations

import argparse
import json
import random
import time
from collections import defaultdict
from datetime import datetime

import httpx
import numpy as np

from common import OUT, load_model, ollama_url, read_jsonl

GOLD_FILE = OUT / "gold" / "questions_gold.json"
KS = (1, 3, 5, 10)
CATS = ["fact", "para", "evol", "speech", "term"]


def normalize(m: np.ndarray) -> np.ndarray:
    m = np.asarray(m, dtype=np.float32)
    return m / np.clip(np.linalg.norm(m, axis=-1, keepdims=True), 1e-12, None)


def bge_embed(texts: list[str]) -> np.ndarray:
    out = []
    with httpx.Client(timeout=300) as cl:
        for s in range(0, len(texts), 32):
            r = cl.post(f"{ollama_url()}/api/embed", json={"model": "bge-m3", "input": texts[s:s + 32]})
            r.raise_for_status()
            out += r.json()["embeddings"]
    return normalize(np.asarray(out))


def first_rank(scores: np.ndarray, relevant: np.ndarray) -> int | None:
    hits = np.nonzero(relevant[np.argsort(-scores)])[0]
    return int(hits[0]) + 1 if len(hits) else None


def summarize(ranks: list[int | None]) -> dict:
    n = max(1, len(ranks))
    res = {f"R@{k}": sum(1 for r in ranks if r and r <= k) / n for k in KS}
    res["MRR"] = sum(1 / r for r in ranks if r) / n
    res["n"] = len(ranks)
    return res


def auc(pos: list[float], neg: list[float]) -> float:
    """Вероятность, что у вопроса с ответом лучший балл выше, чем у вопроса-ловушки."""
    if not pos or not neg:
        return float("nan")
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dim", type=int, default=768, choices=[768, 512, 256, 128])
    ap.add_argument("--device", default="auto")
    ap.add_argument("--no-frames", action="store_true")
    ap.add_argument("--bge-frames", action="store_true")
    ap.add_argument("--blind", type=int, default=15, help="вопросов в слепую проверку (0 — не делать)")
    args = ap.parse_args()

    gold = json.loads(GOLD_FILE.read_text(encoding="utf-8"))
    chunks = read_jsonl(OUT / "chunks.jsonl")
    by_id = {c["id"]: c for c in chunks}

    bge_ids = np.load(OUT / "bge_ids.npy").tolist()
    bge_vecs = normalize(np.load(OUT / "bge_vecs.npy"))
    g_ids = np.load(OUT / "gemma_chunk_ids.npy").tolist()
    g_vecs = normalize(np.load(OUT / f"gemma_chunks_{args.dim}.npy"))
    common = sorted(set(bge_ids) & set(g_ids) & set(by_id))
    bpos = {c: i for i, c in enumerate(bge_ids)}
    gpos = {c: i for i, c in enumerate(g_ids)}

    text_q = [q for q in gold if q["cat"] != "frame"]
    frame_q = [q for q in gold if q["cat"] == "frame"]
    model = load_model(args.device, modalities="text")

    t0 = time.time()
    qg = normalize(model.encode([q["q"] for q in gold], prompt_name="SearchQuery", truncate_dim=args.dim))
    t_g = time.time() - t0
    t0 = time.time()
    qb = bge_embed([q["q"] for q in gold])
    t_b = time.time() - t0
    qidx = {q["id"]: i for i, q in enumerate(gold)}

    rows, side = [], []
    for q in text_q:
        i = qidx[q["id"]]
        cids = [c for c in common if by_id[c]["project_id"] == q["project_id"]]
        sb = bge_vecs[[bpos[c] for c in cids]] @ qb[i]
        sg = g_vecs[[gpos[c] for c in cids]] @ qg[i]
        row = {"id": q["id"], "cat": q["cat"], "project_id": q["project_id"], "q": q["q"], "n_chunks": len(cids),
               "top_bge": float(sb.max()), "top_gemma": float(sg.max())}
        if q["cat"] != "trap":
            relevant = np.array([c in set(q["chunk_ids"]) for c in cids])
            row["n_gold"] = int(relevant.sum())
            if not relevant.any():
                row["skip"] = "правильных фрагментов нет в индексе"
            else:
                row["rank_bge"] = first_rank(sb, relevant)
                row["rank_gemma"] = first_rank(sg, relevant)
                side.append((q, cids, relevant, sb, sg))
        rows.append(row)

    scored = [r for r in rows if "rank_bge" in r]
    summary: dict = {"dim": args.dim, "query_seconds": {"bge": round(t_b, 2), "gemma": round(t_g, 2)}}
    for model_name, key in (("bge-m3", "rank_bge"), (f"gemma2@{args.dim}", "rank_gemma")):
        summary[model_name] = {"всего": summarize([r[key] for r in scored])}
        for cat in CATS:
            summary[model_name][cat] = summarize([r[key] for r in scored if r["cat"] == cat])
    traps = [r for r in rows if r["cat"] == "trap"]
    summary["ловушки_AUC"] = {
        "bge-m3": auc([r["top_bge"] for r in scored], [r["top_bge"] for r in traps]),
        f"gemma2@{args.dim}": auc([r["top_gemma"] for r in scored], [r["top_gemma"] for r in traps]),
    }
    wins = sum(1 for r in scored if (r["rank_gemma"] or 999) < (r["rank_bge"] or 999))
    losses = sum(1 for r in scored if (r["rank_gemma"] or 999) > (r["rank_bge"] or 999))
    summary["по_вопросам"] = {"gemma_лучше": wins, "bge_лучше": losses, "поровну": len(scored) - wins - losses}

    frame_rows = []
    if frame_q and not args.no_frames:
        frames = read_jsonl(OUT / "frames.jsonl")
        img = normalize(np.load(OUT / f"gemma_frames_img_{args.dim}.npy"))
        txt = normalize(np.load(OUT / f"gemma_frames_txt_{args.dim}.npy"))
        fb = bge_embed([f["text"] or " " for f in frames]) if args.bge_frames else None
        for q in frame_q:
            i = qidx[q["id"]]
            keep = [k for k, f in enumerate(frames) if f["project_id"] == q["project_id"]]
            relevant = np.array([frames[k]["frame_key"] in set(q["frame_keys"]) for k in keep])
            s_img, s_txt = img[keep] @ qg[i], txt[keep] @ qg[i]
            r = {"id": q["id"], "project_id": q["project_id"], "q": q["q"], "n_frames": len(keep), "n_gold": int(relevant.sum()),
                 "rank_img": first_rank(s_img, relevant), "rank_txt": first_rank(s_txt, relevant),
                 "rank_sum": first_rank(s_img + s_txt, relevant)}
            if fb is not None:
                r["rank_bge_txt"] = first_rank(fb[keep] @ qb[i], relevant)
            frame_rows.append(r)
        summary["кадры"] = {name: summarize([r[k] for r in frame_rows])
                            for name, k in (("gemma: картинка", "rank_img"), ("gemma: описание", "rank_txt"),
                                            ("gemma: сумма", "rank_sum"), ("bge: описание", "rank_bge_txt"))
                            if not frame_rows or k in frame_rows[0]}

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    (OUT / f"eval_{stamp}.json").write_text(
        json.dumps({"summary": summary, "rows": rows, "frames": frame_rows}, ensure_ascii=False, indent=1), encoding="utf-8")

    gname = f"gemma2@{args.dim}"
    md = [f"# Сравнение поиска: BGE-M3 и EmbeddingGemma 2 @{args.dim} ({stamp})", "",
          f"Вопросов с ответом: {len(scored)}, ловушек: {len(traps)}, кадров: {len(frame_rows)}. "
          f"Пропущено (нет в индексе): {[r['id'] for r in rows if 'skip' in r]}", "",
          "## Текст", "", "| Категория | n | BGE R@1 | Gemma R@1 | BGE R@5 | Gemma R@5 | BGE MRR | Gemma MRR |", "|---|---|---|---|---|---|---|---|"]
    for cat in ["всего"] + CATS:
        b, g = summary["bge-m3"][cat], summary[gname][cat]
        md.append(f"| {cat} | {b['n']} | {b['R@1']:.2f} | {g['R@1']:.2f} | {b['R@5']:.2f} | {g['R@5']:.2f} | {b['MRR']:.3f} | {g['MRR']:.3f} |")
    pv = summary["по_вопросам"]
    md += ["", f"По вопросам: Gemma выше — {pv['gemma_лучше']}, BGE выше — {pv['bge_лучше']}, поровну — {pv['поровну']}.",
           f"Ловушки (AUC, 1.0 — уверенность всегда ниже, чем при настоящем ответе): BGE {summary['ловушки_AUC']['bge-m3']:.2f}, "
           f"Gemma {summary['ловушки_AUC'][gname]:.2f}.",
           f"Время на {len(gold)} вопросов: BGE {t_b:.1f} с (Ollama), Gemma {t_g:.1f} с.", ""]
    if frame_rows:
        md += ["## Кадры", "", "| Способ | R@1 | R@3 | R@5 | MRR |", "|---|---|---|---|---|"]
        for name, s in summary["кадры"].items():
            md.append(f"| {name} | {s['R@1']:.2f} | {s['R@3']:.2f} | {s['R@5']:.2f} | {s['MRR']:.3f} |")
        md.append("")
    md += ["## По вопросам", "", "| Вопрос | Кат. | Пр. | Правильных / всего | BGE | Gemma |", "|---|---|---|---|---|---|"]
    for r in rows:
        if r["cat"] == "trap":
            md.append(f"| {r['id']} | trap | {r['project_id']} | — / {r['n_chunks']} | {r['top_bge']:.3f} | {r['top_gemma']:.3f} |")
        elif "skip" in r:
            md.append(f"| {r['id']} | {r['cat']} | {r['project_id']} | 0 / {r['n_chunks']} | — | — |")
        else:
            md.append(f"| {r['id']} | {r['cat']} | {r['project_id']} | {r['n_gold']} / {r['n_chunks']} | {r['rank_bge']} | {r['rank_gemma']} |")
    for r in frame_rows:
        md.append(f"| {r['id']} | frame | {r['project_id']} | {r['n_gold']} / {r['n_frames']} | "
                  f"{r.get('rank_bge_txt', '—')} | img {r['rank_img']} · txt {r['rank_txt']} · sum {r['rank_sum']} |")
    (OUT / f"eval_{stamp}.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    sbs = []
    for q, cids, relevant, sb, sg in side:
        sbs += [f"## {q['id']} [{q['cat']}]: {q['q']}", ""]
        for label, s in (("BGE-M3", sb), ("Gemma 2", sg)):
            sbs.append(f"**{label}**")
            for j in np.argsort(-s)[:3]:
                text = by_id[cids[j]]["text"].replace("\n", " ")[:240]
                sbs.append(f"- {'✔' if relevant[j] else '·'} {s[j]:.3f} — {text}")
            sbs.append("")
    (OUT / f"side_by_side_{stamp}.md").write_text("\n".join(sbs), encoding="utf-8")

    if args.blind:
        # Слепая проверка: первые 3 фрагмента обеих моделей вперемешку, без подписи модели.
        rnd = random.Random(42)
        picked = rnd.sample(side, min(args.blind, len(side)))
        lines = ["# Слепая проверка поиска", "",
                 "У каждого фрагмента поставьте `+` (отвечает на вопрос), `~` (по теме, но ответа нет) или `-` (мимо) вместо `?`.",
                 "Какая модель нашла фрагмент — скрыто, ключ в отдельном файле.", ""]
        key = {}
        for q, cids, _rel, sb, sg in picked:
            cand = {}
            for label, s in (("bge", sb), ("gemma", sg)):
                for j in np.argsort(-s)[:3]:
                    cand.setdefault(cids[j], set()).add(label)
            items = list(cand.items())
            rnd.shuffle(items)
            lines += [f"## {q['id']}: {q['q']}", ""]
            for n, (cid, who) in enumerate(items, 1):
                tag = f"{q['id']}#{n}"
                key[tag] = {"chunk_id": cid, "found_by": sorted(who)}
                lines.append(f"- [?] {tag} — {by_id[cid]['text'].replace(chr(10), ' ')[:400]}")
            lines.append("")
        (OUT / f"blind_{stamp}.md").write_text("\n".join(lines), encoding="utf-8")
        (OUT / f"blind_key_{stamp}.json").write_text(json.dumps(key, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\n".join(md[:16]))
    print(f"[eval] out/eval_{stamp}.md")


if __name__ == "__main__":
    main()
