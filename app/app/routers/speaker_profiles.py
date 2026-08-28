from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.app.dependencies import get_current_user

from app.app.config import Settings, get_settings
from app.app.db import get_db
from app.app.services.project_tasks import (
    TASK_ARTIFACT_REINDEX,
    TASK_CALL_SUMMARY,
    TASK_SPEAKER_ENROLL,
    TASK_SPEAKER_RECALC,
    TaskAlreadyRunning,
    list_active_tasks,
    track_project_task,
)
from app.app.services.speaker_profiles import (
    confirm_speakers_and_enqueue,
    enroll_speaker,
    list_profiles,
    merge_speakers,
    rebuild_artifact_call_summary,
    recalculate_speaker_match,
    refresh_artifact_project_index,
    speakers_confirm_status,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api",
    tags=["speaker-profiles"],
    dependencies=[Depends(get_current_user)],
)


def _artifact_or_404(db: Session, artifact_id: str):
    from app.app.models.artifact import Artifact

    artifact = db.query(Artifact).filter(Artifact.artifact_id == artifact_id).first()
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return artifact


def _task_conflict(exc: TaskAlreadyRunning) -> HTTPException:
    return HTTPException(status_code=409, detail=str(exc))


@router.get("/artifacts/{artifact_id}/tasks/active")
def artifact_active_tasks(artifact_id: str, db: Session = Depends(get_db)):
    artifact = _artifact_or_404(db, artifact_id)
    return {"tasks": list_active_tasks(db, artifact_db_id=artifact.id)}


class EnrollBody(BaseModel):
    diar_label: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=256)


class MergeBody(BaseModel):
    source_diar_label: str = Field(min_length=1, max_length=64)
    target_diar_label: str = Field(min_length=1, max_length=64)


@router.get("/projects/{project_id}/speaker-profiles")
def api_list_profiles(project_id: int, db: Session = Depends(get_db)):
    return list_profiles(db, project_id)


@router.post("/artifacts/{artifact_id}/speaker-enroll")
def api_enroll_speaker(
    artifact_id: str,
    body: EnrollBody,
    db: Session = Depends(get_db),
):
    artifact = _artifact_or_404(db, artifact_id)
    try:
        with track_project_task(
            db,
            task_type=TASK_SPEAKER_ENROLL,
            project_id=artifact.project_id,
            artifact_db_id=artifact.id,
        ):
            profile = enroll_speaker(
                db,
                project_id=artifact.project_id,
                artifact_id=artifact_id,
                diar_label=body.diar_label,
                display_name=body.display_name,
            )
    except TaskAlreadyRunning as exc:
        raise _task_conflict(exc) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("speaker-enroll failed for %s", artifact_id)
        raise HTTPException(status_code=500, detail="Internal Server Error") from exc
    return {"id": profile.id, "display_name": profile.display_name, "scope": profile.scope}


@router.post("/artifacts/{artifact_id}/speaker-merge")
def api_merge_speakers(
    artifact_id: str,
    body: MergeBody,
    db: Session = Depends(get_db),
):
    from app.app.models.artifact import Artifact
    from app.app.services.speaker_profiles import rebuild_match_report_html

    artifact = db.query(Artifact).filter(Artifact.artifact_id == artifact_id).first()
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")
    try:
        result = merge_speakers(
            db,
            project_id=artifact.project_id,
            artifact_id=artifact_id,
            source_diar_label=body.source_diar_label,
            target_diar_label=body.target_diar_label,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    rebuild_match_report_html(db, project_id=artifact.project_id, artifact_id=artifact_id)
    return result


def _lab_match_or_404(artifact, run_id: str, settings: Settings):
    from app.app.services.artifact_lab import lab_voice_match_path

    try:
        return lab_voice_match_path(settings, artifact, run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Not found") from exc


@router.post("/artifacts/{artifact_id}/lab/{run_id}/speaker-enroll")
def api_lab_enroll_speaker(
    artifact_id: str,
    run_id: str,
    body: EnrollBody,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    artifact = _artifact_or_404(db, artifact_id)
    match_path = _lab_match_or_404(artifact, run_id, settings)
    try:
        with track_project_task(
            db,
            task_type=TASK_SPEAKER_ENROLL,
            project_id=artifact.project_id,
            artifact_db_id=artifact.id,
        ):
            profile = enroll_speaker(
                db,
                project_id=artifact.project_id,
                artifact_id=artifact_id,
                diar_label=body.diar_label,
                display_name=body.display_name,
                match_path=match_path,
                rebuild_views=False,
            )
    except TaskAlreadyRunning as exc:
        raise _task_conflict(exc) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("lab speaker-enroll failed for %s %s", artifact_id, run_id)
        raise HTTPException(status_code=500, detail="Internal Server Error") from exc
    return {"id": profile.id, "display_name": profile.display_name, "scope": profile.scope, "run_id": run_id}


@router.post("/artifacts/{artifact_id}/lab/{run_id}/speaker-merge")
def api_lab_merge_speakers(
    artifact_id: str,
    run_id: str,
    body: MergeBody,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    artifact = _artifact_or_404(db, artifact_id)
    match_path = _lab_match_or_404(artifact, run_id, settings)
    try:
        result = merge_speakers(
            db,
            project_id=artifact.project_id,
            artifact_id=artifact_id,
            source_diar_label=body.source_diar_label,
            target_diar_label=body.target_diar_label,
            match_path=match_path,
            rebuild_views=False,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result["run_id"] = run_id
    return result


@router.post("/artifacts/{artifact_id}/speaker-recalculate")
def api_recalculate_speakers(
    artifact_id: str,
    db: Session = Depends(get_db),
):
    from app.app.config import get_settings
    from app.app.services.artifact_audio import _default_sortformer_json, ensure_audio_wav

    artifact = _artifact_or_404(db, artifact_id)
    if artifact.file_type != "audio":
        raise HTTPException(status_code=400, detail="Speaker recalculate is for audio artifacts only")
    settings = get_settings()
    try:
        with track_project_task(
            db,
            task_type=TASK_SPEAKER_RECALC,
            project_id=artifact.project_id,
            artifact_db_id=artifact.id,
        ):
            from app.app.services.artifact_lab import latest_run_id

            lab_run_id = latest_run_id(settings, artifact)
            sortformer_json = None
            audio = None
            if not lab_run_id:
                sortformer_json = _default_sortformer_json(settings, artifact)
                if sortformer_json is None:
                    raise HTTPException(status_code=404, detail="Sortformer JSON not found for artifact")
                audio = ensure_audio_wav(settings, artifact)
                if audio is None:
                    raise HTTPException(status_code=404, detail="Audio WAV not found for artifact")
            result = recalculate_speaker_match(
                db,
                artifact_id=artifact_id,
                project_id=artifact.project_id,
                sortformer_json=sortformer_json,
                audio=audio,
            )
    except TaskAlreadyRunning as exc:
        raise _task_conflict(exc) from exc
    except SystemExit as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Not found") from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return result


@router.get("/artifacts/{artifact_id}/speakers-status")
def api_speakers_status(artifact_id: str, db: Session = Depends(get_db)):
    artifact = _artifact_or_404(db, artifact_id)
    if artifact.file_type != "audio":
        raise HTTPException(status_code=400, detail="Speaker status is for audio artifacts only")
    return speakers_confirm_status(db, project_id=artifact.project_id, artifact_id=artifact_id)


@router.post("/artifacts/{artifact_id}/speakers-confirm")
def api_speakers_confirm(artifact_id: str, db: Session = Depends(get_db)):
    from app.app.config import get_settings

    artifact = _artifact_or_404(db, artifact_id)
    if artifact.file_type != "audio":
        raise HTTPException(status_code=400, detail="Speaker confirm is for audio artifacts only")
    settings = get_settings()
    if not settings.voice_gate_before_asr:
        raise HTTPException(status_code=400, detail="Voice gate disabled")
    try:
        return confirm_speakers_and_enqueue(db, settings, artifact)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/artifacts/{artifact_id}/rebuild-summary")
def api_rebuild_summary(
    artifact_id: str,
    db: Session = Depends(get_db),
):
    artifact = _artifact_or_404(db, artifact_id)
    try:
        with track_project_task(
            db,
            task_type=TASK_CALL_SUMMARY,
            project_id=artifact.project_id,
            artifact_db_id=artifact.id,
        ):
            return rebuild_artifact_call_summary(
                db,
                project_id=artifact.project_id,
                artifact_id=artifact_id,
            )
    except TaskAlreadyRunning as exc:
        raise _task_conflict(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Not found") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/artifacts/{artifact_id}/reindex")
def api_reindex_artifact(
    artifact_id: str,
    db: Session = Depends(get_db),
):
    artifact = _artifact_or_404(db, artifact_id)
    try:
        with track_project_task(
            db,
            task_type=TASK_ARTIFACT_REINDEX,
            project_id=artifact.project_id,
            artifact_db_id=artifact.id,
        ):
            return refresh_artifact_project_index(
                db,
                project_id=artifact.project_id,
                artifact_id=artifact_id,
            )
    except TaskAlreadyRunning as exc:
        raise _task_conflict(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Not found") from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
