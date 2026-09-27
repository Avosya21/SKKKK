"""Удаление проекта целиком или одного артефакта: записи БД и файлы на диске."""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.logging_setup import log_event
from app.app.models.agent_exchange import AgentExchangeLog
from app.app.models.agent_question import AgentQuestion
from app.app.models.artifact import Artifact
from app.app.models.chat_guidance import ChatGuidance
from app.app.models.chat_message import ChatMessage
from app.app.models.contract import ContractItem
from app.app.models.entity import AsrRun, Changelog, Chunk, Entity, Link, NoteRef, Resolution, Topic, UserNote
from app.app.models.job import Job
from app.app.models.meeting import Meeting, MeetingParticipant
from app.app.models.project import Project
from app.app.models.project_member import ProjectMember
from app.app.models.project_mcp_token import ProjectMcpToken
from app.app.models.project_task import ProjectTask
from app.app.models.speaker_profile import ArtifactSpeakerLink, SpeakerProfile
from app.app.models.user_activity_log import UserActivityLog
from app.app.services.graph import delete_entities_with_deps
from app.app.services.meeting_recordings import remove_room_recordings
from app.app.services.storage import parsed_dir, project_root, raw_input_dir, structured_dir
from app.app.services.vector_index import delete_vectors_for_chunks, delete_vectors_for_entities

logger = logging.getLogger("skkkk.project")

CHILD_DIR_SEP = "__"


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
    _rmtree_child_workspaces(root / "artifacts", aid)
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

    db.execute(delete(Topic).where(Topic.project_id == pid))
    db.execute(delete(ProjectTask).where(ProjectTask.project_id == pid))
    db.execute(delete(ArtifactSpeakerLink).where(ArtifactSpeakerLink.project_id == pid))
    db.execute(delete(SpeakerProfile).where(SpeakerProfile.project_id == pid))
    db.execute(delete(ChatMessage).where(ChatMessage.project_id == pid))
    db.execute(delete(ChatGuidance).where(ChatGuidance.project_id == pid))
    db.execute(delete(UserActivityLog).where(UserActivityLog.project_id == pid))
    db.execute(delete(Changelog).where(Changelog.project_id == pid))
    db.execute(delete(ProjectMember).where(ProjectMember.project_id == pid))
    db.execute(delete(ProjectMcpToken).where(ProjectMcpToken.project_id == pid))
    db.execute(delete(AgentExchangeLog).where(AgentExchangeLog.project_id == pid))
    db.execute(delete(AgentQuestion).where(AgentQuestion.project_id == pid))
    db.execute(delete(ContractItem).where(ContractItem.project_id == pid))
    meeting_ids = list(db.scalars(select(Meeting.id).where(Meeting.project_id == pid)))
    meeting_rooms = list(db.scalars(select(Meeting.room_name).where(Meeting.project_id == pid)))
    if meeting_ids:
        db.execute(delete(MeetingParticipant).where(MeetingParticipant.meeting_id.in_(meeting_ids)))
    db.execute(delete(Meeting).where(Meeting.project_id == pid))
    db.execute(delete(Artifact).where(Artifact.project_id == pid))
    db.delete(project)
    db.commit()
    for room_name in meeting_rooms:
        try:
            remove_room_recordings(settings.data_dir, room_name)
        except ValueError:
            pass
    remove_project_tree(settings, pid)
    log_event(logger, "project.purged", project_id=pid, name=name, artifacts=len(art_pks))


def purge_meeting(db: Session, settings: Settings, meeting: Meeting) -> None:
    """Стереть созвон: очередь, артефакт расшифровки, дорожки комнаты."""
    if meeting.status == "live":
        from fastapi import HTTPException, status

        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="End meeting before deleting it")
    mid = meeting.id
    pid = meeting.project_id
    room = meeting.room_name
    pks = [row.id for row in _meeting_artifacts(db, settings, meeting)]
    job_ids = _analyze_job_ids(settings, room)
    for pk in pks:
        _purge_artifact_if_present(db, settings, pk)
    row = db.get(Meeting, mid)
    if row is None:
        return
    _delete_jobs(db, job_ids, pks)
    db.execute(delete(MeetingParticipant).where(MeetingParticipant.meeting_id == mid))
    db.delete(row)
    db.commit()
    _remove_meeting_room(settings, room)
    log_event(logger, "meeting.purged", project_id=pid, meeting_id=mid, room=room)


def _rmtree_child_workspaces(parent: Path, artifact_id: str) -> None:
    if not parent.is_dir() or not artifact_id:
        return
    prefix = f"{artifact_id}{CHILD_DIR_SEP}"
    names = [path.name for path in parent.iterdir() if path.is_dir() and path.name.startswith(prefix)]
    for name in names:
        _rmtree_named_child(parent, name)


def _artifact_meeting_id(artifact: Artifact) -> int | None:
    from app.app.services.meeting_analyze import MEETING_KIND

    raw = artifact.asr_engines_json or ""
    if MEETING_KIND not in raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("kind") != MEETING_KIND:
        return None
    mid = data.get("meeting_id")
    return int(mid) if mid is not None else None


def _meeting_artifacts(db: Session, settings: Settings, meeting: Meeting) -> list[Artifact]:
    found: dict[int, Artifact] = {}
    for art in db.scalars(select(Artifact).where(Artifact.project_id == meeting.project_id)):
        if _artifact_meeting_id(art) == meeting.id:
            found[art.id] = art
    for key in _analyze_artifact_ids(settings, meeting.room_name):
        art = db.scalar(
            select(Artifact).where(Artifact.project_id == meeting.project_id, Artifact.artifact_id == key)
        )
        if art is None:
            continue
        owner = _artifact_meeting_id(art)
        if owner is not None and owner != meeting.id:
            continue
        found[art.id] = art
    return list(found.values())


def _analyze_payload(settings: Settings, room_name: str) -> dict:
    from app.app.services.meeting_analyze import read_analyze

    return read_analyze(settings.data_dir, room_name) or {}


def _analyze_job_ids(settings: Settings, room_name: str) -> list[int]:
    ids: list[int] = []
    for row in _analyze_payload(settings, room_name).get("jobs") or []:
        job_id = row.get("job_id")
        if job_id is None:
            continue
        ids.append(int(job_id))
    return ids


def _analyze_artifact_ids(settings: Settings, room_name: str) -> list[str]:
    keys: list[str] = []
    for row in _analyze_payload(settings, room_name).get("jobs") or []:
        key = str(row.get("artifact_id") or "")
        if key:
            keys.append(key)
    return keys


def _purge_artifact_if_present(db: Session, settings: Settings, pk: int) -> None:
    art = db.get(Artifact, pk)
    if art is not None:
        purge_artifact(db, settings, art)


def _delete_jobs(db: Session, job_ids: list[int], artifact_pks: list[int]) -> None:
    if not job_ids or not artifact_pks:
        return
    db.execute(delete(Job).where(Job.id.in_(job_ids), Job.artifact_id.in_(artifact_pks)))


# Запись комнаты завершается уже после того, как встречу удалили: процесс записи дописывает events.jsonl, manifest.json
# и дорожки (найдено при аудите целостности — «удалённая» запись возвращалась на диск). Поэтому кроме немедленной
# очистки повторяем её через несколько интервалов. Трогает только каталог только что удалённой комнаты.
ROOM_CLEANUP_RETRY_SEC = (15, 45, 120)


def _remove_meeting_room(settings: Settings, room_name: str) -> None:
    import threading

    def _remove() -> None:
        try:
            remove_room_recordings(settings.data_dir, room_name)
        except ValueError:
            pass
        except OSError:
            logger.warning("meeting.purge.retry_failed room=%s", room_name)

    _remove()
    for delay in ROOM_CLEANUP_RETRY_SEC:
        timer = threading.Timer(delay, _remove)
        timer.daemon = True
        timer.start()
