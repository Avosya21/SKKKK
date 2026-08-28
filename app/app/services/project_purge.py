"""Удаление проекта целиком или одного артефакта: записи БД и файлы на диске."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.logging_setup import log_event
from app.app.models.artifact import Artifact
from app.app.models.chat_guidance import ChatGuidance
from app.app.models.chat_message import ChatMessage
from app.app.models.entity import AsrRun, Changelog, Chunk, Entity, Link, NoteRef, Resolution, UserNote
from app.app.models.job import Job
from app.app.models.project import Project
from app.app.models.project_mcp_token import ProjectMcpToken
from app.app.models.project_task import ProjectTask
from app.app.models.speaker_profile import ArtifactSpeakerLink, SpeakerProfile
from app.app.services.graph import delete_entities_with_deps
from app.app.services.storage import parsed_dir, project_root, raw_input_dir, structured_dir
from app.app.services.vector_index import delete_vectors_for_chunks, delete_vectors_for_entities

logger = logging.getLogger("skkkk.project")


def remove_project_tree(settings: Settings, project_id: int) -> None:
    """Снести только `data/projects/{id}`, без выхода за каталог проектов."""
    parent = (settings.data_dir / "projects").resolve()
    root = project_root(settings, project_id).resolve()
    if root.parent != parent or root.name != str(project_id):
        raise RuntimeError(f"отказ удалять путь {root}")
    if root.is_dir():
        shutil.rmtree(root)


def _is_under(parent: Path, target: Path) -> bool:
    try:
        resolved = target.resolve()
        base = parent.resolve()
    except OSError:
        return False
    return base == resolved or base in resolved.parents


def _unlink_if_in_project(settings: Settings, project_id: int, path: Path | str | None) -> None:
    if not path:
        return
    root = project_root(settings, project_id)
    candidate = Path(path)
    if not _is_under(root, candidate):
        return
    resolved = candidate.resolve()
    if resolved.is_file():
        resolved.unlink()


def _rmtree_named_child(parent: Path, name: str) -> None:
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        return
    try:
        resolved = (parent / name).resolve()
        parent_res = parent.resolve()
    except OSError:
        return
    if resolved.parent != parent_res or resolved.name != name:
        return
    if resolved.is_dir():
        shutil.rmtree(resolved)


def _unlink_prefixed(directory: Path, prefix: str) -> None:
    if not prefix or any(ch in prefix for ch in "*?[]"):
        return
    if not directory.is_dir():
        return
    for path in directory.glob(f"{prefix}_*"):
        if path.is_file() and path.name.startswith(f"{prefix}_"):
            path.unlink()


def remove_artifact_files(settings: Settings, artifact: Artifact | SimpleNamespace) -> None:
    """Снести workspace, оригинал и parsed-файлы только этого artifact_id."""
    pid = artifact.project_id
    aid = artifact.artifact_id
    root = project_root(settings, pid)
    _rmtree_named_child(root / "artifacts", aid)
    _rmtree_named_child(root / "tmp", aid)
    parsed = parsed_dir(settings, pid, artifact.source_type)
    _unlink_prefixed(parsed, aid)
    _unlink_prefixed(structured_dir(settings, pid), aid)
    stored = raw_input_dir(settings, pid, artifact.source_type) / artifact.stored_filename
    _unlink_if_in_project(settings, pid, stored)
    _unlink_if_in_project(settings, pid, artifact.file_path)
    _unlink_if_in_project(settings, pid, artifact.transcription_file_path)


def purge_artifact(db: Session, settings: Settings, artifact: Artifact) -> None:
    """Стереть один артефакт: знания, джобы, файлы. Проект и чужие артефакты не трогает."""
    pid = artifact.project_id
    pk = artifact.id
    aid = artifact.artifact_id
    name = artifact.original_filename
    chunk_ids = list(db.scalars(select(Chunk.id).where(Chunk.artifact_id == pk)))
    entity_ids = list(db.scalars(select(Entity.id).where(Entity.artifact_id == pk)))
    try:
        delete_vectors_for_chunks(db, chunk_ids)
        delete_vectors_for_entities(db, entity_ids)
    except Exception:
        logger.warning("vector cleanup failed for artifact=%s", aid, exc_info=True)

    if entity_ids:
        delete_entities_with_deps(db, entity_ids)

    db.execute(delete(NoteRef).where(NoteRef.target_kind == "artifact", NoteRef.target_id == pk))
    db.execute(delete(Chunk).where(Chunk.artifact_id == pk))
    db.execute(delete(AsrRun).where(AsrRun.artifact_id == pk))
    db.execute(delete(Job).where(Job.artifact_id == pk))
    db.execute(delete(ProjectTask).where(ProjectTask.artifact_db_id == pk))
    db.execute(delete(ArtifactSpeakerLink).where(ArtifactSpeakerLink.artifact_id == aid))
    still_used = {
        *db.scalars(select(ArtifactSpeakerLink.profile_id).where(ArtifactSpeakerLink.profile_id.isnot(None))),
        *db.scalars(
            select(ArtifactSpeakerLink.suggested_profile_id).where(
                ArtifactSpeakerLink.suggested_profile_id.isnot(None)
            )
        ),
    }
    still_used.discard(None)
    profile_q = delete(SpeakerProfile).where(
        SpeakerProfile.project_id == pid,
        SpeakerProfile.source_artifact_id == aid,
        SpeakerProfile.scope == "project",
    )
    if still_used:
        profile_q = profile_q.where(SpeakerProfile.id.notin_(still_used))
    db.execute(profile_q)
    files = SimpleNamespace(
        project_id=pid,
        artifact_id=aid,
        source_type=artifact.source_type,
        stored_filename=artifact.stored_filename,
        file_path=artifact.file_path,
        transcription_file_path=artifact.transcription_file_path,
    )
    db.delete(artifact)
    db.commit()
    remove_artifact_files(settings, files)
    log_event(logger, "artifact.purged", project_id=pid, artifact=aid, file=name)


def purge_project(db: Session, settings: Settings, project: Project) -> None:
    """Стереть сущности, чанки, джобы, файлы и строку проекта."""
    pid = project.id
    name = project.name
    art_pks = list(db.scalars(select(Artifact.id).where(Artifact.project_id == pid)))
    chunk_ids = list(db.scalars(select(Chunk.id).where(Chunk.project_id == pid)))
    entity_ids: list[int] = []
    if art_pks:
        entity_ids = list(db.scalars(select(Entity.id).where(Entity.artifact_id.in_(art_pks))))
    try:
        delete_vectors_for_chunks(db, chunk_ids)
        delete_vectors_for_entities(db, entity_ids)
    except Exception:
        logger.warning("vector cleanup failed for project_id=%s", pid, exc_info=True)

    link_ids = list(db.scalars(select(Link.id).where(Link.project_id == pid)))
    if link_ids:
        db.execute(delete(Resolution).where(Resolution.link_id.in_(link_ids)))
    db.execute(delete(Link).where(Link.project_id == pid))

    note_ids = list(db.scalars(select(UserNote.id).where(UserNote.project_id == pid)))
    if note_ids:
        db.execute(delete(NoteRef).where(NoteRef.note_id.in_(note_ids)))
    db.execute(delete(UserNote).where(UserNote.project_id == pid))

    db.execute(delete(Chunk).where(Chunk.project_id == pid))
    if art_pks:
        db.execute(delete(Entity).where(Entity.artifact_id.in_(art_pks)))
        db.execute(delete(AsrRun).where(AsrRun.artifact_id.in_(art_pks)))
        db.execute(delete(Job).where(Job.artifact_id.in_(art_pks)))

    db.execute(delete(ProjectTask).where(ProjectTask.project_id == pid))
    db.execute(delete(ArtifactSpeakerLink).where(ArtifactSpeakerLink.project_id == pid))
    db.execute(delete(SpeakerProfile).where(SpeakerProfile.project_id == pid))
    db.execute(delete(ChatMessage).where(ChatMessage.project_id == pid))
    db.execute(delete(ChatGuidance).where(ChatGuidance.project_id == pid))
    db.execute(delete(Changelog).where(Changelog.project_id == pid))
    db.execute(delete(ProjectMcpToken).where(ProjectMcpToken.project_id == pid))
    db.execute(delete(Artifact).where(Artifact.project_id == pid))
    db.delete(project)
    db.commit()
    remove_project_tree(settings, pid)
    log_event(logger, "project.purged", project_id=pid, name=name, artifacts=len(art_pks))
