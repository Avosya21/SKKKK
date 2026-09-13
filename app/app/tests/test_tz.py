from app.app.services.tz import _humanize_artifact_refs, _pack_map_chunks, tz_paths


def test_tz_paths_created(tmp_path, monkeypatch):
    from app.app.config import get_settings

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    settings = get_settings()
    current, versions = tz_paths(settings, 1)
    assert current.parent.name == "project_state"
    assert versions.is_dir()


def test_pack_map_chunks_respects_limit():
    parts = [
        ("a", "x" * 3000),
        ("b", "y" * 3000),
        ("c", "z" * 1000),
    ]
    chunks = _pack_map_chunks(parts, max_chars=6000)
    assert len(chunks) >= 2
    assert all(len(c) <= 6500 for c in chunks)


def test_pack_map_chunks_splits_huge_section():
    parts = [("big", "word " * 5000)]
    chunks = _pack_map_chunks(parts, max_chars=6000)
    assert len(chunks) >= 2


def test_humanize_artifact_refs_replaces_ids_with_filenames():
    text = "Решение принято. (art_2026-08-23_022008_d78232)"
    mapping = {"art_2026-08-23_022008_d78232": "ТЗ_интеграция.docx"}
    assert _humanize_artifact_refs(text, mapping) == "Решение принято. (ТЗ_интеграция.docx)"


def test_humanize_artifact_refs_keeps_unknown_ids():
    text = "Факт. (art_2026-08-23_999999_abcdef)"
    assert _humanize_artifact_refs(text, {}) == text


def test_collect_project_material_keeps_decision_task_only(tmp_data, db_engine, client, admin_token):
    from sqlalchemy import select

    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.entity import Entity
    from app.app.models.project import Project
    from app.app.models.user import User
    from app.app.services.tz import _collect_project_material

    db = SessionLocal()
    try:
        user = db.scalar(select(User).limit(1))
        project = Project(name="TzFilter", description="", created_by=user.id, is_active=True)
        db.add(project)
        db.flush()
        art = Artifact(
            artifact_id="art_tz_filter",
            project_id=project.id,
            user_id=user.id,
            original_filename="notes.docx",
            stored_filename="notes.docx",
            file_path="notes.docx",
            file_type="docx",
            source_type="document",
            status="done",
            sha256="tzfilter",
        )
        db.add(art)
        db.flush()
        rows = [
            ("decision", '{"text": "делаем обмен через OData"}'),
            ("task", '{"text": "согласовать права обмена"}'),
            ("fact", '{"text": "в базе 120 номенклатур"}'),
            ("mention", '{"text": "упомянули Bitrix"}'),
            ("participant", '{"text": "Иванов"}'),
            ("summary", '{"text": "коротко о встрече"}'),
        ]
        for etype, value in rows:
            db.add(
                Entity(
                    artifact_id=art.id,
                    entity_type=etype,
                    value_json=value,
                    confidence=0.8,
                    is_active=True,
                    needs_review=True,
                )
            )
        db.commit()
        material = _collect_project_material(db, get_settings(), project.id)
        joined = "\n".join(body for _title, body in material)
        assert "[decision]" in joined
        assert "[task]" in joined
        assert "[mention]" not in joined
        assert "[participant]" not in joined
        assert "[summary]" not in joined
        assert "[fact]" not in joined
    finally:
        db.close()
