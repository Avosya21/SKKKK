"""Поиск по индексу проекта: гибрид (точный FTS5 + смысловой вектор + точное совпадение символа) и структурные режимы.

Каждый результат — с адресом `путь:строка`. Слабый результат честно помечается `confident=False`.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import time
from typing import Any

from app.app.services.project_index import analyzers, store
from app.app.services.project_index.config import ROOT, list_files, load_config, load_glossary, redact

STOP = {
    "кто", "где", "что", "как", "для", "это", "какие", "какой", "какая", "почему", "зачем", "когда", "или", "при", "над", "под",
    "про", "все", "вся", "всё", "его", "она", "они", "есть", "нам", "нужно", "можно", "будет", "чем", "так", "там", "тут", "ли",
    "the", "and", "for", "how", "what", "where", "who", "why", "with", "from", "that", "this", "does", "are",
    "вызывает", "используется", "создаётся", "создается", "лежит", "находится", "делает", "работает", "открывает",
}
DOC_KINDS = {"doc", "finding", "manifest_node", "ops", "commit"}
CODE_KINDS = {"function", "method", "class", "module", "overview", "js", "js_top", "test", "template", "ui_element", "css", "config",
              "route", "dbtable", "env", "ui_link"}
DEFAULT_EXCLUDED = {"manifest_edge"}
DEFAULT_LIMIT = 8

# Веса ранжирования (подобраны перебором на золотом наборе docs/eval/index_questions.json).
W_VEC = 1.0          # вес смыслового (векторного) поиска
W_FTS = 1.0          # вес точного (FTS5) поиска
W_AND = 0.3          # чанки, где есть все значимые слова запроса
W_INTENT = 3.0       # результаты по намерению («кто вызывает», роут, таблица)
DOC_PENALTY = 0.01   # вопрос про код: документы ниже кода
CODE_BONUS = 0.004
OVERVIEW_BONUS = 0.004 # вопрос простым языком (без имён из кода): обзор модуля выше глубоких фрагментов
FILE_AGG = 0.0         # доля суммы соседних фрагментов того же файла, добавляемая к оценке
NAME_BONUS = 0.0     # имя файла созвучно запросу


def _norm_url(url: str) -> str:
    """`/projects/{project_id}/x` и `/projects/18/x` — один и тот же шаблон."""
    url = re.sub(r"\$?\{[^}]*\}", "{}", url.strip())
    return re.sub(r"/\d+(?=/|$)", "/{}", url)


def _is_ident(token: str) -> bool:
    return bool(re.search(r"[A-Za-z]", token)) and bool(re.search(r"[_./]|[a-z][A-Z]", token))


def _ident_parts(token: str) -> list[str]:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", token)
    return [p.lower() for p in re.split(r"[_.\-/]+", spaced) if p]


def _stem(word: str) -> str:
    w = word.lower()
    if len(w) >= 7:
        return w[:-2] + "*"
    if len(w) >= 5:
        return w[:-1] + "*"
    return w


def parse_query(query: str) -> tuple[list[str], list[str]]:
    """(идентификаторы, обычные слова)."""
    idents: list[str] = []
    words: list[str] = []
    for tok in re.findall(r"[A-Za-zА-Яа-яЁё0-9_$.\-/{}]+", query):
        tok = tok.strip(".-/{}")
        if len(tok) < 3:
            continue
        if _is_ident(tok):
            idents.append(tok)
        elif tok.lower() not in STOP:
            words.append(tok)
    return idents, words


def _stem_prefix(word: str) -> str:
    w = word.lower()
    return w[:-2] if len(w) >= 7 else w[:-1] if len(w) >= 5 else w


def glossary_terms(query: str) -> list[str]:
    """Термины из словаря предметной области: ключ подходит, если каждое его слово совпало с началом слова запроса
    (так «дорожек» находит ключ «дорожка», «участников» — «участник»)."""
    q_words = [w.lower() for w in re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", query)]
    out: list[str] = []
    for key, values in load_glossary().items():
        parts = key.split()
        if all(any(qw.startswith(_stem_prefix(p)) or _stem_prefix(qw) == _stem_prefix(p) for qw in q_words) for p in parts):
            out.extend(values)
    return list(dict.fromkeys(out))


def build_fts(idents: list[str], words: list[str], extra: list[str]) -> str:
    terms: list[str] = []
    for tok in idents:
        parts = _ident_parts(tok)
        if len(parts) >= 2:
            terms.append('"' + " ".join(parts) + '"')
        elif parts:
            terms.append(parts[0])
    for w in words:
        terms.append(_stem(w))
    for e in extra:
        parts = _ident_parts(e) if _is_ident(e) else [e.lower()]
        if len(parts) >= 2:
            terms.append('"' + " ".join(parts) + '"')
        elif parts and len(parts[0]) >= 3:
            terms.append(parts[0])
    clean = [t for t in dict.fromkeys(terms) if re.fullmatch(r'[\w"* ]+', t, re.U)]
    return " OR ".join(clean)


_CALLERS_RE = re.compile(r"(?:кто|где|откуда|что)\s+(?:вызывает|вызывают|использует|используют|вызывается|используется|зовёт|зовет)\s+([A-Za-z_][\w.]*)|"
                         r"([A-Za-z_][\w.]*)\s+(?:вызывается|используется)|callers?\s+of\s+([A-Za-z_][\w.]*)|откуда\s+вызывается\s+([A-Za-z_][\w.]*)", re.I)
_CALLEES_RE = re.compile(r"что\s+вызывает\s+([A-Za-z_][\w.]*)|callees?\s+of\s+([A-Za-z_][\w.]*)", re.I)
_ROUTE_RE = re.compile(r"\b(GET|POST|PUT|DELETE|PATCH)\s+(/[^\s]*)", re.I)
_TABLE_RE = re.compile(r"(?:таблиц\w*|столбец|столбц\w*|table)\s+([a-z_][a-z0-9_]{3,})", re.I)


def _detect_intent(query: str) -> tuple[str, str] | None:
    """Распознать вопросы, на которые лучше отвечает таблица индекса, чем текстовый поиск."""
    m = _CALLEES_RE.search(query)
    if m:
        return "callees", next(g for g in m.groups() if g)
    m = _CALLERS_RE.search(query)
    if m:
        return "callers", next(g for g in m.groups() if g)
    m = _ROUTE_RE.search(query)
    if m:
        return "route", f"{m.group(1).upper()} {m.group(2)}"
    if re.search(r"(?:роут|эндпоинт|endpoint|url)\w*", query, re.I):
        tokens = [t for t in re.findall(r"[A-Za-z][A-Za-z0-9_/\-{}]{3,}", query) if re.search(r"[-_/]", t)]
        if tokens:
            return "route", tokens[-1]
    m = _TABLE_RE.search(query)
    if m:
        return "table", m.group(1)
    return None


def _embed_query(text: str) -> bytes | None:
    try:
        from app.app.config import Settings
        from app.app.services.ollama_client import embed_texts

        vec = embed_texts(Settings(), [text], timeout=20)[0]
        return store.pack(vec)
    except Exception:  # noqa: BLE001 — Ollama недоступна: остаётся точный поиск
        return None


def _row_to_hit(row: sqlite3.Row, score: float, sources: list[str]) -> dict:
    return {
        "id": row["id"], "path": row["path"], "line_start": row["line_start"], "line_end": row["line_end"],
        "symbol": row["symbol"], "kind": row["kind"], "title": row["title"], "text": redact(row["text"]),
        "score": round(score, 4), "sources": sources,
    }


RERANK_CANDIDATES = 12


def llm_rerank(query: str, hits: list[dict]) -> list[dict]:
    """Переупорядочить верх выдачи локальной LLM (LM Studio). Только по запросу (`rerank=True`): секунды и загруженная модель.

    Модель видит вопрос и краткие описания кандидатов и возвращает номера от самого подходящего; при любой ошибке порядок
    остаётся прежним. Порядок хвоста (за пределами кандидатов) не меняется."""
    import json as _json

    head, tail = hits[:RERANK_CANDIDATES], hits[RERANK_CANDIDATES:]
    if len(head) < 3:
        return hits
    try:
        from app.app.config import Settings
        from app.app.services.llm import chat_completion_no_think

        lines = []
        for i, h in enumerate(head, 1):
            body = " ".join(h["text"].split())[:260]
            lines.append(f"[{i}] {h['path']}:{h['line_start']} {h['symbol'] or h['title']} ({h['kind']}): {body}")
        system = ("Ты помогаешь разработчику найти в коде проекта нужное место. Тебе дают вопрос и пронумерованные фрагменты. "
                  "Выбери фрагменты, которые лучше всего отвечают на вопрос. Ответ — только JSON вида {\"best\": [3, 1, 7]}: "
                  "номера от самого подходящего, не больше 6.")
        user = f"Вопрос: {query}\n\nФрагменты:\n" + "\n".join(lines)
        text = chat_completion_no_think(Settings(), system=system, user=user, temperature=0.0, max_tokens=60, timeout=60)
        start, end = text.find("{"), text.rfind("}")
        order = [int(n) for n in _json.loads(text[start:end + 1]).get("best", []) if isinstance(n, int) and 1 <= n <= len(head)]
    except Exception:  # noqa: BLE001 — переупорядочивание необязательно: при сбое остаётся гибридный порядок
        return hits
    seen: list[int] = []
    for n in order:
        if n not in seen:
            seen.append(n)
    ranked = [head[n - 1] for n in seen] + [h for i, h in enumerate(head, 1) if i not in seen]
    for h in ranked[: len(seen)]:
        h["sources"] = h["sources"] + ["rerank"] if "rerank" not in h["sources"] else h["sources"]
    return ranked + tail


def search(query: str, *, limit: int = DEFAULT_LIMIT, kinds: set[str] | None = None, path_filter: str | None = None,
           doc_only: bool = False, code_only: bool = False, conn: sqlite3.Connection | None = None,
           use_vectors: bool = True, notes: bool = False, rerank: bool = False) -> dict[str, Any]:
    """Гибридный поиск. Возвращает {'hits': [...], 'confident': bool, 'notes': [...]}."""
    own = conn is None
    conn = conn or store.connect()
    try:
        wanted = max(limit, RERANK_CANDIDATES) if rerank else limit
        result = _search(conn, query, wanted, kinds, path_filter, doc_only, code_only, use_vectors, notes)
        if rerank:
            result["hits"] = llm_rerank(query, result["hits"])[:limit]
        return result
    finally:
        if own:
            conn.close()


def _search(conn, query, limit, kinds, path_filter, doc_only, code_only, use_vectors, with_notes) -> dict[str, Any]:
    idents, words = parse_query(query)
    extra = glossary_terms(query)
    fts = build_fts(idents, words, extra)
    allowed: set[str] | None = kinds
    if doc_only:
        allowed = DOC_KINDS
    elif code_only:
        allowed = CODE_KINDS

    def ok(kind: str, path: str) -> bool:
        if allowed is not None and kind not in allowed:
            return False
        if allowed is None and kind in DEFAULT_EXCLUDED:
            return False
        return not path_filter or path_filter.lower() in path.lower()

    fused: dict[int, float] = {}
    sources: dict[int, list[str]] = {}
    rows_by_id: dict[int, sqlite3.Row] = {}
    notes: list[str] = []
    vec_hits = 0

    def add(chunk_id: int, weight: float, rank: int, source: str) -> None:
        fused[chunk_id] = fused.get(chunk_id, 0.0) + weight / (60 + rank)
        sources.setdefault(chunk_id, [])
        if source not in sources[chunk_id]:
            sources[chunk_id].append(source)

    def fetch(ids: list[int]) -> None:
        need = [i for i in ids if i not in rows_by_id]
        for i in range(0, len(need), 500):
            part = need[i:i + 500]
            for r in conn.execute(f"SELECT * FROM chunks WHERE id IN ({','.join('?' * len(part))})", part):
                rows_by_id[r["id"]] = r

    # 1. точное совпадение символа по идентификатору из запроса (для вопросов о вызовах определение не нужно первым)
    early_intent = _detect_intent(query)
    skip_symbol = early_intent[1] if early_intent and early_intent[0] in {"callers", "callees"} else None
    for tok in idents:
        if skip_symbol and tok == skip_symbol:
            continue
        name = tok.strip("_") if False else tok
        last = name.rsplit(".", 1)[-1]
        rows = conn.execute(
            "SELECT s.path, s.qualname, c.id FROM symbols s JOIN chunks c ON c.path=s.path AND c.symbol=s.qualname "
            "WHERE s.name=? OR s.qualname=? OR s.qualname LIKE ? ORDER BY c.line_start LIMIT 8",
            (last, name, f"%.{last}")).fetchall()
        for rank, r in enumerate(rows, 1):
            add(r["id"], 3.0, rank, "symbol")
    # 1b. намерение запроса: связи из таблиц индекса важнее текстового сходства
    def chunk_ids(path: str, symbol: str) -> list[int]:
        return [r[0] for r in conn.execute("SELECT id FROM chunks WHERE path=? AND symbol=? ORDER BY line_start LIMIT 2", (path, symbol))]

    intent = _detect_intent(query)
    if intent:
        kind, arg = intent
        if kind == "callers":
            seen_pairs: set[tuple[str, str]] = set()
            rank = 0
            for r in callers_of(conn, arg):
                pair = (r["path"], r["caller"])
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                for cid in chunk_ids(*pair):
                    rank += 1
                    add(cid, W_INTENT, rank, "callers")
        elif kind == "route":
            for rank, r in enumerate(routes_info(conn, arg)[:8], 1):
                for cid in chunk_ids(r["path"], r["handler"]):
                    add(cid, W_INTENT, rank, "route")
                for row in conn.execute("SELECT id FROM chunks WHERE kind='route' AND path=? AND symbol=?", (r["path"], r["handler"])):
                    add(row[0], W_INTENT, rank, "route")
        elif kind == "table":
            info = table_info(conn, arg)
            rank = 0
            for a in info.get("access", [])[:12]:
                for cid in chunk_ids(a["path"], a["symbol"]):
                    rank += 1
                    add(cid, 2.0, rank, "table")
            for row in conn.execute("SELECT id FROM chunks WHERE kind='dbtable' AND title=?", (info.get("table", arg),)):
                add(row[0], 3.0, 1, "table")
        elif kind == "callees":
            rank = 0
            for r in callees_of(conn, arg):
                path, _, line = (r["defined"] or "").rpartition(":")
                if path:
                    for row in conn.execute("SELECT id FROM chunks WHERE path=? AND line_start<=? AND line_end>=? AND kind IN ('function','js') LIMIT 1", (path, int(line or 0), int(line or 0))):
                        rank += 1
                        add(row[0], 2.0, rank, "callees")
    # 2a. все значимые слова сразу (точнее, чем OR со словарными добавками)
    core = [_stem(w) for w in words if len(w) >= 4] + [('"' + " ".join(_ident_parts(t)) + '"') if len(_ident_parts(t)) >= 2 else _ident_parts(t)[0] for t in idents if _ident_parts(t)]
    core = [t for t in dict.fromkeys(core) if t and re.fullmatch(r'[\w"* ]+', t, re.U)]
    if len(core) >= 2:
        try:
            arows = conn.execute("SELECT rowid, bm25(chunks_fts, 4.0, 8.0, 2.0, 1.0, 1.0) AS s FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY s LIMIT 40",
                                 (" AND ".join(core),)).fetchall()
        except sqlite3.OperationalError:
            arows = []
        for rank, r in enumerate(arows, 1):
            add(r["rowid"], W_AND, rank, "fts_all")
    # 2. FTS
    if fts:
        try:
            frows = conn.execute(
                "SELECT rowid, bm25(chunks_fts, 4.0, 8.0, 2.0, 1.0, 1.0) AS s FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY s LIMIT 80",
                (fts,)).fetchall()
        except sqlite3.OperationalError:
            frows = []
        for rank, r in enumerate(frows, 1):
            add(r["rowid"], W_FTS, rank, "fts")
    # 3. вектор
    if use_vectors:
        qtext = query + (" " + " ".join(extra[:8]) if extra else "")
        blob = _embed_query(qtext)
        if blob is None:
            notes.append("смысловой поиск недоступен (Ollama не отвечает) — только точный")
        else:
            vrows = conn.execute("SELECT chunk_id, distance FROM chunk_vectors WHERE embedding MATCH ? AND k = 60 ORDER BY distance", (blob,)).fetchall()
            for rank, r in enumerate(vrows, 1):
                add(r["chunk_id"], W_VEC, rank, "vector")
                vec_hits += 1
            if vrows:
                sim = 1 - (vrows[0]["distance"] ** 2) / 2
                notes_sim = round(sim, 2)
            else:
                notes_sim = 0.0
    else:
        notes_sim = 0.0

    fetch(list(fused))
    low = query.lower()
    wants_test = "тест" in low or "test" in low
    wants_docs = any(w in low for w in ("почему", "зачем", "решени", "план", "документ", "инвариант", "находк", "как запуст", "где лог", "порт "))
    for cid, r in list(rows_by_id.items()):
        if cid not in fused:
            continue
        if not ok(r["kind"], r["path"]):
            fused.pop(cid)
            continue
        boost = 0.0
        sym = (r["symbol"] or "").lower()
        for tok in idents:
            t = tok.lower()
            if skip_symbol and tok == skip_symbol:
                continue
            if sym == t or sym.endswith("." + t.rsplit(".", 1)[-1]):
                boost += 0.05
            if t in r["path"].lower():
                boost += 0.02
        for w in words:
            if w.lower() in r["path"].lower():
                boost += 0.004
        if r["kind"] == "test" and not wants_test:
            boost -= 0.006
        if r["kind"] in {"manifest_node"}:
            boost -= 0.003
        if not wants_docs:
            if r["kind"] in DOC_KINDS:
                boost -= DOC_PENALTY  # вопрос про код: документы ниже кода
            elif r["kind"] in CODE_KINDS and r["kind"] != "test":
                boost += CODE_BONUS
        base = re.sub(r"\.\w+$", "", r["path"].rsplit("/", 1)[-1]).lower()
        q_stems = {_stem_prefix(p) for tok in idents + words for p in _ident_parts(tok) if len(p) > 3}
        if len(base) > 3 and any(b.startswith(s) or s.startswith(b) for b in _ident_parts(base) if len(b) > 3 for s in q_stems):
            boost += NAME_BONUS  # имя файла созвучно запросу
        fused[cid] += boost

    if OVERVIEW_BONUS and not idents:
        for cid in fused:
            if rows_by_id[cid]["kind"] == "overview":
                fused[cid] += OVERVIEW_BONUS
    if FILE_AGG:
        by_path: dict[str, list[float]] = {}
        for cid, sc in fused.items():
            by_path.setdefault(rows_by_id[cid]["path"], []).append(sc)
        agg = {p: sum(sorted(v, reverse=True)[:4]) for p, v in by_path.items()}
        fused = {cid: sc + FILE_AGG * (agg[rows_by_id[cid]["path"]] - sc) for cid, sc in fused.items()}
    ordered = sorted(fused.items(), key=lambda kv: -kv[1])
    # части одного длинного фрагмента и модуль+функция из одного файла не должны забивать выдачу
    per_path: dict[str, int] = {}
    hits: list[dict] = []
    for cid, score in ordered:
        r = rows_by_id[cid]
        if per_path.get(r["path"], 0) >= 3:
            continue
        per_path[r["path"]] = per_path.get(r["path"], 0) + 1
        hits.append(_row_to_hit(r, score, sources[cid]))
        if len(hits) >= limit:
            break
    confident = bool(hits) and (
        any("symbol" in h["sources"] for h in hits[:3])
        or any(len(h["sources"]) >= 2 for h in hits[:3])
        or (bool(words or idents) and any("fts" in h["sources"] for h in hits[:2]) and notes_sim >= 0.45)
    )
    if not confident:
        notes.append("уверенности нет: нужный файл мог не найтись. Переформулируйте, добавив имена из репозитория "
                     "(функция, файл, id элемента, URL, переменная) или используйте режимы --callers / --route / --symbol")
    if with_notes:
        for h in hits[:3]:
            h["related"] = related_notes(conn, h["path"])
    _attach_neighbors(conn, hits)
    return {"hits": hits, "confident": confident, "notes": notes, "fts": fts}


def _attach_neighbors(conn: sqlite3.Connection, hits: list[dict]) -> None:
    for h in hits:
        rows = conn.execute("SELECT symbol, title, line_start FROM chunks WHERE path=? AND kind IN ('function','method','js','class','module','test','css','config','template') "
                            "ORDER BY line_start", (h["path"],)).fetchall()
        prev = nxt = None
        for r in rows:
            if r["line_start"] < (h["line_start"] or 0):
                prev = r
            elif r["line_start"] > (h["line_end"] or 0) and nxt is None:
                nxt = r
        h["neighbors"] = {"prev": f"{prev['symbol'] or prev['title']}:{prev['line_start']}" if prev else None,
                          "next": f"{nxt['symbol'] or nxt['title']}:{nxt['line_start']}" if nxt else None}


def related_notes(conn: sqlite3.Connection, path: str, limit: int = 2) -> list[dict]:
    """Записи из DECISIONS / FINDINGS / документов, которые ссылаются на этот файл."""
    base = path.rsplit("/", 1)[-1]
    parts = _ident_parts(base)
    if not parts:
        return []
    query = '"' + " ".join(parts) + '"'
    try:
        rows = conn.execute(
            "SELECT c.path, c.line_start, c.title FROM chunks_fts f JOIN chunks c ON c.id=f.rowid "
            "WHERE chunks_fts MATCH ? AND c.kind IN ('finding','doc') AND (c.path LIKE 'docs/DECISIONS%' OR c.path LIKE 'docs/FINDINGS%') LIMIT ?",
            (query, limit)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"path": r["path"], "line": r["line_start"], "title": r["title"]} for r in rows]


# ───────────────────────────── Структурные режимы ─────────────────────────────

def symbol_info(conn: sqlite3.Connection, name: str) -> list[dict]:
    last = name.rsplit(".", 1)[-1]
    rows = conn.execute(
        "SELECT * FROM symbols WHERE name=? OR qualname=? OR qualname LIKE ? ORDER BY (name=?) DESC, path LIMIT 30",
        (last, name, f"%.{last}", last)).fetchall()
    if not rows:
        rows = conn.execute("SELECT * FROM symbols WHERE name LIKE ? ORDER BY path LIMIT 30", (f"%{last}%",)).fetchall()
    return [dict(r) for r in rows]


def callers_of(conn: sqlite3.Connection, name: str) -> list[dict]:
    last = name.rsplit(".", 1)[-1]
    rows = conn.execute("SELECT path, caller, line, qualifier, lang FROM calls WHERE callee=? ORDER BY path, line", (last,)).fetchall()
    return [dict(r, ref=(r["qualifier"] == "<ref>")) for r in rows]


def callees_of(conn: sqlite3.Connection, name: str) -> list[dict]:
    last = name.rsplit(".", 1)[-1]
    defs = conn.execute("SELECT path, qualname FROM symbols WHERE name=? OR qualname=? OR qualname LIKE ?", (last, name, f"%.{last}")).fetchall()
    out: list[dict] = []
    for d in defs[:6]:
        rows = conn.execute("SELECT callee, qualifier, COUNT(*) n, MIN(line) line FROM calls WHERE path=? AND caller=? GROUP BY callee, qualifier ORDER BY line",
                            (d["path"], d["qualname"])).fetchall()
        for r in rows:
            target = conn.execute("SELECT path, line FROM symbols WHERE name=? AND kind!='module' LIMIT 1", (r["callee"],)).fetchone()
            out.append({"in": f"{d['path']}::{d['qualname']}", "callee": r["callee"], "qualifier": r["qualifier"], "line": r["line"],
                        "defined": f"{target['path']}:{target['line']}" if target else None, "count": r["n"]})
    return out


def imports_info(conn: sqlite3.Connection, target: str, reverse: bool) -> list[dict]:
    t = target.replace("\\", "/")
    if reverse:
        mod = re.sub(r"\.py$", "", t).replace("/", ".")
        last = mod.rsplit(".", 1)[-1]
        rows = conn.execute("SELECT path, module, names, line FROM imports WHERE module=? OR module LIKE ? OR module LIKE ? ORDER BY path",
                            (mod, f"%.{last}", f"%.{mod}")).fetchall()
    else:
        rows = conn.execute("SELECT path, module, names, line FROM imports WHERE path=? OR path LIKE ? ORDER BY line", (t, f"%/{t}")).fetchall()
    return [dict(r) for r in rows]


def routes_info(conn: sqlite3.Connection, query: str = "", role: str | None = None) -> list[dict]:
    q = query.strip()
    method = None
    m = re.match(r"^(GET|POST|PUT|DELETE|PATCH)\s+(.*)$", q, re.I)
    if m:
        method, q = m.group(1).upper(), m.group(2)
    rows = conn.execute("SELECT * FROM routes ORDER BY url, method").fetchall()
    out: list[dict] = []
    nq = _norm_url(q)
    exact = [r for r in rows if q.startswith("/") and _norm_url(r["url"]) == nq]
    if exact:
        rows = exact
    for r in rows:
        if method and r["method"] != method:
            continue
        if q and nq not in _norm_url(r["url"]) and q != r["handler"] and q.lower() not in r["handler"].lower():
            continue
        if role and role.lower() not in (r["guards"] + " " + r["deps"]).lower():
            continue
        item = dict(r)
        item["callees"] = [c["callee"] for c in callees_of(conn, r["handler"]) if c["defined"]][:15]
        item["tables"] = [f"{t['tbl']}{' (пишет)' if t['writes'] else ''}" for t in conn.execute(
            "SELECT tbl, MAX(writes) writes FROM table_access WHERE path=? AND symbol=? GROUP BY tbl", (r["path"], r["handler"]))]
        out.append(item)
    return out


def table_info(conn: sqlite3.Connection, name: str) -> dict:
    tbl = name.strip()
    cols = [dict(r) for r in conn.execute("SELECT col, ctype, source, path, line FROM db_columns WHERE tbl=? ORDER BY source, line", (tbl,))]
    if not cols:
        like = conn.execute("SELECT DISTINCT tbl FROM db_columns WHERE tbl LIKE ?", (f"%{tbl}%",)).fetchall()
        if len(like) == 1:
            return table_info(conn, like[0][0])
        return {"table": tbl, "columns": [], "similar": [r[0] for r in like]}
    acc = [dict(r) for r in conn.execute("SELECT path, symbol, line, writes FROM table_access WHERE tbl=? ORDER BY writes DESC, path", (tbl,))]
    return {"table": tbl, "columns": cols, "access": acc}


def env_info(conn: sqlite3.Connection, name: str) -> list[dict]:
    rows = conn.execute("SELECT * FROM env_vars WHERE name=? OR name LIKE ? ORDER BY name, how", (name.upper(), f"%{name.upper()}%")).fetchall()
    out = [dict(r) for r in rows]
    if not out:
        consts = conn.execute("SELECT path, line, signature FROM symbols WHERE kind='constant' AND name=?", (name,)).fetchall()
        for c in consts:
            out.append({"name": name, "how": "константа модуля (НЕ переменная окружения)", "path": c["path"], "line": c["line"],
                        "default_value": c["signature"], "documented": None})
    field_name = name.lower()
    usages = []
    for r in conn.execute("SELECT DISTINCT path FROM symbols WHERE lang='py' AND path NOT LIKE '%/tests/%'"):
        try:
            for lineno, line in enumerate((ROOT / r["path"]).read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if re.search(rf"\bsettings\.{re.escape(field_name)}\b|\bs\.{re.escape(field_name)}\b", line):
                    usages.append({"path": r["path"], "line": lineno, "text": line.strip()[:120]})
        except OSError:
            continue
    for u in usages[:25]:
        out.append({"name": name.upper(), "how": "использует", "path": u["path"], "line": u["line"], "default_value": u["text"], "documented": None})
    return out


def ui_info(conn: sqlite3.Connection, text: str) -> list[dict]:
    like = f"%{text.lower()}%"
    ids = {r["element_id"] for r in conn.execute("SELECT DISTINCT element_id FROM ui_links WHERE PYLOWER(label) LIKE ? OR PYLOWER(element_id) LIKE ?", (like, like))}
    for r in conn.execute("SELECT symbol FROM chunks WHERE kind='ui_element' AND (PYLOWER(title) LIKE ? OR PYLOWER(text) LIKE ?) LIMIT 20", (like, like)):
        ids.add(r["symbol"])
    out: list[dict] = []
    for eid in sorted(ids)[:20]:
        rows = conn.execute("SELECT * FROM ui_links WHERE element_id=?", (eid,)).fetchall()
        if rows:
            out.extend(dict(r) for r in rows)
    strings = [dict(r) for r in conn.execute("SELECT text, path, line FROM ui_text WHERE PYLOWER(text) LIKE ? ORDER BY path LIMIT 20", (like,))]
    return [{"elements": out, "strings": strings}]


def endpoint_info(conn: sqlite3.Connection, url: str) -> dict:
    n = _norm_url(url)
    front = []
    for r in conn.execute("SELECT path, symbol, line_start, meta FROM chunks WHERE kind IN ('js','js_top') AND meta LIKE '%endpoints%'"):
        eps = json.loads(r["meta"] or "{}").get("endpoints", [])
        if any(n in _norm_url(e) or _norm_url(e) in n for e in eps if len(e) > 3):
            front.append({"path": r["path"], "symbol": r["symbol"], "line": r["line_start"]})
    return {"routes": routes_info(conn, url), "frontend": front}


def tests_info(conn: sqlite3.Connection, target: str) -> dict:
    t = target.replace("\\", "/")
    stem = re.sub(r"\.py$", "", t.rsplit("/", 1)[-1])
    rows = conn.execute(
        "SELECT test_path, test_name, module, symbol FROM test_cover WHERE module LIKE ? OR symbol=? OR symbol LIKE ? ORDER BY test_path",
        (f"%/{stem}.py", t.rsplit(".", 1)[-1], f"%{t.rsplit('.', 1)[-1]}%")).fetchall()
    per_file: dict[str, list[str]] = {}
    for r in rows:
        per_file.setdefault(r["test_path"], [])
        if r["test_name"]:
            per_file[r["test_path"]].append(r["test_name"])
    files = []
    for p, names in per_file.items():
        real = sorted({n for n in names if n.rsplit(".", 1)[-1].startswith("test_")})
        shown = real or sorted(set(names))
        files.append({"path": p, "tests": shown[:8], "count": len(real) if real else 0, "helpers_only": not real})
    files.sort(key=lambda x: -x["count"])
    return {"target": target, "files": files, "run": ("venv\\Scripts\\python.exe -m pytest " + " ".join(f["path"] for f in files[:4]) + " -n0 -q") if files else None}


def history_info(conn: sqlite3.Connection, target: str = "", since: str | None = None) -> list[dict]:
    rows = conn.execute("SELECT * FROM history ORDER BY date DESC").fetchall()
    cutoff = None
    if since:
        m = re.match(r"^(\d+)\s*([dwmh])$", since.strip())
        if m:
            unit = {"h": 3600, "d": 86400, "w": 604800, "m": 2592000}[m.group(2)]
            cutoff = time.time() - int(m.group(1)) * unit
    out = []
    for r in rows:
        if cutoff:
            try:
                ts = time.mktime(time.strptime(r["date"][:19], "%Y-%m-%dT%H:%M:%S"))
            except ValueError:
                ts = 0
            if ts < cutoff:
                continue
        if target and target.lower() not in (r["files"] + " " + r["subject"]).lower():
            continue
        refs = sorted({int(n) for m in re.finditer(r"(?:находк\w*\s*№?\s*|finding[- ]|#)(\d{1,3})", r["subject"], re.I) for n in [m.group(1)]})
        out.append({"hash": r["hash"][:7], "date": r["date"][:10], "author": r["author"], "subject": r["subject"],
                    "files": [f for f in r["files"].splitlines() if not target or target.lower() in f.lower()][:8],
                    "findings": [f"docs/FINDINGS-2026-09.md — находка {n}" for n in refs]})
    return out[:50]


def why_changed(conn: sqlite3.Connection, name: str) -> dict:
    syms = symbol_info(conn, name)
    if not syms:
        return {"symbol": name, "error": "символ не найден в индексе"}
    s = syms[0]
    out = analyzers._git("log", "-n", "6", "--format=%h %ad %s", "--date=short", f"-L{s['line']},{s['end_line']}:{s['path']}", "--no-patch")
    if not out.strip():
        out = analyzers._git("log", "-n", "6", "--format=%h %ad %s", "--date=short", "--", s["path"])
    return {"symbol": s["qualname"], "path": s["path"], "line": s["line"], "log": [l for l in out.splitlines() if l.strip()]}


def invariants_info(conn: sqlite3.Connection, text: str = "") -> list[dict]:
    rows = conn.execute("SELECT * FROM invariants WHERE PYLOWER(text) LIKE ? ORDER BY source, line", (f"%{text.lower()}%",)).fetchall()
    return [dict(r) for r in rows]


def ops_info(conn: sqlite3.Connection, query: str, limit: int = 6) -> dict:
    hits = _search(conn, query, limit * 3, {"ops", "doc", "config"}, None, False, False, True, False)
    keep = [h for h in hits["hits"] if h["kind"] in {"ops", "config"} or h["path"] in {"docs/OPERATIONS.md", "README.md", "docs/PLAN.md", "docs/PROJECT-MAP.md", "docs/HOWTO-MANAGER.md", "docs/TESTING.md"}]
    hits["hits"] = keep[:limit]
    return hits


# ───────────────────────────── Свежесть индекса ─────────────────────────────

def staleness(conn: sqlite3.Connection) -> dict:
    from app.app.services.project_index.indexer import scan_state

    changed, removed, _ = scan_state(conn, list_files(load_config()))
    pending = conn.execute("SELECT COUNT(*) FROM chunks WHERE id NOT IN (SELECT chunk_id FROM chunk_vectors)").fetchone()[0]
    indexed_at = float(store.get_meta(conn, "indexed_at", "0") or 0)
    return {"changed": changed, "removed": removed, "embed_pending": pending,
            "indexed_at": indexed_at, "age_hours": round((time.time() - indexed_at) / 3600, 1) if indexed_at else None}


def ide_link(path: str, line: int | None) -> str:
    return f"pycharm://open?file={(ROOT / path).as_posix()}&line={line or 1}"


# ───────────────────────────── Единый вывод (веб-страница и MCP) ─────────────────────────────

MODES = ["search", "symbol", "callers", "callees", "route", "table", "env", "ui", "tests", "history", "changed",
         "invariants", "ops", "why_changed", "endpoint"]


def _clip(text: str, lines: int = 10, width: int = 170) -> str:
    body = [l.rstrip()[:width] for l in text.splitlines() if l.strip()]
    return "\n".join(body[:lines])


def unified(conn: sqlite3.Connection, mode: str, query: str = "", limit: int = DEFAULT_LIMIT, *, use_vectors: bool = True) -> dict[str, Any]:
    """Результат любого режима в одном виде: {'mode', 'items': [{'title','path','line','text'}], 'notes': [...]}."""
    items: list[dict[str, Any]] = []
    notes: list[str] = []

    def item(title: str, path: str = "", line: int | None = None, text: str = "") -> None:
        items.append({"title": title, "path": path, "line": line or 1, "text": redact(text)})

    if mode == "search":
        res = _search(conn, query, limit, None, None, False, False, use_vectors, True)
        notes += res["notes"]
        for h in res["hits"]:
            lines = h["text"].splitlines()
            body = "\n".join(lines[1:]) if lines and h["path"] in lines[0] else h["text"]
            extra = "".join(f"\nⓘ {r['path']}:{r['line']} {r['title']}" for r in h.get("related", []))
            item(f"{h['symbol'] or h['title']} [{h['kind']}]", h["path"], h["line_start"], _clip(body) + extra)
    elif mode == "symbol":
        for r in symbol_info(conn, query):
            item(f"{r['qualname']} [{r['kind']}]", r["path"], r["line"], f"{r['signature']}\n{r['doc']}".strip())
    elif mode == "callers":
        for r in callers_of(conn, query)[:200]:
            item(f"{r['caller']}" + (" (передана как значение)" if r["ref"] else ""), r["path"], r["line"], r["qualifier"] or "")
    elif mode == "callees":
        for r in callees_of(conn, query)[:200]:
            target = r["defined"] or "внешний"
            path, _, line = (r["defined"] or "").rpartition(":")
            item(f"{r['callee']}  ← {r['in']}", path, int(line) if line.isdigit() else r["line"], f"определён: {target}")
    elif mode in {"route", "endpoint"}:
        rows = routes_info(conn, query) if mode == "route" else endpoint_info(conn, query)["routes"]
        for r in rows[:100]:
            item(f"{r['method']} {r['url']}", r["path"], r["line"],
                 f"обработчик {r['handler']}\nзависимости: {r['deps'] or '—'}\nправа: {r['guards'] or '—'}\n"
                 f"вызывает: {', '.join(r.get('callees', [])) or '—'}\nтаблицы: {', '.join(r.get('tables', [])) or '—'}")
        if mode == "endpoint":
            for f in endpoint_info(conn, query)["frontend"]:
                item(f"фронт: {f['symbol']}", f["path"], f["line"], "обращается к этому URL")
    elif mode == "table":
        t = table_info(conn, query)
        if t.get("columns"):
            item(f"таблица {t['table']}", t["columns"][0]["path"], t["columns"][0]["line"],
                 "\n".join(f"{c['col']}  {c['ctype'] or ''}  [{c['source']}]" for c in t["columns"]))
            for a in t.get("access", [])[:60]:
                item(("ПИШЕТ " if a["writes"] else "читает ") + a["symbol"], a["path"], a["line"])
        elif t.get("similar"):
            notes.append("похожие таблицы: " + ", ".join(t["similar"]))
    elif mode == "env":
        for r in env_info(conn, query):
            item(f"{r['name']} — {r['how']}", r["path"], r["line"], str(r["default_value"] or "") +
                 ("" if r["documented"] is None else ("  [описана в .env.example]" if r["documented"] else "  [НЕ описана в .env.example]")))
    elif mode == "ui":
        for block in ui_info(conn, query):
            for r in block["elements"]:
                item(f"#{r['element_id']} «{r['label']}»", r["path"], r["line"],
                     (f"JS: {r['js_path']}:{r['js_line']} {r['js_symbol']}\n" if r["js_path"] else "JS-обработчик не найден\n") + f"API: {r['endpoints'] or '—'}")
            for s in block["strings"]:
                item(f"текст «{s['text'][:80]}»", s["path"], s["line"])
    elif mode == "tests":
        t = tests_info(conn, query)
        for f in t["files"]:
            item(f["path"], f["path"], 1, ", ".join(f["tests"][:6]))
        if t["run"]:
            notes.append("запуск: " + t["run"])
    elif mode == "history":
        for r in history_info(conn, query):
            item(f"{r['hash']} {r['date']} {r['subject']}", "", 1, ", ".join(r["files"][:6]))
    elif mode == "changed":
        for r in analyzers.changed_symbols(conn):
            item(f"{r['status']} {r['path']}", r["path"], 1, ", ".join(r.get("symbols", [])))
    elif mode == "invariants":
        for r in invariants_info(conn, query):
            item(r["text"][:160], r["source"], r["line"], ("стражи: " + r["guard"]) if r["guard"] else "")
    elif mode == "ops":
        res = ops_info(conn, query, limit)
        for h in res["hits"]:
            item(h["title"], h["path"], h["line_start"], _clip(h["text"]))
    elif mode == "why_changed":
        w = why_changed(conn, query)
        item(w.get("symbol", query), w.get("path", ""), w.get("line"), "\n".join(w.get("log", [])) or w.get("error", ""))
    else:
        notes.append(f"неизвестный режим {mode!r}; доступны: {', '.join(MODES)}")
    if not items and not notes:
        notes.append("ничего не найдено")
    return {"mode": mode, "query": query, "items": items[:max(limit, 1) if mode == "search" else 300], "notes": notes}


def as_text(result: dict[str, Any]) -> str:
    """Текстовый вид результата — для MCP и логов."""
    out: list[str] = []
    for note in result["notes"]:
        out.append(f"⚠ {note}")
    for i, it in enumerate(result["items"], 1):
        loc = f"{it['path']}:{it['line']}" if it["path"] else ""
        out.append(f"[{i}] {loc}  {it['title']}")
        if it["text"]:
            out.append("\n".join("    " + l for l in it["text"].splitlines()))
    return "\n".join(out) or "ничего не найдено"
