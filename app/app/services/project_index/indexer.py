"""Индексатор проекта: инкрементально, с кэшем эмбеддингов, устойчиво к обрыву.

Порядок (важен для надёжности):
  1. в одной транзакции: удалить строки изменённых и исчезнувших файлов, записать новые фрагменты, символы, вызовы,
     импорты, пересчитать производные таблицы (роуты, БД, окружение, UI, тесты, история);
     фрагменты сразу доступны точному поиску (FTS);
  2. потом, отдельными пачками: эмбеддинги для фрагментов, у которых ещё нет вектора (кэш по хешу текста).
Обрыв на шаге 2 ничего не портит: следующий запуск доберёт недостающие векторы.
Индексатор не использует GPU (эмбеддинги — Ollama на CPU) и не пишет в app.db.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from app.app.services.project_index import analyzers, store
from app.app.services.project_index.chunkers import Chunk, FileResult, chunk_file
from app.app.services.project_index.config import CHUNKER_VERSION, LOG_PATH, ROOT, IndexConfig, list_files, load_config


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _sha_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _logger(echo: bool) -> Callable[[str], None]:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    def log(message: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {message}"
        if echo:
            print(line, flush=True)
        try:
            with LOG_PATH.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

    return log


def scan_state(conn: sqlite3.Connection, files: list[str]) -> tuple[list[str], list[str], list[str]]:
    """(изменённые + новые, удалённые, без изменений). Сравнение по размеру и mtime, затем по хешу."""
    known = {r["path"]: r for r in conn.execute("SELECT path, sha, size, mtime FROM files")}
    changed: list[str] = []
    same: list[str] = []
    for rel in files:
        st = (ROOT / rel).stat()
        row = known.get(rel)
        if row is None:
            changed.append(rel)
        elif row["size"] == st.st_size and abs((row["mtime"] or 0) - st.st_mtime) < 1e-6:
            same.append(rel)
        elif _sha_bytes((ROOT / rel).read_bytes()) == row["sha"]:
            conn.execute("UPDATE files SET size=?, mtime=? WHERE path=?", (st.st_size, st.st_mtime, rel))
            same.append(rel)
        else:
            changed.append(rel)
    removed = sorted(set(known) - set(files))
    return changed, removed, same


def _delete_chunks(conn: sqlite3.Connection, where: str, args: tuple) -> None:
    ids = [r[0] for r in conn.execute(f"SELECT id FROM chunks WHERE {where}", args)]
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        marks = ",".join("?" * len(part))
        conn.execute(f"DELETE FROM chunk_vectors WHERE chunk_id IN ({marks})", part)
        conn.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({marks})", part)
        conn.execute(f"DELETE FROM chunks WHERE id IN ({marks})", part)


def _delete_file(conn: sqlite3.Connection, rel: str) -> None:
    _delete_chunks(conn, "path=? AND (meta IS NULL OR meta NOT LIKE '%\"generated\": true%')", (rel,))
    for table in ("symbols", "calls", "imports", "symbol_aliases"):
        conn.execute(f"DELETE FROM {table} WHERE path=?", (rel,))
    conn.execute("DELETE FROM files WHERE path=?", (rel,))


def _insert_chunk(conn: sqlite3.Connection, ch: Chunk, cfg: IndexConfig) -> None:
    embed_text = f"{ch.kind} {ch.title}\n{ch.path}\n{ch.text[:cfg.embed_chars]}"
    cur = conn.execute(
        "INSERT INTO chunks(path, kind, title, symbol, line_start, line_end, text, embed_sha, meta) VALUES (?,?,?,?,?,?,?,?,?)",
        (ch.path, ch.kind, ch.title, ch.symbol, ch.line_start, ch.line_end, ch.text, _sha_text(embed_text), json.dumps(ch.meta, ensure_ascii=False)),
    )
    conn.execute("INSERT INTO chunks_fts(rowid, title, symbol, path, idents, text) VALUES (?,?,?,?,?,?)",
                 (cur.lastrowid, ch.title, ch.symbol or "", ch.path, store.identifiers_text(ch.title + " " + ch.text), ch.text))


def _store_result(conn: sqlite3.Connection, rel: str, res: FileResult, cfg: IndexConfig, raw: bytes) -> None:
    for ch in res.chunks:
        _insert_chunk(conn, ch, cfg)
    for s in res.symbols:
        conn.execute("INSERT INTO symbols(name, qualname, kind, path, line, end_line, signature, decorators, doc, lang) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (s.name, s.qualname, s.kind, s.path, s.line, s.end_line, s.signature, s.decorators, s.doc, s.lang))
    conn.executemany("INSERT INTO calls(path, caller, callee, qualifier, line, lang) VALUES (?,?,?,?,?,?)",
                     [(c.path, c.caller, c.callee, c.qualifier, c.line, c.lang) for c in res.calls])
    conn.executemany("INSERT INTO imports(path, module, names, line) VALUES (?,?,?,?)",
                     [(i.path, i.module, i.names, i.line) for i in res.imports])
    if hasattr(res, "aliases"):
        conn.executemany("INSERT INTO symbol_aliases(alias, file, path, line, kind, name, qualname, module, source) VALUES (?,?,?,?,?,?,?,?,?)",
                         [(a.alias, a.file, a.path, a.line, a.kind, a.name, a.qualname, a.module, a.source) for a in res.aliases])
    st = (ROOT / rel).stat()
    conn.execute("INSERT OR REPLACE INTO files(path, sha, size, mtime, kind, indexed_at) VALUES (?,?,?,?,?,?)",
                 (rel, _sha_bytes(raw), st.st_size, st.st_mtime, rel.rsplit(".", 1)[-1], time.time()))


def _embed_pending(conn: sqlite3.Connection, log: Callable[[str], None], *, batch: int = 64) -> tuple[int, int]:
    """Добрать векторы. Возвращает (посчитано заново, взято из кэша)."""
    rows = conn.execute(
        "SELECT c.id, c.embed_sha, c.kind, c.title, c.path, c.text FROM chunks c "
        "LEFT JOIN chunk_vectors v ON v.chunk_id = c.id WHERE v.chunk_id IS NULL"
    ).fetchall()
    if not rows:
        return 0, 0
    cfg = load_config()
    cached = fresh = 0
    to_compute: list[sqlite3.Row] = []
    for r in rows:
        blob = conn.execute("SELECT vec FROM embed_cache WHERE sha=?", (r["embed_sha"],)).fetchone()
        if blob:
            conn.execute("INSERT INTO chunk_vectors(chunk_id, embedding) VALUES (?,?)", (r["id"], blob[0]))
            cached += 1
        else:
            to_compute.append(r)
    conn.commit()
    log(f"эмбеддинги: из кэша {cached}, считать {len(to_compute)}")
    if not to_compute:
        return 0, cached
    from app.app.config import Settings
    from app.app.services.ollama_client import embed_texts

    settings = Settings()
    started = time.time()
    for i in range(0, len(to_compute), batch):
        part = to_compute[i:i + batch]
        texts = [f"{r['kind']} {r['title']}\n{r['path']}\n{r['text'][:cfg.embed_chars]}" for r in part]
        vectors = embed_texts(settings, texts)
        for r, vec in zip(part, vectors, strict=True):
            blob = store.pack(vec)
            conn.execute("INSERT OR REPLACE INTO embed_cache(sha, vec) VALUES (?,?)", (r["embed_sha"], blob))
            conn.execute("INSERT INTO chunk_vectors(chunk_id, embedding) VALUES (?,?)", (r["id"], blob))
            fresh += 1
        conn.commit()
        done = min(i + batch, len(to_compute))
        rate = done / max(0.1, time.time() - started)
        log(f"  эмбеддинги {done}/{len(to_compute)} ({rate:.1f}/с, осталось ~{(len(to_compute) - done) / max(rate, 0.1):.0f} с)")
    return fresh, cached


def run_index(*, full: bool = False, embed: bool = True, echo: bool = True, only: list[str] | None = None) -> dict:
    """Обновить индекс. `full` — пересобрать всё; `only` — только перечисленные файлы (для хуков)."""
    log = _logger(echo)
    cfg = load_config()
    started = time.time()
    with store.index_lock():
        conn = store.connect(create=True)
        try:
            files = list_files(cfg)
            if store.get_meta(conn, "chunker_version") != CHUNKER_VERSION:
                full = True  # логика нарезки изменилась
            if full:
                conn.execute("DELETE FROM files")
            changed, removed, same = scan_state(conn, files)
            if only:
                wanted = {p.replace("\\", "/") for p in only}
                changed = [p for p in changed if p in wanted]
            head = (analyzers._git("rev-parse", "HEAD") or "").strip()
            if (not full and not changed and not removed and head == store.get_meta(conn, "git_head")
                    and conn.execute("SELECT COUNT(*) FROM chunks WHERE id NOT IN (SELECT chunk_id FROM chunk_vectors)").fetchone()[0] == 0):
                conn.commit()
                summary = {"files": len(files), "changed": 0, "removed": 0, "chunks": conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
                           "embedded_new": 0, "embedded_cached": 0, "embed_pending": 0, "seconds": round(time.time() - started, 1), "skipped": True}
                log(f"без изменений: {summary}")
                return summary
            log(f"файлов: {len(files)}; изменено/новых: {len(changed)}; удалено: {len(removed)}; без изменений: {len(same)}")
            if full:
                _delete_chunks(conn, "1=1", ())
                for table in ("symbols", "calls", "imports"):
                    conn.execute(f"DELETE FROM {table}")
            for rel in removed:
                _delete_file(conn, rel)
            results: list[tuple[str, FileResult, bytes]] = []
            for rel in changed:
                raw = (ROOT / rel).read_bytes()
                text = raw.decode("utf-8", errors="replace")
                try:
                    results.append((rel, chunk_file(rel, text, cfg.max_lines), raw))
                except Exception as exc:  # noqa: BLE001 — один плохой файл не должен ронять индексацию
                    log(f"ПРОПУЩЕН {rel}: {exc!r}")
            for rel, res, raw in results:
                _delete_file(conn, rel)
                _store_result(conn, rel, res, cfg, raw)
            # производные данные и сгенерированные фрагменты пересчитываются целиком
            _delete_chunks(conn, "meta LIKE '%\"generated\": true%'", ())
            generated = analyzers.analyze_all(conn, cfg.history_commits)
            for ch in generated:
                _insert_chunk(conn, ch, cfg)
            store.set_meta(conn, "chunker_version", CHUNKER_VERSION)
            store.set_meta(conn, "indexed_at", str(time.time()))
            store.set_meta(conn, "git_head", head)
            conn.commit()
            n_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            log(f"структура записана: фрагментов {n_chunks} (сгенерировано {len(generated)})")
            fresh = cached = 0
            if embed:
                try:
                    fresh, cached = _embed_pending(conn, log)
                except Exception as exc:  # noqa: BLE001
                    log(f"эмбеддинги не досчитаны ({exc!r}); точный поиск работает, смысловой — после следующего запуска")
            pending = conn.execute("SELECT COUNT(*) FROM chunks c LEFT JOIN chunk_vectors v ON v.chunk_id=c.id WHERE v.chunk_id IS NULL").fetchone()[0]
            store.set_meta(conn, "embed_pending", str(pending))
            conn.commit()
            summary = {"files": len(files), "changed": len(changed), "removed": len(removed), "chunks": n_chunks,
                       "embedded_new": fresh, "embedded_cached": cached, "embed_pending": pending,
                       "seconds": round(time.time() - started, 1)}
            log(f"готово: {summary}")
            return summary
        except BaseException:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
