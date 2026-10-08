"""MCP базы знаний: полный текст материалов порциями, порции списков, страж размера (docs/plans/MCP-KB-PLAN.md, Э1–Э2).

Имена и тексты вымышленные."""
from __future__ import annotations

import json
import uuid

from app.app.mcp import project_protocol as pp


def _project(client, manager_token) -> int:
    return client.post("/projects", json={"name": "MCP порции"}, headers={"Authorization": f"Bearer {manager_token}"}).json()["id"]


def _art(db, pid, *, file_type, source_type, name, status="done", file_path="x", error=None):
    from app.app.models.artifact import Artifact

    tag = uuid.uuid4().hex[:8]
    art = Artifact(artifact_id=f"art_2026-10-08_120000_{tag}", project_id=pid, user_id=1, original_filename=name, stored_filename="s",
                   file_path=file_path, file_type=file_type, source_type=source_type, sha256=tag.ljust(64, "0"), status=status, error_message=error)
    db.add(art)
    db.commit()
    return art


def _write(settings, art, suffix: str, text: str) -> None:
    from app.app.services.storage import parsed_dir

    d = parsed_dir(settings, art.project_id, art.source_type)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{art.artifact_id}{suffix}").write_text(text, encoding="utf-8")


def _call(db, settings, pid, name, **args):
    res = pp.call_tool(db, settings, pid, name, args)
    text = res["content"][0]["text"]
    return json.loads(text), len(text.encode("utf-8")), res["isError"]


def test_long_transcript_is_read_whole_by_cursor_within_budget(client, manager_token, tmp_data):
    from app.app.config import get_settings
    from app.app.db import SessionLocal

    settings, db = get_settings(), SessionLocal()
    pid = _project(client, manager_token)
    call = _art(db, pid, file_type="audio", source_type="call", name="созвон.wav")
    transcript = "".join(f"[{i:05d}] Ольга: обсуждаем выгрузку остатков, «кавычки» и \\ слэш\n" for i in range(5000))  # ~300 тыс. символов
    _write(settings, call, "_final.txt", transcript)
    _write(settings, call, "_call_summary.md", "Итог: выгрузка остатков раз в сутки.")

    auto, _, _ = _call(db, settings, pid, "get_artifact_content", artifact_id=call.artifact_id)
    assert auto["kind"] == "summary" and auto["material"] == "call" and "text" in auto["available_kinds"]

    got, cursor, pages = "", None, 0
    while True:
        args = {"artifact_id": call.artifact_id, "kind": "transcript"}
        if cursor:
            args["cursor"] = cursor
        page, size, err = _call(db, settings, pid, "get_artifact_content", **args)
        assert not err and size <= 60_000 * 1.05
        assert page["total_chars"] == len(transcript) and page["offset"] == len(got)
        got += page["content"]
        pages += 1
        cursor = page["cursor"]
        assert page["truncated"] == bool(cursor)
        if not cursor:
            break
    assert got == transcript and pages > 5  # раньше — первые 120 000 символов и ни слова об обрезке

    big, size, _ = _call(db, settings, pid, "get_artifact_content", artifact_id=call.artifact_id, kind="text", page_bytes=200_000)
    assert 60_000 < size <= 200_000 * 1.05 and big["cursor"]


def test_documents_chats_and_exchange_files_give_their_text(client, manager_token, tmp_data):
    from app.app.config import get_settings
    from app.app.db import SessionLocal

    settings, db = get_settings(), SessionLocal()
    pid = _project(client, manager_token)
    doc = _art(db, pid, file_type="docx", source_type="document", name="ТЗ.docx")
    _write(settings, doc, "_clean.txt", "1. Выгрузка остатков в Битрикс24.")
    _write(settings, doc, "_doc_summary.md", "Документ о выгрузке остатков.")
    chat = _art(db, pid, file_type="html", source_type="document", name="messages.html")
    _write(settings, chat, "_clean.txt", "Павел: согласовано")
    _write(settings, chat, "_messages.json", json.dumps({"messages": [{"id": 1, "author": "Павел", "text": "согласовано"}]}, ensure_ascii=False))
    raw = tmp_data / "q.json"
    raw.write_text('{"request_id": 3, "question": "Какой срок?"}', encoding="utf-8")
    exch = _art(db, pid, file_type="txt", source_type="document", name="analysis_question_3.json", file_path=str(raw))
    bad = _art(db, pid, file_type="txt", source_type="document", name="мусор.txt", status="rejected", error="не относится к проекту")

    d, _, _ = _call(db, settings, pid, "get_artifact_content", artifact_id=doc.artifact_id)
    assert d["kind"] == "summary" and d["material"] == "document"
    d, _, _ = _call(db, settings, pid, "get_artifact_content", artifact_id=doc.artifact_id, kind="transcript")  # прежнее имя — текст
    assert d["kind"] == "text" and "Битрикс24" in d["content"]
    m, _, _ = _call(db, settings, pid, "get_artifact_content", artifact_id=chat.artifact_id, kind="messages")
    assert m["material"] == "chat" and "согласовано" in m["content"]
    e, _, _ = _call(db, settings, pid, "get_artifact_content", artifact_id=exch.artifact_id)
    assert e["material"] == "exchange" and "Какой срок" in e["content"]
    r, _, _ = _call(db, settings, pid, "get_artifact_content", artifact_id=bad.artifact_id)
    assert r["content"] == "" and "отклонён" in r["reason"] and "не относится" in r["reason"]
    none, _, _ = _call(db, settings, pid, "get_artifact_content", artifact_id=doc.artifact_id, kind="outline")
    assert none["content"] == "" and "есть: summary, text" in none["reason"]


def test_entity_and_conflict_lists_come_in_pages(client, manager_token, tmp_data):
    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.entity import Entity, Link

    settings, db = get_settings(), SessionLocal()
    pid = _project(client, manager_token)
    art = _art(db, pid, file_type="docx", source_type="document", name="ТЗ.docx")
    ents = [Entity(artifact_id=art.id, entity_type="decision", value_json=json.dumps({"text": f"Решение {i}: " + "длинное описание " * 60}, ensure_ascii=False))
            for i in range(300)]
    db.add_all(ents)
    db.commit()
    db.add(Link(project_id=pid, source_entity_id=ents[0].id, target_entity_id=ents[1].id, link_type="POTENTIAL_CONFLICT", similarity_score=0.9))
    db.commit()

    seen, cursor = set(), None
    while True:
        page, size, err = _call(db, settings, pid, "list_entities", limit=500, **({"cursor": cursor} if cursor else {}))
        assert not err and size <= 60_000 * 1.05 and page["total"] == 300
        seen |= {e["id"] for e in page["items"]}
        cursor = page["cursor"]
        if not cursor:
            break
    assert seen == {e.id for e in ents}

    conflicts, _, _ = _call(db, settings, pid, "list_conflicts")
    item = conflicts["items"][0]
    assert isinstance(item["source"]["value"], dict) and item["source"]["verification"] == "extracted"  # не строка JSON в JSON

    graph, size, _ = _call(db, settings, pid, "get_knowledge_graph", limit=500)
    assert size <= 60_000 * 1.05 and graph["total_nodes"] == 301 and graph["cursor"]


def test_size_guard_pages_lists_and_refuses_other_oversized_answers(monkeypatch):
    big = [{"n": i, "text": "ж" * 500} for i in range(400)]
    monkeypatch.setattr(pp.data, "mcp_list_artifacts", lambda *a, **k: big)
    res = pp.call_tool(None, None, 1, "list_artifacts", {})
    page = json.loads(res["content"][0]["text"])
    assert page["total"] == 400 and page["cursor"] and len(res["content"][0]["text"].encode("utf-8")) <= 60_000 * 1.05

    monkeypatch.setattr(pp.data, "mcp_get_project_summary", lambda *a, **k: {"content": "ж" * 100_000})
    res = pp.call_tool(None, None, 1, "get_project_summary", {})
    body = json.loads(res["content"][0]["text"])
    assert res["isError"] and body["detail"].startswith("too_large")
    ok = pp.call_tool(None, None, 1, "get_project_summary", {"page_bytes": 200_000})
    assert not ok["isError"]


def test_changes_cursor_is_accepted(client, manager_token, tmp_data):
    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.entity import Entity

    settings, db = get_settings(), SessionLocal()
    pid = _project(client, manager_token)
    art = _art(db, pid, file_type="docx", source_type="document", name="ТЗ.docx")
    _write(settings, art, "_clean.txt", "текст")
    db.add_all([Entity(artifact_id=art.id, entity_type="decision", value_json=json.dumps({"text": f"Сделать отчёт номер {i}"}, ensure_ascii=False))
                for i in range(30)])
    db.commit()
    first, _, _ = _call(db, settings, pid, "get_changes", limit=10)
    assert first["cursor"] == "10" and len(first["items"]) == 10
    second, _, _ = _call(db, settings, pid, "get_changes", limit=10, cursor=first["cursor"])  # раньше курсор не принимался
    assert second["items"] and second["items"][0]["id"] != first["items"][0]["id"]
