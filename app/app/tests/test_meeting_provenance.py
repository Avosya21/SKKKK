import json

from app.app.config import get_settings
from app.app.models.entity import Entity
from app.app.services.entities import EntitiesPayload, TaskItem, persist_entities
from app.app.services.meeting_merge import attach_turn_ids
from app.app.services.storage import artifact_dir


def _call_art(db, source_type="call"):
    from sqlalchemy import select

    from app.app.models.artifact import Artifact
    from app.app.models.project import Project
    from app.app.models.user import User

    user = db.scalar(select(User).limit(1))
    project = Project(name="Prov", created_by=user.id)
    db.add(project)
    db.flush()
    art = Artifact(
        artifact_id="art_prov_1",
        project_id=project.id,
        user_id=user.id,
        original_filename="встреча.wav",
        stored_filename="встреча.wav",
        file_path="встреча.wav",
        file_type="audio",
        source_type=source_type,
        sha256="a" * 64,
        status="transcript_ready",
    )
    db.add(art)
    db.flush()
    return project, art


def _write_proof(settings, project_id, artifact_id, replicas, leaks=None, attach_ids=True):
    from asr.lab.paths import LabRun, new_run_id
    from asr.lab.run import init_run

    root = artifact_dir(settings, project_id, artifact_id)
    (root / "audio").mkdir(parents=True, exist_ok=True)
    wav = root / "audio" / "input_16k.wav"
    wav.write_bytes(b"RIFFfake")
    run = LabRun(root, new_run_id())
    init_run(run, artifact_id=artifact_id, project_id=project_id, source_audio=wav)
    proof = run.resolve("proof/transcript.json")
    proof.parent.mkdir(parents=True, exist_ok=True)
    rows = attach_turn_ids(replicas, artifact_id) if attach_ids else list(replicas)
    proof.write_text(
        json.dumps({"replicas": rows, "words": [], "leaks": leaks or []}, ensure_ascii=False),
        encoding="utf-8",
    )
    (root / "lab" / "latest.json").write_text(json.dumps({"run_id": run.run_id}), encoding="utf-8")
    return rows


def test_turn_prompt_lists_span_ids(tmp_data, client, admin_token):
    from app.app.db import SessionLocal
    from app.app.services.entities import _turn_prompt

    settings = get_settings()
    db = SessionLocal()
    try:
        project, art = _call_art(db)
        rows = _write_proof(
            settings,
            project.id,
            art.artifact_id,
            [{"speaker": "Анна", "start": 12.0, "end": 14.0, "text": "ставим задачу X"}],
        )
        db.commit()
        prompt = _turn_prompt(settings, art)
        assert rows[0]["source_span_id"] in prompt
        assert "ставим задачу X" in prompt
    finally:
        db.close()


def test_call_entity_keeps_turn_source(tmp_data, client, admin_token):
    from app.app.db import SessionLocal

    settings = get_settings()
    db = SessionLocal()
    try:
        project, art = _call_art(db)
        replicas = _write_proof(
            settings,
            project.id,
            art.artifact_id,
            [{"speaker": "Анна", "start": 12.0, "end": 14.0, "text": "ставим задачу X"}],
        )
        persist_entities(
            db,
            settings,
            art,
            EntitiesPayload(artifact_id=art.artifact_id, source_type="call", tasks=[]),
        )
        persist_entities(
            db,
            settings,
            art,
            EntitiesPayload(artifact_id=art.artifact_id, source_type="call", decisions=["ставим задачу X"]),
        )
        db.commit()
        row = db.query(Entity).filter(Entity.artifact_id == art.id, Entity.entity_type == "decision").one()
        value = json.loads(row.value_json)
        assert value["source_span_id"] == replicas[0]["source_span_id"]
        assert value["speaker"] == "Анна"
        assert value["start_sec"] == 12.0
    finally:
        db.close()


def test_unmatched_call_entity_kept_for_review(tmp_data, client, admin_token):
    from app.app.db import SessionLocal

    settings = get_settings()
    db = SessionLocal()
    try:
        project, art = _call_art(db)
        _write_proof(
            settings,
            project.id,
            art.artifact_id,
            [{"speaker": "Анна", "start": 1.0, "end": 2.0, "text": "привет"}],
        )
        persist_entities(
            db,
            settings,
            art,
            EntitiesPayload(artifact_id=art.artifact_id, source_type="call", decisions=["выдуманный факт"]),
        )
        db.commit()
        row = db.query(Entity).filter(Entity.artifact_id == art.id, Entity.entity_type == "decision").one()
        value = json.loads(row.value_json)
        assert value["source_span_id"] is None
        assert row.needs_review is True
    finally:
        db.close()


def test_paraphrase_task_keeps_source_span(tmp_data, client, admin_token):
    from app.app.db import SessionLocal

    settings = get_settings()
    db = SessionLocal()
    try:
        project, art = _call_art(db)
        replicas = _write_proof(
            settings,
            project.id,
            art.artifact_id,
            [{"speaker": "Анна", "start": 8.0, "end": 11.0, "text": "выгрузку надо к пятнице сделать"}],
        )
        persist_entities(
            db,
            settings,
            art,
            EntitiesPayload(
                artifact_id=art.artifact_id,
                source_type="call",
                tasks=[TaskItem(description="сделать выгрузку до пятницы")],
            ),
        )
        db.commit()
        row = db.query(Entity).filter(Entity.artifact_id == art.id, Entity.entity_type == "task").one()
        value = json.loads(row.value_json)
        assert value["source_span_id"] == replicas[0]["source_span_id"]
        assert value["speaker"] == "Анна"
    finally:
        db.close()


def test_external_call_without_span_leaves_payload(tmp_data, client, admin_token):
    from app.app.db import SessionLocal

    settings = get_settings()
    db = SessionLocal()
    try:
        project, art = _call_art(db)
        _write_proof(
            settings,
            project.id,
            art.artifact_id,
            [{"speaker": "Анна", "start": 1.0, "end": 2.0, "text": "привет"}],
            attach_ids=False,
        )
        persist_entities(
            db,
            settings,
            art,
            EntitiesPayload(
                artifact_id=art.artifact_id,
                source_type="call",
                decisions=["внешний факт"],
                summary="итог внешней записи",
            ),
        )
        db.commit()
        row = db.query(Entity).filter(Entity.artifact_id == art.id, Entity.entity_type == "decision").one()
        value = json.loads(row.value_json)
        assert "source_span_id" not in value
        assert value["text"] == "внешний факт"
        summary = db.query(Entity).filter(Entity.artifact_id == art.id, Entity.entity_type == "summary").one()
        assert json.loads(summary.value_json)["text"] == "итог внешней записи"
    finally:
        db.close()


def test_legacy_call_without_proof_leaves_payload(tmp_data, client, admin_token):
    from app.app.db import SessionLocal

    settings = get_settings()
    db = SessionLocal()
    try:
        _project, art = _call_art(db)
        persist_entities(
            db,
            settings,
            art,
            EntitiesPayload(
                artifact_id=art.artifact_id,
                source_type="call",
                decisions=["legacy решение"],
                summary="legacy выжимка",
            ),
        )
        db.commit()
        row = db.query(Entity).filter(Entity.artifact_id == art.id, Entity.entity_type == "decision").one()
        value = json.loads(row.value_json)
        assert "source_span_id" not in value
        assert value["text"] == "legacy решение"
        summary = db.query(Entity).filter(Entity.artifact_id == art.id, Entity.entity_type == "summary").one()
        assert json.loads(summary.value_json)["text"] == "legacy выжимка"
    finally:
        db.close()


def test_summary_survives_unmatched_room_call(tmp_data, client, admin_token):
    from app.app.db import SessionLocal

    settings = get_settings()
    db = SessionLocal()
    try:
        project, art = _call_art(db)
        _write_proof(
            settings,
            project.id,
            art.artifact_id,
            [{"speaker": "Анна", "start": 1.0, "end": 2.0, "text": "привет"}],
        )
        persist_entities(
            db,
            settings,
            art,
            EntitiesPayload(
                artifact_id=art.artifact_id,
                source_type="call",
                summary="краткая выжимка всего созвона",
            ),
        )
        db.commit()
        from app.app.services.storage import structured_dir

        dumped = json.loads(
            (structured_dir(settings, project.id) / f"{art.artifact_id}_entities.json").read_text(encoding="utf-8")
        )
        assert dumped["summary"] == "краткая выжимка всего созвона"
        row = db.query(Entity).filter(Entity.artifact_id == art.id, Entity.entity_type == "summary").one()
        assert json.loads(row.value_json)["text"] == "краткая выжимка всего созвона"
    finally:
        db.close()


def test_leaked_text_does_not_become_entity(tmp_data, client, admin_token):
    from app.app.db import SessionLocal

    settings = get_settings()
    db = SessionLocal()
    try:
        project, art = _call_art(db)
        _write_proof(
            settings,
            project.id,
            art.artifact_id,
            [{"speaker": "Анна", "start": 1.0, "end": 2.0, "text": "один два три"}],
            leaks=[{"text": "один два три", "start": 1.0, "end": 2.0, "speaker": "B", "from_speaker": "A", "by": "rank"}],
        )
        persist_entities(
            db,
            settings,
            art,
            EntitiesPayload(artifact_id=art.artifact_id, source_type="call", decisions=["один два три"]),
        )
        db.commit()
        assert db.query(Entity).filter(Entity.artifact_id == art.id, Entity.entity_type == "decision").count() == 0
    finally:
        db.close()


def test_leaked_text_stays_out_of_file_and_tz(tmp_data, client, admin_token):
    from app.app.db import SessionLocal
    from app.app.services.storage import structured_dir
    from app.app.services.tz import _collect_project_material

    settings = get_settings()
    db = SessionLocal()
    leak_text = "один два три"
    keep_text = "ставим задачу X"
    try:
        project, art = _call_art(db)
        _write_proof(
            settings,
            project.id,
            art.artifact_id,
            [
                {"speaker": "Анна", "start": 1.0, "end": 2.0, "text": keep_text},
                {"speaker": "Анна", "start": 3.0, "end": 4.0, "text": leak_text},
            ],
            leaks=[{"text": leak_text, "start": 3.0, "end": 4.0, "speaker": "B", "from_speaker": "A", "by": "rank"}],
        )
        persist_entities(
            db,
            settings,
            art,
            EntitiesPayload(
                artifact_id=art.artifact_id,
                source_type="call",
                decisions=[keep_text, leak_text],
            ),
        )
        db.commit()
        dumped = (structured_dir(settings, project.id) / f"{art.artifact_id}_entities.json").read_text(
            encoding="utf-8"
        )
        assert keep_text in dumped
        assert leak_text not in dumped
        material = "\n".join(body for _title, body in _collect_project_material(db, settings, project.id))
        assert keep_text in material
        assert leak_text not in material
    finally:
        db.close()


def test_document_entity_still_persists_without_turn(tmp_data, client, admin_token):
    from app.app.db import SessionLocal

    settings = get_settings()
    db = SessionLocal()
    try:
        _project, art = _call_art(db, source_type="document")
        persist_entities(
            db,
            settings,
            art,
            EntitiesPayload(artifact_id=art.artifact_id, source_type="document", decisions=["из документа"]),
        )
        db.commit()
        assert db.query(Entity).filter(Entity.artifact_id == art.id, Entity.entity_type == "decision").count() == 1
    finally:
        db.close()
