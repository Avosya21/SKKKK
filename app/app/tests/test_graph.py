from app.app.services.graph import CANCEL_PATTERNS, build_graph_payload, purge_orphan_links


def test_cancel_pattern_matches():
    assert CANCEL_PATTERNS.search("Мы отменяем прежнее решение по бюджету")


def test_build_graph_payload_empty(client, db_engine, admin_token):
    r = client.post("/projects", json={"name": "G", "description": ""}, headers={"Authorization": f"Bearer {admin_token}"})
    pid = r.json()["id"]
    r2 = client.get(f"/projects/{pid}/graph/data", headers={"Authorization": f"Bearer {admin_token}"})
    assert r2.status_code == 200
    data = r2.json()
    assert "nodes" in data and "edges" in data


def test_purge_orphan_links_drops_stale_edges(client, db_engine, admin_token, tmp_data):
    import sqlite3

    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.entity import Entity, Link
    from app.app.models.project import Project
    from sqlalchemy import select

    db = SessionLocal()
    p = Project(name="OrphanGraph", description="", created_by=1, is_active=True)
    db.add(p)
    db.commit()
    db.refresh(p)
    art = Artifact(
        project_id=p.id,
        user_id=1,
        artifact_id="art_orphan_link",
        original_filename="a.txt",
        stored_filename="a.txt",
        file_path="/tmp/a.txt",
        sha256="orphanlinkhash",
        file_type="txt",
        source_type="document",
        status="done",
    )
    db.add(art)
    db.flush()
    live = Entity(
        artifact_id=art.id,
        entity_type="fact",
        value_json='{"key": "live"}',
        confidence=0.6,
        is_active=True,
        needs_review=True,
    )
    db.add(live)
    db.commit()
    pid = p.id
    db.close()

    raw = sqlite3.connect(db_engine.url.database)
    raw.execute("PRAGMA foreign_keys=OFF")
    raw.execute(
        "INSERT INTO links (project_id, source_entity_id, target_entity_id, link_type, is_automatic) "
        "VALUES (?, ?, ?, ?, 1)",
        (pid, 999001, 999002, "POTENTIAL_CONFLICT"),
    )
    raw.commit()
    raw.close()

    db2 = SessionLocal()
    n = purge_orphan_links(db2, project_id=pid)
    db2.commit()
    left = list(db2.scalars(select(Link).where(Link.project_id == pid)).all())
    db2.close()
    assert n == 1
    assert left == []


def test_build_graph_payload_does_not_drop_entity_nodes(client, db_engine, admin_token, tmp_data):
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.entity import Entity, Link
    from app.app.models.project import Project

    db = SessionLocal()
    p = Project(name="GraphCap", description="", created_by=1, is_active=True)
    db.add(p)
    db.commit()
    db.refresh(p)
    arts = []
    for i in range(3):
        art = Artifact(
            project_id=p.id,
            user_id=1,
            artifact_id=f"art_graph_cap_{i}",
            original_filename=f"f{i}.txt",
            stored_filename=f"f{i}.txt",
            file_path=f"/tmp/f{i}.txt",
            sha256=f"graphcaphash{i}",
            file_type="txt",
            source_type="document",
            status="done",
        )
        db.add(art)
        arts.append(art)
    db.flush()
    ents = []
    for art in arts:
        ent = Entity(
            artifact_id=art.id,
            entity_type="fact",
            value_json='{"key": "x"}',
            confidence=0.6,
            is_active=True,
            needs_review=True,
        )
        db.add(ent)
        ents.append(ent)
    db.flush()
    db.add(
        Link(
            project_id=p.id,
            source_entity_id=ents[0].id,
            target_entity_id=ents[1].id,
            link_type="SEMANTICALLY_RELATED",
            is_automatic=True,
        )
    )
    db.commit()
    payload = build_graph_payload(db, p.id, limit=2)
    db.close()
    node_ids = {n["id"] for n in payload["nodes"]}
    assert len([n for n in payload["nodes"] if n["kind"] == "artifact"]) == 3
    assert len([n for n in payload["nodes"] if n["kind"] == "entity"]) == 2
    for edge in payload["edges"]:
        assert edge["source"] in node_ids
        assert edge["target"] in node_ids
