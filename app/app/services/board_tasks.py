"""Задачи личной доски: неназванные голоса у загрузившего и неотправлённый анализ у создателя комнаты.

Список не хранится отдельно. Открытая строка есть, пока работа ещё не сделана: другой человек
назвал голоса или отправил созвон — строка пропадает у того, кому она пришла. Снятие урной
помнится в ``user_board_dismissals`` и скрывает строку только у этого человека.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.app.config import get_settings
from app.app.models.artifact import Artifact
from app.app.models.job import Job
from app.app.models.meeting import STATUS_ENDED, Meeting, MeetingParticipant
from app.app.models.project import Project
from app.app.models.user import User
from app.app.models.user_board import KIND_MEETING, KIND_SPEAKERS, UserBoardDismissal
from app.app.services.dashboard import visible_project_ids
from app.app.services.meeting_analyze import STATUS_DONE, STATUS_READY, analyze_state
from app.app.services.speaker_profiles import SPEAKER_MATCH_UI_STATUSES, speaker_action_flags

logger = logging.getLogger("primaryanalitics.board")

DONE_LIMIT = 15
DONE_SCAN = 80
OPEN_MEETING_SCAN = 100
MEETING_FILE_PREFIX = "встреча-"
MEETING_FILE_RE = re.compile(r"^встреча-(\d+)\.wav$")
FINISHED_STATUSES = frozenset(
    {
        "speakers_confirmed",
        "transcript_ready",
        "entities_extracted",
        "indexed",
        "graph_seeded",
        "done",
    }
)
MATERIAL_TITLES = {
    "call": "Созвон",
    "document": "Документ",
    "note": "Заметка",
    "screenshot": "Скриншот",
}


def read_board(db: Session, user: User) -> dict:
    """Открытые задачи этого человека и последние 15 его обработок."""
    visible = visible_project_ids(db, user)
    if not visible:
        return {"open": [], "done": []}
    names = {
        row.id: row.name
        for row in db.scalars(select(Project).where(Project.id.in_(visible))).all()
    }
    hidden = _hidden(db, user.id)
    open_items: list[dict] = []
    open_items.extend(_open_speakers(db, user, visible, names))
    meeting_done, meeting_open = _meetings(db, user, visible, names)
    open_items.extend(meeting_open)
    open_items = [item for item in open_items if (item["kind"], item["ref"]) not in hidden]
    open_items.sort(key=_when, reverse=True)
    done = _done_materials(db, user, visible, names) + meeting_done
    done.sort(key=_when, reverse=True)
    return {"open": [_public(item) for item in open_items], "done": [_public(item) for item in done[:DONE_LIMIT]]}


def dismiss_task(db: Session, user: User, kind: str, ref: str) -> None:
    """Снять открытую задачу. Чужую или уже закрытую работу снять нельзя."""
    if kind not in (KIND_SPEAKERS, KIND_MEETING):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown task")
    current = next(
        (item for item in read_board(db, user)["open"] if item["kind"] == kind and item["ref"] == ref),
        None,
    )
    if current is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task is not open")
    db.add(
        UserBoardDismissal(
            user_id=user.id,
            project_id=current["project_id"],
            kind=kind,
            ref=ref,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()


def _hidden(db: Session, user_id: int) -> set[tuple[str, str]]:
    rows = db.execute(
        select(UserBoardDismissal.kind, UserBoardDismissal.ref).where(UserBoardDismissal.user_id == user_id)
    ).all()
    return {(kind, ref) for kind, ref in rows}


def _open_speakers(db: Session, user: User, visible: set[int], names: dict[int, str]) -> list[dict]:
    arts = db.scalars(
        select(Artifact).where(
            Artifact.user_id == user.id,
            Artifact.project_id.in_(visible),
            Artifact.file_type == "audio",
            Artifact.status.in_(SPEAKER_MATCH_UI_STATUSES),
        )
    ).all()
    items = []
    for art in arts:
        try:
            flags = speaker_action_flags(
                db,
                project_id=art.project_id,
                artifact_id=art.artifact_id,
                status=art.status,
                file_type=art.file_type,
            )
        except Exception:
            logger.warning("speaker flags failed artifact=%s", art.artifact_id, exc_info=True)
            continue
        if not flags.get("speakers_match_available") or flags.get("speakers_match_ready"):
            continue
        items.append(
            _item(
                kind=KIND_SPEAKERS,
                ref=art.artifact_id,
                title="Определить спикеров",
                hint=art.original_filename,
                project_id=art.project_id,
                project_name=names.get(art.project_id, ""),
                href=f"/artifacts/{art.artifact_id}/report-speaker-match",
                at=art.upload_date,
            )
        )
    return items


def _meetings(db: Session, user: User, visible: set[int], names: dict[int, str]) -> tuple[list[dict], list[dict]]:
    created = db.scalars(
        select(Meeting)
        .where(
            Meeting.created_by == user.id,
            Meeting.status == STATUS_ENDED,
            Meeting.project_id.in_(visible),
        )
        .order_by(Meeting.ended_at.desc(), Meeting.id.desc())
        .limit(OPEN_MEETING_SCAN)
    ).all()
    attended_ids = set(
        db.scalars(
            select(Meeting.id)
            .join(MeetingParticipant, MeetingParticipant.meeting_id == Meeting.id)
            .where(
                MeetingParticipant.user_id == user.id,
                Meeting.status == STATUS_ENDED,
                Meeting.project_id.in_(visible),
            )
        ).all()
    )
    created_ids = set(
        db.scalars(
            select(Meeting.id).where(
                Meeting.created_by == user.id,
                Meeting.project_id.in_(visible),
            )
        ).all()
    )
    owned_ids = created_ids | attended_ids
    files = _meeting_files(db, visible)
    done_items = _done_meetings(db, user, owned_ids, files, names)
    open_items = _open_meetings(db, created, files, names)
    return done_items, open_items


def _meeting_files(db: Session, visible: set[int]) -> dict[int, Artifact]:
    """Артефакт `встреча-{id}.wav` значит, что анализ этой комнаты уже отправляли."""
    rows = db.scalars(
        select(Artifact).where(
            Artifact.project_id.in_(visible),
            Artifact.original_filename.startswith(MEETING_FILE_PREFIX),
        )
    ).all()
    found: dict[int, Artifact] = {}
    for art in rows:
        match = MEETING_FILE_RE.match(art.original_filename)
        if match:
            found[int(match.group(1))] = art
    return found


def _open_meetings(db: Session, created: list[Meeting], files: dict[int, Artifact], names: dict[int, str]) -> list[dict]:
    pending = [meeting for meeting in created if meeting.id not in files]
    if not pending:
        return []
    settings = get_settings()
    items = []
    for meeting in pending:
        try:
            state = analyze_state(settings, meeting, db)
        except Exception:
            logger.warning("meeting state failed meeting=%s", meeting.id, exc_info=True)
            continue
        if state.get("analyze_status") != STATUS_READY:
            continue
        items.append(_meeting_item(meeting, names))
    return items


def _done_meetings(
    db: Session,
    user: User,
    owned_ids: set[int],
    files: dict[int, Artifact],
    names: dict[int, str],
) -> list[dict]:
    arts = [art for mid, art in files.items() if mid in owned_ids and art.status in FINISHED_STATUSES]
    if not arts:
        return []
    arts.sort(key=lambda art: _when({"_at": art.upload_date}), reverse=True)
    arts = arts[:DONE_SCAN]
    moments = {
        pk: moment
        for pk, moment in db.execute(
            select(Job.artifact_id, func.max(Job.finished_at)).where(
                Job.artifact_id.in_([art.id for art in arts]),
                Job.finished_at.is_not(None),
            ).group_by(Job.artifact_id)
        ).all()
    }
    meetings = {
        row.id: row
        for row in db.scalars(select(Meeting).where(Meeting.id.in_([_meeting_id(art) for art in arts]))).all()
    }
    items = []
    for art in arts:
        mid = _meeting_id(art)
        meeting = meetings.get(mid) if mid is not None else None
        # У загрузившего неназванные голоса ещё висят задачей, в «Сделано» созвон встанет после них.
        if meeting is None or (art.user_id == user.id and _speakers_still_open(db, art)):
            continue
        item = _meeting_item(meeting, names)
        # В «Сделано» время — конец обработки, не конец самой встречи.
        item["hint"] = ""
        item["_at"] = moments.get(art.id) or art.upload_date or item["_at"]
        items.append(item)
    return items


def _meeting_id(art: Artifact) -> int | None:
    match = MEETING_FILE_RE.match(art.original_filename)
    return int(match.group(1)) if match else None


def _meeting_item(meeting: Meeting, names: dict[int, str]) -> dict:
    return _item(
        kind=KIND_MEETING,
        ref=str(meeting.id),
        title="Анализ внутреннего созвона",
        hint=_clock(meeting.ended_at or meeting.created_at),
        project_id=meeting.project_id,
        project_name=names.get(meeting.project_id, ""),
        href=f"/projects/{meeting.project_id}#meetings",
        at=meeting.ended_at or meeting.created_at,
    )


def _done_materials(db: Session, user: User, visible: set[int], names: dict[int, str]) -> list[dict]:
    arts = db.scalars(
        select(Artifact)
        .where(
            Artifact.user_id == user.id,
            Artifact.project_id.in_(visible),
            Artifact.status.in_(FINISHED_STATUSES),
            or_(Artifact.recorded_at_source.is_(None), Artifact.recorded_at_source != "jira"),
            ~Artifact.original_filename.startswith(MEETING_FILE_PREFIX),
        )
        .order_by(Artifact.upload_date.desc())
        .limit(DONE_SCAN)
    ).all()
    if not arts:
        return []
    finished = {
        pk: moment
        for pk, moment in db.execute(
            select(Job.artifact_id, func.max(Job.finished_at)).where(
                Job.artifact_id.in_([art.id for art in arts]),
                Job.finished_at.is_not(None),
            ).group_by(Job.artifact_id)
        ).all()
    }
    items = []
    for art in arts:
        if art.file_type == "audio" and _speakers_still_open(db, art):
            continue
        items.append(
            _item(
                kind="material",
                ref=art.artifact_id,
                title=MATERIAL_TITLES.get(art.source_type, "Материал"),
                hint=art.original_filename,
                project_id=art.project_id,
                project_name=names.get(art.project_id, ""),
                href=_material_href(art),
                at=finished.get(art.id) or art.upload_date,
            )
        )
    return items


def _speakers_still_open(db: Session, art: Artifact) -> bool:
    try:
        flags = speaker_action_flags(
            db,
            project_id=art.project_id,
            artifact_id=art.artifact_id,
            status=art.status,
            file_type=art.file_type,
        )
    except Exception:
        logger.warning("speaker flags failed artifact=%s", art.artifact_id, exc_info=True)
        return False
    return bool(flags.get("speakers_match_available") and not flags.get("speakers_match_ready"))


def _material_href(art: Artifact) -> str:
    if art.file_type == "audio":
        return f"/artifacts/{art.artifact_id}/report"
    return f"/projects/{art.project_id}"


def _item(**fields) -> dict:
    at = fields.pop("at")
    fields["_at"] = at
    return fields


def _when(item: dict) -> datetime:
    moment = item.get("_at")
    if moment is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def _public(item: dict) -> dict:
    return {
        "kind": item["kind"],
        "ref": item["ref"],
        "title": item["title"],
        "hint": item["hint"],
        "project_id": item["project_id"],
        "project_name": item["project_name"],
        "href": item["href"],
        "at": _iso(item.get("_at")),
    }


def _iso(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _clock(moment: datetime | None) -> str:
    if moment is None:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    local = moment.astimezone()
    return local.strftime("%d.%m.%Y %H:%M")
