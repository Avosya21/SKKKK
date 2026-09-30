"""Подтверждение требований человеком: только admin, мягкое отклонение, статус с причиной, отражение в контракте."""
from __future__ import annotations

import pytest

from app.app.config import get_settings
from app.app.services import contract as ctr
from app.app.tests.test_contract import _seed


@pytest.fixture(autouse=True)
def _clear():
    ctr._cache.clear()
    yield
    ctr._cache.clear()


def _setup(client, manager_token, tmp_data):
    db, pid, art, _ = _seed(client, manager_token, tmp_data)
    reqs = ctr.get_contract(db, get_settings(), pid).files["requirements"]["items"]
    return db, pid, reqs


def test_list_visible_to_manager_actions_only_admin(client, manager_token, admin_token, tmp_data):
    db, pid, reqs = _setup(client, manager_token, tmp_data)
    hm, ha = {"Authorization": f"Bearer {manager_token}"}, {"Authorization": f"Bearer {admin_token}"}
    lst = client.get(f"/projects/{pid}/requirements/review", headers=hm)
    assert lst.status_code == 200 and lst.json()["can_edit"] is False and lst.json()["total"] == len(reqs)
    rid = reqs[0]["id"]
    assert client.post(f"/projects/{pid}/requirements/{rid}/confirm", json={}, headers=hm).status_code == 403
    assert client.post(f"/projects/{pid}/requirements/confirm", json={"ids": [rid]}, headers=hm).status_code == 403
    assert client.get(f"/projects/{pid}/requirements/review", headers=ha).json()["can_edit"] is True


def test_confirm_shows_human_confirmed_in_contract(client, manager_token, admin_token, tmp_data):
    db, pid, reqs = _setup(client, manager_token, tmp_data)
    ha = {"Authorization": f"Bearer {admin_token}"}
    rid = reqs[0]["id"]
    assert reqs[0]["verification"] == "extracted"
    assert client.post(f"/projects/{pid}/requirements/{rid}/confirm", json={}, headers=ha).status_code == 200
    db.expire_all()
    after = next(r for r in ctr.get_contract(db, get_settings(), pid).files["requirements"]["items"] if r["id"] == rid)
    assert after["verification"] == "human_confirmed" and after["human_review"]["action"] == "confirm" and after["human_review"]["by"]
    only_un = client.get(f"/projects/{pid}/requirements/review?verification=extracted", headers=ha).json()
    assert rid not in {r["id"] for r in only_un["items"]}


def test_status_needs_reason_and_valid_value(client, manager_token, admin_token, tmp_data):
    db, pid, reqs = _setup(client, manager_token, tmp_data)
    ha = {"Authorization": f"Bearer {admin_token}"}
    rid = reqs[0]["id"]
    assert client.post(f"/projects/{pid}/requirements/{rid}/status", json={"status": "agreed"}, headers=ha).status_code == 400  # без причины
    assert client.post(f"/projects/{pid}/requirements/{rid}/status", json={"status": "bogus", "reason": "x"}, headers=ha).status_code == 400
    assert client.post(f"/projects/{pid}/requirements/{rid}/status", json={"status": "agreed", "reason": "Подтверждено по звонку 15.09"}, headers=ha).status_code == 200
    db.expire_all()
    got = next(r for r in ctr.get_contract(db, get_settings(), pid).files["requirements"]["items"] if r["id"] == rid)
    assert got["status"] == "agreed" and got["status_basis"] == "human_set" and got["human_review"]["reason"].startswith("Подтверждено")


def test_reject_is_soft_and_batch_confirm(client, manager_token, admin_token, tmp_data):
    from app.app.models.entity import Entity

    db, pid, reqs = _setup(client, manager_token, tmp_data)
    ha = {"Authorization": f"Bearer {admin_token}"}
    assert len(reqs) >= 2
    a, b = reqs[0], reqs[1]
    assert client.post(f"/projects/{pid}/requirements/{a['id']}/reject", json={"reason": "дубль"}, headers=ha).status_code == 200
    db.expire_all()
    ent = db.get(Entity, a["source_entity_id"])
    assert ent is not None and ent.is_active is False  # сущность не удалена
    ids = {r["id"] for r in ctr.get_contract(db, get_settings(), pid).files["requirements"]["items"]}
    assert a["id"] not in ids
    r = client.post(f"/projects/{pid}/requirements/confirm", json={"ids": [b["id"], "req-x-9999"]}, headers=ha).json()
    assert r["confirmed"] == [b["id"]] and r["failed"][0]["id"] == "req-x-9999"
    assert client.post(f"/projects/{pid}/requirements/confirm", json={"ids": []}, headers=ha).status_code == 400
    assert client.post(f"/projects/{pid}/requirements/{b['id']}/explode", json={}, headers=ha).status_code == 400  # неизвестное действие


def test_unsourced_item_leaves_needs_human_queue():
    """DECISION-MEMORY-PLAN А1: пункт без цитаты источника — на своей вкладке, а не в «Ждут вас»."""
    from app.app.services.requirement_review import is_unsourced, waits_for_human

    bare = {"verification": "extracted", "evidence": [{"quote": ""}], "model_review": None}
    quoted = {"verification": "extracted", "evidence": [{"quote": "Сделал"}], "model_review": None}
    confirmed = {"verification": "human_confirmed", "evidence": [], "model_review": None}
    assert is_unsourced(bare) and not waits_for_human(bare)
    assert not is_unsourced(quoted) and waits_for_human(quoted)
    assert not is_unsourced(confirmed)


def test_verify_document_quote_requires_verbatim_text():
    """DECISION-MEMORY-PLAN этап 2: цитата документа принимается, только если она есть в тексте материала."""
    from app.app.services.entities import verify_document_quote

    text = "Клиент: включите режим отладки на сервере 1С.\n\nОтвет:   отладка включена, ёлка не нужна."
    assert verify_document_quote("Включите режим  отладки на сервере 1С", text) == "Включите режим  отладки на сервере 1С"
    assert verify_document_quote("Отладка включена, елка не нужна.", text)  # регистр, пробелы и ё не важны
    assert verify_document_quote("включить отладку для возможности разработки и тестирования", text) is None
    assert verify_document_quote("короткая", "короткая цитата") is None  # короче 12 знаков не принимаем
    assert verify_document_quote("", text) is None
