"""Смена статуса артефакта."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.app.logging_setup import log_event
from app.app.models.artifact import Artifact

logger = logging.getLogger("primaryanalitics.status")


def set_status(db: Session, artifact: Artifact, status: str, **extra) -> None:
    """Сменить статус артефакта и зафиксировать переход в логе.

    Единая точка нужна, чтобы в консоли была видна вся цепочка обработки файла,
    а не только её начало и конец.
    """
    previous = artifact.status
    # Имя и номер берём до commit: после него чтение полей открыло бы новую транзакцию, и она висела бы весь следующий вызов модели.
    artifact_ref, file_name = artifact.artifact_id, artifact.original_filename
    artifact.status = status
    if status != "error":
        artifact.error_message = None
    db.commit()
    log_event(
        logger,
        "artifact.status",
        artifact=artifact_ref,
        file=file_name,
        **{"from": previous, "to": status},
        **extra,
    )
