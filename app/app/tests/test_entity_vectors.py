from __future__ import annotations

from sqlalchemy import event, select

from app.app.config import Settings
from app.app.models.artifact import Artifact
from app.app.models.entity import Entity
from app.app.models.project import Project
from app.app.models.user import User
from app.app.services.entities import EntitiesPayload, persist_entities
from app.app.services.graph import entity_text, seed_graph
from app.app.services.ollama_client import EMBED_DIM
from app.app.services.vector_index import (
    delete_vectors_for_entities,
    init_vector_store,
    load_entity_vectors,
    load_sqlite_vec,
)


def _settings(tmp_data, *, dry_run: bool) -> Settings:
    return Settings(
        secret_key="test-secret-key-32chars!!",
        data_dir=tmp_data,
        dry_run=dry_run,
        database_url=f"sqlite:///{(tmp_data / 'test.db').as_posix()}",
    )


def _prepare_vec(engine) -> None:
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _record):
        load_sqlite_vec(dbapi_conn)

    init_vector_store(engine)


def _fake_embed(calls: list[list[str]]):
    def _embed(_settings, texts, **_kwargs):
        calls.append(list(texts))
        return [[0.1] * EMBED_DIM for _ in texts]

    return _embed


def _project_artifact(db):
    user = db.scalar(select(User).limit(1))
    project = Project(name="VecProj", description="", created_by=user.id, is_active=True)
    db.add(project)
    db.flush()
    art = Artifact(
        artifact_id="art_vec_a",
        project_id=project.id,
        user_id=user.id,
        original_filename="a.docx",
        stored_filename="a.docx",
        file_path="a.docx",
        file_type="docx",
        source_type="document",
        status="text_extracted",
        sha256="aaa",
    )
    db.add(art)
    db.flush()
    return project, art


def test_graph_conflicts_only_for_decision_task_fact(tmp_data, db_engine, client, admin_token, monkeypatch):
    """Разные участники — не противоречие (раньше давали половину всех конфликтов), разные решения — конфликт."""
    from app.app.db import SessionLocal
    from app.app.models.entity import Link

    _prepare_vec(db_engine)
    fake = _fake_embed([])  # одинаковые векторы: сходство 1.0, выше любого порога
    monkeypatch.setattr("app.app.services.entities.embed_texts", fake)
    monkeypatch.setattr("app.app.services.graph.embed_texts", fake)
    settings = _settings(tmp_data, dry_run=False)
    db = SessionLocal()
    try:
        project, art_a = _project_artifact(db)
        art_b = Artifact(artifact_id="art_vec_c", project_id=project.id, user_id=art_a.user_id, original_filename="c.docx",
                         stored_filename="c.docx", file_path="c.docx", file_type="docx", source_type="document",
                         status="text_extracted", sha256="ccc")
        db.add(art_b)
        db.flush()
        from app.app.services.entities import ParticipantItem

        persist_entities(db, settings, art_a, EntitiesPayload(decisions=["мастер-система Битрикс"], participants=[ParticipantItem(name="Компания-2")]))
        persist_entities(db, settings, art_b, EntitiesPayload(decisions=["мастер-система 1С"], participants=[ParticipantItem(name="Компания-7")]))
        db.commit()
        db.refresh(art_b)
        seed_graph(db, settings, art_b, "текст")
        db.commit()
        links = db.scalars(select(Link).where(Link.project_id == project.id)).all()
        by_type = {}
        for link in links:
            src = db.get(Entity, link.source_entity_id)
            tgt = db.get(Entity, link.target_entity_id)
            if src.entity_type == tgt.entity_type:
                by_type[src.entity_type] = link.link_type
        assert by_type["decision"] == "POTENTIAL_CONFLICT"
        assert by_type["participant"] == "SEMANTICALLY_RELATED"
    finally:
        db.close()


def test_entity_text_matches_entity_wrapper():
    raw = '{"text": "Решение"}'
    ent = Entity(
        artifact_id=1,
        entity_type="decision",
        value_json=raw,
        confidence=0.6,
        is_active=True,
        needs_review=True,
    )
    from app.app.services.graph import _entity_text

    assert entity_text(raw) == _entity_text(ent) == "Решение"


def test_persist_writes_vectors_and_replaces_ids(tmp_data, db_engine, client, admin_token, monkeypatch):
    from app.app.db import SessionLocal

    _prepare_vec(db_engine)
    calls: list[list[str]] = []
    fake = _fake_embed(calls)
    monkeypatch.setattr("app.app.services.entities.embed_texts", fake)
    monkeypatch.setattr("app.app.services.graph.embed_texts", fake)

    settings = _settings(tmp_data, dry_run=False)
    db = SessionLocal()
    try:
        _project, art = _project_artifact(db)
        persist_entities(
            db,
            settings,
            art,
            EntitiesPayload(artifact_id=art.artifact_id, decisions=["один", "два"]),
        )
        db.commit()
        ids1 = list(db.scalars(select(Entity.id).where(Entity.artifact_id == art.id)))
        stored = load_entity_vectors(db, ids1)
        assert set(stored) == set(ids1)
        assert len(calls) == 1

        persist_entities(
            db,
            settings,
            art,
            EntitiesPayload(artifact_id=art.artifact_id, decisions=["три"]),
        )
        db.commit()
        ids2 = list(db.scalars(select(Entity.id).where(Entity.artifact_id == art.id)))
        leftover = load_entity_vectors(db, list(set(ids1) | set(ids2)))
        assert set(leftover) == set(ids2)
        assert len(ids2) == 1
        assert len(calls) == 2
    finally:
        db.close()


def test_persist_entities_replaces_rows_with_graph_links(tmp_data, db_engine, client, admin_token):
    from app.app.db import SessionLocal
    from app.app.models.entity import Link, Resolution

    settings = _settings(tmp_data, dry_run=True)
    db = SessionLocal()
    try:
        project, art = _project_artifact(db)
        other = Artifact(
            artifact_id="art_vec_b",
            project_id=project.id,
            user_id=art.user_id,
            original_filename="b.docx",
            stored_filename="b.docx",
            file_path="b.docx",
            file_type="docx",
            source_type="document",
            status="text_extracted",
            sha256="bbb",
        )
        db.add(other)
        db.flush()
        persist_entities(db, settings, art, EntitiesPayload(artifact_id=art.artifact_id, decisions=["один"]))
        persist_entities(db, settings, other, EntitiesPayload(artifact_id=other.artifact_id, decisions=["два"]))
        db.flush()
        e1 = db.scalar(select(Entity).where(Entity.artifact_id == art.id))
        e2 = db.scalar(select(Entity).where(Entity.artifact_id == other.id))
        assert e1 is not None and e2 is not None
        link = Link(
            project_id=project.id,
            source_entity_id=e1.id,
            target_entity_id=e2.id,
            link_type="related",
            is_automatic=True,
        )
        db.add(link)
        db.flush()
        db.add(
            Resolution(
                link_id=link.id,
                winning_entity_id=e1.id,
                losing_entity_id=e2.id,
                reason="merge",
                status="resolved",
            )
        )
        db.flush()
        persist_entities(db, settings, art, EntitiesPayload(artifact_id=art.artifact_id, decisions=["три"]))
        db.commit()
        kept = db.get(Entity, e2.id)
        assert kept is not None
        ids = list(db.scalars(select(Entity.id).where(Entity.artifact_id == art.id)))
        assert len(ids) == 1
        assert ids[0] != e1.id
        assert db.scalars(select(Link).where(Link.project_id == project.id)).all() == []
    finally:
        db.close()


def test_persist_dry_run_skips_embed(tmp_data, db_engine, client, admin_token, monkeypatch):
    from app.app.db import SessionLocal

    _prepare_vec(db_engine)
    calls: list[list[str]] = []
    fake = _fake_embed(calls)
    monkeypatch.setattr("app.app.services.entities.embed_texts", fake)

    settings = _settings(tmp_data, dry_run=True)
    db = SessionLocal()
    try:
        _project, art = _project_artifact(db)
        persist_entities(
            db,
            settings,
            art,
            EntitiesPayload(artifact_id=art.artifact_id, decisions=["x"]),
        )
        db.commit()
        assert calls == []
        ids = list(db.scalars(select(Entity.id).where(Entity.artifact_id == art.id)))
        assert load_entity_vectors(db, ids) == {}
    finally:
        db.close()


def test_seed_uses_cache_for_prior(tmp_data, db_engine, client, admin_token, monkeypatch):
    from app.app.db import SessionLocal

    _prepare_vec(db_engine)
    calls: list[list[str]] = []
    fake = _fake_embed(calls)
    monkeypatch.setattr("app.app.services.entities.embed_texts", fake)
    monkeypatch.setattr("app.app.services.graph.embed_texts", fake)

    settings = _settings(tmp_data, dry_run=False)
    db = SessionLocal()
    try:
        project, art_a = _project_artifact(db)
        art_b = Artifact(
            artifact_id="art_vec_b",
            project_id=project.id,
            user_id=art_a.user_id,
            original_filename="b.docx",
            stored_filename="b.docx",
            file_path="b.docx",
            file_type="docx",
            source_type="document",
            status="text_extracted",
            sha256="bbb",
        )
        db.add(art_b)
        db.flush()
        persist_entities(db, settings, art_a, EntitiesPayload(decisions=["старое"]))
        persist_entities(db, settings, art_b, EntitiesPayload(decisions=["новое"]))
        db.commit()
        db.refresh(art_b)
        calls.clear()
        seed_graph(db, settings, art_b, "текст")
        db.commit()
        first_seed = len(calls)
        calls.clear()
        seed_graph(db, settings, art_b, "текст")
        assert calls == []
        assert first_seed == 0
    finally:
        db.close()


def test_seed_backfills_missing(tmp_data, db_engine, client, admin_token, monkeypatch):
    from app.app.db import SessionLocal

    _prepare_vec(db_engine)
    calls: list[list[str]] = []
    fake = _fake_embed(calls)
    monkeypatch.setattr("app.app.services.entities.embed_texts", fake)
    monkeypatch.setattr("app.app.services.graph.embed_texts", fake)

    settings = _settings(tmp_data, dry_run=False)
    db = SessionLocal()
    try:
        _project, art = _project_artifact(db)
        persist_entities(db, settings, art, EntitiesPayload(decisions=["a", "b"]))
        db.commit()
        ids = list(db.scalars(select(Entity.id).where(Entity.artifact_id == art.id)))
        delete_vectors_for_entities(db, ids)
        db.commit()
        calls.clear()
        db.refresh(art)
        seed_graph(db, settings, art, "текст")
        db.commit()
        assert len(calls) == 1
        assert len(calls[0]) == 2
        calls.clear()
        seed_graph(db, settings, art, "текст")
        assert calls == []
    finally:
        db.close()


def test_review_edit_upserts_one(tmp_data, db_engine, client, admin_token, monkeypatch):
    from app.app.db import SessionLocal
    from app.app.routers.knowledge import EntityReview, review_entity

    _prepare_vec(db_engine)
    calls: list[list[str]] = []
    fake = _fake_embed(calls)
    monkeypatch.setattr("app.app.services.entities.embed_texts", fake)
    monkeypatch.setattr("app.app.routers.knowledge.embed_texts", fake)

    settings = _settings(tmp_data, dry_run=False)
    db = SessionLocal()
    try:
        _project, art = _project_artifact(db)
        persist_entities(db, settings, art, EntitiesPayload(decisions=["старое"]))
        db.commit()
        ent = db.scalar(select(Entity).where(Entity.artifact_id == art.id))
        user = db.scalar(select(User).limit(1))
        calls.clear()
        review_entity(
            ent.id,
            EntityReview(action="edit", value_json='{"text": "новое"}'),
            db,
            settings,
            user,
        )
        assert len(calls) == 1
        assert calls[0] == [entity_text('{"text": "новое"}')]
        stored = load_entity_vectors(db, [ent.id])
        assert ent.id in stored
    finally:
        db.close()


def _unit(axis: int) -> list[float]:
    vec = [0.0] * EMBED_DIM
    vec[axis] = 1.0
    return vec


def test_search_entities_finds_fact_and_skips_mention(tmp_data, db_engine, client, admin_token):
    from app.app.db import SessionLocal
    from app.app.services.retrieval import search_entities
    from app.app.services.vector_index import upsert_entity_vectors

    _prepare_vec(db_engine)
    db = SessionLocal()
    try:
        project, call = _project_artifact(db)
        other = Artifact(
            artifact_id="art_vec_other",
            project_id=project.id,
            user_id=call.user_id,
            original_filename="budget.docx",
            stored_filename="budget.docx",
            file_path="budget.docx",
            file_type="docx",
            source_type="document",
            status="done",
            sha256="ccc",
        )
        db.add(other)
        db.flush()
        mention = Entity(
            artifact_id=call.id,
            entity_type="mention",
            value_json='{"text": "упомянули бюджет вскользь"}',
            confidence=0.8,
            is_active=True,
            needs_review=True,
        )
        fact = Entity(
            artifact_id=other.id,
            entity_type="fact",
            value_json='{"text": "бюджет 500 тысяч на лицензии 1С"}',
            confidence=0.9,
            is_active=True,
            needs_review=True,
        )
        db.add_all([mention, fact])
        db.flush()
        upsert_entity_vectors(db, [(mention.id, _unit(1)), (fact.id, _unit(0))])
        db.commit()

        hits = search_entities(db, _unit(0), project_id=project.id, min_score=0.35)
        assert [h["entity_id"] for h in hits] == [fact.id]
        assert hits[0]["entity_type"] == "fact"
        assert "бюджет 500 тысяч" in hits[0]["text"]
        assert hits[0]["filename"] == "budget.docx"
    finally:
        db.close()
