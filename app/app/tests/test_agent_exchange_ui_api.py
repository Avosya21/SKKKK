"""API карточек «Обмен с агентами»: вердикты (подтвердить/отклонить), сводка, фильтры журнала, права."""
from __future__ import annotations

import json

import pytest

from app.app.config import get_settings
from app.app.mcp.project_protocol import call_tool
from app.app.services import agent_gateway as gw
from app.app.services import contract as ctr
from app.app.services.project_mcp_tokens import create_project_mcp_token
from app.app.tests.test_contract import _seed


@pytest.fixture(autouse=True)
def _reset():
    gw._LIMITER.reset()
    ctr._cache.clear()
    yield
    gw._LIMITER.reset()
    ctr._cache.clear()


def _submit_verdict(db, pid, art, rid, request_id):
    tok, _ = create_project_mcp_token(db, project_id=pid, label="t", user_id=art.user_id, can_submit=True)
    res = call_tool(db, get_settings(), pid, "submit_verdicts", {"request_id": request_id, "items": [{"requirement_id": rid, "verdict": "implemented", "confidence": "high"}]}, token=tok)
    return json.loads(res["content"][0]["text"])


def test_verdict_confirm_reject_and_roles(client, manager_token, admin_token, tmp_data):
    db, pid, art, _ = _seed(client, manager_token, tmp_data)
    hm, ha = {"Authorization": f"Bearer {manager_token}"}, {"Authorization": f"Bearer {admin_token}"}
    reqs = ctr.get_contract(db, get_settings(), pid).files["requirements"]["items"]
    assert _submit_verdict(db, pid, art, reqs[0]["id"], 1)["status"] == "accepted"
    lst = client.get(f"/projects/{pid}/agent-verdicts?pending_only=true", headers=hm).json()
    assert lst["waiting"] == 1 and lst["can_edit"] is False
    vid = lst["items"][0]["id"]
    assert client.post(f"/projects/{pid}/agent-verdicts/{vid}/confirm", json={}, headers=hm).status_code == 403
    assert client.post(f"/projects/{pid}/agent-verdicts/{vid}/confirm", json={"reason": "проверил"}, headers=ha).status_code == 200
    db.expire_all()
    ctr._cache.clear()
    req = next(r for r in ctr.get_contract(db, get_settings(), pid).files["requirements"]["items"] if r["id"] == reqs[0]["id"])
    assert req["analysis"]["human_confirmed"] is True and req["analysis"]["verified_by"] == "machine_code"
    assert client.get(f"/projects/{pid}/agent-verdicts?pending_only=true", headers=ha).json()["waiting"] == 0
    # отклонение мягкое: запись остаётся
    assert _submit_verdict(db, pid, art, reqs[1]["id"], 2)["status"] == "accepted"
    vid2 = client.get(f"/projects/{pid}/agent-verdicts?pending_only=true", headers=ha).json()["items"][0]["id"]
    assert client.post(f"/projects/{pid}/agent-verdicts/{vid2}/reject", json={}, headers=ha).status_code == 200
    allv = client.get(f"/projects/{pid}/agent-verdicts", headers=ha).json()
    assert allv["total"] == 2 and [v["is_active"] for v in allv["items"]].count(False) == 1
    assert client.post(f"/projects/{pid}/agent-verdicts/999999/confirm", json={}, headers=ha).status_code == 400
    assert client.post(f"/projects/{pid}/agent-verdicts/{vid2}/explode", json={}, headers=ha).status_code == 400


def test_summary_and_journal_filters(client, manager_token, admin_token, tmp_data):
    db, pid, art, _ = _seed(client, manager_token, tmp_data)
    hm = {"Authorization": f"Bearer {manager_token}"}
    tok, _ = create_project_mcp_token(db, project_id=pid, label="t", user_id=art.user_id, can_submit=True)
    call_tool(db, get_settings(), pid, "submit_question", {"question": "Вопрос?"}, token=tok)
    call_tool(db, get_settings(), pid, "submit_question", {"question": "rm -rf C:\\data"}, token=tok)
    s = client.get(f"/projects/{pid}/agent-exchange/summary", headers=hm).json()
    assert s["questions_waiting"] == 1 and s["rejected_24h"] == 1 and s["requirements_unconfirmed"] >= 1
    rej = client.get(f"/projects/{pid}/agent-exchange?decision=rejected", headers=hm).json()["items"]
    assert len(rej) == 1 and rej[0]["source"].startswith("mcp")
    assert len(client.get(f"/projects/{pid}/agent-exchange?source=mcp&kind=ask_question", headers=hm).json()["items"]) == 2
    assert client.get(f"/projects/{pid}/agent-exchange?source=user", headers=hm).json()["items"] == []


def test_inbox_actions_admin_only(client, manager_token, admin_token, tmp_data):
    db, pid, art, _ = _seed(client, manager_token, tmp_data)
    hm, ha = {"Authorization": f"Bearer {manager_token}"}, {"Authorization": f"Bearer {admin_token}"}
    tok, _ = create_project_mcp_token(db, project_id=pid, label="t", user_id=art.user_id, can_submit=True)
    call_tool(db, get_settings(), pid, "submit_question", {"question": "Что решили?"}, token=tok)
    q = client.get(f"/projects/{pid}/agent-inbox", headers=hm).json()["items"][0]
    assert client.post(f"/projects/{pid}/agent-inbox/{q['id']}/answer", json={"text": "Ответ"}, headers=hm).status_code == 403
    assert client.post(f"/projects/{pid}/agent-inbox/{q['id']}/answer", json={"text": "Ответ"}, headers=ha).status_code == 200
    assert client.get(f"/projects/{pid}/agent-inbox", headers=hm).json()["items"][0]["status"] == "answered"
