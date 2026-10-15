"""Итоговая выжимка по разделам из памяти решений и задач (docs/plans/PORTAL-ACCURACY-MANAGER-PLAN.md, Д2).

Для проектов с памятью решений в режиме live. Один вызов модели (как у прежней сборки) пишет «Суть проекта», «Ключевые
решения» и «Что сделано» из компактного входа: карточка проекта, несколько фрагментов про цель и периметр, список предметов
памяти «согласовано / сделано / в документе» одной строкой на предмет. Модель ставит у пунктов номера предметов; код проверяет,
что каждый согласованный и сделанный предмет упомянут, а не упомянутые дописывает короткой строкой — решение не теряется
молча. «Актуальные задачи» (из ``task_board``) и «Открытые вопросы» (вопросы к заказчику и нерешённые противоречия)
собирает код без модели. Номера ``[#id]`` из готового текста убираются.

Почему не одним большим запросом: замер 14.10 — при ~51 тыс. токенов материала модель теряла при сжатии больше фактов,
чем при ~9 тыс. (проект 18: 7/12 → 6/12).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.logging_setup import log_event

logger = logging.getLogger("skkkk.tz")
ROOT = Path(__file__).resolve().parents[3]
PROMPT_PATH = ROOT / "docs" / "prompts" / "tz_sections.md"
PROMPT_VERSION = "tz_sections-v2"
ITEM_STATES = ("agreed", "implemented", "documented")
CHECK_STATES = ("agreed", "implemented")  # их код обязательно находит в тексте
STATE_LABELS = {"agreed": "согласовано", "implemented": "сделано", "documented": "в документе"}
ITEM_SUMMARY_CHARS = 160
ESSENCE_QUERY = "Цель проекта, заказчик и исполнитель, периметр работ, конфигурация 1С, системы и интеграции, этапы"
ESSENCE_CHUNKS = 8
ESSENCE_CHUNK_CHARS = 500
TASKS_IN_SUMMARY = 15
QUESTIONS_IN_SUMMARY = 15
MAX_TOKENS = 6000
_IDS = re.compile(r"\[\s*#\d+(?:\s*,\s*#\d+)*\s*\]")
_ID = re.compile(r"#(\d+)")


def _prompts() -> tuple[str, str]:
    blocks = PROMPT_PATH.read_text(encoding="utf-8").split("```")
    if len(blocks) < 5:
        raise RuntimeError(f"{PROMPT_PATH}: ожидались 2 блока промпта")
    return blocks[1].strip(), blocks[3].strip()


def decision_subjects(db: Session, settings: Settings, project_id: int) -> list:
    """Предметы памяти «согласовано / сделано / в документе» проекта в режиме live, по дате; иначе пусто."""
    from app.app.models.decision_memory import Subject
    from app.app.models.project import Project
    from app.app.services.memory_mode import mode_of

    if mode_of(settings, db.get(Project, project_id)) != "live":
        return []
    rows = db.scalars(select(Subject).where(Subject.project_id == project_id, Subject.is_active.is_(True),
                                            Subject.superseded_by.is_(None), Subject.state.in_(ITEM_STATES))).all()
    return sorted(rows, key=lambda s: (s.state_since or "", s.id))


def item_line(subj: Any) -> str:
    summary = re.sub(r"\s+", " ", subj.summary or "").strip()
    if len(summary) > ITEM_SUMMARY_CHARS:
        summary = summary[:ITEM_SUMMARY_CHARS].rsplit(" ", 1)[0] + "…"
    day = (subj.state_since or "")[:10]
    head = f"#{subj.id} [{STATE_LABELS.get(subj.state, subj.state)}{', ' + day if day else ''}] {subj.title.strip()}"
    return head + (f" — {summary}" if summary else "")


def cited_ids(text: str) -> set[int]:
    return {int(n) for group in _IDS.findall(text or "") for n in _ID.findall(group)}


def strip_ids(text: str) -> str:
    """Убрать номера предметов и строку «Организационное: [...]» из готового текста."""
    lines = [line for line in (text or "").splitlines() if not re.match(r"^\s*-?\s*Организационное\s*:", line)]
    out = _IDS.sub("", "\n".join(lines))
    out = re.sub(r"[ \t]+([.,;:])", r"\1", out)
    return re.sub(r"[ \t]+$", "", out, flags=re.M).strip()


def missing_items(text: str, subjects: list) -> list:
    """Согласованные и сделанные предметы, номера которых модель не поставила ни у одного пункта."""
    have = cited_ids(text)
    return [s for s in subjects if s.state in CHECK_STATES and s.id not in have]


def _essence_fragments(db: Session, settings: Settings, project_id: int) -> str:
    from app.app.services.ollama_client import embed_texts
    from app.app.services.vector_index import search_vectors

    vec = embed_texts(settings, [ESSENCE_QUERY])[0]
    parts = []
    for hit in search_vectors(db, vec, project_id=project_id, limit=ESSENCE_CHUNKS):
        text = re.sub(r"\s+", " ", hit.get("text") or "").strip()
        if text:
            parts.append("- " + (text[:ESSENCE_CHUNK_CHARS] + "…" if len(text) > ESSENCE_CHUNK_CHARS else text))
    return "\n".join(parts) or "—"


def tasks_section(db: Session, project_id: int, *, today: date | None = None) -> str:
    """«Актуальные задачи» из списка задач (с правками менеджера): открытые, сначала просроченные и ближайшие."""
    from app.app.services.task_board import all_tasks

    rows = [t for t in all_tasks(db, project_id, today=today) if t["status"] not in ("done", "cancelled") and not t.get("reference")]
    rows.sort(key=lambda t: (not t.get("overdue"), t.get("deadline_date") is None, t.get("deadline_date") or "", t["description"].casefold()))
    if not rows:
        return ""
    lines = ["## Актуальные задачи", "", "| Кто | Что | Срок |", "| --- | --- | --- |"]
    for t in rows[:TASKS_IN_SUMMARY]:
        due = date.fromisoformat(t["deadline_date"]).strftime("%d.%m.%Y") if t.get("deadline_date") else (t.get("deadline") or "—")
        if t.get("overdue"):
            due += " (просрочено)"
        what = t["description"].replace("|", "/")
        lines.append(f"| {(t.get('assignee') or '—').replace('|', '/')} | {what} | {due} |")
    rest = len(rows) - TASKS_IN_SUMMARY
    if rest > 0:
        lines += ["", f"Ещё {rest} открытых — в блоке «Задачи» на странице проекта."]
    return "\n".join(lines)


def questions_section(db: Session, project_id: int) -> str:
    """«Открытые вопросы и риски»: вопросы к заказчику (без источников) и нерешённые противоречия."""
    from app.app.models.artifact import Artifact
    from app.app.models.entity import Entity
    from app.app.services.conflict_context import conflict_lines
    from app.app.services.task_board import hidden

    items: list[str] = []
    seen: set[str] = set()
    rows = db.scalars(
        select(Entity).join(Artifact, Artifact.id == Entity.artifact_id)
        .where(Artifact.project_id == project_id, Entity.entity_type == "open_question", Entity.is_active.is_(True))
        .order_by(Entity.id.desc())
    ).all()
    for ent in rows:
        try:
            value = json.loads(ent.value_json or "{}")
        except ValueError:
            continue
        text = re.sub(r"\s+", " ", str(value.get("text") or "")).strip()
        key = text.casefold()
        if not text or hidden(value) or key in seen:
            continue
        seen.add(key)
        options = [str(o).strip() for o in value.get("options") or [] if str(o).strip()]
        items.append(f"- {text}" + (f" Варианты: {'; '.join(options)}." if options and "вариант" not in text.casefold() else ""))
        if len(items) >= QUESTIONS_IN_SUMMARY:
            break
    _resolved, unresolved = conflict_lines(db, project_id, limit=10)
    items += unresolved
    return "## Открытые вопросы и риски\n\n" + "\n".join(items) if items else ""


def build_sections_body(db: Session, settings: Settings, project_id: int, *, trace: bool = False) -> tuple[str | None, dict]:
    """Текст выжимки по разделам или (None, {}), если память решений проекта не в live или в ней нечего показать."""
    from app.app.services.extraction_context import read_project_card
    from app.app.services.llm import chat_completion_no_think

    subjects = decision_subjects(db, settings, project_id)
    if not subjects:
        return None, {}
    system, user_tmpl = _prompts()
    card, _meta = read_project_card(settings, project_id)
    items = "\n".join(item_line(s) for s in subjects)
    fragments = _essence_fragments(db, settings, project_id)
    user = user_tmpl.format(card=card.strip() or "—", fragments=fragments, items=items)
    tasks = tasks_section(db, project_id)
    questions = questions_section(db, project_id)
    stats: dict[str, Any] = {"mode": "sections", "prompt_version": PROMPT_VERSION, "subjects": len(subjects),
                             "material_tokens": len(system + user) // 4}
    if trace:
        stats["material"] = user
    db.commit()  # ответ модели идёт десятки секунд — соединение из пула не держим
    raw = chat_completion_no_think(settings, system=system, user=user, max_tokens=MAX_TOKENS, temperature=0.0).strip()
    if not raw:
        raise RuntimeError("tz_sections: модель вернула пустой текст")
    from app.app.services.plain_text import clean_summary_output

    missing = missing_items(raw, subjects)
    body = strip_ids(clean_summary_output(raw))
    if missing:
        tail = "; ".join(f"{s.title.strip()} ({STATE_LABELS[s.state]})" for s in missing)
        body += f"\n\nТакже согласовано и сделано: {tail}."
    body = "\n\n".join(part for part in (body, tasks, questions) if part.strip())
    stats.update(missing=len(missing), cited=len(cited_ids(raw)))
    log_event(logger, "tz.sections.done", project_id=project_id, subjects=len(subjects),
              cited=stats["cited"], missing=len(missing), material_tokens=stats["material_tokens"])
    return body.strip(), stats
