"""Контракт обмена «база знаний ↔ агент анализа конфигурации» (v1.0).

Строит из данных проекта машиночитаемые файлы contract/*.json: требования с доказательствами, решения, реестр
материалов, ТЗ по пунктам, расхождения, глоссарий и state. Ничего не выдумывает: если нет дословной цитаты, автора,
даты или связи — поле пустое, а причина названа в `gaps` (см. state.json). Записи в проект не делает (кроме служебного
реестра стабильных id в contract_items).
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.models.artifact import Artifact
from app.app.models.contract import ContractItem
from app.app.models.entity import Chunk, Entity
from app.app.models.project import Project
from app.app.services import agent_settings
from app.app.services import contract_text as ct
from app.app.services.auto_review import public_model_review, verification_for
from app.app.services.recorded_at import MINUTE_SOURCES, material_date, to_local
from app.app.services.source_links import source_url
from app.app.services.storage import project_root
from app.app.services.transcript_turns import format_timecode, load_turns, turn_at, turns_in_range
from app.app.services.tz import tz_paths

SCHEMA_VERSION = "1.0"
DEFAULT_TZ = timezone(timedelta(hours=3))  # по умолчанию время в переписках московское; пояс проекта задаётся в настройках (agent_settings)
_TZ: contextvars.ContextVar[timezone] = contextvars.ContextVar("contract_tz", default=DEFAULT_TZ)
FILES = ["requirements", "decisions", "open_questions", "conflicts", "tz", "artifacts", "glossary", "state"]
CONTRACT_PREFIX = "contract/"
MAX_PAGE_BYTES = 200_000
MIN_EVIDENCE_COVERAGE = 0.4
_CACHE_TTL = 30.0
_cache: dict[int, tuple[float, tuple, "Contract"]] = {}
_build_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()

_TRANSLIT = dict(zip("абвгдежзийклмнопрстуфхцчшщыэюяё", "a b v g d e zh z i y k l m n o p r s t u f h c ch sh sch y e yu ya e".split()))
_TRANSLIT.update({"ь": "", "ъ": ""})


def project_slug(name: str, project_id: int) -> str:
    word = re.findall(r"[а-яёa-z0-9]+", (name or "").lower())
    first = word[0] if word else ""
    out = "".join(_TRANSLIT.get(ch, ch) for ch in first)
    out = re.sub(r"[^a-z0-9]", "", out)
    return out[:16] or f"p{project_id}"


def _iso(dt: datetime | None, *, assume_local: bool = True) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_TZ.get() if assume_local else timezone.utc)
    return dt.isoformat(timespec="seconds")


def _entity_value(ent: Entity) -> dict:
    try:
        value = json.loads(ent.value_json)
    except json.JSONDecodeError:
        return {"text": ent.value_json}
    return value if isinstance(value, dict) else {"text": str(value)}


def _statement(ent: Entity, value: dict) -> str:
    return str(value.get("text") or value.get("description") or "").strip()


def _hash(obj: Any) -> str:
    return hashlib.sha1(json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


@dataclass
class ArtifactCtx:
    art: Artifact
    type: str
    text: str = ""
    messages: list[ct.Message] = field(default_factory=list)
    msg_tokens: list[set[str]] = field(default_factory=list)
    chunks: list[Chunk] = field(default_factory=list)
    occurred_at: datetime | None = None
    occurred_from: datetime | None = None
    precision: str = "day"
    occurred_source: str | None = None
    turns: list = field(default_factory=list)  # реплики созвона из канона _final.txt (transcript_turns)


@dataclass
class Contract:
    project_id: int
    files: dict[str, dict]
    generated_at: str


# ---------------------------------------------------------------- материалы

def _read_text(settings: Settings, art: Artifact) -> str:
    base = project_root(settings, art.project_id) / "parsed"
    for sub, suffix in (("documents", "_clean.txt"), ("documents", "_raw.txt"), ("calls", "_transcript.txt"), ("calls", ".txt")):
        path = base / sub / f"{art.artifact_id}{suffix}"
        if path.is_file():
            try:
                return path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return ""
    return ""


def _artifact_type(art: Artifact, text: str, has_messages: bool) -> str:
    name = (art.original_filename or "").lower()
    if art.file_type == "audio":
        return "call"
    if has_messages:
        return "chat"
    if re.search(r"\bтз\b|тех\w*\s*задан|technical", name):
        return "tz"
    if art.file_type in ("docx", "xlsx", "pptx", "pdf", "txt", "html", "other"):
        return "document"
    return "other"


def _load_artifacts(db: Session, settings: Settings, project_id: int) -> list[ArtifactCtx]:
    ctxs: list[ArtifactCtx] = []
    arts = db.scalars(select(Artifact).where(Artifact.project_id == project_id).order_by(Artifact.id)).all()
    for art in arts:
        text = _read_text(settings, art)
        from app.app.services.chat_export import load_artifact_export

        export = load_artifact_export(settings, art) if art.file_type != "audio" else None
        # Выгрузка Telegram: номер, автор, время из разметки (SOURCE-ANCHORS-PLAN этап 2); иначе — разбор текста
        messages = (ct.messages_from_export(export, _TZ.get()) if export else
                    ct.parse_chat(text) if text and ct.looks_like_chat(text) else [])
        ctx = ArtifactCtx(art=art, type=_artifact_type(art, text, bool(messages)), text=text, messages=messages)
        ctx.msg_tokens = [ct.stems(m.text) for m in messages]
        ctx.chunks = list(db.scalars(select(Chunk).where(Chunk.artifact_id == art.id).order_by(Chunk.chunk_index)).all())
        ctx.turns = load_turns(settings, art) if art.file_type == "audio" else []
        dated = [m.at() for m in messages if m.at()]
        recorded = art.recorded_at if art.recorded_at_source in MINUTE_SOURCES else None  # встреча, метаданные, ручное время (Я5)
        if dated:
            ctx.occurred_at, ctx.occurred_from, ctx.precision = max(dated), min(dated), "minute"
            ctx.occurred_source = "message_timestamps"
        elif recorded is not None:
            # Встреча из комнаты / метаданные файла: момент начала записи; _iso считает наивное время местным.
            ctx.occurred_at = to_local(recorded, _TZ.get()).replace(tzinfo=None)
            ctx.precision = "minute"
            ctx.occurred_source = f"recorded_at:{art.recorded_at_source}"
        else:
            # Дата записи с точностью «день» (имя файла, ручная). Нет фактической — дата загрузки с источником
            # recorded_at:upload (решение владельца 2026-09-28); интерфейс подписывает её «по дате загрузки» (А9).
            day, source = material_date(art)
            if day:
                ctx.occurred_at = datetime(day.year, day.month, day.day)
                ctx.occurred_source = f"recorded_at:{source}"
        ctxs.append(ctx)
    return ctxs


# ---------------------------------------------------------------- стороны и участники

def _participant_sides(db: Session, project_id: int, cfg: dict | None = None) -> dict[str, str]:
    """Имя (полное и по первому слову) → сторона: сначала явный список участников из настроек проекта, затем роли сущностей-участников."""
    cfg = cfg or {}
    rows = db.execute(
        select(Entity).join(Artifact, Artifact.id == Entity.artifact_id)
        .where(Artifact.project_id == project_id, Entity.entity_type == "participant", Entity.is_active.is_(True))
    ).scalars().all()
    sides: dict[str, str] = {}
    for row in cfg.get("participants") or []:  # явное указание владельца важнее эвристики по роли
        n = str(row.get("name", "")).strip().lower()
        if n and row.get("side") in ("customer", "vendor"):
            sides[n] = row["side"]
            sides.setdefault(n.split()[0], row["side"])
    for ent in rows:
        value = _entity_value(ent)
        name, side = str(value.get("name") or "").strip(), ct.side_from_role(value.get("role"), cfg.get("side_hints"))
        if name and side:
            sides.setdefault(name.lower(), side)
            sides.setdefault(name.lower().split()[0], side)
    return sides


def _resolve_side(name: str, known: dict[str, str], all_speakers: list[str]) -> str:
    low = (name or "").lower()
    if low in known:
        return known[low]
    if low.split() and low.split()[0] in known:
        return known[low.split()[0]]
    others = [s for s in all_speakers if s != name]
    resolved = {(_resolve_side_plain(s, known)) for s in others} - {None}
    if len(all_speakers) == 2 and len(resolved) == 1:  # двое в переписке: если второй — подрядчик, этот — заказчик
        return "customer" if resolved == {"vendor"} else "vendor"
    return "unknown"


def _resolve_side_plain(name: str, known: dict[str, str]) -> str | None:
    low = (name or "").lower()
    return known.get(low) or (known.get(low.split()[0]) if low.split() else None)


# ---------------------------------------------------------------- доказательства

def _chunk_for_quote(ctx: ArtifactCtx, quote: str) -> int | None:
    probe = re.sub(r"\s+", " ", quote)[:40]
    for ch in ctx.chunks:
        if probe and probe in re.sub(r"\s+", " ", ch.text):
            return ch.id
    return None


def _turn_for_quote(ctx: ArtifactCtx, chunk: Chunk, quote: str):
    """Реплика созвона внутри отрезка чанка, лучше всех покрывающая цитату."""
    if not ctx.turns or chunk.start_sec is None:
        return None
    candidates = turns_in_range(ctx.turns, chunk.start_sec, chunk.end_sec)
    if not candidates:
        return None
    need = ct.stems(quote)
    scored = [(ct.coverage(need, ct.stems(t.text)), t) for t in candidates]
    score, turn = max(scored, key=lambda item: item[0])
    return turn if score > 0 else None


def _ctx_speakers(ctx: ArtifactCtx) -> list[str]:
    """Участники материала: из переписки, из реплик созвона, иначе из chunk.speaker (старые чанки)."""
    if ctx.messages:
        return sorted({m.speaker for m in ctx.messages})
    from_turns = sorted({t.known_speaker for t in ctx.turns if t.is_speech and t.known_speaker})
    if from_turns:
        return from_turns
    return sorted({c.speaker for c in ctx.chunks if c.speaker and c.speaker != "unknown"})


def _evidence(statement: str, ctx: ArtifactCtx, known_sides: dict[str, str]) -> list[dict]:
    need = ct.stems(statement)
    if len(need) < 2:
        return []
    speakers = _ctx_speakers(ctx)
    out: list[dict] = []
    if ctx.messages:
        ranked = sorted(
            ((ct.coverage(need, toks), i) for i, toks in enumerate(ctx.msg_tokens) if len(need & toks) >= 2),
            reverse=True,
        )
        for score, i in ranked[:2]:
            if score < MIN_EVIDENCE_COVERAGE:
                continue
            msg = ctx.messages[i]
            found = ct.best_quote(statement, msg.text) or (msg.text[: ct.QUOTE_MAX].strip(), score)
            out.append({
                "artifact_id": ctx.art.artifact_id,
                "chunk_id": _chunk_for_quote(ctx, found[0]),
                "said_at": _iso(msg.at()),
                "speaker": msg.speaker,
                "side": _resolve_side(msg.speaker, known_sides, speakers),
                "quote": found[0],
                "match_score": found[1],
                "message_index": msg.idx,
                "message_id": msg.msg_id,
                "url": source_url(ctx.art.artifact_id, message_id=msg.msg_id, quote=found[0]),
            })
        return out
    best: tuple[float, Chunk] | None = None
    for ch in ctx.chunks:
        score = ct.coverage(need, ct.stems(ch.text))
        if best is None or score > best[0]:
            best = (score, ch)
    if best and best[0] >= MIN_EVIDENCE_COVERAGE:
        found = ct.best_quote(statement, best[1].text)
        if found:
            speaker = best[1].speaker if best[1].speaker and best[1].speaker != "unknown" else None
            said_at = ctx.occurred_at
            turn = _turn_for_quote(ctx, best[1], found[0])
            if turn is not None:
                # Окно реплик: говорящий — из реплики с цитатой, а не из chunk.speaker (у смешанного окна он пуст).
                speaker = turn.known_speaker
                if ctx.occurred_at is not None and ctx.precision == "minute" and turn.start_sec is not None:
                    said_at = ctx.occurred_at + timedelta(seconds=turn.start_sec)
            out.append({
                "artifact_id": ctx.art.artifact_id,
                "chunk_id": best[1].id,
                "said_at": _iso(said_at),
                "speaker": speaker,
                "side": _resolve_side(speaker, known_sides, speakers) if speaker else "unknown",
                "quote": found[0],
                "match_score": found[1],
                "message_index": None,
                # говорящий найден по совпадению слов цитаты, а не по привязке пункта к реплике (А9)
                "speaker_basis": "words" if speaker else None,
                "timecode": format_timecode(turn.start_sec) if turn is not None and turn.start_sec is not None else None,
                "start_sec": turn.start_sec if turn is not None else None,
                "url": source_url(ctx.art.artifact_id, start_sec=turn.start_sec if turn is not None else None, quote=found[0]),
            })
    return out


def _bound_chat_evidence(statement: str, value: dict, ctx: ArtifactCtx, known_sides: dict[str, str]) -> dict | None:
    """Источник пункта переписки из привязки к сообщению (SOURCE-ANCHORS-PLAN этап 2): автор, время, номер — из выгрузки."""
    mid = value.get("message_id")
    msg = next((m for m in ctx.messages if m.msg_id == mid), None) if mid is not None else None
    if msg is None:
        return None
    speakers = _ctx_speakers(ctx)
    found = ct.best_quote(statement, msg.text)
    quote = found[0] if found else msg.text[: ct.QUOTE_MAX].strip()
    agreed = [{"name": d.get("name"), "side": _resolve_side(str(d.get("name") or ""), known_sides, speakers),
               "at": (None if d.get("at_precision") == "day"  # копия чата без времени — момент неизвестен
                      else _iso(datetime.fromisoformat(d["at"]).astimezone(_TZ.get()).replace(tzinfo=None)) if d.get("at") else None),
               "message_id": d.get("message_id")}
              for d in value.get("decided_by") or [] if isinstance(d, dict) and d.get("name")]
    return {"artifact_id": ctx.art.artifact_id, "chunk_id": _chunk_for_quote(ctx, quote) if quote else None,
            "said_at": _iso(msg.at()), "speaker": msg.speaker, "side": _resolve_side(msg.speaker, known_sides, speakers),
            "quote": quote, "match_score": found[1] if found else None, "message_index": msg.idx, "message_id": mid,
            "speaker_basis": "chat", "timecode": None, "agreed_by": agreed,
            "url": source_url(ctx.art.artifact_id, message_id=mid, quote=quote)}


def _bound_evidence(statement: str, value: dict, ctx: ArtifactCtx, known_sides: dict[str, str]) -> dict | None:
    """Источник из привязки самого пункта к реплике созвона (REQUIREMENTS-PLAN А9): кто сказал, таймкод, кто утвердил.

    Привязку делает извлечение (`entities.bind_call_source`): дорожка комнаты (`track`) или голос (`voice`). Совпадение
    слов — только запасной путь `_evidence`: так «кто сказал» — автор реплики, а не тот, кто повторил похожие слова.
    """
    if ctx.type != "call" or value.get("start_sec") is None:
        return None
    start = float(value["start_sec"])
    turn = turn_at(ctx.turns, start) if ctx.turns else None
    text = turn.text if turn is not None else ""
    found = ct.best_quote(statement, text) if text else None
    quote = found[0] if found else text[: ct.QUOTE_MAX].strip()
    speaker = value.get("speaker") or (turn.known_speaker if turn is not None else None)
    speakers = _ctx_speakers(ctx)
    said_at = ctx.occurred_at
    if said_at is not None and ctx.precision == "minute":
        said_at = said_at + timedelta(seconds=start)
    agreed = [{"name": d.get("name"), "side": _resolve_side(str(d.get("name") or ""), known_sides, speakers),
               "timecode": format_timecode(d.get("start_sec")) if d.get("start_sec") is not None else None}
              for d in value.get("decided_by") or [] if isinstance(d, dict) and d.get("name")]
    return {
        "artifact_id": ctx.art.artifact_id,
        "chunk_id": _chunk_for_quote(ctx, quote) if quote else None,
        "said_at": _iso(said_at),
        "speaker": speaker,
        "side": _resolve_side(speaker, known_sides, speakers) if speaker else "unknown",
        "quote": quote,
        "match_score": found[1] if found else None,
        "message_index": None,
        "speaker_basis": value.get("speaker_source") or ("turn" if speaker else None),
        "timecode": format_timecode(start),
        "start_sec": start,
        "agreed_by": agreed,
        "url": source_url(ctx.art.artifact_id, start_sec=start),
    }


def _requirement_evidence(statement: str, value: dict, ctx: ArtifactCtx, known_sides: dict[str, str]) -> list[dict]:
    """Источники требования: сначала привязка пункта к реплике, затем совпадение слов (без повтора той же цитаты).
    У каждого — дата записи материала (`said_date`, None — неизвестна) и откуда она (`date_basis`)."""
    bound = _bound_chat_evidence(statement, value, ctx, known_sides) if ctx.messages else _bound_evidence(statement, value, ctx, known_sides)
    rest = _evidence(statement, ctx, known_sides)
    out = [bound] if bound else []
    out += [ev for ev in rest if not bound or ev.get("quote") != bound.get("quote")]
    day = ctx.occurred_at.date().isoformat() if ctx.occurred_at is not None else None
    for ev in out:
        if ctx.messages and ev.get("said_at"):
            ev.setdefault("said_date", str(ev["said_at"])[:10])
            ev.setdefault("date_basis", "message")
            ev.setdefault("speaker_basis", "message")
        else:
            ev.setdefault("said_date", day)
            ev.setdefault("date_basis", ctx.occurred_source.split(":", 1)[-1] if ctx.occurred_source and day else "unknown")
        ev.setdefault("material", ctx.art.original_filename)
        ev.setdefault("material_type", ctx.type)
    return out


def _estimate(evidence: list[dict], ctx: ArtifactCtx) -> dict | None:
    """Оценка часов: в самой цитате либо в сообщениях в пределах 15 минут после неё."""
    if not evidence or not ctx.messages:
        return None
    ev = evidence[0]
    if ev.get("message_index") is None:
        return None
    base = ctx.messages[ev["message_index"]]
    # без времени сообщений (копия чата) «15 минут» не проверить — только само сообщение и два следующих
    window = 12 if base.at() is not None else 3
    for msg in ctx.messages[ev["message_index"]: ev["message_index"] + window]:
        if msg.at() and base.at() and (msg.at() - base.at()) > timedelta(minutes=15):
            break
        hours = ct.find_hours(msg.text)
        if hours:
            return {"hours_min": hours[0], "hours_max": hours[1], "stated_by": ev["side"] if msg is base else None,
                    "speaker": msg.speaker, "stated_at": _iso(msg.at()), "basis": "same_message" if msg is base else "nearby_message"}
    return None


# ---------------------------------------------------------------- ТЗ

def _tz_points(text: str) -> list[dict]:
    sections: list[dict] = []
    cur: dict | None = None
    for line in text.splitlines():
        if line.startswith("#"):
            cur = {"id": f"tz-{len(sections) + 1}", "title": line.lstrip("# ").strip(), "text_lines": [], "points": []}
            sections.append(cur)
        elif cur is not None and line.strip():
            cur["text_lines"].append(line)
            point = line.strip().lstrip("-*• ").strip()
            if point and not point.startswith("|"):
                cur["points"].append({"id": f"{cur['id']}.{len(cur['points']) + 1}", "text": point})
    return sections


def _tz_ref(statement: str, sections: list[dict]) -> list[str]:
    need = ct.stems(statement)
    if len(need) < 2:
        return []
    refs = []
    for sec in sections:
        for pt in sec["points"]:
            if ct.coverage(need, ct.stems(pt["text"])) >= 0.5:
                refs.append(pt["id"])
    return refs


# ---------------------------------------------------------------- стабильные id

def _insert_item(db: Session, project_id: int, kind: str, key: str, chash: str, now: datetime, seq: int) -> ContractItem:
    """Новый элемент контракта, устойчиво к гонке: другой процесс (воркер, второй запрос API) мог вставить тот же ключ между
    чтением ключей и вставкой — тогда берём его строку, а не падаем на UNIQUE (урок 2026-09-28: 500 при первой загрузке
    очереди после переизвлечения). Номер — не меньше текущего максимума в базе, чтобы два построения не выдали один номер."""
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    top = db.scalar(select(func.max(ContractItem.seq)).where(ContractItem.project_id == project_id, ContractItem.kind == kind)) or 0
    values = {"project_id": project_id, "kind": kind, "key": key, "seq": max(seq, top + 1), "content_hash": chash,
              "first_seen_at": now, "updated_at": now}
    if db.get_bind().dialect.name == "sqlite":
        db.execute(sqlite_insert(ContractItem).values(**values).on_conflict_do_nothing(index_elements=["project_id", "kind", "key"]))
    else:
        db.add(ContractItem(**values))
    db.flush()
    return db.scalar(select(ContractItem).where(ContractItem.project_id == project_id, ContractItem.kind == kind, ContractItem.key == key))


def _assign_ids(db: Session, project_id: int, slug: str, kind: str, items: dict[str, str]) -> dict[str, str]:
    """items: norm_key → content_hash. Возвращает norm_key → id. Переиспользует id при близкой формулировке."""
    now = datetime.now(timezone.utc)
    rows = {r.key: r for r in db.scalars(select(ContractItem).where(ContractItem.project_id == project_id, ContractItem.kind == kind))}
    next_seq = max((r.seq for r in rows.values()), default=0) + 1
    seen: set[int] = set()
    result: dict[str, str] = {}
    leftovers = [k for k in rows if k not in items]
    for key, chash in items.items():
        row = rows.get(key)
        if row is None:  # правка формулировки: ищем близкий несопоставленный ключ
            toks = ct.stems(key)
            match = max(((len(toks & ct.stems(k)) / max(1, len(toks | ct.stems(k))), k) for k in leftovers if rows[k].id not in seen), default=(0.0, None))
            if match[1] and match[0] >= 0.8:
                row = rows.pop(match[1])
                leftovers.remove(match[1])
                row.key = key[:512]
                rows[key] = row
        if row is None:
            row = _insert_item(db, project_id, kind, key[:512], chash, now, next_seq)
            next_seq = max(next_seq, row.seq) + 1
            rows[key] = row
        elif row.content_hash != chash:
            row.content_hash, row.updated_at = chash, now
        row.removed_at = None
        seen.add(row.id)
        result[key] = f"{kind}-{slug}-{row.seq:04d}"
    for row in rows.values():
        if row.id not in seen and row.removed_at is None:
            row.removed_at = now
    db.commit()
    return result


# ---------------------------------------------------------------- сборка

def _fingerprint(db: Session, settings: Settings, project_id: int) -> tuple:
    max_ent = db.execute(select(Entity.id).join(Artifact, Artifact.id == Entity.artifact_id).where(Artifact.project_id == project_id).order_by(Entity.id.desc()).limit(1)).scalar()
    n_art = db.execute(select(Artifact.id).where(Artifact.project_id == project_id)).all()
    tz_path, _ = tz_paths(settings, project_id)
    proj = db.get(Project, project_id)
    reviewed = db.execute(select(func.count()).select_from(Entity).join(Artifact, Artifact.id == Entity.artifact_id)
                          .where(Artifact.project_id == project_id, Entity.needs_review.is_(False), Entity.entity_type.in_(("decision", "task", "analysis_verdict")))).scalar()
    inactive = db.execute(select(func.count()).select_from(Entity).join(Artifact, Artifact.id == Entity.artifact_id)
                          .where(Artifact.project_id == project_id, Entity.is_active.is_(False), Entity.entity_type.in_(("decision", "task", "analysis_verdict")))).scalar()
    # Вердикты модели меняют только value_json — без этого счётчика кэш контракта их не заметил бы.
    model_reviewed = db.execute(select(func.count(), func.sum(func.length(Entity.value_json))).select_from(Entity).join(Artifact, Artifact.id == Entity.artifact_id)
                                .where(Artifact.project_id == project_id, Entity.value_json.like('%"model_review"%'))).one()
    # Темы, вердикты актуальности (в тени меняют только value_json), вопросы к заказчику и даты материалов, поставленные
    # воркером (переписка, шапка документа), — иначе API до истечения срока кэша показывал бы старую очередь.
    items = db.execute(select(func.count(), func.sum(func.length(Entity.value_json)), func.sum(Entity.topic_id)).select_from(Entity)
                       .join(Artifact, Artifact.id == Entity.artifact_id)
                       .where(Artifact.project_id == project_id, Entity.entity_type.in_(("decision", "task", "open_question")))).one()
    dates = db.execute(select(Artifact.recorded_at, Artifact.recorded_at_source).where(Artifact.project_id == project_id)
                       .order_by(Artifact.id)).all()
    return (reviewed, inactive, max_ent, len(n_art), tz_path.stat().st_mtime if tz_path.is_file() else 0, hash(proj.settings_json) if proj else 0,
            tuple(model_reviewed), tuple(items), hash(tuple((str(d), s) for d, s in dates)))


def _project_lock(project_id: int) -> threading.Lock:
    with _locks_guard:
        return _build_locks.setdefault(project_id, threading.Lock())


def get_contract(db: Session, settings: Settings, project_id: int, *, force: bool = False) -> Contract:
    fp = _fingerprint(db, settings, project_id)
    hit = _cache.get(project_id)
    if hit and not force and hit[1] == fp and time.time() - hit[0] < _CACHE_TTL:
        return hit[2]
    # Страница проекта одновременно просит очередь и счётчики: без замка два запроса строили бы контракт параллельно
    # и дрались за одни и те же новые ключи. Второй дожидается первого и берёт готовое из кэша.
    with _project_lock(project_id):
        hit = _cache.get(project_id)
        if hit and not force and hit[1] == fp and time.time() - hit[0] < _CACHE_TTL:
            return hit[2]
        contract = build_contract(db, settings, project_id)
        _cache[project_id] = (time.time(), fp, contract)
        return contract


def build_contract(db: Session, settings: Settings, project_id: int) -> Contract:
    project = db.get(Project, project_id)
    if not project or not project.is_active:
        raise ValueError("Project not found")
    slug = project_slug(project.name, project_id)
    now = datetime.now(timezone.utc)
    cfg = agent_settings.get_settings_for(project)
    local_tz = agent_settings.parse_tz(cfg["timezone"])
    anchor_rules = agent_settings.load_pack_rules(cfg["domain_pack"])
    token = _TZ.set(local_tz)
    try:
        return _build_contract(db, settings, project, project_id, slug, now, cfg, local_tz, anchor_rules)
    finally:
        _TZ.reset(token)


def _build_contract(db, settings, project, project_id, slug, now, cfg, LOCAL_TZ, anchor_rules) -> "Contract":
    ctxs = _load_artifacts(db, settings, project_id)
    by_pk = {c.art.id: c for c in ctxs}
    known = _participant_sides(db, project_id, cfg)

    tz_path, versions = tz_paths(settings, project_id)
    tz_text = tz_path.read_text(encoding="utf-8") if tz_path.is_file() else ""
    sections = _tz_points(tz_text)
    tz_version = datetime.fromtimestamp(tz_path.stat().st_mtime, LOCAL_TZ).date().isoformat() if tz_path.is_file() else None

    ents = db.execute(
        select(Entity).join(Artifact, Artifact.id == Entity.artifact_id)
        .where(Artifact.project_id == project_id, Entity.is_active.is_(True),
               Entity.entity_type.in_(("decision", "task", "analysis_verdict", "open_question")))
        .order_by(Entity.id)
    ).scalars().all()
    verdicts: dict[str, dict] = {}
    cand: list[tuple[Entity, dict, str]] = []
    questions: list[tuple[Entity, dict, str]] = []
    for ent in ents:
        value = _entity_value(ent)
        if ent.entity_type == "analysis_verdict":
            if value.get("requirement_id"):
                verdicts[str(value["requirement_id"])] = {**value, "human_confirmed": not ent.needs_review}
            continue
        stmt = _statement(ent, value)
        if stmt and ent.entity_type == "open_question":
            questions.append((ent, value, stmt))
        elif stmt:
            cand.append((ent, value, stmt))

    # Выключенные моделью (live) пункты: заменённое более поздним остаётся в контракте со статусом superseded
    # (агент не должен принять его за требование, REQUIREMENTS-PLAN А6); источники дублей показываются у основного пункта.
    superseded: dict[int, int | None] = {}
    merged: dict[int, list[tuple[Entity, dict, str]]] = {}
    retired = db.execute(
        select(Entity).join(Artifact, Artifact.id == Entity.artifact_id)
        .where(Artifact.project_id == project_id, Entity.is_active.is_(False), Entity.entity_type.in_(("decision", "task")),
               Entity.value_json.like('%"model_review"%'))
        .order_by(Entity.id)
    ).scalars().all()
    for ent in retired:
        value = _entity_value(ent)
        review = value.get("model_review") if isinstance(value.get("model_review"), dict) else {}
        stmt = _statement(ent, value)
        if not stmt or not review.get("deactivated") or review.get("undone") or review.get("mode") != "live":
            continue
        if review.get("reject_kind") == "superseded":
            superseded[ent.id] = review.get("winner")
            cand.append((ent, value, stmt))
        elif review.get("reject_kind") == "duplicate" and review.get("merged_into"):
            merged.setdefault(int(review["merged_into"]), []).append((ent, value, stmt))

    # --- требования и решения
    req_items: dict[str, dict] = {}
    dec_items: dict[str, dict] = {}
    for ent, value, stmt in cand:
        ctx = by_pk.get(ent.artifact_id)
        if ctx is None:
            continue
        evidence = _requirement_evidence(stmt, value, ctx, known)
        for dup, dup_value, dup_stmt in merged.get(ent.id, []):
            dup_ctx = by_pk.get(dup.artifact_id)
            if dup_ctx is not None:
                evidence += _requirement_evidence(dup_stmt, dup_value, dup_ctx, known)
        first = evidence[0] if evidence else {}
        tz_refs = _tz_ref(stmt, sections)
        verification = verification_for(ent.needs_review, value)
        commercial = bool(ct.COMMERCIAL_RE.search(stmt))
        base = {
            "statement": stmt,
            "evidence": evidence,
            "verification": verification,
            "model_review": public_model_review(value),
            "confidence": round(ent.confidence, 2),
            "source_entity_id": ent.id,
            "source_artifact_id": ctx.art.artifact_id,
        }
        if ent.entity_type == "decision" and ent.id not in superseded:
            dec_items[ct.norm_key(stmt)] = {**base, "decided_at": first.get("said_at"), "decided_by": first.get("speaker"), "side": first.get("side"),
                                             "supersedes": [], "affects_requirements": []}
        # Виды пунктов (REQUIREMENTS-PLAN А7): организационная задача — не требование; решение по проекту и требование к
        # системе — требования. Старые пункты без вида — по прежнему правилу.
        kind = value.get("kind")
        if ent.entity_type == "task":
            is_req = kind != "org"
        else:
            is_req = kind in ("requirement", "project") or bool(ct.REQ_VERB_RE.search(stmt))
        if not is_req:
            continue
        task_status = str(value.get("status") or "unknown")
        status, status_basis = ct.requirement_status(ent.entity_type, task_status, evidence)
        if value.get("human_status"):  # статус, заданный человеком (admin), важнее эвристики
            status, status_basis = str(value["human_status"]), "human_set"
        if ent.id in superseded:  # позже заменён другим решением (А6): не требование, а история
            status, status_basis = "superseded", "superseded_by_later"
        req_items[ct.norm_key(stmt)] = {
            **base,
            "title": stmt[:90],
            "kind": "task" if ent.entity_type == "task" else ("commercial" if commercial else "functional"),
            "status": status,
            "status_basis": status_basis,
            "human_review": value.get("human_review"),
            "in_current_tz": bool(tz_refs),
            "tz_refs": tz_refs,
            "side_agreed": [],
            "estimate": _estimate(evidence, ctx),
            "business_reason": None,
            "acceptance": [],
            "code_anchors": ct.anchors_for(stmt, anchor_rules),
            "assignee": value.get("assignee"),
            "deadline_raw": value.get("deadline"),
            "item_kind": kind, "object": value.get("object"), "topic_id": ent.topic_id,
            "unclear_wording": bool(value.get("unclear_wording")),
            "depends_on": [], "supersedes": [], "open_question_ids": [],
            "superseded_by_entity": superseded.get(ent.id), "merged_from": [d.id for d, _v, _s in merged.get(ent.id, [])],
            # вердикт актуальности по теме (REQUIREMENTS-PLAN этап 7): в тени пункт активен, но «заменён» / «дубль»
            "relevance": {k: value["relevance"].get(k) for k in ("state", "by", "quote", "reason", "mode", "note", "undone")}
                         if isinstance(value.get("relevance"), dict) else None,
        }

    req_ids = _assign_ids(db, project_id, slug, "req", {k: _hash(v) for k, v in req_items.items()})
    dec_ids = _assign_ids(db, project_id, slug, "dec", {k: _hash(v) for k, v in dec_items.items()})

    # --- открытые вопросы и варианты без выбора (REQUIREMENTS-PLAN А7): вопросы к заказчику, не требования
    q_items: dict[str, dict] = {}
    for ent, value, stmt in questions:
        ctx = by_pk.get(ent.artifact_id)
        if ctx is None:
            continue
        q_items[ct.norm_key(stmt)] = {"question": stmt, "options": value.get("options") or [], "object": value.get("object"),
                                      "topic_id": ent.topic_id, "evidence": _requirement_evidence(stmt, value, ctx, known),
                                      "source_entity_id": ent.id, "source_artifact_id": ctx.art.artifact_id, "status": "open"}
    q_ids = _assign_ids(db, project_id, slug, "oq", {k: _hash(v) for k, v in q_items.items()})
    open_questions = [{"id": q_ids[k], **v} for k, v in q_items.items()]
    requirements = []
    req_by_entity = {item["source_entity_id"]: req_ids[key] for key, item in req_items.items()}
    for key, item in req_items.items():
        item = {"id": req_ids[key], **item}
        if item.get("superseded_by_entity"):
            item["superseded_by"] = req_by_entity.get(item["superseded_by_entity"])
        if not item["code_anchors"]:
            item["code_anchors_reason"] = "в формулировке нет терминов, привязываемых к объектам 1С/Битрикс24"
        if not item["acceptance"]:
            item["acceptance_status"] = "not_generated"
        if not item["evidence"]:
            item["evidence_missing_reason"] = "в источнике не найдено сообщение/фрагмент с достаточным совпадением формулировки"
        verdict = verdicts.get(item["id"])
        if verdict:
            item["analysis"] = {k: verdict.get(k) for k in ("verdict", "confidence", "blockers", "questions_for_customer", "conflicts_found", "human_confirmed")}
            # проверено машиной по коду/данным (не человеческое подтверждение, verification не меняется); not_verifiable = проверки не было
            item["analysis"]["verified_by"] = "machine_code" if verdict.get("verdict") != "not_verifiable" else None
            for fix in verdict.get("anchor_corrections") or []:
                for anchor in item["code_anchors"]:
                    if anchor.get("object") == fix.get("given"):
                        anchor.update({"object": fix.get("actual"), "basis": "analysis_reply", "corrected_from": fix.get("given")})
        requirements.append(item)
    decisions = [{"id": dec_ids[k], **v} for k, v in dec_items.items()]
    # решения → требования, которых они касаются (та же формулировка)
    req_by_key = {k: req_ids[k] for k in req_items}
    for key, dec in zip(dec_items, decisions):
        if key in req_by_key:
            dec["affects_requirements"] = [req_by_key[key]]

    # --- расхождения (правило «согласовано, но вне текущего ТЗ»)
    merged: dict[tuple, dict] = {}
    for req in requirements:
        if req["in_current_tz"] or not tz_text or req["status"] == "rejected":
            continue
        scope_ev = next((e for e in req["evidence"] if ct.SCOPE_RE.search(e["quote"])), None)
        if not scope_ev:
            continue
        key = (scope_ev["artifact_id"], scope_ev["said_at"], scope_ev["speaker"])  # одна реплика — одно расхождение
        if key in merged:
            merged[key]["affects_requirements"].append(req["id"])
            if req["estimate"]:
                merged[key]["severity"] = "high"
            continue
        merged[key] = {
            "kind": "scope",
            "severity": "high" if req["estimate"] else "medium",
            "left": {"statement": req["statement"], "artifact_id": scope_ev["artifact_id"], "said_at": scope_ev["said_at"], "speaker": scope_ev["speaker"], "quote": scope_ev["quote"]},
            "right": {"statement": "Требование не отражено ни в одном пункте текущего ТЗ (совпадение формулировки < 50%)", "basis": "tz_overlap", "tz_version": tz_version},
            "affects_requirements": [req["id"]],
            "status": "open",
        }
    conflicts_raw = list(merged.values())
    cft_ids = _assign_ids(db, project_id, slug, "cft", {ct.norm_key(c["left"]["statement"]): _hash(c) for c in conflicts_raw})
    conflicts = [{"id": cft_ids[ct.norm_key(c["left"]["statement"])], **c} for c in conflicts_raw]

    # --- ТЗ по пунктам
    req_for_point: dict[str, list[str]] = {}
    for req in requirements:
        for ref in req["tz_refs"]:
            req_for_point.setdefault(ref, []).append(req["id"])
    tz_json = {
        "schema_version": SCHEMA_VERSION, "project_id": project_id, "tz_version": tz_version,
        "source_artifact_id": None,
        "derived_from": "итоговая выжимка проекта (current_tz.md), а не исходный документ ТЗ заказчика",
        "sections": [
            {"id": s["id"], "title": s["title"], "text": "\n".join(s["text_lines"]),
             "points": [{**p, "requirement_ids": req_for_point.get(p["id"], [])} for p in s["points"]],
             "requirement_ids": sorted({r for p in s["points"] for r in req_for_point.get(p["id"], [])})}
            for s in sections
        ],
        "requirements_without_tz_point": [r["id"] for r in requirements if not r["in_current_tz"]],
    }

    # --- реестр материалов
    sides_all: dict[str, str] = {}
    registry = []
    for ctx in ctxs:
        names = _ctx_speakers(ctx)
        participants = [{"name": n, "side": _resolve_side(n, known, names)} for n in names]
        for p in participants:
            sides_all[p["name"]] = p["side"]
        registry.append({
            "artifact_id": ctx.art.artifact_id, "filename": ctx.art.original_filename, "type": ctx.type,
            "occurred_at": _iso(ctx.occurred_at), "occurred_from": _iso(ctx.occurred_from),
            "occurred_at_precision": ctx.precision if ctx.occurred_at else None,
            "occurred_at_source": ctx.occurred_source if ctx.occurred_at else None,
            "ingested_at": _iso(ctx.art.upload_date, assume_local=False),
            "participants": participants,
            "duration_min": round(ctx.art.duration_sec / 60, 1) if ctx.art.duration_sec else None,
            "status": ctx.art.status,
            "external_links": ct.find_urls(ctx.text),
        })

    glossary = [
        {"term": term, "said_by": None, "means": None, "tech_hints": [obj], "origin": "kb_rules",
         "note": "правило базы знаний, а не слова заказчика; агент анализа уточняет через anchor_corrections"}
        for _rx, _sys, obj, term in ct.ANCHOR_RULES
    ]
    n_evid = sum(1 for r in requirements if r["evidence"])
    gaps = {
        "open_questions": "вопросы и варианты без выбора извлекаются из материалов (извлечение v2); у материалов, извлечённых раньше, их нет, пока материал не переизвлекли",
        "acceptance": "критерии приёмки не генерируются автоматически; поле пустое, acceptance_status=not_generated",
        "side_agreed": "кто согласовал требование, не определяется; поле пустое",
        "supersedes": "цепочки отмены решений не определяются; поле пустое",
        "estimates": "оценка часов берётся из реплик в пределах 15 минут от цитаты и может относиться к соседней теме (см. basis)",
        "timezone": f"время сообщений считается в поясе проекта ({cfg['timezone']}): в источнике пояса нет",
        "chat_dates": "дата сообщения берётся из заголовков дат переписки; у звонков — день, без минуты",
        "verification": (
            f"human_confirmed только у сущностей, подтверждённых человеком; сейчас {sum(1 for r in requirements if r['verification'] == 'human_confirmed')} из {len(requirements)}. "
            f"model_confirmed — проверено локальной моделью по реплике-источнику, не человеком; сейчас {sum(1 for r in requirements if r['verification'] == 'model_confirmed')}"
        ),
        "tz": "ТЗ — итоговая выжимка проекта, а не исходный документ",
    }
    counts = {"requirements": len(requirements), "requirements_with_evidence": n_evid, "requirements_with_anchors": sum(1 for r in requirements if r["code_anchors"]),
              "requirements_in_tz": sum(1 for r in requirements if r["in_current_tz"]), "decisions": len(decisions), "conflicts": len(conflicts),
              "artifacts": len(registry), "tz_sections": len(sections)}

    def wrap(items: list, **extra) -> dict:
        return {"schema_version": SCHEMA_VERSION, "project_id": project_id, "updated_at": now.astimezone(LOCAL_TZ).isoformat(timespec="seconds"),
                "total": len(items), "items": items, **extra}

    files = {
        "requirements": wrap(requirements),
        "decisions": wrap(decisions),
        "open_questions": wrap(open_questions, note=gaps["open_questions"]),
        "conflicts": wrap(conflicts, rules=["scope: «обсуждено или обещано, но пункта в текущем ТЗ нет» (реплика содержит «в текущем ТЗ», «по дооценке», «вне ТЗ» и т. п., пункта ТЗ нет)"],
                          not_detected=["contradiction", "stale", "estimate", "owner"]),
        "tz": tz_json,
        "artifacts": wrap(registry),
        "glossary": wrap(glossary),
    }
    # хеш по каждому файлу без метки времени: меняется только когда изменилось содержимое (агент качает лишь изменённое)
    file_hashes = {f"{CONTRACT_PREFIX}{k}.json": _hash({a: b for a, b in v.items() if a != "updated_at"}) for k, v in files.items()}
    state_hash = _hash({k: v for k, v in files.items()})
    files["state"] = {"schema_version": SCHEMA_VERSION, "project_id": project_id, "project_slug": slug,
                      "updated_at": now.astimezone(LOCAL_TZ).isoformat(timespec="seconds"), "content_hash": state_hash,
                      "counts": counts, "gaps": gaps, "file_hashes": file_hashes, "files": [f"{CONTRACT_PREFIX}{n}.json" for n in FILES]}
    return Contract(project_id=project_id, files=files, generated_at=now.isoformat(timespec="seconds"))


# ---------------------------------------------------------------- выдача порциями

def paginate(payload: dict, *, cursor: str | None = None, limit: int | None = None, max_bytes: int = MAX_PAGE_BYTES) -> dict:
    """Отдаёт items порциями не больше max_bytes: {total, cursor (следующий или None), items}."""
    items = payload.get("items")
    if not isinstance(items, list):
        return payload
    start = int(cursor or 0)
    take = min(limit or len(items), len(items) - start)
    while take > 1:
        page = {**payload, "items": items[start: start + take], "cursor": str(start + take) if start + take < len(items) else None}
        if len(json.dumps(page, ensure_ascii=False).encode("utf-8")) <= max_bytes:
            return page
        take = max(1, take // 2)
    return {**payload, "items": items[start: start + max(take, 0)], "cursor": str(start + 1) if start + 1 < len(items) else None}


def get_file(db: Session, settings: Settings, project_id: int, name: str, *, cursor: str | None = None, limit: int | None = None) -> dict:
    name = name.removeprefix(CONTRACT_PREFIX).removesuffix(".json")
    if name not in FILES:
        raise ValueError(f"Unknown contract file: {name}")
    return paginate(get_contract(db, settings, project_id).files[name], cursor=cursor, limit=limit)
