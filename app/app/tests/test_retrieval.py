from app.app.services.retrieval import enrich_and_rerank_hits, weighted_score


def test_weighted_score_prefers_heavier_source():
    low = weighted_score(0.8, 0.5)
    high = weighted_score(0.75, 0.8)
    assert high > low


def test_enrich_and_rerank_hits(client, db_engine, admin_token):
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.project import Project
    from app.app.models.user import User
    from sqlalchemy import select

    db = SessionLocal()
    user = db.scalar(select(User).limit(1))
    p = Project(name="RankProj", description="", created_by=user.id, is_active=True)
    db.add(p)
    db.commit()
    db.refresh(p)
    call = Artifact(
        project_id=p.id,
        user_id=user.id,
        artifact_id="art_call_rank",
        original_filename="call.webm",
        stored_filename="c.webm",
        file_path="/tmp/c.webm",
        sha256="c1",
        file_type="audio",
        source_type="call",
        status="done",
        priority_weight=0.5,
    )
    doc = Artifact(
        project_id=p.id,
        user_id=user.id,
        artifact_id="art_doc_rank",
        original_filename="doc.txt",
        stored_filename="d.txt",
        file_path="/tmp/d.txt",
        sha256="d1",
        file_type="txt",
        source_type="document",
        status="done",
        priority_weight=0.8,
    )
    db.add_all([call, doc])
    db.commit()
    db.refresh(call)
    db.refresh(doc)
    hits = [
        {"artifact_id": call.id, "chunk_index": 0, "text": "a", "score": 0.9},
        {"artifact_id": doc.id, "chunk_index": 0, "text": "b", "score": 0.85},
    ]
    ranked = enrich_and_rerank_hits(db, hits, limit=2)
    db.close()
    assert ranked[0]["artifact_id"] == doc.id
    assert ranked[0]["weighted_score"] > ranked[1]["weighted_score"]
