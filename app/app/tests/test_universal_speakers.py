import struct

import pytest
from sqlalchemy.orm import sessionmaker

from app.app.services.speaker_profiles import (
    backfill_global_speaker_profiles,
    load_universal_voice_profiles,
    sync_confirmed_to_global,
)


def _vec(*values: float) -> bytes:
    return struct.pack(f"{len(values)}f", *values)


def test_load_universal_voice_profiles_includes_other_project(db_engine):
    from app.app.models.project import Project
    from app.app.models.speaker_profile import ArtifactSpeakerLink, SpeakerProfile

    Session = sessionmaker(bind=db_engine)
    db = Session()
    try:
        p12 = Project(name="p12", description="", created_by=1)
        p13 = Project(name="p13", description="", created_by=1)
        db.add_all([p12, p13])
        db.flush()

        elena = SpeakerProfile(
            scope="project",
            project_id=p12.id,
            display_name="Елена Соколова",
            embedding=_vec(1.0, 0.0, 0.0),
            embedding_dim=3,
        )
        db.add(elena)
        db.flush()
        db.add(
            ArtifactSpeakerLink(
                artifact_id="art_p12",
                project_id=p12.id,
                diar_label="SPEAKER_00",
                profile_id=elena.id,
                status="confirmed",
            )
        )
        db.commit()

        pool = load_universal_voice_profiles(db, auto_backfill=False)
        assert len(pool) == 1
        assert pool[0]["display_name"] == "Елена Соколова"

        names = {row["display_name"] for row in load_universal_voice_profiles(db)}
        assert "Елена Соколова" in names
    finally:
        db.close()


def test_sync_confirmed_to_global_creates_and_merges(db_engine):
    from app.app.models.project import Project
    from app.app.models.speaker_profile import SpeakerProfile

    Session = sessionmaker(bind=db_engine)
    db = Session()
    try:
        project = Project(name="p", description="", created_by=1)
        db.add(project)
        db.flush()

        local = SpeakerProfile(
            scope="project",
            project_id=project.id,
            display_name="Елена Соколова",
            embedding=_vec(1.0, 0.0, 0.0),
            embedding_dim=3,
        )
        db.add(local)
        db.commit()
        db.refresh(local)

        global_prof = sync_confirmed_to_global(db, local)
        assert global_prof.scope == "global"
        assert global_prof.display_name == "Елена Соколова"

        local2 = SpeakerProfile(
            scope="project",
            project_id=project.id,
            display_name="Елена Соколова",
            embedding=_vec(0.99, 0.01, 0.0),
            embedding_dim=3,
        )
        db.add(local2)
        db.commit()
        db.refresh(local2)

        merged = sync_confirmed_to_global(db, local2)
        assert merged.id == global_prof.id
        assert db.query(SpeakerProfile).filter(SpeakerProfile.scope == "global").count() == 1
    finally:
        db.close()


def test_backfill_global_speaker_profiles(db_engine):
    from app.app.models.project import Project
    from app.app.models.speaker_profile import ArtifactSpeakerLink, SpeakerProfile

    Session = sessionmaker(bind=db_engine)
    db = Session()
    try:
        project = Project(name="p", description="", created_by=1)
        db.add(project)
        db.flush()
        prof = SpeakerProfile(
            scope="project",
            project_id=project.id,
            display_name="Anton",
            embedding=_vec(0.0, 1.0, 0.0),
            embedding_dim=3,
        )
        db.add(prof)
        db.flush()
        db.add(
            ArtifactSpeakerLink(
                artifact_id="art_x",
                project_id=project.id,
                diar_label="SPEAKER_01",
                profile_id=prof.id,
                status="confirmed",
            )
        )
        db.commit()

        stats = backfill_global_speaker_profiles(db)
        assert stats["synced"] == 1
        assert stats["globals"] == 1
    finally:
        db.close()
