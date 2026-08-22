from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.app.logging_setup import log_event
from app.app.models.artifact import Artifact

logger = logging.getLogger("skkkk.status")


def set_status(db: Session, artifact: Artifact, status: str, **extra) -> None:
    """Сменить статус артефакта и зафиксировать переход в логе.

    Единая точка нужна, чтобы в консоли была видна вся цепочка обработки файла,
    а не только её начало и конец.
    """
    previous = artifact.status
    artifact.status = status
    if status == "done":
        artifact.error_message = None
    db.commit()
    log_event(
        logger,
        "artifact.status",
        artifact=artifact.artifact_id,
        file=artifact.original_filename,
        **{"from": previous, "to": status},
        **extra,
    )
