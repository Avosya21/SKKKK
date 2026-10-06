"""Режим памяти решений, готовность и сравнение с прежним контуром (docs/plans/DECISION-MEMORY-STAGES-6-8.md, Э6–Э7). Имена вымышленные."""
from __future__ import annotations

import json

import pytest

from app.app.services import memory_compare as mcmp
from app.app.services import memory_mode as mm


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def world(client, admin_token, manager_token, user_token, monkeypatch):
    from app.app.config import get_settings

    monkeypatch.setattr(get_settings(), "decision_memory_mode", "off")  # тесты не зависят от локального .env
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.decision_memory import Subject, SubjectPosition
    from app.app.models.entity import Entity
    from app.app.models.speech_act import SpeechAct
    from app.app.models.user import User

    pid = client.post("/projects", json={"name": "Режим памяти"}, headers=_auth(manager_token)).json()["id"]
    db = SessionLocal()
    user = db.query(User).filter(User.telegram_id == "tg-admin").one()
    art = Artifact(artifact_id="art_mode", project_id=pid, user_id=user.id, original_filename="чат.html", stored_filename="c", file_path="/x",
                   file_type="html", source_type="document", sha256="mode", status="done")
    db.add(art)
    db.commit()
    price = Subject(project_id=pid, title="Цена работ", state="agreed", state_source="judge", summary="Согласились на 126 000.")
    marks = Subject(project_id=pid, title="Пометка старой номенклатуры", state="proposed", state_source="fold")
    db.add_all([price, marks])
    db.commit()
    spec = [(1, price, "proposed", "Цена 144 000"), (2, price, "proposed", "Цена будет 126 000"), (3, price, "agreed", "Стартуем"),
            (4, marks, "proposed", "Поставим знак")]
    acts = {}
    for key, subject, kind, quote in spec:
        row = SpeechAct(project_id=pid, artifact_id=art.id, unit_ref=f"M{key}", unit_key=key, author="А", side="customer", act=kind, quote=quote,
                        said_at=f"2026-09-11T12:0{key}:00", subject_id=subject.id, prompt_version="v", model="m")
        db.add(row)
        db.flush()
        acts[key] = row
    db.add_all([SubjectPosition(subject_id=price.id, label="цена 144 000", state="superseded", basis_json=json.dumps([acts[1].id]), replaced_by="цена 126 000"),
                SubjectPosition(subject_id=price.id, label="цена 126 000", state="current", basis_json=json.dumps([acts[2].id]))])
    # пункты прежнего контура: № 1 — «144 000» ещё действует (спор), № 2 — «126 000» действует (согласие), № 4 — отклонён, а память: предложено (спор)
    def entity(message_id, text, active=True, relevance=None):
        value = {"text": text, "message_id": message_id}
        if relevance:
            value["relevance"] = relevance
        db.add(Entity(artifact_id=art.id, entity_type="decision", value_json=json.dumps(value, ensure_ascii=False), is_active=active))

    entity(1, "Цена работ 144 000")
    entity(2, "Цена работ 126 000")
    entity(4, "Пометка знаком", active=False)
    db.add(Entity(artifact_id=art.id, entity_type="decision", value_json=json.dumps({"text": "Из документа", "source_paragraph": 3}), is_active=True))
    db.commit()
    out = {"pid": pid, "price": price.id, "marks": marks.id}
    db.close()
    return out


def test_mode_resolution_and_set(world):
    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.project import Project

    db = SessionLocal()
    project = db.get(Project, world["pid"])
    settings = get_settings()
    assert mm.mode_of(settings, project) == "off" and not mm.uses_in_chat(settings, project)
    mm.set_mode(db, project, "shadow")
    assert mm.mode_of(settings, project) == "shadow" and not mm.uses_in_chat(settings, project)
    mm.set_mode(db, project, "live")
    assert mm.uses_in_chat(settings, project)
    with pytest.raises(ValueError):
        mm.set_mode(db, project, "бред")
    db.close()


def test_compare_marks_agree_and_asks_arbiter_only_for_disputes(world):
    from app.app.db import SessionLocal
    from app.app.config import get_settings
    from app.app.models.decision_memory import MemoryShadowReview

    db = SessionLocal()
    asked: list[str] = []

    def ask(system, user):
        if "Что известно по памяти решений" in user:  # перевод состояния предмета на пункт: тут «неясно», остаётся механическое состояние
            return json.dumps({"truth": "unclear", "reason": ""})
        asked.append(user)
        return json.dumps({"truth": "inactive" if ("144 000" in user.split("Лента")[0] or "Пометка" in user.split("Лента")[0]) else "active", "reason": "по репликам"}, ensure_ascii=False)

    stats = mcmp.compare_project(db, get_settings(), world["pid"], ask=ask)
    # № 1 «144 000» прежний контур считает действующим, память — заменённым (спор, арбитр: не действует -> права память);
    # № 2 согласие; № 4 прежний — отклонён, память — «предложено» (спор, арбитр: не действует -> прав прежний контур)
    assert stats["entities"] == 4 and stats["not_covered"] == 1 and stats["compared"] == 3 and stats["agree"] == 1 and stats["disputed"] == 2
    assert stats["new_right"] == 1 and stats["old_right"] == 1 and len(asked) == 2
    rows = {r.entity_id: r for r in db.query(MemoryShadowReview).filter(MemoryShadowReview.project_id == world["pid"]).all()}
    assert sorted(r.verdict for r in rows.values()) == ["agree", "new", "old"]
    assert all("Метод A:" in u and "Метод B:" in u and "Лента реплик" in u for u in asked)
    again = mcmp.compare_project(db, get_settings(), world["pid"], ask=ask)  # повтор заменяет строки, а не копит
    assert db.query(MemoryShadowReview).filter(MemoryShadowReview.project_id == world["pid"]).count() == 3 and again["disputed"] == 2
    db.close()


def test_arbiter_failure_is_unclear_not_crash(world):
    from app.app.db import SessionLocal
    from app.app.config import get_settings

    db = SessionLocal()
    stats = mcmp.compare_project(db, get_settings(), world["pid"], ask=lambda s, u: "не json")
    assert stats["errors"] == 2 and stats["unclear"] == 2
    db.close()


def test_readiness_needs_judged_quotes_and_agreement(world):
    from app.app.db import SessionLocal
    from app.app.config import get_settings
    from app.app.models.decision_memory import MemoryShadowReview

    db = SessionLocal()
    before = mm.readiness(db, world["pid"])
    keys = {c["key"]: c["ok"] for c in before["checks"]}
    assert keys["built"] and keys["quotes"] and not keys["compared"] and not before["ready"]
    for i in range(12):  # 10 совпадений + 2 спора, оба в пользу памяти
        db.add(MemoryShadowReview(project_id=world["pid"], entity_id=100 + i, old_state="current", new_state="current" if i < 10 else "superseded",
                                  verdict="agree" if i < 10 else "new"))
    db.commit()
    after = mm.readiness(db, world["pid"])
    keys = {c["key"]: c["ok"] for c in after["checks"]}
    assert keys["compared"] and keys["agreement"]
    # «judged»: у предмета с решениями итог вынесен судьёй; предмет «Пометка…» (proposed, fold) решающий акт имеет — доля 1/2 < 80 %
    assert not keys["judged"] and not after["ready"]
    from app.app.models.decision_memory import Subject

    db.get(Subject, world["marks"]).state_source = "judge"
    db.commit()
    assert mm.readiness(db, world["pid"])["ready"]
    db.close()


def test_auto_promotion_only_from_shadow_when_ready(world, monkeypatch):
    from app.app.db import SessionLocal
    from app.app.config import get_settings
    from app.app.models.project import Project

    db = SessionLocal()
    project = db.get(Project, world["pid"])
    settings = get_settings()
    monkeypatch.setattr(mm, "readiness", lambda _db, _pid: {"ready": True, "checks": []})
    assert mm.maybe_promote(db, settings, project) is None  # off не продвигается
    mm.set_mode(db, project, "shadow")
    monkeypatch.setattr(settings, "decision_memory_auto_live", False)
    assert mm.maybe_promote(db, settings, project) is None  # автопереход выключен
    monkeypatch.setattr(settings, "decision_memory_auto_live", True)
    assert mm.maybe_promote(db, settings, project) == "live" and mm.mode_of(settings, project) == "live"
    db.close()


def test_api_summary_mode_and_manager_visibility(client, admin_token, manager_token, user_token, world):
    pid = world["pid"]
    h = _auth(admin_token)
    summary = client.get(f"/projects/{pid}/memory/summary", headers=h).json()
    assert summary["mode"] == "off" and summary["is_admin"] is True and summary["acts_total"] == 4 and summary["counts"] == {"agreed": 1, "proposed": 1}
    assert {c["key"] for c in summary["readiness"]["checks"]} == {"built", "quotes", "judged", "compared", "agreement"}
    # менеджер проекта до включения в live память не видит; пользователь — никогда
    assert client.get(f"/projects/{pid}/memory/summary", headers=_auth(manager_token)).status_code == 404
    assert client.get(f"/projects/{pid}/memory/subjects", headers=_auth(manager_token)).status_code == 404
    assert client.get(f"/projects/{pid}/memory/summary", headers=_auth(user_token)).status_code in (403, 404)
    assert client.put(f"/projects/{pid}/memory/mode", json={"mode": "бред"}, headers=h).status_code == 400
    assert client.put(f"/projects/{pid}/memory/mode", json={"mode": "live"}, headers=h).json() == {"mode": "live"}
    seen = client.get(f"/projects/{pid}/memory/subjects", headers=_auth(manager_token))
    assert seen.status_code == 200 and seen.json()["total"] == 2
    assert client.put(f"/projects/{pid}/memory/mode", json={"mode": "off"}, headers=_auth(manager_token)).status_code == 403  # режим меняет админ
    listed = {i["title"]: i for i in seen.json()["items"]}
    assert listed["Цена работ"]["positions"] == 2 and listed["Цена работ"]["human"] is False


def test_project_page_has_card_for_staff_only(client, admin_token, user_token, world):
    page = client.get(f"/projects/{world['pid']}", headers=_auth(admin_token))
    assert page.status_code == 200 and 'id="mem-card"' in page.text and "/static/memory.js" in page.text
    other = client.get(f"/projects/{world['pid']}", headers=_auth(user_token))
    assert 'id="mem-card"' not in other.text


def test_memory_view_translates_outcome_phrased_item(world):
    """Пункт «Пометка знаком» сформулирован как итог: память о предмете «предложено», но для пункта перевод говорит «не действует» — спора нет."""
    from app.app.config import get_settings
    from app.app.db import SessionLocal

    db = SessionLocal()
    arbiter_calls: list[str] = []

    def ask(system, user):
        if "Что известно по памяти решений" in user:
            inactive = "Пометка" in user  # пункт 4 (прежний контур: отклонён)
            return json.dumps({"truth": "inactive" if inactive else "active", "reason": "по смыслу пункта"}, ensure_ascii=False)
        arbiter_calls.append(user)
        return json.dumps({"truth": "active", "reason": "x"})

    stats = mcmp.compare_project(db, get_settings(), world["pid"], ask=ask)
    assert stats["agree_after_view"] >= 1 and stats["disputed"] == len(arbiter_calls)
    assert all("Пометка знаком" not in u for u in arbiter_calls)  # спор по пункту 4 снят переводом
    db.close()
