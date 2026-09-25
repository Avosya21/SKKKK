"""Правила разрешения противоречий без модели — docs/plans/CONFLICT-AUTONOMY-PLAN.md, решение Р1 (DECISIONS 2026-09-25).

Модель говорит, КАКОЕ отношение между двумя пунктами (отмена, противоречие, уточнение…), код по этим правилам решает,
КАКОЙ пункт в силе. Вес источника:
- 3 — документ заказчика (``priority_weight`` ≥ 0.95, по умолчанию ``document_customer`` = 1.0);
- 2 — решение созвона с явным согласием (``decided_by``) или внутренний документ (``priority_weight`` ≥ 0.8);
- 1 — прочее высказывание;
- 0 — вопрос или вариант (текст пункта кончается «?»): никогда не отменяет решение.
При противоречии без явной отмены в силе более весомый, при равном весе — более поздний. Явная отмена
(«передумали», «отменяем…») работает, только если отменяющий пункт сам не слабее решения (вес ≥ 2) или не слабее отменяемого.
"""

from __future__ import annotations

import re
from typing import Any

from app.app.models.artifact import Artifact

WEIGHT_CUSTOMER_DOC = 3
WEIGHT_AGREED = 2
WEIGHT_STATEMENT = 1
WEIGHT_TENTATIVE = 0
CUSTOMER_DOC_MIN_PRIORITY = 0.95
INTERNAL_DOC_MIN_PRIORITY = 0.8

WEIGHT_LABELS = {
    WEIGHT_CUSTOMER_DOC: "документ заказчика",
    WEIGHT_AGREED: "утверждённое решение",
    WEIGHT_STATEMENT: "высказывание",
    WEIGHT_TENTATIVE: "вопрос или вариант",
}

# Явная отмена прежнего. Без «исправляем»: в живом созвоне это «исправляем ошибку в отчёте», не отмена решения.
CANCEL_PATTERNS = re.compile(
    r"(отменяем|отменили|вместо\s+пункта|передумали|пересмотрели|меняем\s+решение|больше\s+не\s+актуальн)",
    re.I,
)

# Важные темы (Р3): деньги и сроки — авторешение по ним попадает в сводку «Модель изменила важное».
_MONEY = re.compile(r"(\d[\d\s.,]*\s*(млн|млрд|тыс|руб|₽|р\.)|бюджет|стоимост|цена|оплат)", re.I)
_DEADLINE = re.compile(
    # «8.3» — версия платформы 1С, не дата: дата с годом или «до 15.10»
    r"(\b\d{1,2}[./]\d{1,2}[./]\d{2,4}\b|\bдо\s+\d{1,2}[./]\d{1,2}\b|срок|дедлайн|до\s+конца|недел|январ|феврал|март|апрел|ма[йя]\b|июн|июл|август|сентябр|октябр|ноябр|декабр)",
    re.I,
)


def source_weight(entity_type: str, value: dict[str, Any], artifact: Artifact | None, text: str) -> int:
    """Вес источника пункта по Р1."""
    if (text or "").strip().endswith("?"):
        return WEIGHT_TENTATIVE
    is_doc = artifact is not None and artifact.source_type != "call" and artifact.file_type != "audio"
    priority = float(artifact.priority_weight or 0.0) if artifact is not None else 0.0
    if is_doc and priority >= CUSTOMER_DOC_MIN_PRIORITY:
        return WEIGHT_CUSTOMER_DOC
    if is_doc and priority >= INTERNAL_DOC_MIN_PRIORITY:
        return WEIGHT_AGREED
    agreed = [d for d in value.get("decided_by") or [] if isinstance(d, dict) and d.get("name")]
    if entity_type == "decision" and agreed:
        return WEIGHT_AGREED
    return WEIGHT_STATEMENT


def cancel_phrase(lines: list[str]) -> str | None:
    """Фраза явной отмены в источнике пункта (реплики вокруг него / абзац документа)."""
    match = CANCEL_PATTERNS.search(" ".join(lines or []))
    return match.group(0) if match else None


def winner_by_weight(older: dict[str, Any], newer: dict[str, Any]) -> tuple[str, str]:
    """Противоречие без явной отмены: вердикт (``newer_wins``/``older_wins``) и правило."""
    if older["weight"] > newer["weight"]:
        return "older_wins", "rule:source_weight"
    if newer["weight"] > older["weight"]:
        return "newer_wins", "rule:source_weight"
    return "newer_wins", "rule:later_equal_weight"


def cancel_allowed(canceller: dict[str, Any], cancelled: dict[str, Any]) -> bool:
    """Может ли явная отмена в ``canceller`` выключить ``cancelled``: вопрос не отменяет ничего,
    простое высказывание не отменяет утверждённое решение или документ."""
    if canceller["weight"] <= WEIGHT_TENTATIVE:
        return False
    return canceller["weight"] >= min(cancelled["weight"], WEIGHT_AGREED)


def is_material_topic(*texts: str) -> bool:
    """Деньги или сроки — авторешение попадает в сводку важного."""
    joined = " ".join(t for t in texts if t)
    return bool(_MONEY.search(joined) or _DEADLINE.search(joined))
