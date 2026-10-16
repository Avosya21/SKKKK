"""Привязка уже извлечённых пунктов документов к абзацам ``P<n>`` без модели (docs/plans/DECISION-MEMORY-PLAN.md, Э1).

Новые пункты привязываются при извлечении (``entities.bind_document_source``). У старых — ни абзаца, ни цитаты: здесь они
получают их по совпадению слов пункта со словами единицы документа (``source_basis=words``) или по уже проверенной
цитате. Модель не вызывается, решения человека и проверки модели не затрагиваются: правятся только ключи ``source_*``.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.models.artifact import Artifact
from app.app.models.entity import Entity
from app.app.services import contract_text as ct
from app.app.services.doc_units import find_unit_by_quote, split_units
from app.app.services.storage import artifact_knowledge_text

MIN_WORDS_COVERAGE = 0.5  # доля слов пункта, которая должна быть в единице, чтобы привязать по словам
HUMAN_TYPES = ("decision", "task", "open_question")


def document_artifacts(db: Session, settings: Settings, project_id: int) -> list[Artifact]:
    """Готовые материалы-документы проекта (не созвоны и не выгрузки переписки: у них свои адреса — T<n> и M<n>)."""
    from app.app.services.chat_export import artifact_messages_path, load_export

    out = []
    for art in db.scalars(select(Artifact).where(Artifact.project_id == project_id, Artifact.status == "done").order_by(Artifact.id)).all():
        if art.file_type == "audio":  # звук — реплики T<n>; документ, загруженный как «созвон» (source_type=call), всё равно документ
            continue
        if load_export(artifact_messages_path(settings, art)):
            continue
        out.append(art)
    return out


def _statement(value: dict[str, Any]) -> str:
    return str(value.get("text") or value.get("description") or "")


def bind_artifact(db: Session, settings: Settings, art: Artifact, *, apply: bool, rebind: bool = False) -> dict[str, int]:
    """Привязать пункты одного документа. Счётчики: пунктов, уже с абзацем, привязано, осталось без. ``rebind`` — заново и у пунктов
    с абзацем: нумерация единиц документа изменилась (2026-10-09: строки таблиц с подписью «Таблица N: …» стали единицами)."""
    units = split_units(artifact_knowledge_text(settings, art) or "")
    stats = {"items": 0, "already": 0, "bound": 0, "left": 0}
    if not units:
        return stats
    for ent in db.scalars(select(Entity).where(Entity.artifact_id == art.id, Entity.entity_type.in_(HUMAN_TYPES))).all():
        try:
            value = json.loads(ent.value_json or "{}")
        except ValueError:
            continue
        if not isinstance(value, dict):
            continue
        stats["items"] += 1
        if isinstance(value.get("source_paragraph"), int) and not rebind:
            stats["already"] += 1
            continue
        unit, quote, basis = None, None, None
        if value.get("source_quote"):  # цитата уже проверена кодом (этап 2): адрес находим по ней
            unit = find_unit_by_quote(units, str(value["source_quote"]))
            quote = value.get("source_quote")
        if unit is None:
            need = ct.stems(_statement(value))
            if len(need) >= 2:
                scored = [(ct.coverage(need, ct.stems(u.text)), -u.id, u) for u in units]
                score, _neg, best = max(scored, key=lambda t: (t[0], t[1]))
                if score >= MIN_WORDS_COVERAGE:
                    unit, basis = best, "words"
                    found = ct.best_quote(_statement(value), best.text)
                    quote = found[0] if found else best.text[: ct.QUOTE_MAX]
        if unit is None:
            stats["left"] += 1
            if apply and rebind and "source_paragraph" in value:  # прежний номер указывает уже на другую единицу
                value.pop("source_paragraph", None)
                ent.value_json = json.dumps(value, ensure_ascii=False)
            continue
        stats["bound"] += 1
        if apply:
            value["source_paragraph"] = unit.id
            if quote and not value.get("source_quote"):
                value["source_quote"] = quote
                value.pop("source_status", None)
            if basis and not value.get("source_basis"):
                value["source_basis"] = basis
            ent.value_json = json.dumps(value, ensure_ascii=False)
    if apply:
        db.flush()
    return stats


def bind_project(db: Session, settings: Settings, project_id: int, *, apply: bool = False, rebind: bool = False,
                 artifact_ids: set[int] | None = None) -> dict[str, Any]:
    """Привязать пункты всех документов проекта (или только ``artifact_ids``); ``apply=False`` — только посчитать."""
    per: list[dict[str, Any]] = []
    total = {"items": 0, "already": 0, "bound": 0, "left": 0}
    for art in document_artifacts(db, settings, project_id):
        if artifact_ids and art.id not in artifact_ids:
            continue
        stats = bind_artifact(db, settings, art, apply=apply, rebind=rebind)
        per.append({"artifact_id": art.id, "name": art.original_filename, **stats})
        for k in total:
            total[k] += stats[k]
    if apply:
        from app.app.services import contract as ctr

        db.commit()
        ctr._cache.pop(project_id, None)
    return {"project_id": project_id, "total": total, "documents": per}
