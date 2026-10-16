"""Документы в памяти решений (docs/plans/DOCUMENT-MEMORY-PLAN.md): документ разбирается как созвон — по единицам.

Единица документа — абзац, пункт списка или строка таблицы (``doc_units``, адрес ``P<n>``); заголовки и шапки таблиц не размечаются,
а идут контекстом к строкам («раздел», «таблица: шапка»). Автор всех единиц — сам документ.

Паспорт документа — один вопрос модели по началу и концу текста: что это за документ, чья сторона его написала, есть ли в нём
признаки утверждения и дата. На каждый ответ нужна дословная цитата из документа; цитату проверяет код, не подтверждённое
становится «не известно». Паспорт лежит рядом с разбором (``<artifact_id>_passport.json``) и пересчитывается, когда меняется
текст документа или версия промпта.

Из паспорта акты документа получают сторону (кто писал), дату (если у материала нет фактической даты, а в документе она есть) и,
если документ утверждён заказчиком или обеими сторонами, пункты «зафиксировано в документе» становятся согласием заказчика.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

from app.app.config import Settings
from app.app.logging_setup import log_event
from app.app.models.artifact import Artifact

logger = logging.getLogger("skkkk.doc_memory")

PASSPORT_VERSION = "document_passport-v1"
ACTS_PROMPT_VERSION = "speech_acts_doc-v2"  # v2: контекст не размечается, ответ одной строкой
HEAD_CHARS = 9000  # начало документа для паспорта: титул, реквизиты, лист согласования, введение
TAIL_CHARS = 3000  # конец: подписи, «утверждаю», приложения
SECTION_CHARS = 120
TABLE_HEAD_CHARS = 300
KINDS = ("contract", "tz", "protocol", "plan", "status_report", "spec", "presentation", "correspondence", "other")
KIND_LABELS = {"contract": "договор", "tz": "техническое задание", "protocol": "протокол", "plan": "план работ",
               "status_report": "отчёт о статусе", "spec": "описание или регламент", "presentation": "презентация",
               "correspondence": "письмо", "other": "документ"}
SIDES = ("customer", "executor")
APPROVERS = ("customer", "executor", "both")
SIDE_LABELS = {"customer": "заказчик", "executor": "исполнитель", "both": "обе стороны"}
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower().replace("ё", "е")).strip()


def _verified(quote: str, text_norm: str) -> bool:
    q = _norm(quote).strip(" .,;:!?«»\"'()")
    return len(q) >= 4 and q in text_norm


def _prompt(name: str) -> str:
    from app.app.services.prompts import load_prompt_block
    from app.app.services.speech_acts import ROOT

    return load_prompt_block(ROOT / "docs" / "prompts" / name)


def acts_system() -> str:
    return _prompt("speech_acts_doc.md")


def is_document(artifact: Artifact) -> bool:
    """Материал без говорящих: всё, что не звук (выгрузки переписки отсеивает раньше ``speech_acts.dialog_units``), кроме файлов
    обмена с агентами (``analysis_question_*.json`` и ответы): это служебная переписка портала, а не материал проекта."""
    from app.app.services.agent_gateway import is_exchange_filename

    return getattr(artifact, "file_type", "") != "audio" and not is_exchange_filename(getattr(artifact, "original_filename", "") or "")


def document_text(settings: Settings, artifact: Artifact) -> str:
    from app.app.services.storage import artifact_knowledge_text

    return artifact_knowledge_text(settings, artifact)


def document_label(artifact: Artifact) -> str:
    return f"Документ «{artifact.original_filename}»"


def document_units(text: str, label: str) -> list:
    """Единицы документа для разметки: абзацы, пункты, строки таблиц. Заголовок даёт «раздел» следующим единицам, первая строка
    таблицы — её шапку (сама не размечается)."""
    from app.app.services.doc_units import KIND_HEADING, KIND_ROW, split_units
    from app.app.services.speech_acts import DUnit

    out = []
    section = header = ""
    prev = None
    for u in split_units(text):
        if u.kind == KIND_HEADING:
            section, prev = u.text[:SECTION_CHARS], u.kind
            continue
        lead = ""
        if u.kind == KIND_ROW:
            if prev != KIND_ROW:
                header, prev = u.text[:TABLE_HEAD_CHARS], u.kind
                continue
            lead = f"таблица: {header}"
        out.append(DUnit(u.address, u.id, label, u.text, section=section, lead=lead))
        prev = u.kind
    return out


def _passport_path(settings: Settings, artifact: Any):
    from app.app.services.storage import parsed_dir

    return parsed_dir(settings, artifact.project_id, artifact.source_type) / f"{artifact.artifact_id}_passport.json"


def parse_passport(raw: str, text: str) -> dict[str, Any]:
    """Ответ модели -> паспорт. Поле без дословной цитаты из документа становится «не известно»."""
    from app.app.services.llm import strip_json_fence

    data = json.loads(strip_json_fence(raw))
    if not isinstance(data, dict):
        raise ValueError("паспорт: нужен объект JSON")
    hay = _norm(text)

    def quoted(key: str) -> str:
        q = re.sub(r"\s+", " ", str(data.get(key) or "")).strip()
        return q if q and _verified(q, hay) else ""

    kind = str(data.get("kind") or "").strip().lower()
    kind_quote = quoted("kind_quote")
    side = str(data.get("author_side") or "").strip().lower()
    side_quote = quoted("author_quote")
    approved = str(data.get("approved") or "").strip().lower()
    approved_by = str(data.get("approved_by") or "").strip().lower()
    approved_quote = quoted("approved_quote")
    day = str(data.get("date") or "").strip()
    date_quote = quoted("date_quote")
    return {
        "kind": kind if kind in KINDS else "other",
        "kind_quote": kind_quote,
        "author_side": side if side in SIDES and side_quote else "unknown",
        "author_quote": side_quote if side in SIDES else "",
        "approved": "yes" if approved == "yes" and approved_by in APPROVERS and approved_quote else ("no" if approved == "no" else "unknown"),
        "approved_by": approved_by if approved == "yes" and approved_by in APPROVERS and approved_quote else "unknown",
        "approved_quote": approved_quote if approved == "yes" else "",
        "date": day if _DATE.match(day) and date_quote else "",
        "date_quote": date_quote if _DATE.match(day) else "",
    }


def build_passport_user(artifact: Any, text: str) -> str:
    head = text[:HEAD_CHARS]
    tail = text[-TAIL_CHARS:] if len(text) > HEAD_CHARS + TAIL_CHARS else text[HEAD_CHARS:]
    parts = [f"Файл: {artifact.original_filename}", "", "Начало документа:", head]
    if tail.strip():
        parts += ["", "[…]", "", "Конец документа:", tail]
    return "\n".join(parts)


def passport(settings: Settings, artifact: Any, text: str, ask: Callable[[str, str], str]) -> dict[str, Any]:
    """Паспорт документа: из файла, если текст и версия те же, иначе — вопрос модели. Ошибка модели — паспорт «всё не известно»."""
    sig = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
    path = _passport_path(settings, artifact)
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if cached.get("sig") == sig and cached.get("version") == PASSPORT_VERSION:
            return cached
    except (OSError, ValueError):
        pass
    system = _prompt("document_passport.md")
    user = build_passport_user(artifact, text)
    result: dict[str, Any] | None = None
    error = ""
    for attempt in range(2):
        try:
            result = parse_passport(ask(system, user), text)
            break
        except (ValueError, json.JSONDecodeError, RuntimeError) as exc:
            error = str(exc)[:200]
            user += f"\n\nПредыдущий ответ невалиден: {error}. Верни только JSON паспорта."
    if result is None:
        log_event(logger, "doc_memory.passport_failed", artifact=artifact.artifact_id, error=error)
        result = parse_passport("{}", text)
    result.update({"sig": sig, "version": PASSPORT_VERSION})
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("passport not saved for %s: %s", artifact.artifact_id, exc)
    log_event(logger, "doc_memory.passport", artifact=artifact.artifact_id, kind=result["kind"], side=result["author_side"],
              approved=result["approved"], approved_by=result["approved_by"], date=result["date"] or "-")
    return result


@dataclass
class DocContext:
    """Всё, что нужно разметке актов документа: единицы, паспорт и то, как акты получают сторону, дату и вид."""

    units: list
    passport: dict[str, Any]
    label: str
    said_at: str | None  # дата из документа, если у материала нет фактической даты

    @property
    def side(self) -> str | None:
        return self.passport.get("author_side") if self.passport.get("author_side") in SIDES else None

    @property
    def approved_by_customer(self) -> bool:
        return self.passport.get("approved") == "yes" and self.passport.get("approved_by") in ("customer", "both")

    def act_and_side(self, act: str) -> tuple[str, str | None]:
        """Утверждённый заказчиком (или обеими сторонами) документ: «зафиксировано» — это согласие заказчика."""
        if act == "documented" and self.approved_by_customer:
            return "agreed", "customer"
        return act, self.side

    def header(self) -> str:
        p = self.passport
        bits = [f"вид: {KIND_LABELS.get(p.get('kind'), 'документ')}"]
        if p.get("author_side") in SIDES:
            bits.append(f"написан стороной: {SIDE_LABELS[p['author_side']]}")
        if p.get("approved") == "yes":
            bits.append(f"утверждён: {SIDE_LABELS.get(p.get('approved_by'), 'да')}")
        if p.get("date"):
            bits.append(f"дата: {p['date']}")
        return f"{self.label} ({'; '.join(bits)})"

    def build_user(self, context: list, targets: list) -> str:
        targets_refs = ", ".join(u.ref for u in targets)
        lines = [self.header(), f"Размечай ТОЛЬКО единицы: {targets_refs}. Строки «·» — контекст, их метки в ответе не допускаются.",
                 "Окно единиц документа:"]
        section = None
        for mark, units in (("·", context), (">", targets)):
            for u in units:
                if u.section and u.section != section:
                    lines.append(f"  § {u.section}")
                    section = u.section
                lead = f" [{u.lead}]" if u.lead else ""
                lines.append(f"{mark} {u.ref}{lead} {u.text}")
        return "\n".join(lines)


def prepare(settings: Settings, artifact: Artifact, ask: Callable[[str, str], str], *, release: Callable[[], None] | None = None) -> DocContext | None:
    """Документ для разметки актов или None (не документ, нет текста). Всё нужное из базы читается до вопроса модели; ``release``
    (``db.commit``) отпускает соединение перед ним — паспорт спрашивается по снимку, а не по объекту сессии."""
    from types import SimpleNamespace

    from app.app.services.recorded_at import SOURCE_UPLOAD, material_date

    if not is_document(artifact):
        return None
    text = document_text(settings, artifact)
    label = document_label(artifact)
    units = document_units(text, label)
    if not units:
        return None
    _day, source = material_date(artifact)
    snap = SimpleNamespace(artifact_id=artifact.artifact_id, original_filename=artifact.original_filename, project_id=artifact.project_id,
                           source_type=artifact.source_type)
    if release is not None:
        release()  # соединение БД не держим на время ответа модели
    pas = passport(settings, snap, text, ask)
    said_at = pas["date"] if pas.get("date") and source == SOURCE_UPLOAD else None
    return DocContext(units=units, passport=pas, label=label, said_at=said_at)
