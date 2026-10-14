"""Точность разбора по решениям людей и выборочная проверка (PORTAL-ACCURACY-MANAGER-PLAN, Ш1)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.app.config import get_settings
from app.app.services import contract as ctr
from app.app.services import contract_quality as cq
from app.app.tests.test_contract import _seed


@pytest.fixture(autouse=True)
def _clear():
    ctr._cache.clear()
    yield
    ctr._cache.clear()


class _Ent:
    def __init__(self, kind: str, value: dict):
        self.entity_type = kind
        self.value_json = json.dumps(value, ensure_ascii=False)


def _quality(monkeypatch, ents):
    monkeypatch.setattr(cq, "_project_entities", lambda db, pid: ents)
    monkeypatch.setattr(cq, "_conflicts", lambda db, pid: cq._metric(0, 0))
    return cq.project_quality(None, 1)


def test_labels_from_human_and_spot(monkeypatch):
    ents = [
        _Ent("decision", {"human_review": {"action": "confirm"}}),
        _Ent("decision", {"human_review": {"action": "reject"}}),
        _Ent("decision", {"human_review": {"action": "status"}, "human_status": "agreed"}),
        _Ent("task", {"spot_check": {"verdict": "wrong"}}),
        _Ent("task", {"spot_check": {"verdict": "ok"}, "human_review": {"action": "reject"}}),  # человек главнее выборки
        _Ent("open_question", {}),
    ]
    q = _quality(monkeypatch, ents)
    assert q["extraction"]["decision"]["n"] == 3 and q["extraction"]["decision"]["good"] == 2
    assert q["extraction"]["task"]["n"] == 2 and q["extraction"]["task"]["good"] == 0
    assert q["extraction"]["open_question"]["n"] == 0
    assert q["status_changed_by_human"] == 1 and q["spot_checked"] == 2


def test_model_agreement_and_undo(monkeypatch):
    ents = [
        _Ent("decision", {"model_review": {"verdict": "confirm"}, "spot_check": {"verdict": "ok"}}),     # согласие
        _Ent("decision", {"model_review": {"verdict": "confirm"}, "spot_check": {"verdict": "wrong"}}),  # ошибка модели
        _Ent("decision", {"model_review": {"verdict": "reject"}, "spot_check": {"verdict": "wrong"}}),   # согласие
        _Ent("task", {"model_review": {"verdict": "reject", "undone": {"by": "x"}}}),                    # «Вернуть» — ошибка модели
        _Ent("task", {"model_review": {"verdict": "unsure"}, "spot_check": {"verdict": "ok"}}),          # «не уверена» не считается
        _Ent("task", {"model_review": {"verdict": "confirm"}}),                                           # человек не смотрел
    ]
    q = _quality(monkeypatch, ents)
    assert q["model_agreement"]["decision"] == {"n": 3, "good": 2, "share": None, "enough": False}
    assert q["model_agreement"]["task"]["n"] == 1 and q["model_agreement"]["task"]["good"] == 0
    assert q["model_agreement"]["all"]["n"] == 4


def test_small_sample_has_no_share_and_honest_line(monkeypatch):
    few = [_Ent("decision", {"model_review": {"verdict": "confirm"}, "spot_check": {"verdict": "ok"}}) for _ in range(cq.MIN_SAMPLE - 1)]
    q = _quality(monkeypatch, few)
    assert q["model_agreement"]["all"]["share"] is None
    assert "Мало данных" in cq.summary_line(q)
    enough = few + [_Ent("decision", {"model_review": {"verdict": "reject"}, "spot_check": {"verdict": "ok"}})]
    q = _quality(monkeypatch, enough)
    assert q["model_agreement"]["all"]["share"] == round(19 / 20, 3)
    assert cq.summary_line(q).startswith("Модель права в 19 из 20")


def test_relevance_hidden_and_returned(monkeypatch):
    ents = [
        _Ent("decision", {"relevance": {"state": "superseded"}}),
        _Ent("decision", {"relevance": {"state": "duplicate", "undone": {"by": "x"}}}),
        _Ent("decision", {"relevance": {"state": "current"}}),
    ]
    assert _quality(monkeypatch, ents)["relevance"] == {"hidden": 2, "returned_by_human": 1}


def test_spot_sample_is_stable_within_week_and_shrinks():
    items = [{"source_entity_id": i, "id": f"r{i}"} for i in range(1, 31)]
    now = datetime(2026, 10, 14, tzinfo=timezone.utc)
    week, done = cq.spot_sample(items, {}, 5, now=now)
    assert len(week) == cq.SPOT_WEEKLY and done == 0
    again, _ = cq.spot_sample(list(reversed(items)), {}, 5, now=now)
    assert [r["id"] for r in again] == [r["id"] for r in week]  # порядок входа не важен
    first = week[0]["source_entity_id"]
    spots = {first: {"verdict": "ok", "at": now.isoformat()}}
    after, done = cq.spot_sample(items, spots, 5, now=now)
    assert done == 1 and len(after) == cq.SPOT_WEEKLY - 1 and first not in {r["source_entity_id"] for r in after}
    old = {first: {"verdict": "ok", "at": "2026-09-01T10:00:00+00:00"}}  # отмечен на прошлой неделе — из пула выходит
    later, done = cq.spot_sample(items, old, 5, now=now)
    assert done == 0 and first not in {r["source_entity_id"] for r in later}


def test_spot_check_route_roles_and_no_side_effects(client, manager_token, admin_token, user_token, tmp_data):
    from app.app.models.entity import Entity

    db, pid, _art, _ = _seed(client, manager_token, tmp_data)
    reqs = ctr.get_contract(db, get_settings(), pid).files["requirements"]["items"]
    hm, hu = {"Authorization": f"Bearer {manager_token}"}, {"Authorization": f"Bearer {user_token}"}
    spot = client.get(f"/projects/{pid}/requirements/review?verification=spot", headers=hm).json()
    assert spot["spot"]["size"] == cq.SPOT_WEEKLY and spot["items"] and all(r["spot"] for r in spot["items"])
    assert "quality" in spot and spot["quality"]["line"]
    eid = spot["items"][0]["source_entity_id"]
    before = db.get(Entity, eid)
    needs_review, active = before.needs_review, before.is_active
    assert client.post(f"/projects/{pid}/spot-check/{eid}", json={"verdict": "ok"}, headers=hu).status_code == 403
    assert client.post(f"/projects/{pid}/spot-check/{eid}", json={"verdict": "maybe"}, headers=hm).status_code == 400
    assert client.post(f"/projects/{pid}/spot-check/999999", json={"verdict": "ok"}, headers=hm).status_code == 400
    r = client.post(f"/projects/{pid}/spot-check/{eid}", json={"verdict": "wrong", "reason": "не говорили"}, headers=hm)
    assert r.status_code == 200 and r.json()["spot_check"]["verdict"] == "wrong"
    db.expire_all()
    ent = db.get(Entity, eid)
    value = json.loads(ent.value_json)
    assert value["spot_check"]["reason"] == "не говорили" and "human_review" not in value
    assert ent.needs_review == needs_review and ent.is_active == active
    after = client.get(f"/projects/{pid}/requirements/review?verification=spot", headers=hm).json()
    assert eid not in {r["source_entity_id"] for r in after["items"]} and after["spot"]["done"] == 1
    assert any(r["id"] == next(x["id"] for x in reqs if x["source_entity_id"] == eid)
               for r in client.get(f"/projects/{pid}/requirements/review?verification=extracted", headers=hm).json()["items"])
