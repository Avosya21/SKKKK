"""Фаза 5 обмена с агентом: права ключей (read / questions / questions_verdicts), имя агента, лимит на ключ, журнал."""
from __future__ import annotations

import json

import pytest

from app.app.config import get_settings
from app.app.mcp.project_protocol import call_tool
from app.app.services import agent_gateway as gw
from app.app.services import contract as ctr
from app.app.services.project_mcp_tokens import (
    create_project_mcp_token,
    list_project_mcp_tokens,
    normalize_scope,
    set_project_mcp_token_scope,
    set_project_mcp_token_submit,
    token_scope,
)
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


def _mk(db, pid, art, **kw):
    row, _plain = create_project_mcp_token(db, project_id=pid, label="t", user_id=art.user_id, **kw)
    return row


def test_scope_normalization_and_legacy_flag():
    assert normalize_scope(None) == "read" and normalize_scope(None, True) == "questions_verdicts"
    assert normalize_scope("questions") == "questions"
    with pytest.raises(ValueError):
        normalize_scope("admin")


def test_questions_scope_can_ask_but_not_send_verdicts(client, manager_token, tmp_data):
    db, pid, art, _ = _seed(client, manager_token, tmp_data)
    tok = _mk(db, pid, art, scope="questions", agent_name="Агент анализа")
    assert token_scope(tok) == "questions" and tok.can_submit is True
    err, body = _call(db, pid, tok, "submit_question", request_id=31, question="Что с профнастилом?")
    assert not err and body["status"] == "queued"
    err, body = _call(db, pid, tok, "submit_verdicts", request_id=32, items=[{}])
    assert err and body["detail"] == "verdicts_not_allowed" and "только на вопросы" in body["error"]
    # журнал обмена показывает имя агента, а не «mcp:N»
    journal = client.get(f"/projects/{pid}/agent-exchange", headers={"Authorization": f"Bearer {manager_token}"}).json()["items"]
    assert any(item["agent"] == "Агент анализа" for item in journal)


def test_read_key_and_full_key(client, manager_token, tmp_data):
    db, pid, art, _ = _seed(client, manager_token, tmp_data)
    err, body = _call(db, pid, _mk(db, pid, art, scope="read"), "submit_question", request_id=33, question="Q?")
    assert err and body["detail"] == "read_only_key"
    full = _mk(db, pid, art, scope="questions_verdicts")
    assert not _call(db, pid, full, "submit_question", request_id=34, question="Q?")[0]


def test_legacy_can_submit_true_means_questions_and_verdicts(client, manager_token, tmp_data):
    db, pid, art, _ = _seed(client, manager_token, tmp_data)
    tok = _mk(db, pid, art, can_submit=True)
    assert token_scope(tok) == "questions_verdicts"
    # старая строка: scope ещё «read», а can_submit=1 (до миграции) — права сохраняются
    tok.scope = "read"
    tok.can_submit = True
    db.commit()
    assert token_scope(tok) == "questions_verdicts"


def test_scope_change_keeps_flag_in_sync_and_lists_agent_name(client, manager_token, tmp_data):
    db, pid, art, _ = _seed(client, manager_token, tmp_data)
    tok = _mk(db, pid, art, scope="questions", agent_name="  Агент  ")
    row = list_project_mcp_tokens(db, project_id=pid)[0]
    assert row["scope"] == "questions" and row["agent_name"] == "Агент" and row["can_submit"] is True
    assert set_project_mcp_token_scope(db, project_id=pid, token_id=tok.id, scope="read")
    row = list_project_mcp_tokens(db, project_id=pid)[0]
    assert row["scope"] == "read" and row["can_submit"] is False and row["scope_title"] == "только чтение"
    assert set_project_mcp_token_submit(db, project_id=pid, token_id=tok.id, enabled=True)
    assert list_project_mcp_tokens(db, project_id=pid)[0]["scope"] == "questions_verdicts"


def test_per_key_rate_limit_overrides_default(client, manager_token, tmp_data):
    db, pid, art, _ = _seed(client, manager_token, tmp_data)
    tok = _mk(db, pid, art, scope="questions", rate_limit=2)
    results = [_call(db, pid, tok, "submit_question", request_id=40 + i, question=f"Вопрос {i}?") for i in range(4)]
    assert [r[0] for r in results[:2]] == [False, False]
    assert any(r[0] or r[1].get("status") == "rejected" for r in results[2:]), "после исчерпания лимита ключа обращения должны отклоняться"
    with pytest.raises(ValueError):
        _mk(db, pid, art, scope="questions", rate_limit=5000)


def test_rest_create_change_scope_and_journal_shows_agent_name(client, admin_token, tmp_data):
    h = {"Authorization": f"Bearer {admin_token}"}
    pid = client.post("/projects", json={"name": "Keys", "description": ""}, headers=h).json()["id"]
    r = client.post(f"/projects/{pid}/mcp/tokens", json={"label": "Cursor", "scope": "questions", "agent_name": "Агент анализа", "rate_limit": 20}, headers=h)
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["scope"] == "questions" and created["agent_name"] == "Агент анализа" and created["rate_limit"] == 20
    assert client.post(f"/projects/{pid}/mcp/tokens", json={"label": "x", "scope": "root"}, headers=h).status_code == 422
    tokens = client.get(f"/projects/{pid}/mcp/tokens", headers=h).json()["tokens"]
    assert tokens[0]["scope"] == "questions" and tokens[0]["scope_title"]
    r = client.post(f"/projects/{pid}/mcp/tokens/{created['id']}/scope", json={"scope": "questions_verdicts", "clear_rate_limit": True}, headers=h)
    assert r.status_code == 200
    after = client.get(f"/projects/{pid}/mcp/tokens", headers=h).json()["tokens"][0]
    assert after["scope"] == "questions_verdicts" and after["rate_limit"] is None
    assert client.post(f"/projects/{pid}/mcp/tokens/{created['id']}/scope", json={"scope": "bogus"}, headers=h).status_code == 422
    assert client.post(f"/projects/{pid}/mcp/tokens/9999/scope", json={"scope": "read"}, headers=h).status_code == 404
    # старое поле can_submit продолжает работать
    old = client.post(f"/projects/{pid}/mcp/tokens", json={"label": "old", "can_submit": True}, headers=h).json()
    assert old["scope"] == "questions_verdicts"


def test_migration_promotes_old_can_submit_keys(tmp_path, monkeypatch):
    import sqlite3

    from sqlalchemy import create_engine

    import app.app.db as db_mod

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE project_mcp_tokens (id INTEGER PRIMARY KEY, project_id INT, can_submit BOOLEAN NOT NULL DEFAULT 0)")
    con.execute("INSERT INTO project_mcp_tokens(id, project_id, can_submit) VALUES (1, 1, 1), (2, 1, 0)")
    con.commit()
    con.close()
    for table in ("artifacts", "chat_messages", "meeting_participants", "agent_exchange_log"):
        c = sqlite3.connect(path)
        c.execute(f"CREATE TABLE IF NOT EXISTS {table} (id INTEGER PRIMARY KEY)")
        c.commit()
        c.close()
    monkeypatch.setattr(db_mod, "engine", create_engine(f"sqlite:///{path.as_posix()}"))
    monkeypatch.setattr(db_mod.settings, "database_url", f"sqlite:///{path.as_posix()}", raising=False)
    db_mod.apply_schema_migrations()
    db_mod.apply_schema_migrations()  # повторный запуск безопасен
    rows = dict(sqlite3.connect(path).execute("SELECT id, scope FROM project_mcp_tokens").fetchall())
    assert rows == {1: "questions_verdicts", 2: "read"}


def test_project_page_has_scope_form_and_default_from_settings(client, admin_token, tmp_data):
    h = {"Authorization": f"Bearer {admin_token}"}
    pid = client.post("/projects", json={"name": "UI", "description": ""}, headers=h).json()["id"]
    page = client.get(f"/projects/{pid}", headers=h)
    assert page.status_code == 200
    for needle in ("mcp-rights-questions", "mcp-agent-name", "mcp-rate-limit", "changeMcpScope", "default_key_scope"):
        assert needle in page.text, needle
    put = client.put(f"/projects/{pid}/agent-settings", json={"default_key_scope": "questions"}, headers=h)
    assert put.status_code == 200, put.text
    assert client.get(f"/projects/{pid}/agent-settings", headers=h).json()["settings"]["default_key_scope"] == "questions"
    assert client.put(f"/projects/{pid}/agent-settings", json={"default_key_scope": "root"}, headers=h).status_code in {400, 422}
