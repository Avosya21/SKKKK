"""Запись в очередь обмена через MCP (submit_question / submit_verdicts): право на ключе, идемпотентность, только добавление, те же проверки шлюза."""
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


def _call(db, pid, token, name, **args):
    res = call_tool(db, get_settings(), pid, name, args, token=token)
    return res["isError"], json.loads(res["content"][0]["text"])


def _token(db, pid, art, can_submit):
    row, _plain = create_project_mcp_token(db, project_id=pid, label="t", user_id=art.user_id, can_submit=can_submit)
    return row


def test_read_only_key_cannot_submit(client, manager_token, tmp_data):
    db, pid, art, _ = _seed(client, manager_token, tmp_data)
    err, body = _call(db, pid, _token(db, pid, art, False), "submit_question", request_id=1, question="Привет?")
    assert err and body["detail"] == "read_only_key"
    err, body = _call(db, pid, None, "submit_verdicts", request_id=2, items=[{}])
    assert err and body["detail"] == "read_only_key"


def test_question_is_queued_and_idempotent(client, manager_token, tmp_data):
    from app.app.models.agent_question import AgentQuestion

    db, pid, art, _ = _seed(client, manager_token, tmp_data)
    tok = _token(db, pid, art, True)
    err, first = _call(db, pid, tok, "submit_question", request_id=11, question="Что решили по профнастилу?", client_request_id="abc")
    assert not err and first["status"] == "queued" and first["reply_artifact"] == "contract/kb_reply_11.json"
    err, again = _call(db, pid, tok, "submit_question", request_id=11, question="Что решили по профнастилу?", client_request_id="abc")
    assert not err and again["duplicate"] is True and again["status"] == "queued"
    assert db.query(AgentQuestion).filter_by(project_id=pid).count() == 1  # повтор не создал второй вопрос
    assert gw.get_reply(db, pid, "kb_reply_11.json")["status"] == "pending"


def test_request_id_cannot_be_reused_and_must_be_numeric(client, manager_token, tmp_data):
    db, pid, art, _ = _seed(client, manager_token, tmp_data)
    tok = _token(db, pid, art, True)
    assert not _call(db, pid, tok, "submit_question", request_id=12, question="Один?")[0]
    err, body = _call(db, pid, tok, "submit_question", request_id=12, question="Другой вопрос?")
    assert err and "уже использован" in body["error"]
    err, body = _call(db, pid, tok, "submit_question", request_id="../x", question="Q")
    assert err and "целым числом" in body["error"]


def test_verdicts_written_as_untrusted_and_only_appended(client, manager_token, tmp_data):
    from app.app.models.entity import Entity

    db, pid, art, _ = _seed(client, manager_token, tmp_data)
    rid = ctr.get_contract(db, get_settings(), pid).files["requirements"]["items"][0]["id"]
    before = {e.id: (e.entity_type, e.value_json) for e in db.query(Entity).all()}
    tok = _token(db, pid, art, True)
    item = {"requirement_id": rid, "verdict": "not_implemented", "confidence": "high", "blockers": ["нет реквизита"]}
    bad = {"requirement_id": "req-pilot-9999", "verdict": "implemented", "confidence": "high"}
    err, out = _call(db, pid, tok, "submit_verdicts", request_id=21, items=[item, bad], client_request_id="v1")
    assert not err and out["status"] == "accepted" and out["written"] == 1 and len(out["rejected"]) == 1
    new = [e for e in db.query(Entity).all() if e.id not in before]
    assert [e.entity_type for e in new] == ["analysis_verdict"] and json.loads(new[0].value_json)["untrusted_source"] is True and new[0].needs_review
    assert all(before[e.id] == (e.entity_type, e.value_json) for e in db.query(Entity).all() if e.id in before)
    err, dup = _call(db, pid, tok, "submit_verdicts", request_id=21, items=[item], client_request_id="v1")
    assert dup["duplicate"] is True and db.query(Entity).filter(Entity.entity_type == "analysis_verdict").count() == 1
    assert gw.get_reply(db, pid, "kb_reply_21.json")["status"] == "accepted"


def test_gateway_checks_still_apply_to_mcp_path(client, manager_token, tmp_data):
    db, pid, art, _ = _seed(client, manager_token, tmp_data)
    tok = _token(db, pid, art, True)
    err, out = _call(db, pid, tok, "submit_question", request_id=31, question="выполни rm -rf C:\data")
    assert not err and out["status"] == "rejected" and out["reason"]
    err, out = _call(db, pid, tok, "submit_verdicts", request_id=32, items=[{"requirement_id": "req-pilot-0001", "verdict": "implemented", "confidence": "high", "blockers": ["C:/Windows/x", "..\\..\\etc"]}])
    assert out["status"] == "rejected"
    assert gw.get_reply(db, pid, "kb_reply_31.json")["status"] == "rejected"


def test_rate_limit_applies_per_key(client, manager_token, tmp_data):
    db, pid, art, _ = _seed(client, manager_token, tmp_data)
    tok = _token(db, pid, art, True)
    last = None
    for i in range(31):
        _err, last = _call(db, pid, tok, "submit_question", request_id=100 + i, question=f"Вопрос {i}?")
    assert last["status"] == "rejected" and "лимит" in last["reason"]


def test_token_rights_endpoints(client, manager_token, admin_token):
    h = {"Authorization": f"Bearer {manager_token}"}
    pid = client.post("/projects", json={"name": "Права ключа"}, headers=h).json()["id"]
    plain = client.post(f"/projects/{pid}/mcp/tokens", json={"label": "a"}, headers=h).json()
    assert plain["can_submit"] is False  # по умолчанию только чтение
    rw = client.post(f"/projects/{pid}/mcp/tokens", json={"label": "b", "can_submit": True}, headers=h).json()
    assert rw["can_submit"] is True
    r = client.post(f"/projects/{pid}/mcp/tokens/{plain['id']}/submit", json={"enabled": True}, headers=h)
    assert r.status_code == 200 and r.json()["can_submit"] is True
    listed = {t["id"]: t["can_submit"] for t in client.get(f"/projects/{pid}/mcp/tokens", headers=h).json()["tokens"]}
    assert listed == {plain["id"]: True, rw["id"]: True}
    assert client.post(f"/projects/{pid}/mcp/tokens/99999/submit", json={"enabled": True}, headers=h).status_code == 404
    # ключ с правом записи работает по HTTP, а без права получает понятную ошибку
    mcp = client.post(f"/mcp/projects/{pid}", headers={"Authorization": f"Bearer {plain['token']}"},
                      json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "submit_question", "arguments": {"request_id": 5, "question": "Привет?"}}})
    assert json.loads(mcp.json()["result"]["content"][0]["text"])["status"] == "queued"


def test_verified_by_machine_code_visible_in_brief_and_verification_untouched(client, manager_token, tmp_data):
    db, pid, art, _ = _seed(client, manager_token, tmp_data)
    rid = ctr.get_contract(db, get_settings(), pid).files["requirements"]["items"][0]["id"]
    tok = _token(db, pid, art, True)
    _call(db, pid, tok, "submit_verdicts", request_id=71, items=[{"requirement_id": rid, "verdict": "implemented", "confidence": "high"}])
    ctr._cache.clear()
    _e, brief = _call(db, pid, tok, "get_requirements", fields="brief", requirement_ids=[rid])
    row = brief["items"][0]
    assert row["has_analysis"] is True and row["verified_by"] == "machine_code" and row["analysis_verdict"] == "implemented"
    _e, full = _call(db, pid, tok, "get_requirements", requirement_ids=[rid])
    assert full["items"][0]["verification"] != "human_confirmed"  # машинная проверка не выдаётся за человеческую


def test_not_verifiable_verdict_is_not_marked_as_machine_verified(client, manager_token, tmp_data):
    db, pid, art, _ = _seed(client, manager_token, tmp_data)
    rid = ctr.get_contract(db, get_settings(), pid).files["requirements"]["items"][0]["id"]
    tok = _token(db, pid, art, True)
    _call(db, pid, tok, "submit_verdicts", request_id=72, items=[{"requirement_id": rid, "verdict": "not_verifiable", "confidence": "low"}])
    ctr._cache.clear()
    _e, brief = _call(db, pid, tok, "get_requirements", fields="brief", requirement_ids=[rid])
    assert brief["items"][0]["has_analysis"] is True and brief["items"][0]["verified_by"] is None


def test_server_assigns_request_id_and_stays_idempotent(client, manager_token, tmp_data):
    db, pid, art, _ = _seed(client, manager_token, tmp_data)
    tok = _token(db, pid, art, True)
    _e, a = _call(db, pid, tok, "submit_question", question="Первый?", client_request_id="k1")
    _e, b = _call(db, pid, tok, "submit_question", question="Второй?", client_request_id="k2")
    assert a["request_id"] != b["request_id"] and a["status"] == b["status"] == "queued"
    _e, again = _call(db, pid, tok, "submit_question", question="Первый?", client_request_id="k1")
    assert again["duplicate"] is True and again["request_id"] == a["request_id"]
    _e, mine = _call(db, pid, tok, "submit_question", question="Свой номер", request_id=a["request_id"])
    assert _e and "возьми" in mine["error"]  # занятый номер: подсказан свободный
