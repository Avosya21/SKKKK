"""Речевые акты реплик переписки и созвонов (docs/plans/DECISION-MEMORY-PLAN.md, Э2).

Модель отвечает на один узкий вопрос по окну реплик: что делает реплика в обсуждении решений (предложил, согласился,
отклонил, отложил, спросил, ответил, сообщил о выполненном, отменил прежнее, сообщил о препятствии, сообщил факт).
Код всё проверяет: метка реплики должна быть из целевых, вид акта — из списка, цитата — дословно в тексте этой реплики;
остальное отбрасывается. Автор и сторона берутся из реестра людей проекта (если человек там есть), а не из ответа модели.
Результат — таблица ``speech_acts`` (``models/speech_act.py``); строки материала заменяются целиком при повторном разборе.
Промпт — ``docs/prompts/speech_acts.md``. Документ (без говорящих) размечается по единицам ``P<n>`` своим промптом
``docs/prompts/speech_acts_doc.md``: автор — документ, сторона, дата и утверждение — из паспорта (``services/doc_memory.py``);
вид «зафиксировано в документе» (``documented``) бывает только у единиц документа.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.logging_setup import log_event
from app.app.models.artifact import Artifact
from app.app.models.decision_memory import DecisionEvent
from app.app.models.speech_act import ACTS, SpeechAct

logger = logging.getLogger("primaryanalitics.speech_acts")

ROOT = Path(__file__).resolve().parents[3]
PROMPT_PATH = ROOT / "docs" / "prompts" / "speech_acts.md"
PROMPT_VERSION = "speech_acts-v5"  # v5: второй проход — проверка видов актов по контексту (speech_acts_review.md)
TARGETS_PER_WINDOW = 8  # целевых реплик в одном вопросе к модели
CONTEXT_UNITS = 3  # реплик до окна — только контекст
WINDOW_CHARS = 4500  # предел знаков целевых реплик в окне
MIN_QUOTE = 4
MAX_ABOUT = 160
MAX_ACTS_PER_UNIT = 3
MAX_TOKENS = 2048
DOC_MAX_TOKENS = 4096  # документ: строка плана или длинный абзац дают несколько пунктов


@dataclass(frozen=True)
class DUnit:
    """Реплика переписки (``M<n>``), созвона (``T<n>``) или единица документа (``P<n>``)."""

    ref: str
    key: int
    author: str
    text: str
    said_at: str | None = None
    start_sec: float | None = None
    stamp: str = ""  # время для строки окна
    section: str = ""  # документ: раздел (последний заголовок)
    lead: str = ""  # документ: шапка таблицы для строки


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower().replace("ё", "е")).strip()


def dialog_units(settings: Settings, artifact: Artifact) -> list[DUnit]:
    """Реплики материала: сообщения выгрузки переписки или реплики речи созвона. Документ без говорящих — пустой список."""
    from app.app.services.chat_export import artifact_messages_path, load_export

    if artifact.file_type != "audio":
        export = load_export(artifact_messages_path(settings, artifact))
        if not export:
            return []
        out = []
        for m in export["messages"]:
            text = _flat(m.get("text") or "")
            if not text:
                continue
            at = str(m.get("at") or "")
            day_only = m.get("at_precision") == "day"
            out.append(DUnit(f"M{m['id']}", int(m["id"]), str(m.get("author") or "?"), text, at or None, None,
                             at[:10] if day_only else at[:16].replace("T", " ")))
        return out
    from app.app.services.recorded_at import recorded_moment
    from app.app.services.transcript_turns import format_timecode, load_turns

    start = recorded_moment(artifact)
    out = []
    for t in load_turns(settings, artifact):
        text = _flat(t.text)
        if not t.is_speech or not text:
            continue
        said = None
        if start is not None and t.start_sec is not None:
            from datetime import timedelta

            said = (start + timedelta(seconds=float(t.start_sec))).isoformat()
        out.append(DUnit(f"T{t.index}", int(t.index), t.known_speaker or (t.speaker or "?"), text, said,
                         float(t.start_sec) if t.start_sec is not None else None, format_timecode(t.start_sec) if t.start_sec is not None else ""))
    return out


def windows(units: list[DUnit]) -> list[tuple[list[DUnit], list[DUnit]]]:
    """Окна: (контекст до, целевые реплики). Целевые идут подряд, ``TARGETS_PER_WINDOW`` штук или ``WINDOW_CHARS`` знаков."""
    out: list[tuple[list[DUnit], list[DUnit]]] = []
    i = 0
    while i < len(units):
        j, size = i, 0
        while j < len(units) and j - i < TARGETS_PER_WINDOW and (j == i or size + len(units[j].text) <= WINDOW_CHARS):
            size += len(units[j].text)
            j += 1
        out.append((units[max(0, i - CONTEXT_UNITS): i], units[i:j]))
        i = j
    return out


def _line(mark: str, u: DUnit) -> str:
    stamp = f" [{u.stamp}]" if u.stamp else ""
    return f"{mark} {u.ref}{stamp} {u.author}: {u.text}"


def build_user(context: list[DUnit], targets: list[DUnit]) -> str:
    lines = [_line("·", u) for u in context] + [_line(">", u) for u in targets]
    return "Окно реплик:\n" + "\n".join(lines)


def _system() -> str:
    from app.app.services.prompts import load_prompt_block

    return load_prompt_block(PROMPT_PATH)


def parse_acts(raw: str, targets: list[DUnit]) -> tuple[list[dict[str, str]], dict[str, int]]:
    """Ответ модели -> проверенные акты. Возвращает (акты, счётчик отброшенного по причинам)."""
    from app.app.services.llm import strip_json_fence

    data = json.loads(strip_json_fence(raw))
    items = data.get("acts") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError("нет списка acts")
    by_ref = {u.ref: u for u in targets}
    seen_per_unit: dict[str, int] = {}
    dropped = {"unit": 0, "act": 0, "quote": 0, "limit": 0, "duplicate": 0}
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        ref = str(item.get("unit") or "").strip().upper()
        unit = by_ref.get(ref)
        if unit is None:
            dropped["unit"] += 1
            continue
        act = str(item.get("act") or "").strip().lower()
        if act not in ACTS or (act == "documented" and not ref.startswith("P")):  # «в документе» — только у единиц документа
            dropped["act"] += 1
            continue
        quote = _flat(str(item.get("quote") or ""))
        if len(quote) < MIN_QUOTE or _norm(quote).strip(" .,;:!?«»\"'()") not in _norm(unit.text):
            dropped["quote"] += 1
            continue
        key = (ref, act, _norm(quote))
        if key in seen:
            dropped["duplicate"] += 1
            continue
        if seen_per_unit.get(ref, 0) >= MAX_ACTS_PER_UNIT:
            dropped["limit"] += 1
            continue
        seen.add(key)
        seen_per_unit[ref] = seen_per_unit.get(ref, 0) + 1
        out.append({"unit": ref, "act": act, "quote": quote, "about": _flat(str(item.get("about") or ""))[:MAX_ABOUT]})
    return out, dropped


REVIEW_PATH = ROOT / "docs" / "prompts" / "speech_acts_review.md"
REVIEW_CONTEXT_UNITS = 5  # реплик контекста у проверяющего прохода


def _review_system() -> str:
    from app.app.services.prompts import load_prompt_block

    return load_prompt_block(REVIEW_PATH)


def build_review_user(context: list[DUnit], targets: list[DUnit], draft: list[dict[str, str]]) -> str:
    lines = build_user(context, targets)
    draft_json = json.dumps({"acts": draft}, ensure_ascii=False)
    return f"{lines}\n\nЧерновая разметка:\n{draft_json}"


def _ask_default(settings: Settings, max_tokens: int = MAX_TOKENS) -> Callable[[str, str], str]:
    from app.app.services.llm import chat_completion_no_think

    def ask(system: str, user: str) -> str:
        return chat_completion_no_think(settings, system=system, user=user, max_tokens=max_tokens)

    return ask


def _author_card(db: Session, project_id: int, author: str, cache: dict[str, tuple[int | None, str | None]]) -> tuple[int | None, str | None]:
    if author in cache:
        return cache[author]
    from app.app.services import people

    hit = people.resolve(db, project_id, author)
    value = (hit.person.id, hit.person.side) if hit is not None else (None, None)
    cache[author] = value
    return value


def extract_artifact(db: Session, settings: Settings, artifact: Artifact, *, ask: Callable[[str, str], str] | None = None,
                     only_refs: set[str] | None = None, review: bool = True) -> dict[str, Any]:
    """Разобрать материал: окна -> модель -> проверка -> замена строк ``speech_acts`` материала. ``only_refs`` — только эти реплики
    (для проверок); тогда прежние строки этих реплик заменяются, остальные не трогаются."""
    units = dialog_units(settings, artifact)
    own_ask = ask is None
    ask = ask or _ask_default(settings)
    doc = None
    if not units:
        from app.app.services import doc_memory

        doc = doc_memory.prepare(settings, artifact, ask, release=db.commit)
        units = doc.units if doc is not None else []
    stats: dict[str, Any] = {"artifact_id": artifact.id, "units": len(units), "windows": 0, "acts": 0, "errors": 0,
                             "dropped": {"unit": 0, "act": 0, "quote": 0, "limit": 0, "duplicate": 0},
                             "reviewed": 0, "changed": 0, "review_errors": 0}
    if doc is not None:
        stats["document"] = {k: doc.passport.get(k) for k in ("kind", "author_side", "approved", "approved_by", "date")}
    if not units:
        return stats
    if doc is not None:
        from app.app.services import doc_memory

        system, review = doc_memory.acts_system(), False  # у документа нет реакций на чужие реплики: проверочный проход реплик не нужен
        if own_ask:  # строка плана или длинный абзац дают много пунктов: ответу нужен запас
            ask = _ask_default(settings, max_tokens=DOC_MAX_TOKENS)
    else:
        system = _system()
    review_system = _review_system() if review else ""
    rows: list[SpeechAct] = []
    cache: dict[str, tuple[int | None, str | None]] = {}
    db.commit()  # соединение БД не держим на время ответа модели
    pending_windows = list(windows(units))
    while pending_windows:
        context, targets = pending_windows.pop(0)
        if only_refs is not None:
            targets = [u for u in targets if u.ref in only_refs]
            if not targets:
                continue
        stats["windows"] += 1
        user = doc.build_user(context, targets) if doc is not None else build_user(context, targets)
        acts: list[dict[str, str]] = []
        for attempt in range(2):
            try:
                acts, dropped = parse_acts(ask(system, user), targets)
                for k, v in dropped.items():
                    stats["dropped"][k] += v
                break
            except (ValueError, json.JSONDecodeError, RuntimeError) as exc:
                if attempt == 0:
                    user += f"\n\nПредыдущий ответ невалиден: {exc}. Верни только JSON вида {{\"acts\": [...]}}."
                elif doc is not None and len(targets) > 1:
                    # документ: плотная порция не уместилась в ответ — делим пополам, контекст второй половины — конец первой
                    half = len(targets) // 2
                    pending_windows[:0] = [(context, targets[:half]), ((context + targets[:half])[-CONTEXT_UNITS:], targets[half:])]
                    stats["split"] = stats.get("split", 0) + 1
                    log_event(logger, "speech_acts.window_split", artifact=artifact.artifact_id, unit=targets[0].ref, units=len(targets))
                    acts = None
                else:
                    stats["errors"] += 1
                    log_event(logger, "speech_acts.window_failed", artifact=artifact.artifact_id, unit=targets[0].ref, error=str(exc)[:200])
        if acts is None:
            stats["windows"] -= 1  # порция разделена: считаются её половины
            continue
        if review and acts is not None:
            stats["reviewed"] += 1
            wide = units[max(0, units.index(targets[0]) - REVIEW_CONTEXT_UNITS): units.index(targets[0])]
            try:
                fixed, dropped = parse_acts(ask(review_system, build_review_user(wide, targets, acts)), targets)
                if fixed or not acts:
                    stats["changed"] += int([(a["unit"], a["act"]) for a in fixed] != [(a["unit"], a["act"]) for a in acts])
                    acts = fixed
            except (ValueError, json.JSONDecodeError, RuntimeError) as exc:
                stats["review_errors"] += 1
                log_event(logger, "speech_acts.review_failed", artifact=artifact.artifact_id, unit=targets[0].ref, error=str(exc)[:200])
        by_ref = {u.ref: u for u in targets}
        for a in acts:
            unit = by_ref[a["unit"]]
            kind, said_at = a["act"], unit.said_at
            if doc is not None:
                person_id = None
                kind, side = doc.act_and_side(kind)
                said_at = doc.said_at
            else:
                person_id, side = _author_card(db, artifact.project_id, unit.author, cache)
            rows.append(SpeechAct(project_id=artifact.project_id, artifact_id=artifact.id, unit_ref=unit.ref, unit_key=unit.key,
                                  start_sec=unit.start_sec, said_at=said_at, author=unit.author, person_id=person_id, side=side,
                                  act=kind, quote=a["quote"], about=a["about"] or None,
                                  prompt_version=doc_acts_version() if doc is not None else PROMPT_VERSION, model=settings.lmstudio_model))
        db.rollback()  # чтение карточек автора открыло транзакцию: перед следующим ответом модели её закрываем
    old_acts = select(SpeechAct.id).where(SpeechAct.artifact_id == artifact.id)
    if only_refs is not None:
        old_acts = old_acts.where(SpeechAct.unit_ref.in_(only_refs))
    # события журнала ссылаются на акты: события от актов (правило, модель) пересоберёт журнал, у решений человека ссылка снимается
    db.execute(delete(DecisionEvent).where(DecisionEvent.speech_act_id.in_(old_acts), DecisionEvent.decided_by != "human"))
    db.execute(update(DecisionEvent).where(DecisionEvent.speech_act_id.in_(old_acts)).values(speech_act_id=None))
    if only_refs is None:
        db.execute(delete(SpeechAct).where(SpeechAct.artifact_id == artifact.id))
    else:
        db.execute(delete(SpeechAct).where(SpeechAct.artifact_id == artifact.id, SpeechAct.unit_ref.in_(only_refs)))
    db.add_all(rows)
    db.commit()
    stats["acts"] = len(rows)
    log_event(logger, "speech_acts.done", artifact=artifact.artifact_id, units=stats["units"], acts=stats["acts"], errors=stats["errors"])
    return stats


def doc_acts_version() -> str:
    from app.app.services.doc_memory import ACTS_PROMPT_VERSION

    return ACTS_PROMPT_VERSION


def project_dialog_artifacts(db: Session, settings: Settings, project_id: int) -> list[Artifact]:
    """Готовые материалы проекта для памяти решений: созвоны, выгрузки переписки и документы с текстом."""
    from app.app.services.doc_memory import document_text, is_document

    out = []
    for art in db.scalars(select(Artifact).where(Artifact.project_id == project_id, Artifact.status == "done").order_by(Artifact.id)).all():
        if dialog_units(settings, art) or (is_document(art) and document_text(settings, art).strip()):
            out.append(art)
    return out


def extract_project(db: Session, settings: Settings, project_id: int, *, artifact_ids: list[int] | None = None,
                    ask: Callable[[str, str], str] | None = None, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    results = []
    for art in project_dialog_artifacts(db, settings, project_id):
        if artifact_ids and art.id not in artifact_ids:
            continue
        r = extract_artifact(db, settings, art, ask=ask)
        results.append(r)
        if progress:
            progress(f"#{art.id} {art.original_filename[:40]}: реплик {r['units']}, актов {r['acts']}, ошибок окон {r['errors']}")
    return {"project_id": project_id, "artifacts": results, "acts": sum(r["acts"] for r in results), "errors": sum(r["errors"] for r in results)}
