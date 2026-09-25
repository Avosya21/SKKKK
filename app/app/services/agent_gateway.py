"""Шлюз обмена с внешним агентом (агент анализа конфигурации): всё, что он присылает, — недоверенные ДАННЫЕ.

Принципы (см. docs/agent-exchange/POLICY.md):
1. Ничего из присланного не исполняется. Шлюз только классифицирует запрос по БЕЛОМУ СПИСКУ возможностей
   (`CAPABILITIES`); запрос вне списка отклоняется. Обработчиков «выполни команду / удали / измени настройку» не существует.
2. Единственная запись — вердикты в сущности типа `analysis_verdict` проекта, из строго проверенных и обрезанных полей.
   Существующие сущности, файлы, пользователи, токены, настройки и другие проекты недоступны.
3. Свободный текст (`ask`, цитаты, «почему») — только данные: он ищется, хранится и показывается, но не читается как указание.
4. Каждый запрос и ответ пишутся в журнал agent_exchange_log (решение, риск, причины, хеш) — для разбора и аудита.
5. Обработчики детерминированы и не зависят от LLM: локальная модель может лишь помогать человеку в очереди needs_human,
   но не принимать решения за шлюз.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.models.agent_exchange import AgentExchangeLog
from app.app.models.artifact import Artifact
from app.app.models.entity import Entity
from app.app.services import contract as ctr
from app.app.services.rate_limit import FailureLimiter

SCHEMA_MAJOR = "1"
_WAIT_SLOTS = threading.BoundedSemaphore(4)  # одновременных ожиданий wait_reply на весь сервер
_KEY_LIMIT: contextvars.ContextVar[int | None] = contextvars.ContextVar("agent_key_rate_limit", default=None)


class KeyScopeError(PermissionError):
    """У ключа нет права на это действие (`detail` в ответе MCP — `code`)."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


_CLIENT_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar("agent_client_request_id", default=None)
MAX_BYTES = 200_000
MAX_ITEMS = 500
REPLY_RE = re.compile(r"^analysis_reply_(\d+)\.json$", re.IGNORECASE)
REQUEST_RE = re.compile(r"^analysis_request_(\d+)\.json$", re.IGNORECASE)
QUESTION_RE = re.compile(r"^analysis_question_(\d+)\.json$", re.IGNORECASE)
VERDICTS = {"implemented", "partially_implemented", "not_implemented", "contradicts_data", "not_verifiable"}
CONFIDENCE = {"high", "medium", "low"}
REQ_ID_RE = re.compile(r"^req-[a-z0-9]{1,16}-\d{4,6}$")
CFT_ID_RE = re.compile(r"^cft-[a-z0-9]{1,16}-\d{4,6}$")
# Не более 30 обращений за 10 минут с одного источника (защита от зацикленного или враждебного агента)
_LIMITER = FailureLimiter(limit=30, window_sec=600)


@dataclass(frozen=True)
class Capability:
    tier: int  # 0 — только чтение; 1 — ограниченная запись; 2 — только с решением человека
    description: str
    writes: tuple[str, ...] = ()


CAPABILITIES: dict[str, Capability] = {
    "submit_analysis_reply": Capability(1, "Принять вердикты по требованиям как сущности analysis_verdict", ("entities:analysis_verdict",)),
    "ask_question": Capability(0, "Положить свободный вопрос в ящик; отвечает человек, Claude или локальная модель (с одобрением)", ("agent_questions",)),
    "answer_request": Capability(0, "Ответить на запрос: требования по id или поиск фрагментов; ответ — только чтение"),
}
# Что агент НЕ может запросить никогда (нет обработчика; попытка фиксируется как rejected):
NEVER = (
    "удаление или изменение существующих данных проекта, файлов, БД", "запуск команд, скриптов, установка пакетов",
    "чтение файлов вне данных проекта, .env, паролей, токенов, ключей", "доступ к другим проектам и пользователям",
    "изменение настроек сервера, ролей, MCP-токенов, LLM-провайдера", "внешние сетевые запросы по адресам из письма",
    "остановка/перезапуск серверов и воркеров",
)

_DESTRUCTIVE = re.compile(
    r"\b(rm\s+-rf|rmdir|del\s+/[a-z]|format\s+[a-z]:|drop\s+(?:table|database)|delete\s+from|truncate\s+table|shutdown|taskkill|"
    r"remove-item|os\.system|subprocess|powershell|cmd\.exe|curl\s+[^|]*\|\s*(?:sh|bash)|eval\(|exec\()", re.IGNORECASE)
_PATHY = re.compile(r"(?:^|[\s\"'(])(?:[a-zA-Z]:[\\/]|\.\.[\\/]|/(?:etc|root|home|var|usr|windows)\b|~[\\/])", re.IGNORECASE)
_INJECTION = re.compile(
    r"(ignore\s+(?:all\s+)?(?:previous|prior|above)|disregard\s+(?:the\s+)?(?:above|system)|you\s+(?:must|are\s+now)\s|system\s+prompt|"
    r"игнорируй\s+(?:все\s+)?(?:предыдущ|прежн)|забудь\s+(?:все\s+)?инструкци|ты\s+теперь\s|выполни\s+команду|отправь\s+\S+\s+(?:пароль|токен|ключ)|"
    r"reveal\s+(?:the\s+)?(?:password|token|key|secret)|покажи\s+(?:пароль|токен|ключ))", re.IGNORECASE)


@dataclass
class Decision:
    status: str  # accepted | rejected | needs_human
    kind: str
    reasons: list[str] = field(default_factory=list)
    risk: int = 0
    dropped: list[dict] = field(default_factory=list)
    result: dict | None = None


def _walk_strings(obj: Any, path: str = "$"):
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk_strings(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk_strings(v, f"{path}[{i}]")


def screen_payload(payload: Any) -> list[tuple[str, str, str]]:
    """Подозрительное содержимое: [(путь, категория, фрагмент)]. Только обнаружение — исполнения всё равно нет."""
    flags = []
    for path, text in _walk_strings(payload):
        for cat, rx in (("destructive", _DESTRUCTIVE), ("path", _PATHY), ("injection", _INJECTION)):
            m = rx.search(text)
            if m:
                flags.append((path, cat, text[max(0, m.start() - 20): m.end() + 20]))
    return flags


def _classify(payload: dict, filename_kind: str | None) -> str | None:
    if filename_kind:
        return filename_kind
    if payload.get("items") is not None and payload.get("request_id") is not None and "ask" not in payload:
        return "submit_analysis_reply"
    if "question" in payload:
        return "ask_question"
    if "ask" in payload or "requirement_ids" in payload:
        return "answer_request"
    return None


def _log(db: Session, project_id: int, source: str, dec: Decision, payload: Any, raw_size: int, *, request_id: Any = None,
         artifact_id: str | None = None, response: dict | None = None) -> AgentExchangeLog:
    row = AgentExchangeLog(
        project_id=project_id, source=source[:64], kind=dec.kind[:48], request_id=str(request_id)[:64] if request_id is not None else None,
        decision=dec.status, risk=dec.risk, reasons_json=json.dumps(dec.reasons + [f"dropped: {d}" for d in dec.dropped[:20]], ensure_ascii=False)[:8000],
        payload_sha=hashlib.sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest(),
        size_bytes=raw_size, artifact_id=artifact_id, response_json=json.dumps(response, ensure_ascii=False) if response is not None else None,
    )
    db.add(row)
    db.commit()
    return row


def evaluate(db: Session, settings: Settings, project_id: int, raw: bytes, *, source: str, filename: str = "",
             artifact_id: str | None = None) -> Decision:
    """Единая точка входа: разбор, проверка, (если разрешено) исполнение в рамках белого списка, журнал."""
    kind_by_name = "submit_analysis_reply" if REPLY_RE.match(filename or "") else ("ask_question" if QUESTION_RE.match(filename or "") else ("answer_request" if REQUEST_RE.match(filename or "") else None))
    size = len(raw)
    key_limit = _KEY_LIMIT.get()
    if _LIMITER.retry_after(f"{project_id}:{source}", key_limit):
        dec = Decision("rejected", kind_by_name or "unknown", [f"слишком много обращений: лимит {key_limit or _LIMITER.limit} за 10 минут"], risk=20)
        return _finish(db, project_id, source, dec, {}, size, None, artifact_id)
    _LIMITER.fail(f"{project_id}:{source}")
    from app.app.models.project import Project
    from app.app.services import agent_settings

    proj = db.get(Project, project_id)
    if proj is not None and not agent_settings.get_settings_for(proj)["exchange_enabled"]:
        return _finish(db, project_id, source, Decision("rejected", kind_by_name or "unknown", ["обмен с агентами выключен в настройках проекта"], risk=0), {}, size, None, artifact_id)
    if size > MAX_BYTES:
        return _finish(db, project_id, source, Decision("rejected", kind_by_name or "unknown", [f"размер {size} байт больше лимита {MAX_BYTES}"], risk=30), {}, size, None, artifact_id)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _finish(db, project_id, source, Decision("rejected", kind_by_name or "unknown", ["не UTF-8 JSON"], risk=10), {}, size, None, artifact_id)
    if not isinstance(payload, dict):
        return _finish(db, project_id, source, Decision("rejected", kind_by_name or "unknown", ["корень должен быть объектом"], risk=10), {}, size, None, artifact_id)
    request_id = payload.get("request_id")
    kind = _classify(payload, kind_by_name)
    if kind is None or kind not in CAPABILITIES:
        dec = Decision("rejected", kind or "unknown", ["запрос вне белого списка возможностей: такого обработчика нет"], risk=40)
        return _finish(db, project_id, source, dec, payload, size, request_id, artifact_id)
    lenient = kind in ("answer_request", "ask_question") and "schema_version" not in payload  # у запроса из контракта поля версии нет
    if not lenient and str(payload.get("schema_version", "")).split(".")[0] != SCHEMA_MAJOR:
        return _finish(db, project_id, source, Decision("rejected", kind, [f"поддерживается schema_version {SCHEMA_MAJOR}.x"], risk=10), payload, size, request_id, artifact_id)
    if payload.get("project_id") not in (None, project_id):
        return _finish(db, project_id, source, Decision("rejected", kind, ["project_id не совпадает с проектом обмена"], risk=60), payload, size, request_id, artifact_id)

    flags = screen_payload(payload)
    risk = 0
    reasons: list[str] = []
    if any(cat in ("destructive", "path") for _p, cat, _f in flags):
        bad = [f"{p}: {cat}" for p, cat, _f in flags if cat in ("destructive", "path")][:5]
        return _finish(db, project_id, source, Decision("rejected", kind, ["в данных найдены команды или пути вне проекта: " + "; ".join(bad)], risk=90), payload, size, request_id, artifact_id)
    injection = [p for p, cat, _f in flags if cat == "injection"]
    if injection:  # не исполняется и не обрабатывается как указание; для записи требуется человек
        risk = 50
        reasons.append("текст похож на указание к ассистенту (данные не исполняются): " + ", ".join(injection[:5]))
        if CAPABILITIES[kind].tier >= 1:
            return _finish(db, project_id, source, Decision("needs_human", kind, reasons, risk=risk), payload, size, request_id, artifact_id)

    if kind == "submit_analysis_reply":
        dec = _import_reply(db, project_id, payload, artifact_id)
    elif kind == "ask_question":
        dec = _ask_question(db, settings, project_id, payload, source)
    else:
        dec = _answer_request(db, settings, project_id, payload)
    dec.risk = max(dec.risk, risk)
    dec.reasons = reasons + dec.reasons
    return _finish(db, project_id, source, dec, payload, size, request_id, artifact_id)


def _finish(db, project_id, source, dec: Decision, payload, size, request_id, artifact_id) -> Decision:
    row = _log(db, project_id, source, dec, payload, size, request_id=request_id, artifact_id=artifact_id, response=dec.result)
    cid = _CLIENT_ID.get()
    if cid:
        row.client_request_id = cid
        db.commit()
    return dec


def _s(value: Any, limit: int) -> str:
    return str(value if value is not None else "").replace("\x00", "").strip()[:limit]


def _clean_item(raw: dict) -> dict:
    """Пересобирает вердикт только из разрешённых полей с обрезкой длин; неизвестные ключи отбрасываются."""
    evidence = []
    for e in (raw.get("code_evidence") or [])[:20]:
        if isinstance(e, dict):
            evidence.append({k: _s(e.get(k), lim) for k, lim in (("project", 64), ("object", 200), ("path", 300), ("lines", 32), ("note", 500))})
    data_ev = []
    for e in (raw.get("data_evidence") or [])[:20]:
        if isinstance(e, dict):
            v = e.get("value")
            data_ev.append({"probe": _s(e.get("probe"), 64), "metric": _s(e.get("metric"), 100), "value": v if isinstance(v, (int, float)) else _s(v, 100), "as_of": _s(e.get("as_of"), 32)})
    fixes = [{"given": _s(f.get("given"), 200), "actual": _s(f.get("actual"), 200), "why": _s(f.get("why"), 500)}
             for f in (raw.get("anchor_corrections") or [])[:10] if isinstance(f, dict)]
    return {
        "requirement_id": _s(raw.get("requirement_id"), 32), "verdict": _s(raw.get("verdict"), 32), "confidence": _s(raw.get("confidence"), 16),
        "code_evidence": evidence, "data_evidence": data_ev,
        "blockers": [_s(b, 500) for b in (raw.get("blockers") or [])[:10]],
        "questions_for_customer": [_s(q, 500) for q in (raw.get("questions_for_customer") or [])[:10]],
        "conflicts_found": [_s(c, 32) for c in (raw.get("conflicts_found") or [])[:10] if CFT_ID_RE.match(_s(c, 32))],
        "anchor_corrections": fixes, "untrusted_source": True,
    }


def _import_reply(db: Session, project_id: int, payload: dict, artifact_id: str | None) -> Decision:
    dec = Decision("accepted", "submit_analysis_reply")
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        dec.status, dec.reasons = "rejected", ["items должен быть непустым списком"]
        return dec
    if len(items) > MAX_ITEMS:
        dec.status, dec.reasons = "rejected", [f"больше {MAX_ITEMS} вердиктов за раз"]
        return dec
    art = db.scalar(select(Artifact).where(Artifact.project_id == project_id, Artifact.artifact_id == artifact_id)) if artifact_id else None
    if art is None:
        dec.status, dec.reasons = "rejected", ["ответ принимается только загруженным файлом-артефактом проекта"]
        return dec
    known = set()
    try:
        known = {r["id"] for r in ctr.get_contract(db, _settings_for(db), project_id, force=False).files["requirements"]["items"]}
    except Exception:  # noqa: BLE001 — без контракта сверить id нельзя: принимаем только по формату
        known = set()
    request_id = _s(payload.get("request_id"), 64)
    imported = 0
    active_old: dict[str, list[Entity]] = {}
    for old in db.scalars(select(Entity).join(Artifact, Artifact.id == Entity.artifact_id).where(
            Artifact.project_id == project_id, Entity.entity_type == "analysis_verdict", Entity.is_active.is_(True))):
        try:
            active_old.setdefault(str(json.loads(old.value_json).get("requirement_id")), []).append(old)
        except ValueError:
            continue
    for i, raw in enumerate(items):
        if not isinstance(raw, dict):
            dec.dropped.append({"index": i, "why": "элемент не объект"})
            continue
        item = _clean_item(raw)
        if not REQ_ID_RE.match(item["requirement_id"]):
            dec.dropped.append({"index": i, "why": "requirement_id неверного формата"})
        elif known and item["requirement_id"] not in known:
            dec.dropped.append({"index": i, "why": f"неизвестное требование {item['requirement_id']}"})
        elif item["verdict"] not in VERDICTS:
            dec.dropped.append({"index": i, "why": "недопустимый verdict"})
        elif item["confidence"] not in CONFIDENCE:
            dec.dropped.append({"index": i, "why": "недопустимый confidence"})
        else:
            item["request_id"] = request_id
            item["produced_at"] = _s(payload.get("produced_at"), 40)
            # прежний вердикт по этому требованию заменяется мягко (is_active=False), ничего не удаляется
            for old in active_old.pop(item["requirement_id"], []):
                old.is_active = False
            db.add(Entity(artifact_id=art.id, entity_type="analysis_verdict", value_json=json.dumps(item, ensure_ascii=False), confidence=0.5, needs_review=True))
            imported += 1
    db.commit()
    dec.result = {"imported": imported, "dropped": len(dec.dropped)}
    if imported == 0:
        dec.status, dec.reasons = "rejected", dec.reasons + ["ни один вердикт не прошёл проверку"]
    return dec


def _settings_for(db: Session) -> Settings:
    from app.app.config import get_settings

    return get_settings()


def _answer_request(db: Session, settings: Settings, project_id: int, payload: dict) -> Decision:
    """Только чтение: требования по id либо поиск фрагментов. Текст ask — поисковый запрос, а не инструкция."""
    from app.app.services import project_mcp_contract as tools
    from app.app.services import project_mcp_data as data

    dec = Decision("accepted", "answer_request")
    request_id = payload.get("request_id")
    ids = payload.get("requirement_ids")
    ask = _s(payload.get("ask"), 1000)
    body: dict[str, Any] = {"schema_version": "1.0", "request_id": request_id, "produced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "answered_by": "kb"}
    if isinstance(ids, list) and ids:
        wanted = [_s(x, 32) for x in ids[:50] if REQ_ID_RE.match(_s(x, 32))]
        page = tools.get_requirements(db, settings, project_id, requirement_ids=wanted, limit=50)
        body.update({"kind": "requirements", "total": page["total"], "items": page["items"], "ask_echo": ask})
    elif ask:
        result = data.mcp_search_project(db, settings, project_id, query=ask, limit=8)
        body.update({"kind": "search", "query": ask, "items": [{**r, "text": r["text"][:600]} for r in result["results"]], "total": len(result["results"])})
    else:
        dec.status, dec.reasons = "rejected", ["нужен ask или requirement_ids"]
        return dec
    dec.result = body
    return dec


def _ask_question(db: Session, settings: Settings, project_id: int, payload: dict, source: str) -> Decision:
    """Вопрос идёт в ящик как данные. Инъекции в тексте вопроса не блокируют приём (вопрос ничего не исполняет),
    но ответ всегда даёт человек/Claude либо модель с одобрением, а не автоматика."""
    from app.app.services import agent_inbox as inbox

    dec = Decision("accepted", "ask_question")
    question = _s(payload.get("question"), inbox.MAX_QUESTION)
    request_id = payload.get("request_id")
    if not question or request_id is None:
        dec.status, dec.reasons = "rejected", ["нужны request_id и question"]
        return dec
    ids = payload.get("requirement_ids")
    wanted = [_s(x, 32) for x in ids[:50] if REQ_ID_RE.match(_s(x, 32))] if isinstance(ids, list) else []
    row = inbox.create_question(db, settings, project_id, request_id=request_id, question=question, requirement_ids=wanted, source=source)
    dec.result = inbox.reply_body(row)
    return dec


def _status_body(row: AgentExchangeLog) -> dict:
    """Ответ о судьбе файла агента: rejected / needs_human / accepted (для вердиктов — сколько принято)."""
    reasons = json.loads(row.reasons_json or "[]")
    body = {"schema_version": "1.0", "request_id": row.request_id, "kind": "status", "status": row.decision, "handled": row.kind,
            "at": row.at.isoformat() if row.at else None}
    if row.decision != "accepted":
        body["reason"] = "; ".join(str(r) for r in reasons)[:500] or "без причины"
    if row.decision == "needs_human":
        body["note"] = "Файл ушёл человеку на проверку; ничего не записано"
    if row.response_json:
        try:
            body.update({k: v for k, v in json.loads(row.response_json).items() if k in ("imported", "dropped")})
        except ValueError:
            pass
    return body


def _reply_rows(db: Session, project_id: int) -> list[dict]:
    """Ответы базы знаний по request_id (последнее обращение с этим id): answer_request, ask_question, статусы отказов и приёма вердиктов."""
    from app.app.services import agent_inbox as inbox

    rows = db.scalars(select(AgentExchangeLog).where(AgentExchangeLog.project_id == project_id, AgentExchangeLog.request_id.is_not(None))
                      .order_by(AgentExchangeLog.id.desc()).limit(300)).all()
    questions = {q.request_id: q for q in inbox.list_questions(db, project_id, limit=500)}
    out, seen = [], set()
    for r in rows:
        if r.request_id in seen:
            continue
        seen.add(r.request_id)
        item = {"name": f"kb_reply_{r.request_id}.json", "request_id": r.request_id, "at": r.at.isoformat() if r.at else None}
        if r.kind == "answer_request" and r.decision == "accepted" and r.response_json:
            item["body"] = json.loads(r.response_json)
        elif r.kind == "ask_question" and r.decision == "accepted" and r.request_id in questions:
            q = questions[r.request_id]
            item["body"] = inbox.reply_body(q)
            item["at"] = (q.answered_at or q.created_at).isoformat() if (q.answered_at or q.created_at) else item["at"]
        else:
            item["body"] = _status_body(r)
        out.append(item)
    return out


def list_replies(db: Session, project_id: int) -> list[dict]:
    """Ответы базы знаний на обращения агента — виртуальные файлы contract/kb_reply_<n>.json."""
    return _reply_rows(db, project_id)


def get_reply(db: Session, project_id: int, name: str) -> dict:
    m = re.match(r"^(?:contract/)?kb_reply_([A-Za-z0-9_-]{1,64})\.json$", name)
    if not m:
        raise ValueError("Unknown reply")
    for r in _reply_rows(db, project_id):
        if r["request_id"] == m.group(1):
            return r["body"]
    raise ValueError("Reply not found")


def wait_reply(db: Session, project_id: int, request_id: str, wait_s: float = 30.0) -> dict:
    """Ждать, пока статус ответа перестанет быть pending (не дольше wait_s ≤ 60 с). Один вызов вместо десятков опросов."""
    import time

    wait = max(0.0, min(float(wait_s), 60.0))
    name = f"kb_reply_{request_id}.json"
    # Ожидание занимает поток на время wait_s: одновременных ожиданий не больше _WAITERS, остальные получают текущий статус сразу
    slot = _WAIT_SLOTS.acquire(blocking=False) if wait > 0 else False
    if not slot:
        wait = 0.0
    deadline = time.monotonic() + wait
    try:
        while True:
            db.expire_all()
            try:
                body = get_reply(db, project_id, name)
            except ValueError:
                body = None
            db.rollback()  # не держать соединение с БД, пока спим
            if body is not None and body.get("status") != "pending":
                return body
            if time.monotonic() >= deadline:
                if body is None:
                    return {"schema_version": "1.0", "request_id": request_id, "kind": "status", "status": "not_found", "reason": "обращения с таким request_id нет"}
                if not slot and float(wait_s or 0) > 0:
                    return {**body, "note": "сервер сейчас держит максимум ожиданий: повтори wait_reply через несколько секунд"}
                return body
            time.sleep(1.0)
    finally:
        if slot:
            _WAIT_SLOTS.release()


def exchange_status(db: Session, project_id: int, source: str | None = None) -> dict:
    """Кто кого ждёт: очередь вопросов, последний ответ, вердикты без подтверждения человеком, остаток лимита."""
    from app.app.services import agent_inbox as inbox

    qs = inbox.list_questions(db, project_id, limit=500)
    counts = {s: sum(1 for q in qs if q.status == s) for s in ("pending", "drafted", "answered")}
    answered = [q.answered_at for q in qs if q.answered_at]
    waiting = 0
    for e in db.scalars(select(Entity).join(Artifact, Artifact.id == Entity.artifact_id).where(
            Artifact.project_id == project_id, Entity.entity_type == "analysis_verdict", Entity.is_active.is_(True), Entity.needs_review.is_(True))):
        waiting += 1
    from app.app.services import agent_letters

    return {"letters_from_kb": agent_letters.summary(project_id), "questions": {"waiting_for_answer": counts["pending"] + counts["drafted"], "answered": counts["answered"]},
            "last_answer_at": max(answered).isoformat() if answered else None, "verdicts_waiting_human_confirmation": waiting,
            "rate_limit_remaining": _LIMITER.remaining(f"{project_id}:{source}") if source else None}


SUBMIT_KINDS = {"question": ("analysis_question", "ask_question"), "verdicts": ("analysis_reply", "submit_analysis_reply")}


def submit_via_mcp(db: Session, settings: Settings, project_id: int, token, what: str, *, request_id: Any, body: dict,
                   client_request_id: str | None = None) -> dict:
    """Запись в очередь обмена через MCP: тот же путь, что у загруженного файла (артефакт-транспорт → evaluate → журнал), без переноса человеком.

    Только добавление. Право даёт флаг ключа can_submit (выдаёт владелец проекта). Повтор с тем же client_request_id возвращает первый результат."""
    from app.app.services.storage import ensure_inside, generate_stored_filename, raw_input_dir
    from app.app.services.upload import create_artifact_record, detect_file_type

    from app.app.services.project_mcp_tokens import token_scope

    scope = token_scope(token)
    if scope == "questions" and what == "verdicts":
        raise KeyScopeError("У этого ключа право только на вопросы: отправлять вердикты он не может. Право «вопросы и вердикты» "
                            "выдаёт владелец проекта.", "verdicts_not_allowed")
    if scope == "read":
        raise KeyScopeError("У этого ключа нет права записи в очередь обмена. Его включает владелец проекта в карточке проекта → «Удалённый доступ (MCP)».", "read_only_key")
    rid = str(request_id).strip() if request_id not in (None, "") else ""
    if rid and not re.fullmatch(r"\d{1,9}", rid):
        raise ValueError("request_id должен быть целым числом (до 9 цифр); можно не передавать: номер выдаст сервер")
    cid = (str(client_request_id).strip()[:64] or None) if client_request_id is not None else None
    prefix, kind = SUBMIT_KINDS[what]
    if cid:
        prev = db.scalar(select(AgentExchangeLog).where(AgentExchangeLog.project_id == project_id, AgentExchangeLog.client_request_id == cid,
                                                        AgentExchangeLog.kind == kind).order_by(AgentExchangeLog.id))
        if prev is not None:
            return {**_submit_result(db, what, prev, None), "duplicate": True}
    if not rid:
        rid = str(_next_request_id(db, project_id))  # номер выдаёт сервер: конфликтов не бывает
    taken = db.scalar(select(AgentExchangeLog.id).where(AgentExchangeLog.project_id == project_id, AgentExchangeLog.request_id == rid,
                                                        AgentExchangeLog.decision == "accepted").limit(1))
    if taken is not None:
        raise ValueError(f"request_id {rid} уже использован: ответ ищи в contract/kb_reply_{rid}.json; не передавай request_id (номер выдаст сервер) или возьми {_next_request_id(db, project_id)}")
    payload = {"schema_version": "1.0", "request_id": int(rid), **body}
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    name = f"{prefix}_{rid}.json"
    stored = generate_stored_filename(name)
    dest = ensure_inside(settings.data_dir, raw_input_dir(settings, project_id, "document") / stored)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)
    art, _job = create_artifact_record(db, project_id=project_id, user_id=token.created_by, original_filename=name, stored_filename=stored, file_path=dest,
                                       file_type=detect_file_type(name, "application/json"), source_type="document",
                                       sha256=hashlib.sha256(raw).hexdigest(), priority_weight=1.0, duration_sec=None, queue_job=False)
    source = f"mcp:{token.id}"
    tok = _CLIENT_ID.set(cid)
    lim_tok = _KEY_LIMIT.set(getattr(token, "rate_limit", None))
    try:
        dec = handle_exchange_upload(db, settings, art, dest, source=source, filename=name)
    finally:
        _CLIENT_ID.reset(tok)
        _KEY_LIMIT.reset(lim_tok)
    row = db.scalar(select(AgentExchangeLog).where(AgentExchangeLog.project_id == project_id, AgentExchangeLog.artifact_id == art.artifact_id).order_by(AgentExchangeLog.id.desc()))
    out = _submit_result(db, what, row, dec)
    out["rate_limit_remaining"] = _LIMITER.remaining(f"{project_id}:{source}", getattr(token, "rate_limit", None))
    return out


def _next_request_id(db: Session, project_id: int) -> int:
    ids = [int(r) for r in db.scalars(select(AgentExchangeLog.request_id).where(AgentExchangeLog.project_id == project_id, AgentExchangeLog.request_id.is_not(None))) if str(r).isdigit()]
    return max(ids, default=0) + 1


def _submit_result(db: Session, what: str, row: AgentExchangeLog, dec: Decision | None) -> dict:
    rid = row.request_id
    if row.decision != "accepted":
        return {"status": row.decision, "request_id": rid, "reason": "; ".join(json.loads(row.reasons_json or "[]"))[:500], "reply_artifact": f"contract/kb_reply_{rid}.json"}
    if what == "question":
        return {"status": "queued", "request_id": rid, "reply_artifact": f"contract/kb_reply_{rid}.json", "hint": f"wait_reply(request_id={rid})"}
    res = json.loads(row.response_json or "{}")
    dropped = json.loads(row.reasons_json or "[]")
    return {"status": "accepted", "request_id": rid, "written": res.get("imported", 0), "rejected": [d for d in dropped if str(d).startswith("dropped:")][:50],
            "note": "вердикты записаны как analysis_verdict с пометкой untrusted_source и ждут подтверждения человеком", "reply_artifact": f"contract/kb_reply_{rid}.json"}


def handle_exchange_upload(db: Session, settings: Settings, art: Artifact, path, *, source: str, filename: str) -> Decision:
    """Загруженный файл analysis_reply_/analysis_request_*.json: в конвейер обработки НЕ попадает, идёт через шлюз."""
    raw = path.read_bytes() if path.is_file() else b""
    dec = evaluate(db, settings, art.project_id, raw, source=source, filename=filename, artifact_id=art.artifact_id)
    art.status = "done" if dec.status == "accepted" else ("rejected" if dec.status == "rejected" else "needs_human")
    art.error_message = None if dec.status == "accepted" else "; ".join(dec.reasons)[:500]
    db.commit()
    return dec


def is_exchange_filename(name: str) -> bool:
    return bool(REPLY_RE.match(name or "") or REQUEST_RE.match(name or "") or QUESTION_RE.match(name or ""))
