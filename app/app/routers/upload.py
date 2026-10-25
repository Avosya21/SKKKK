"""Загрузка файлов (в том числе частями с докачкой) и список артефактов проекта, удаление артефакта."""

from __future__ import annotations

import logging
import shutil
from datetime import date as date_type
from datetime import datetime as datetime_type
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse
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
from app.app.services.recorded_at import (
    SOURCE_MANUAL,
    SOURCE_MANUAL_TIME,
    manual_recorded_at,
    manual_recorded_at_time,
    recorded_precision,
    set_recorded,
    material_date,
    resolve_recorded_at,
)
from app.app.services.speaker_profiles import speaker_action_flags
from app.app.services.storage import artifact_report_url
from app.app.services.storage import ensure_inside, generate_stored_filename, raw_input_dir
from app.app.services.upload import (
    create_artifact_record,
    default_priority,
    detect_file_type,
    reconcile_source_type,
    ffprobe_duration,
    find_duplicate,
    hash_file,
    save_upload_stream,
)
from app.app.services.user_activity import ACTION_DELETE, ACTION_REVIEW, ACTION_UPLOAD, BLOCK_UPLOAD, log_activity

router = APIRouter(prefix="/projects", tags=["upload"])
logger = logging.getLogger("primaryanalitics.upload")


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


def _reject_unsupported(original: str, content_type: str | None = None, dest: Path | None = None) -> None:
    """Неизвестный формат — отказ 415 при загрузке, а не «no text extracted» потом в очереди."""
    from app.app.services.agent_gateway import is_exchange_filename

    if detect_file_type(original, content_type) != "other" or is_exchange_filename(original):
        return
    if dest is not None:
        dest.unlink(missing_ok=True)
    ext = Path(original).suffix.lower() or "без расширения"
    log_event(logger, "upload.rejected", file=original, reason="unsupported format", ext=ext)
    raise HTTPException(
        status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        detail=f"Формат «{ext}» не поддерживается. Документы: PDF, Word, Excel/CSV, PowerPoint, HTML, текст; "
        "а также аудио/видео и изображения.",
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
    fixed_source = reconcile_source_type(source_type, file_type)
    if fixed_source != source_type:
        log_event(logger, "upload.source_type_corrected", file=original, was=source_type, now=fixed_source)
        source_type = fixed_source
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
    from app.app.services.agent_gateway import handle_exchange_upload, is_exchange_filename

    exchange = is_exchange_filename(original)
    _reject_unsupported(original, content_type, dest)
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
        queue_job=not exchange,
    )
    if exchange:
        decision = handle_exchange_upload(db, settings, art, dest, source=f"user:{user.id}", filename=original)
        log_event(logger, "agent_exchange.upload", artifact=art.artifact_id, file=original, decision=decision.status, risk=decision.risk)
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
    if job is not None:
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
        updates["material_date"], updates["material_date_source"] = material_date(row)
        updates["material_date_precision"] = recorded_precision(row)
        if updates["material_date_source"] == "messages":
            from app.app.services.chat_export import load_artifact_export, period

            export = load_artifact_export(settings, row)
            last = period(export)[1] if export else None
            updates["material_period_end"] = last.date() if last else None
        item = item.model_copy(update=updates)
        out.append(item)
    return out


class RecordedAtIn(BaseModel):
    # None — вернуть автоматическую дату (имя файла → метаданные → дата загрузки)
    date: date_type | None = None
    # местные дата и время начала «2026-07-22T14:30» (Я5): созвон сравнивается с перепиской того же дня
    datetime_local: datetime_type | None = None


@router.patch("/{project_id}/artifacts/{artifact_id}/recorded-at")
def set_artifact_recorded_at(
    project_id: int,
    artifact_id: str,
    body: RecordedAtIn,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    """Дата записи материала вручную (только admin, REQUIREMENTS-PLAN этап 1): от неё зависит «что сказано позже»."""
    require_project(db, user, project_id)
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Дату материала меняет только администратор")
    art = db.scalar(select(Artifact).where(Artifact.project_id == project_id, Artifact.artifact_id == artifact_id))
    if not art:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found")
    if body.datetime_local is not None:
        set_recorded(art, manual_recorded_at_time(body.datetime_local), SOURCE_MANUAL_TIME)
    elif body.date is not None:
        set_recorded(art, manual_recorded_at(body.date), SOURCE_MANUAL)
    else:
        auto_at, auto_source = resolve_recorded_at(
            original_filename=art.original_filename, file_path=None, file_type=art.file_type, upload_date=art.upload_date
        )
        set_recorded(art, auto_at, auto_source)
    db.commit()
    from app.app.services import contract as ctr

    ctr._cache.pop(project_id, None)
    day, source = material_date(art)
    log_activity(db, user_id=user.id, project_id=project_id, block=BLOCK_UPLOAD, action=ACTION_REVIEW,
                 title="Изменил дату материала", content=f"{art.original_filename}: {day or 'неизвестна'} ({source})",
                 target_kind="artifact", target_id=artifact_id)
    return {"artifact_id": artifact_id, "material_date": day, "material_date_source": source, "material_date_precision": recorded_precision(art)}


@router.delete("/{project_id}/artifacts/{artifact_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_artifact(
    project_id: int,
    artifact_id: str,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
    background: BackgroundTasks,
):
    project = require_project(db, user, project_id)
    art = db.scalar(
        select(Artifact).where(Artifact.project_id == project_id, Artifact.artifact_id == artifact_id)
    )
    if not art:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found")
    filename = art.original_filename
    from app.app.services.indexing import chat_export_siblings, reindex_artifacts

    siblings = chat_export_siblings(db, settings, art)
    purge_artifact(db, settings, art)
    if siblings:
        # Сообщения удалённой выгрузки переписки были источником только у неё (Я1): поздние выгрузки берут их себе
        background.add_task(reindex_artifacts, settings, siblings)
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
    from app.app.services import exchange_files as xf

    project = require_project(db, user, project_id)
    max_bytes = settings.max_upload_bytes
    if xf.user_upload_kind(body.filename):  # .cf/.dt — в файлы проекта (посылкой), не в материалы; предел и место — свои
        try:
            xf.check_user_upload(settings, body.size)
        except xf.FilesError as exc:
            code = status.HTTP_507_INSUFFICIENT_STORAGE if exc.code == "disk_full" else status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
            raise HTTPException(status_code=code, detail=str(exc)) from None
        max_bytes = body.size
    else:
        _reject_unsupported(body.filename)
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
            max_bytes=max_bytes,
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

    from app.app.services import exchange_files as xf

    if xf.user_upload_kind(session.filename):
        bundle = xf.store_user_upload(db, settings, project.id, user, part, filename=session.filename, sha256=sha256, size=size)
        resumable.cleanup_session(settings, project_id, upload_id)
        log_activity(db, user_id=user.id, project_id=project.id, block=BLOCK_UPLOAD, action=ACTION_UPLOAD,
                     title="Загрузил файл в файлы проекта", content=session.filename, target_kind="exchange_bundle",
                     target_id=str(bundle["bundle_id"]), meta={"kind": bundle["kind"], "duplicate": bundle["duplicate"]})
        log_event(logger, "upload.exchange", file=session.filename, bytes=size, sha256=sha256[:12], bundle=bundle["bundle_id"],
                  duplicate=bundle["duplicate"])
        # не артефакт: в обработку и материалы не идёт; ответ помечен exchange, форма показывает его отдельно
        return JSONResponse({"exchange": True, "original_filename": session.filename, **bundle})

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
