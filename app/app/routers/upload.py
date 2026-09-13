from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.config import Settings, get_settings
from app.app.db import get_db
from app.app.dependencies import get_current_user
from app.app.services.project_access import require_project
from app.app.logging_setup import log_event
from app.app.models.artifact import Artifact
from app.app.models.project import Project
from app.app.models.user import User
from app.app.schemas.artifact import ArtifactOut, ArtifactProgressOut
from app.app.services import resumable_upload as resumable
from app.app.services.artifact_download import artifact_download_available, artifact_download_label
from app.app.services.artifact_lab import list_runs as list_lab_runs
from app.app.services.artifact_reports import document_report_url
from app.app.services.pipeline_meeting import is_meeting_artifact
from app.app.services.pipeline_progress import build_artifact_progress, reconcile_artifact_status
from app.app.services.project_purge import purge_artifact
from app.app.services.speaker_profiles import speaker_action_flags
from app.app.services.storage import artifact_report_url
from app.app.services.storage import ensure_inside, generate_stored_filename, raw_input_dir
from app.app.services.upload import (
    create_artifact_record,
    default_priority,
    detect_file_type,
    ffprobe_duration,
    find_duplicate,
    hash_file,
    save_upload_stream,
)
from app.app.services.user_activity import ACTION_DELETE, ACTION_UPLOAD, BLOCK_UPLOAD, log_activity

router = APIRouter(prefix="/projects", tags=["upload"])
logger = logging.getLogger("skkkk.upload")


class UploadInitIn(BaseModel):
    filename: str = Field(min_length=1, max_length=512)
    size: int = Field(ge=0)
    source_type: str = "document"
    client_key: str | None = Field(default=None, max_length=256)


class UploadSessionOut(BaseModel):
    upload_id: str
    filename: str
    size: int
    source_type: str
    chunk_size: int
    total_chunks: int
    received: list[int]
    missing: list[int]
    bytes_received: int


def _session_out(session: resumable.UploadSession) -> UploadSessionOut:
    return UploadSessionOut(
        upload_id=session.upload_id,
        filename=session.filename,
        size=session.size,
        source_type=session.source_type,
        chunk_size=session.chunk_size,
        total_chunks=session.total_chunks,
        received=sorted(session.received),
        missing=resumable.missing_chunks(session),
        bytes_received=session.bytes_received,
    )


def _register_file_on_disk(
    db: Session,
    settings: Settings,
    *,
    project: Project,
    user: User,
    original: str,
    dest: Path,
    sha256: str,
    size: int,
    source_type: str,
    content_type: str | None = None,
) -> ArtifactOut:
    dup = find_duplicate(db, project.id, sha256)
    if dup:
        dest.unlink(missing_ok=True)
        log_event(logger, "upload.duplicate", file=original, existing_artifact=dup.artifact_id)
        return ArtifactOut.model_validate(dup).model_copy(update={"duplicate": True})

    file_type = detect_file_type(original, content_type)
    duration = None
    if file_type == "audio":
        duration = ffprobe_duration(dest)
        if duration and duration > settings.max_audio_duration_sec:
            dest.unlink(missing_ok=True)
            log_event(logger, "upload.rejected", file=original, reason="audio longer than limit", duration_sec=duration)
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Audio too long",
            )

    weight = default_priority(source_type, project.settings())
    stored = dest.name
    art, job = create_artifact_record(
        db,
        project_id=project.id,
        user_id=user.id,
        original_filename=original,
        stored_filename=stored,
        file_path=dest,
        file_type=file_type,
        source_type=source_type,
        sha256=sha256,
        priority_weight=weight,
        duration_sec=duration,
    )
    log_event(
        logger,
        "artifact.created",
        artifact=art.artifact_id,
        file=original,
        file_type=file_type,
        status=art.status,
        duration_sec=duration,
        bytes=size,
    )
    log_event(logger, "job.queued", job_id=job.id, artifact=art.artifact_id, priority=weight)
    log_activity(
        db,
        user_id=user.id,
        project_id=project.id,
        block=BLOCK_UPLOAD,
        action=ACTION_UPLOAD,
        title="Загрузил файл",
        content=original,
        target_kind="artifact",
        target_id=art.artifact_id,
        meta={"file_type": file_type, "source_type": source_type},
    )
    return ArtifactOut.model_validate(art)


@router.get("/{project_id}/artifacts", response_model=list[ArtifactOut])
def list_artifacts(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    require_project(db, user, project_id)
    rows = db.scalars(
        select(Artifact).where(Artifact.project_id == project_id).order_by(Artifact.id.desc())
    ).all()
    updated = False
    for row in rows:
        if reconcile_artifact_status(db, settings, row):
            updated = True
    if updated:
        db.commit()
    for row in rows:
        if row.status != "done":
            continue
        if row.file_type == "audio":
            canonical = artifact_report_url(row.artifact_id)
            old = row.transcript_html_url or ""
            if old != canonical or old.startswith("http://") or old.startswith("https://"):
                row.transcript_html_url = canonical
                updated = True
        elif row.transcription_text:
            old = row.transcript_html_url or ""
            canonical = document_report_url(row)
            if not old or old != canonical or old.startswith("http://") or old.startswith("https://"):
                row.transcript_html_url = canonical
                updated = True
    if updated:
        db.commit()
    out: list[ArtifactOut] = []
    for row in rows:
        item = ArtifactOut.model_validate(row)
        progress = build_artifact_progress(settings, row)
        available = artifact_download_available(settings, row)
        updates: dict = {
            "download_available": available,
            "download_filename": artifact_download_label(row) if available else None,
            "lab_runs": (
                len(list_lab_runs(settings, row))
                if user.role == "admin" and row.file_type == "audio"
                else 0
            ),
            "meeting_lab": user.role == "admin" and is_meeting_artifact(row),
            **speaker_action_flags(
                db,
                project_id=row.project_id,
                artifact_id=row.artifact_id,
                status=row.status,
                file_type=row.file_type,
            ),
        }
        if progress is not None:
            updates["progress"] = progress
        item = item.model_copy(update=updates)
        out.append(item)
    return out


@router.delete("/{project_id}/artifacts/{artifact_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_artifact(
    project_id: int,
    artifact_id: str,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    project = require_project(db, user, project_id)
    art = db.scalar(
        select(Artifact).where(Artifact.project_id == project_id, Artifact.artifact_id == artifact_id)
    )
    if not art:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found")
    filename = art.original_filename
    purge_artifact(db, settings, art)
    log_activity(
        db,
        user_id=user.id,
        project_id=project_id,
        block=BLOCK_UPLOAD,
        action=ACTION_DELETE,
        title="Удалил файл",
        content=filename,
        target_kind="artifact",
        target_id=artifact_id,
    )


@router.post("/{project_id}/uploads/init", response_model=UploadSessionOut)
def init_resumable_upload(
    project_id: int,
    body: UploadInitIn,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    project = require_project(db, user, project_id)
    try:
        session = resumable.create_session(
            settings,
            project_id=project_id,
            user_id=user.id,
            filename=body.filename,
            size=body.size,
            source_type=body.source_type,
            chunk_size=settings.upload_chunk_size,
            client_key=body.client_key,
            max_bytes=settings.max_upload_bytes,
        )
    except ValueError as exc:
        detail = str(exc)
        code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE if "too large" in detail else status.HTTP_400_BAD_REQUEST
        raise HTTPException(status_code=code, detail=detail) from None

    log_event(
        logger,
        "upload.init",
        project_id=project_id,
        upload_id=session.upload_id,
        file=session.filename,
        size=session.size,
        resumed=bool(session.received),
    )
    return _session_out(session)


@router.get("/{project_id}/uploads/{upload_id}", response_model=UploadSessionOut)
def get_resumable_upload(
    project_id: int,
    upload_id: str,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    project = require_project(db, user, project_id)
    try:
        session = resumable.get_session(settings, project_id, upload_id)
    except FileNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload not found") from None
    if session.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    return _session_out(session)


@router.put("/{project_id}/uploads/{upload_id}/chunks/{index}", response_model=UploadSessionOut)
async def put_resumable_chunk(
    project_id: int,
    upload_id: str,
    index: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    project = require_project(db, user, project_id)
    try:
        session = resumable.get_session(settings, project_id, upload_id)
    except FileNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload not found") from None
    if session.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    try:
        expected = resumable.expected_chunk_size(session, index)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None

    data = await request.body()
    if len(data) > expected:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="chunk too large")
    try:
        session = resumable.write_chunk(
            settings,
            project_id=project_id,
            upload_id=upload_id,
            index=index,
            data=data,
            user_id=user.id,
        )
    except PermissionError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden") from None
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None

    return _session_out(session)


@router.post("/{project_id}/uploads/{upload_id}/complete", response_model=ArtifactOut)
def complete_resumable_upload(
    project_id: int,
    upload_id: str,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    project = require_project(db, user, project_id)
    try:
        session = resumable.get_session(settings, project_id, upload_id)
    except FileNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload not found") from None
    if session.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    missing = resumable.missing_chunks(session)
    if missing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"message": "missing chunks", "missing": missing},
        )

    part = resumable.finalize_part_path(settings, project_id, upload_id)
    if not part.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload payload missing")

    sha256, size = hash_file(part)
    if size != session.size:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="size mismatch after assemble")

    stored = generate_stored_filename(session.filename)
    dest_dir = raw_input_dir(settings, project_id, session.source_type)
    dest = ensure_inside(settings.data_dir, dest_dir / stored)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(part), str(dest))
    try:
        result = _register_file_on_disk(
            db,
            settings,
            project=project,
            user=user,
            original=session.filename,
            dest=dest,
            sha256=sha256,
            size=size,
            source_type=session.source_type,
        )
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    resumable.cleanup_session(settings, project_id, upload_id)
    log_event(
        logger,
        "upload.saved",
        file=session.filename,
        bytes=size,
        sha256=sha256[:12],
        path=dest.name,
        mode="resumable",
    )
    return result


@router.post("/{project_id}/upload", response_model=list[ArtifactOut])
async def upload_files(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
    files: Annotated[list[UploadFile], File(...)],
    source_type: Annotated[str, Form()] = "document",
):
    project = require_project(db, user, project_id)

    if not files:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No files")

    log_event(
        logger,
        "upload.received",
        project_id=project_id,
        user=user.name,
        role=user.role,
        files=len(files),
        source_type=source_type,
    )

    out: list[ArtifactOut] = []
    for upload in files:
        original = Path(upload.filename or "file.bin").name
        stored = generate_stored_filename(original)
        dest_dir = raw_input_dir(settings, project_id, source_type)
        dest = ensure_inside(settings.data_dir, dest_dir / stored)
        try:
            sha256, size = save_upload_stream(dest, upload.file, max_bytes=settings.max_upload_bytes)
        except ValueError:
            if dest.exists():
                dest.unlink(missing_ok=True)
            log_event(logger, "upload.rejected", file=original, reason="file exceeds max_upload_bytes")
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="File too large") from None

        log_event(logger, "upload.saved", file=original, bytes=size, sha256=sha256[:12], path=dest.name)
        out.append(
            _register_file_on_disk(
                db,
                settings,
                project=project,
                user=user,
                original=original,
                dest=dest,
                sha256=sha256,
                size=size,
                source_type=source_type,
                content_type=upload.content_type,
            )
        )

    log_event(logger, "upload.done", project_id=project_id, accepted=len(out))
    return out
