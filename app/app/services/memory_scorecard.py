"""Табло памяти решений: метрики качества и чек-лист приёмки платформы (DECISION-MEMORY-PLAN §4.3, PLATFORM-FOUNDATION-PLAN ПФ10).

Считается только по базе и файлам, модель не вызывается: пересчёт занимает секунды. Эталон лежит в ``DATA_DIR/eval`` (вне git:
имена, цитаты и суммы проекта) — ``memory_gold.csv`` (размеченные ленты), ``memory_rules.json`` (правила итогов M2),
``memory_people.csv`` (лист людей: кто на чьей стороне, M8). История прогонов — ``memory_scorecard.csv`` (одна строка на прогон).

Метрики:
- M1 — доля пунктов контракта с цитатой источника;
- M2 — доля правил эталона, по которым состояние пунктов верное;
- M3~ — доля эталонных событий, у которых есть пункт на том же сообщении или реплике (грубо: вопросы и «сделал» пунктами не становятся);
- M7 — доля людей из эталона, найденных в реестре проекта с известной стороной;
- M8 — доля эталонных событий, у которых сторона источника в контракте совпала с листом людей (там, где человек известен);
- A1 — речевые акты (Э2): доля эталонных событий, у реплики которых найден хоть какой-то акт (полнота; цель ≥ 80 %);
- A2 — речевые акты: среди найденных доля с верным видом акта по эталону (ложный вид ≤ 10 % ⇒ A2 ≥ 90 %);
- M9 — доля действующих пунктов документов, у которых есть абзац-источник P<n> (Э1; цель 100 %);
- M4–M6 появятся вместе с речевыми актами, журналом событий и ответами чата.
"""
from __future__ import annotations

import csv
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.models.artifact import Artifact
from app.app.models.entity import Entity

HEADER = ["дата", "этап", "проект", "промпт", "модель", "M1", "M2", "M3", "M7", "M8", "M9", "A1", "A2", "детали"]
PENDING_METRICS = ("M4", "M5", "M6")
SIDE_TO_CONTRACT = {"customer": "customer", "executor": "vendor"}
TURN_TOLERANCE = 0.02  # секунд: соседние реплики бывают в одну секунду, поэтому реплику узнаём по точному началу


def eval_dir(settings: Settings) -> Path:
    return settings.data_dir / "eval"


def _paths(settings: Settings) -> dict[str, Path]:
    d = eval_dir(settings)
    return {"gold": d / "memory_gold.csv", "rules": d / "memory_rules.json", "people": d / "memory_people.csv", "board": d / "memory_scorecard.csv"}


# ---------------------------------------------------------------- чтение эталона

def gold_events(settings: Settings) -> list[dict[str, str]]:
    path = _paths(settings)["gold"]
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f, delimiter=";") if r.get("акт") != "ИТОГ" and r.get("источник")]


def _rules(settings: Settings) -> list[dict[str, Any]]:
    path = _paths(settings)["rules"]
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def people_sheet(settings: Settings) -> dict[str, str]:
    """Лист людей: ключ написания -> сторона (customer / executor). Нет файла — пусто."""
    from app.app.services.people import alias_key

    path = _paths(settings)["people"]
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f, delimiter=";"):
            side = (row.get("сторона") or "").strip()
            if side not in SIDE_TO_CONTRACT:
                continue
            for name in [row.get("человек") or ""] + (row.get("написания") or "").split("|"):
                if alias_key(name):
                    out[alias_key(name)] = side
    return out


def export_people_sheet(db: Session, settings: Settings, project_id: int) -> int:
    """Записать лист людей из реестра проекта (люди с известной стороной): то, что владелец проверил на экране «Люди и стороны».
    Лист — эталон для M8: после выгрузки его проверяют глазами и правят вручную."""
    from app.app.services import people

    path = _paths(settings)["people"]
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for p in people.list_people(db, project_id):
        if p.side in SIDE_TO_CONTRACT:
            aliases = [a.alias for a in people.aliases_of(db, p.id) if a.alias != p.display_name]
            rows.append([p.display_name, p.side, p.company_name or "", "|".join(aliases)])
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["человек", "сторона", "компания", "написания"])
        w.writerows(rows)
    return len(rows)


# ---------------------------------------------------------------- вспомогательное

def _anchors(db: Session, project_id: int) -> set[tuple[int, str, float]]:
    """Якоря пунктов: (номер материала, 'M'|'T', номер сообщения или начало реплики в секундах)."""
    out: set[tuple[int, str, float]] = set()
    rows = db.execute(select(Entity, Artifact).join(Artifact, Artifact.id == Entity.artifact_id)
                      .where(Artifact.project_id == project_id, Entity.entity_type.in_(("decision", "task", "open_question")))).all()
    for ent, art in rows:
        try:
            value = json.loads(ent.value_json or "{}")
        except ValueError:
            continue
        if value.get("message_id") is not None:
            out.add((art.id, "M", int(value["message_id"])))
        if value.get("start_sec") is not None:
            out.add((art.id, "T", round(float(value["start_sec"]), 2)))
    return out


def _turn_start(db: Session, settings: Settings, art_id: int, index: int) -> float | None:
    from app.app.services.transcript_turns import load_turns

    art = db.get(Artifact, art_id)
    if art is None:
        return None
    for t in load_turns(settings, art):
        if t.index == index and t.start_sec is not None:
            return round(float(t.start_sec), 2)
    return None


def _state(value: dict, active: bool) -> str:
    rel = value.get("relevance") or {}
    rel_state = rel.get("state") if not rel.get("undone") else None
    if not active:
        return rel_state if rel_state in ("superseded", "duplicate") else "rejected"
    if rel_state in ("superseded", "duplicate", "done"):
        return rel_state
    if value.get("status") == "done":
        return "done"
    return "current"


def _parse_anchor(raw: str) -> tuple[int, str, int] | None:
    m = re.fullmatch(r"a(\d+):([MT])(\d+)", raw or "")
    return (int(m.group(1)), m.group(2), int(m.group(3))) if m else None


# ---------------------------------------------------------------- метрики

def _m1(counts: dict[str, int]) -> float:
    return 1 - counts["unsourced"] / max(1, counts["total"])


def _m2(db: Session, settings: Settings, project_id: int) -> tuple[float | None, list[dict[str, Any]]]:
    rules = _rules(settings)
    if not rules:
        return None, []
    ents = []
    for ent, _art in db.execute(select(Entity, Artifact).join(Artifact, Artifact.id == Entity.artifact_id)
                                .where(Artifact.project_id == project_id, Entity.entity_type.in_(("decision", "task")))).all():
        value = json.loads(ent.value_json or "{}")
        ents.append((str(value.get("text") or value.get("description") or ""), _state(value, bool(ent.is_active))))
    details, passed = [], 0
    for rule in rules:
        pat = re.compile(rule["pattern"], re.I)
        states = [st for text, st in ents if pat.search(text)]
        live = [st for st in states if st not in ("rejected", "duplicate")] or states
        got = live or ["none"]
        ok = any(st in rule["accept"] for st in got) if rule.get("positive") else all(st in rule["accept"] for st in got)
        passed += ok
        details.append({"subject": rule["subject"], "note": rule["note"], "ok": ok, "got": sorted(set(got))})
    return passed / len(rules), details


SUBJECT_TO_RULE_STATE = {"open": "current", "proposed": "current", "agreed": "current", "implemented": "done", "rejected": "rejected",
                         "withdrawn": "rejected", "superseded": "superseded"}


def _m2_subjects(db: Session, settings: Settings, project_id: int) -> tuple[float | None, list[dict[str, Any]]]:
    """M2 по журналу: те же правила эталона, но состояние берётся у предметов памяти решений (текст — название, итог и цитаты актов)."""
    from app.app.models.decision_memory import Subject
    from app.app.models.speech_act import SpeechAct

    rules = _rules(settings)
    subjects = db.scalars(select(Subject).where(Subject.project_id == project_id, Subject.is_active.is_(True))).all()
    if not rules or not subjects:
        return None, []
    quotes: dict[int, list[str]] = {}
    for sid, quote in db.execute(select(SpeechAct.subject_id, SpeechAct.quote).where(SpeechAct.project_id == project_id, SpeechAct.subject_id.isnot(None))).all():
        quotes.setdefault(sid, []).append(quote or "")
    from app.app.models.decision_memory import SubjectPosition

    positions: dict[int, list[Any]] = {}
    for pos in db.scalars(select(SubjectPosition).where(SubjectPosition.subject_id.in_([s.id for s in subjects]))).all():
        positions.setdefault(pos.subject_id, []).append(pos)
    # единицы проверки: (название-текст, цитаты, состояние). У предмета с позициями каждая позиция — отдельная единица (текст — её название)
    units: list[tuple[str, list[str], str]] = []
    for s in subjects:
        if positions.get(s.id):  # состояние берётся у позиции (вариант/значение), а не у предмета целиком
            for pos in positions[s.id]:
                units.append((pos.label, [], "current" if pos.state == "open" else pos.state))
            continue
        units.append((f"{s.title} {s.summary or ''}", quotes.get(s.id, []), SUBJECT_TO_RULE_STATE.get(s.state, "current")))
    details, passed = [], 0
    for rule in rules:
        pat = re.compile(rule["pattern"], re.I)
        scored = [(3 * bool(pat.search(head)) + sum(bool(pat.search(q)) for q in qs), st) for head, qs, st in units]
        scored = [(score, st) for score, st in scored if score]
        if not rule.get("positive") and scored:  # «не должно висеть»: проверяется предмет, который о ней (главный по числу совпадений), а не любое упоминание слова
            top = max(score for score, _ in scored)
            scored = [(score, st) for score, st in scored if score * 2 >= top]
        got = sorted({st for _score, st in scored}) or ["none"]
        ok = any(st in rule["accept"] for st in got) if rule.get("positive") else all(st in rule["accept"] for st in got)
        passed += ok
        details.append({"subject": rule["subject"], "note": rule["note"], "ok": ok, "got": got})
    return passed / len(rules), details


def _m3(db: Session, settings: Settings, project_id: int, gold: list[dict[str, str]]) -> tuple[float | None, int, int]:
    if not gold:
        return None, 0, 0
    anchors = _anchors(db, project_id)
    found = 0
    for ev in gold:
        parsed = _parse_anchor(ev["источник"])
        if parsed is None:
            continue
        art_id, kind, num = parsed
        if kind == "T":
            start = _turn_start(db, settings, art_id, num)
            hit = start is not None and any(a[0] == art_id and a[1] == "T" and abs(a[2] - start) <= TURN_TOLERANCE for a in anchors)
        else:
            hit = (art_id, "M", num) in anchors
        found += hit
    return found / len(gold), found, len(gold)


def _m7(db: Session, project_id: int, gold: list[dict[str, str]]) -> tuple[float | None, list[str]]:
    """Авторы эталона, найденные в реестре проекта с известной стороной."""
    from app.app.models.person import SIDE_UNKNOWN
    from app.app.services import people

    authors = sorted({ev["автор"] for ev in gold if ev.get("автор")})
    if not authors:
        return None, []
    missing = []
    for author in authors:
        hit = people.resolve(db, project_id, author)
        if hit is None or hit.person.side == SIDE_UNKNOWN:
            missing.append(author)
    return 1 - len(missing) / len(authors), missing


def _evidence_sides(db: Session, settings: Settings, project_id: int) -> list[tuple[str, str, int | float, str]]:
    """Источники требований контракта: (строковый номер материала, 'M'|'T', сообщение или секунда, сторона)."""
    from app.app.services import contract as ctr

    out = []
    for item in ctr.get_contract(db, settings, project_id).files["requirements"]["items"]:
        for ev in item.get("evidence") or []:
            if ev.get("message_id") is not None:
                out.append((ev["artifact_id"], "M", int(ev["message_id"]), ev.get("side") or "unknown"))
            if ev.get("start_sec") is not None:
                out.append((ev["artifact_id"], "T", float(ev["start_sec"]), ev.get("side") or "unknown"))
    return out


# Вид акта эталона (слова человека) -> допустимые виды акта системы
GOLD_ACT_CLASSES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("согласился с отказом", ("agreed",)),
    ("отклонил и предложил", ("rejected", "proposed")),
    ("отклонил/встречное", ("rejected", "proposed")),
    ("предложила другое", ("proposed", "rejected")),
    ("отклон", ("rejected",)),
    ("согласил", ("agreed",)),
    ("подтвердил", ("agreed", "info")),
    ("отложил", ("deferred",)),
    ("спросил", ("asked",)),
    ("ответил", ("answered",)),
    ("о выполненном", ("done",)),
    ("о препятствии", ("blocked",)),
    ("предложил", ("proposed",)),
    ("просит", ("proposed",)),
    ("поручил", ("proposed",)),
    ("решил", ("proposed", "agreed")),
    ("зафиксировал", ("proposed", "agreed")),
    ("оговорка", ("info", "proposed")),
    ("сообщил", ("info", "answered", "done")),
)


def gold_act_classes(label: str) -> tuple[str, ...]:
    """Допустимые виды акта системы для эталонного вида; неизвестный — любой (не проверяется)."""
    low = (label or "").lower()
    for key, classes in GOLD_ACT_CLASSES:
        if key in low:
            return classes
    return ()


def _acts_metrics(db: Session, project_id: int, gold: list[dict[str, str]]) -> dict[str, Any]:
    """A1 — у реплики эталонного события найден акт; A2 — среди найденных вид акта верный. Нет актов в проекте — значения None."""
    from app.app.models.speech_act import SpeechAct

    rows = db.scalars(select(SpeechAct).where(SpeechAct.project_id == project_id)).all()
    if not rows or not gold:
        return {"a1": None, "a2": None, "found": 0, "right": 0, "total": 0, "wrong": []}
    by_unit: dict[tuple[int, str], list[str]] = {}
    for r in rows:
        by_unit.setdefault((r.artifact_id, r.unit_ref), []).append(r.act)
    found = right = 0
    wrong = []
    for ev in gold:
        parsed = _parse_anchor(ev["источник"])
        if parsed is None:
            continue
        got = by_unit.get((parsed[0], f"{parsed[1]}{parsed[2]}"))
        if not got:
            continue
        found += 1
        accepted = gold_act_classes(ev.get("акт", ""))
        if not accepted or any(a in accepted for a in got):
            right += 1
        elif len(wrong) < 12:
            wrong.append({"source": ev["источник"], "gold": ev.get("акт", ""), "got": sorted(set(got))})
    total = len(gold)
    return {"a1": found / total, "a2": right / found if found else None, "found": found, "right": right, "total": total, "wrong": wrong}


def _m9(db: Session, settings: Settings, project_id: int) -> tuple[float | None, int, int]:
    """Пункты документов проекта с абзацем-источником: (доля, с абзацем, всего). Нет документов с пунктами — (None, 0, 0)."""
    from app.app.services import doc_anchors

    total = bound = 0
    for art in doc_anchors.document_artifacts(db, settings, project_id):
        for ent in db.scalars(select(Entity).where(Entity.artifact_id == art.id, Entity.is_active.is_(True),
                                                   Entity.entity_type.in_(doc_anchors.HUMAN_TYPES))).all():
            try:
                value = json.loads(ent.value_json or "{}")
            except ValueError:
                continue
            total += 1
            bound += isinstance(value, dict) and isinstance(value.get("source_paragraph"), int)
    return (bound / total if total else None), bound, total


def _m8(db: Session, settings: Settings, project_id: int, gold: list[dict[str, str]]) -> dict[str, Any]:
    """Сторона источника в контракте против листа людей. Считаются только события, где автор есть в листе и у события есть пункт."""
    from app.app.services.people import alias_key

    sheet = people_sheet(settings)
    if not sheet or not gold:
        return {"value": None, "checked": 0, "wrong": []}
    evidence = _evidence_sides(db, settings, project_id)
    checked, wrong = 0, []
    for ev in gold:
        expected = sheet.get(alias_key(ev.get("автор")))
        parsed = _parse_anchor(ev["источник"])
        if expected is None or parsed is None:
            continue
        art_id, kind, num = parsed
        art = db.get(Artifact, art_id)
        if art is None:
            continue
        key = float(_turn_start(db, settings, art_id, num) or -1) if kind == "T" else num
        got = [side for aid, k, pos, side in evidence if aid == art.artifact_id and k == kind and abs(pos - key) <= (TURN_TOLERANCE if kind == "T" else 0)]
        if not got:
            continue
        checked += 1
        if SIDE_TO_CONTRACT[expected] not in got:
            wrong.append({"source": ev["источник"], "author": ev["автор"], "expected": SIDE_TO_CONTRACT[expected], "got": sorted(set(got))})
    return {"value": (checked - len(wrong)) / checked if checked else None, "checked": checked, "wrong": wrong[:10]}


# ---------------------------------------------------------------- чек-лист приёмки

def _item(key: str, title: str, status: str, detail: str) -> dict[str, str]:
    return {"id": key, "title": title, "status": status, "detail": detail}  # status: ok | warn | fail | manual


def checklist(db: Session, settings: Settings, project_id: int, *, m8: dict[str, Any]) -> list[dict[str, str]]:
    from app.app.models.company import Company
    from app.app.models.person import SIDE_UNKNOWN
    from app.app.models.project import Project
    from app.app.services import memory_platform, people, recorded_at as ra, voice_link
    from app.app.services.companies import load_companies_file, name_key

    items: list[dict[str, str]] = []
    listed = people.list_people(db, project_id)
    unknown_side = [p.display_name for p in listed if p.side == SIDE_UNKNOWN]
    open_q = len(people.open_unknown(db, project_id))
    if not listed:
        items.append(_item("people", "Все действующие лица опознаны и подтверждены", "fail", "реестр проекта пуст"))
    elif unknown_side or open_q:
        bits = []
        if unknown_side:
            bits.append(f"без стороны: {len(unknown_side)}")
        if open_q:
            bits.append(f"вопросов «Кто это?»: {open_q}")
        items.append(_item("people", "Все действующие лица опознаны и подтверждены", "warn", ", ".join(bits)))
    else:
        items.append(_item("people", "Все действующие лица опознаны и подтверждены", "ok", f"людей: {len(listed)}, у всех известна сторона"))

    on = memory_platform.enabled(settings, db.get(Project, project_id))
    items.append(_item("platform", "Стороны в контракте берутся из реестра (флаг проекта включён)", "ok" if on else "fail", "включено" if on else "выключено: стороны по догадке из ролей"))

    if m8["value"] is None:
        items.append(_item("m8", "Верная сторона источника там, где человек известен (M8 = 100 %)", "warn", "нет листа людей или эталона: выгрузите лист (scripts/memory_scorecard.py --export-people)"))
    elif m8["wrong"]:
        items.append(_item("m8", "Верная сторона источника там, где человек известен (M8 = 100 %)", "fail", f"не совпало {len(m8['wrong'])} из {m8['checked']}"))
    else:
        items.append(_item("m8", "Верная сторона источника там, где человек известен (M8 = 100 %)", "ok", f"проверено событий: {m8['checked']}, расхождений нет"))

    linked_people = {p.user_id for p in listed if p.user_id}
    waiting = [s for s in voice_link.suggestions(db) if s.user_id in linked_people]
    items.append(_item("voices", "Голоса сотрудников с согласием привязаны к карточкам", "ok" if not waiting else "warn",
                       "все привязаны" if not waiting else f"ждут привязки: {len(waiting)} (кнопка «Привязать к учётке» в карточке «Люди и стороны»)"))

    calls = [a for a in db.scalars(select(Artifact).where(Artifact.project_id == project_id, Artifact.file_type == "audio")).all()
             if ra.recorded_precision(a) == ra.PRECISION_DAY]
    by_day: dict[Any, int] = {}
    for art in calls:
        by_day[ra.material_date(art)[0]] = by_day.get(ra.material_date(art)[0], 0) + 1
    clashes = sum(1 for n in by_day.values() if n > 1)
    items.append(_item("times", "У созвонов известно время начала; созвоны одного дня упорядочены", "ok" if not calls else "warn",
                       "время известно у всех" if not calls else f"только день: {len(calls)} созвон(ов), дней с несколькими: {clashes} (scripts/material_dates.py --day-only)"))

    file_names = {name_key(c.name) for c in load_companies_file(settings.companies_path)}
    db_names = {c.name_key for c in db.scalars(select(Company).where(Company.active.is_(True))).all()}
    same = file_names == db_names
    items.append(_item("companies", "Список компаний в Companies.txt совпадает с базой", "ok" if same else "warn",
                       f"компаний: {len(db_names)}" if same else f"различаются: {len(file_names ^ db_names)}"))

    items.append(_item("wizard", "Новый проект создаётся мастером со сторонами и людьми", "manual", "проверьте вручную: «Новый проект» → блок «Стороны и люди заказчика»"))
    items.append(_item("tests", "Тесты платформы и существующие тесты проходят; миграция проверена на копии боевой БД", "manual",
                       "запуск вручную: venv\\Scripts\\python -m pytest app/app/tests"))
    items.append(_item("scan", "В git нет имён и цитат проектов", "manual", "venv\\Scripts\\python scripts\\scan_personal_data.py (перед каждым коммитом и отправкой)"))
    items.append(_item("docs", "PROJECT-MAP, DECISIONS, TESTING обновлены", "manual", "карту проверяет хук коммита; DECISIONS и TESTING — вручную"))
    return items


# ---------------------------------------------------------------- прогон и история

def compute(db: Session, settings: Settings, project_id: int) -> dict[str, Any]:
    """Все метрики и чек-лист по проекту (без записи в историю)."""
    from app.app.services import requirement_review as rr
    from app.app.services.entities import PROMPT_VERSION

    counts = rr.review_list(db, settings, project_id, limit=1)["counts"]
    gold = gold_events(settings)
    m2, m2_details = _m2(db, settings, project_id)
    m2j, m2j_details = _m2_subjects(db, settings, project_id)
    m3, m3_found, m3_total = _m3(db, settings, project_id, gold)
    m7, m7_missing = _m7(db, project_id, gold)
    m8 = _m8(db, settings, project_id, gold)
    m9, m9_bound, m9_total = _m9(db, settings, project_id)
    acts = _acts_metrics(db, project_id, gold)
    return {
        "project_id": project_id, "prompt": PROMPT_VERSION, "model": settings.lmstudio_model,
        "has_gold": bool(gold), "has_people_sheet": bool(people_sheet(settings)),
        "metrics": {"M1": _m1(counts), "M2": m2, "M3": m3, "M7": m7, "M8": m8["value"], "M9": m9, "A1": acts["a1"], "A2": acts["a2"]},
        "journal_m2": {"value": m2j, "details": m2j_details},
        "details": {"unsourced": counts["unsourced"], "total": counts["total"], "m2": m2_details, "m3": [m3_found, m3_total],
                    "m7_missing": m7_missing, "m8_checked": m8["checked"], "m8_wrong": m8["wrong"], "m9": [m9_bound, m9_total], "acts": acts},
        "pending": list(PENDING_METRICS),
        "checklist": checklist(db, settings, project_id, m8=m8),
    }


def _fmt(value: float | None) -> str:
    return "" if value is None else f"{value:.2f}"


def _ensure_board(path: Path) -> None:
    """Старый файл табло (до M7/M8) переписывается с новыми столбцами; прежние строки сохраняются."""
    if not path.exists():
        return
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f, delimiter=";"))
    if not rows or rows[0] == HEADER:
        return
    old = rows[0]
    mapping = {"M1_цитата": "M1", "M2_итог": "M2", "M3_грубо": "M3", "M2_подробно": "детали"}
    out = [HEADER]
    for row in rows[1:]:
        data = {mapping.get(h, h): v for h, v in zip(old, row)}
        out.append([data.get(h, "") for h in HEADER])
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f, delimiter=";").writerows(out)


def history(settings: Settings, project_id: int | None = None) -> list[dict[str, str]]:
    path = _paths(settings)["board"]
    _ensure_board(path)
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    if project_id is not None:
        rows = [r for r in rows if r.get("проект") == str(project_id)]
    return rows


def record(settings: Settings, result: dict[str, Any], stage: str) -> dict[str, str]:
    """Добавить строку в историю прогонов."""
    path = _paths(settings)["board"]
    path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_board(path)
    m = result["metrics"]
    wrong = result["details"]["m8_wrong"]
    detail = " | ".join(f"{d['subject']} {'OK' if d['ok'] else 'НЕТ'}" for d in result["details"]["m2"])
    if wrong:
        detail += f" | M8 расхождений: {len(wrong)}"
    row = [datetime.now().strftime("%Y-%m-%d %H:%M"), stage, result["project_id"], result["prompt"], result["model"],
           _fmt(m["M1"]), _fmt(m["M2"]), _fmt(m["M3"]), _fmt(m["M7"]), _fmt(m["M8"]), _fmt(m.get("M9")), _fmt(m.get("A1")), _fmt(m.get("A2")), detail]
    new = not path.exists()
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        if new:
            w.writerow(HEADER)
        w.writerow(row)
    return dict(zip(HEADER, [str(x) for x in row]))


def changed_since_last_run(settings: Settings, project_id: int) -> bool:
    """Сменились промпт или модель с последнего прогона по проекту (или прогонов ещё нет)."""
    from app.app.services.entities import PROMPT_VERSION

    rows = history(settings, project_id)
    if not rows:
        return True
    last = rows[-1]
    return (last.get("промпт"), last.get("модель")) != (PROMPT_VERSION, settings.lmstudio_model)


def run(db: Session, settings: Settings, project_id: int, *, stage: str) -> dict[str, Any]:
    result = compute(db, settings, project_id)
    result["row"] = record(settings, result, stage)
    return result
