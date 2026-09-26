"""Быстрый векторный свод: retrieval top-K из sqlite-vec (чанки+сущности) + заметки
-> один reduce-вызов на Swift -> обновление current_tz.md + changelog.

  venv\\Scripts\\python.exe scripts\\fast_summarize_tz.py [project_id] [logfile]
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import bindparam, func, select, text  # noqa: E402

from app.app.config import get_settings  # noqa: E402
from app.app.db import SessionLocal  # noqa: E402
from app.app.logging_setup import log_event  # noqa: E402
from app.app.models.artifact import Artifact  # noqa: E402
from app.app.models.entity import Changelog, Entity, UserNote  # noqa: E402
from app.app.services.graph import _cosine, entity_text  # noqa: E402
from app.app.services.llm import chat_completion_no_think  # noqa: E402
from app.app.services.ollama_client import embed_texts  # noqa: E402
from app.app.services.plain_text import clean_summary_output  # noqa: E402
from app.app.services.retrieval import artifact_sort_key, search_entities  # noqa: E402
from app.app.services.tz import (  # noqa: E402
    _estimate_tokens,
    _artifact_filename_map,
    _humanize_artifact_refs,
    _parse_prompts,
    tz_paths,
)
from app.app.services.vector_index import (  # noqa: E402
    deserialize_float32,
    load_entity_vectors,
    load_sqlite_vec,
    search_vectors,
)

PROJECT_ID = int(sys.argv[1]) if len(sys.argv) > 1 else 18
LOG = sys.argv[2] if len(sys.argv) > 2 else r"C:\Users\rene_\AppData\Local\Temp\opencode\fast_tz.log"
HITS_PER_QUERY = 7
ENTS_PER_QUERY = 6
MAX_CHUNKS = 22
MAX_ENTS = 20
MMR_LAMBDA = 0.65
CHUNK_TRIM = 620
MAX_TOKENS = 20000
QUERIES = [
    "Цель проекта, заказчик, периметр работ, конфигурация 1С и интеграция с Bitrix24",
    "Принятые ключевые технические решения: обмен данными, номенклатура, реквизиты, расписание, поля",
    "Что уже сделано, выполнено, настроено, запущено, подключено",
    "Актуальные задачи, сроки, исполнители, план работ, этапы",
    "Открытые вопросы, риски, проблемы, ограничения, доступы, права, нерешённое",
    "профнастил, нормализация номенклатуры, характеристики, длина, реквизиты табличной части, погонные метры, старая номенклатура",
    "коннектор, обмен данными, синхронизация, Bitrix24, эквайринг, тестовый контур, резервные копии",
]


def log(msg: str) -> None:
    line = f"{datetime.now():%H:%M:%S} {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def label(art: Artifact, detail: str) -> str:
    return f"{art.original_filename} ({detail})"


def _mmr_select(ids: list[int], rel: dict[int, float], vecs: dict[int, list[float]], k: int, lam: float = MMR_LAMBDA) -> list[int]:
    """Greedy MMR: релевантность минус штраф за близость к уже выбранным."""
    pool = [i for i in ids if i in rel]
    if not pool or k <= 0:
        return []
    if not vecs:
        return sorted(pool, key=lambda i: rel[i], reverse=True)[:k]
    chosen: list[int] = []
    chosen_vecs: list[list[float]] = []
    while len(chosen) < k and pool:
        best_i, best_score, best_vec = None, -1e9, None
        for i in pool:
            v = vecs.get(i)
            if v is None:
                continue
            d = max((_cosine(v, c) for c in chosen_vecs), default=0.0)
            s = lam * rel.get(i, 0.0) - (1 - lam) * d
            if s > best_score:
                best_i, best_score, best_vec = i, s, v
        if best_i is None:
            break
        chosen.append(best_i)
        chosen_vecs.append(best_vec)
        pool = [x for x in pool if x != best_i]
    return chosen


def _chunk_embeddings(ids: list[int]) -> dict[int, list[float]]:
    """Векторы чанков прямо из vec0 (BLOB/hex), без переэмбеддинга."""
    from sqlalchemy.orm import Session

    from app.app.db import SessionLocal as _S

    s: Session = _S()
    try:
        conn = s.connection()
        load_sqlite_vec(conn.connection.dbapi_connection)
        out: dict[int, list[float]] = {}
        for start in range(0, len(ids), 100):
            batch = ids[start : start + 100]
            if not batch:
                break
            rows = conn.execute(
                text("SELECT chunk_id, embedding FROM chunk_vectors WHERE chunk_id IN :ids").bindparams(
                    bindparam("ids", expanding=True)
                ),
                {"ids": batch},
            ).mappings().all()
            for row in rows:
                emb = row["embedding"]
                if isinstance(emb, str) and emb.startswith("X"):
                    raw = bytes.fromhex(emb[2:-1])
                else:
                    raw = emb
                vec = deserialize_float32(raw)
                if len(vec) == 1024:
                    out[int(row["chunk_id"])] = vec
        return out
    finally:
        s.close()


def _compact_prev(prev: str, limit: int = 3000) -> str:
    """Компактная сводка предыдущей версии для сверки (головы + пункты, без пустых строк)."""
    if not prev.strip():
        return ""
    lines = []
    for line in prev.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("##", "- ", "|", "---")) or stripped in ("Кто", "Что", "Срок"):
            lines.append(stripped)
    text = "\n".join(lines)
    return text[:limit] + (" …" if len(text) > limit else "")


def main() -> int:
    db = SessionLocal()
    settings = get_settings()
    log(f"fast_summarize project={PROJECT_ID} model={settings.lmstudio_model}")

    map_sys, _mu, _ms, _meu, reduce_sys, reduce_user_tmpl = _parse_prompts()
    current, versions = tz_paths(settings, PROJECT_ID)
    prev = current.read_text(encoding="utf-8") if current.is_file() else ""

    q_vectors = embed_texts(settings, QUERIES)
    log(f"embedded {len(q_vectors)} queries dim={len(q_vectors[0])}")

    chunk_seen: dict[int, dict] = {}
    ent_seen: dict[int, dict] = {}
    chunk_rel: dict[int, float] = {}
    ent_rel: dict[int, float] = {}
    for vi, q_vec in enumerate(q_vectors):
        for h in search_vectors(db, q_vec, project_id=PROJECT_ID, limit=HITS_PER_QUERY):
            chunk_seen.setdefault(h["chunk_id"], h)
            chunk_rel[h["chunk_id"]] = max(chunk_rel.get(h["chunk_id"], 0.0), h.get("score") or 0.0)
        for e in search_entities(db, q_vec, project_id=PROJECT_ID, limit=ENTS_PER_QUERY):
            ent_seen.setdefault(e["entity_id"], e)
            ent_rel[e["entity_id"]] = max(ent_rel.get(e["entity_id"], 0.0), e.get("score") or 0.0)
    log(f"retrieval totals: chunks={len(chunk_seen)} ents={len(ent_seen)}")

    chunk_vecs = _chunk_embeddings(list(chunk_seen.keys()))
    ent_vecs = load_entity_vectors(db, list(ent_seen.keys()))
    chunk_ids = _mmr_select(list(chunk_seen.keys()), chunk_rel, chunk_vecs, MAX_CHUNKS)
    ent_ids = _mmr_select(list(ent_seen.keys()), ent_rel, ent_vecs, MAX_ENTS)
    hits = [chunk_seen[cid] for cid in chunk_ids]
    ents = [ent_seen[eid] for eid in ent_ids]
    log(f"mmr selected chunks={len(hits)} ents={len(ents)}")

    arts: dict[int, Artifact] = {}
    for h in hits:
        arts.setdefault(h["artifact_id"], db.get(Artifact, h["artifact_id"]))
    rng = lambda a: (artifact_sort_key(a) if a is not None else (0, 0, 0, ""))  # noqa: E731
    hits.sort(key=lambda h: (rng(arts.get(h["artifact_id"])), h["chunk_index"]))

    orders = select(Entity).where(Entity.id.in_([e["entity_id"] for e in ents]))
    db_ents = {e.id: e for e in db.scalars(orders).all()}

    notes = db.scalars(
        select(UserNote).where(UserNote.project_id == PROJECT_ID).order_by(UserNote.id)
    ).all()

    blocks: list[str] = []
    for h in hits:
        text = (h["text"] or "").strip()
        if not text:
            continue
        if len(text) > CHUNK_TRIM:
            text = text[:CHUNK_TRIM].rstrip() + " …"
        art = arts.get(h["artifact_id"])
        title = label(art, f"фрагмент {h['chunk_index']}") if art else f"чанк {h['chunk_id']}"
        blocks.append(f"## {title}\n\n{text}")
    if ents:
        lines = [f"- [{e['entity_type']}] {e['text']}" for e in ents if e["text"]]
        if lines:
            blocks.append("## Сущности проекта (БД)\n\n" + "\n".join(lines))
    note_lines = [f"- (note #{n.id}) {n.text.strip()}" for n in notes if n.text.strip()]
    if note_lines:
        blocks.append("## Заметки пользователей\n\n" + "\n".join(note_lines))

    prev_ref = _compact_prev(prev)
    if prev_ref:
        blocks.append(
            "## СПРАВОЧНЫЙ БЛОК: предыдущая версия резюме\n\n"
            "Этот блок — НЕ источник фактов и НЕ для сравнения текста, а фильтр достоверности. "
            "Правило: если пункт нового резюме противоречит какому-либо утверждению в этом блоке "
            "и при этом не подтверждается материалами выше — этот пункт исключи. "
            "Из справочного блока ничего не цитируй и ничего не добавляй в ответ.\n\n"
            + prev_ref
        )

    if not blocks:
        log("EMPTY_MATERIAL — нет релевантных материалов после retrieval")
        db.close()
        return 1

    material = "\n\n".join(blocks)
    est = _estimate_tokens(material)
    log(f"retrieval: chunks={len(hits)} ents={len(ents)} notes={len(note_lines)} material_tokens~{est}")
    reduce_user = reduce_user_tmpl.format(drafts=material)

    body = chat_completion_no_think(
        settings,
        system=reduce_sys,
        user=reduce_user,
        max_tokens=MAX_TOKENS,
        temperature=0.0,
    ).strip()
    if not body:
        log(f"EMPTY first reduce at max_tokens={MAX_TOKENS}, retrying at 22000")
        body = chat_completion_no_think(
            settings,
            system=reduce_sys,
            user=reduce_user,
            max_tokens=22000,
            temperature=0.0,
        ).strip()
    if not body:
        log("EMPTY twice — abort, не пишем выжимку")
        db.close()
        return 2
    log(f"reduce done chars={len(body)}")

    body = clean_summary_output(body)
    body = _humanize_artifact_refs(body, _artifact_filename_map(db, PROJECT_ID))
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    if current.is_file() and prev.strip():
        (versions / f"current_tz_{stamp}.md").write_text(prev, encoding="utf-8")
    current.write_text(body.strip() + "\n", encoding="utf-8")
    arts_all = db.scalars(
        select(Artifact.artifact_id).where(Artifact.project_id == PROJECT_ID)
    ).all()
    db.add(
        Changelog(
            project_id=PROJECT_ID,
            version_label=stamp,
            artifact_ids_json=str(list(arts_all)),
            summary_of_changes="fast vector summarize",
            tz_path=str(current),
        )
    )
    db.commit()
    db.close()
    log(f"DONE stamp={stamp} chars={len(body)} path={current}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())