"""Помощники для сборки эталонного набора вопросов (out/gold/).

Правильные фрагменты ищутся без обеих сравниваемых моделей: по цитатам из памяти решений
(decision_events.quote) и по текстовым шаблонам. Так эталон не подыгрывает ни BGE-M3, ни Gemma.

    python gold_tools.py digest 18            # темы проекта: заголовок, статус, цитаты -> out/gold/digest_18.md
    python gold_tools.py grep 18 "коннектор" "тестов\\w+ контур"   # фрагменты, где есть все шаблоны
    python gold_tools.py build                # out/gold/spec_*.json -> out/gold/questions_gold.json + отчёт
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys

from common import OUT, SNAPSHOT_DB, read_jsonl

GOLD = OUT / "gold"


def db() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{SNAPSHOT_DB}?mode=ro", uri=True)


def norm(s: str) -> str:
    s = s.lower().replace("ё", "е")
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def digest(project_id: int) -> None:
    c = db()
    subs = c.execute(
        """SELECT s.id, s.title, s.state, s.summary, count(e.id), count(DISTINCT e.artifact_id)
           FROM subjects s JOIN decision_events e ON e.subject_id = s.id AND e.is_active
           WHERE s.project_id = ? AND s.is_active GROUP BY s.id ORDER BY 6 DESC, 5 DESC""",
        (project_id,),
    ).fetchall()
    arts = dict(c.execute("SELECT id, source_type || '/' || file_type FROM artifacts WHERE project_id = ?", (project_id,)))
    lines = [f"# Проект {project_id}: {len(subs)} тем", ""]
    for sid, title, state, summary, n, na in subs:
        lines.append(f"## S{sid} [{state}] {title}  (событий {n}, материалов {na})")
        if summary:
            lines.append(f"> {summary[:300]}")
        for kind, when, aid, anchor, quote in c.execute(
            """SELECT kind, substr(occurred_at,1,10), artifact_id, anchor, quote FROM decision_events
               WHERE subject_id = ? AND is_active AND quote IS NOT NULL ORDER BY occurred_at LIMIT 6""",
            (sid,),
        ):
            lines.append(f"- {when} {kind} a{aid}({arts.get(aid, '?')}) {anchor}: {(quote or '')[:220]}")
        lines.append("")
    GOLD.mkdir(parents=True, exist_ok=True)
    out = GOLD / f"digest_{project_id}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"{out} ({len(subs)} тем)")


def _chunks_of(project_id: int) -> list[dict]:
    return [c for c in read_jsonl(OUT / "chunks.jsonl") if c["project_id"] == project_id]


def match_patterns(chunks: list[dict], patterns: list[str]) -> list[dict]:
    return [c for c in chunks if all(re.search(p, c["text"], re.I) for p in patterns)]


def match_quotes(chunks: list[dict], quotes: list[str]) -> list[dict]:
    """Фрагмент содержит начало или конец цитаты (цитата могла попасть на границу фрагментов)."""
    hits = []
    normed = [(c, norm(c["text"])) for c in chunks]
    for q in quotes:
        nq = norm(q)
        if len(nq) < 12:
            continue
        probes = {nq[:60], nq[-60:]} if len(nq) > 60 else {nq}
        for c, nt in normed:
            if any(p in nt for p in probes) and c not in hits:
                hits.append(c)
    return hits


def grep(project_id: int, patterns: list[str]) -> None:
    for c in match_patterns(_chunks_of(project_id), patterns)[:40]:
        t = c["text"].replace("\n", " ")
        m = re.search(patterns[0], t, re.I)
        s = max(0, (m.start() if m else 0) - 120)
        print(f"#{c['id']} a{c['artifact_id']} [{c.get('speaker') or ''}] …{t[s:s + 320]}…")


def build() -> None:
    """spec_*.json: [{id, cat, project_id, q, quotes?, subjects?, patterns?, extra_ids?, drop_ids?, frames?}]"""
    spec = []
    for f in sorted(GOLD.glob("spec_*.json")):
        spec += json.loads(f.read_text(encoding="utf-8"))
    c = db()
    cache: dict[int, list[dict]] = {}
    out, report = [], []
    for q in spec:
        pid = q["project_id"]
        chunks = cache.setdefault(pid, _chunks_of(pid))
        quotes = list(q.get("quotes", []))
        for sid in q.get("subjects", []):
            quotes += [r[0] for r in c.execute(
                "SELECT quote FROM decision_events WHERE subject_id = ? AND is_active AND quote IS NOT NULL", (sid,))]
        ids = {h["id"] for h in match_quotes(chunks, quotes)}
        for pats in q.get("patterns", []):
            ids |= {h["id"] for h in match_patterns(chunks, pats)}
        ids |= set(q.get("extra_ids", []))
        ids -= set(q.get("drop_ids", []))
        row = {k: q[k] for k in ("id", "cat", "project_id", "q") if k in q}
        if q.get("cat") == "trap":
            row["trap"] = True
        elif q.get("frames"):
            row["frame_keys"] = q["frames"]
        else:
            row["chunk_ids"] = sorted(ids)
        out.append(row)
        n = len(row.get("chunk_ids", row.get("frame_keys", [])))
        flag = "" if row.get("trap") or 0 < n <= 15 else "  <-- ПРОВЕРИТЬ"
        report.append(f"{q['id']:<28} {q.get('cat', ''):<10} p{pid:<3} gold={n}{flag}")
    (GOLD / "questions_gold.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n".join(report))
    print(f"вопросов: {len(out)}")


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "digest":
        for p in sys.argv[2:]:
            digest(int(p))
    elif cmd == "grep":
        grep(int(sys.argv[2]), sys.argv[3:])
    elif cmd == "build":
        build()
