"""MCP get_decisions: память решений агенту — только в режиме live (docs/plans/MCP-KB-PLAN.md, Э5, В2). Имена вымышленные."""
from __future__ import annotations

import json

from app.app.mcp import project_protocol as pp
from app.app.tests.test_project_memory_api import world  # noqa: F401 — та же заготовка, что у экрана памяти


def _call(pid, name="get_decisions", **args):
    from app.app.config import get_settings
    from app.app.db import SessionLocal

    db = SessionLocal()
    try:
        res = pp.call_tool(db, get_settings(), pid, name, args)
    finally:
        db.close()
    return json.loads(res["content"][0]["text"]), res["isError"]


def _mode(pid, mode):
    from app.app.db import SessionLocal
    from app.app.models.project import Project
    from app.app.services import memory_mode

    db = SessionLocal()
    memory_mode.set_mode(db, db.get(Project, pid), mode)
    db.close()


def test_decisions_closed_until_memory_is_live(world, monkeypatch):  # noqa: F811
    from app.app.config import get_settings

    monkeypatch.setattr(get_settings(), "decision_memory_mode", "off")
    pid, _sid = world
    for mode in ("off", "shadow"):
        _mode(pid, mode)
        body, err = _call(pid)
        assert err and body["detail"] == "memory_not_live" and mode in body["error"]


def test_decisions_list_search_detail_and_date(world, monkeypatch):  # noqa: F811
    from app.app.config import get_settings

    monkeypatch.setattr(get_settings(), "dry_run", True)  # поиск предметов — по словам, без векторов
    pid, sid = world
    _mode(pid, "live")
    listing, err = _call(pid)
    assert not err and listing["total"] == 1 and listing["items"][0]["id"] == sid and "subject_id" in listing["hint"]
    found, _ = _call(pid, query="разбить подпапки по толщине")
    assert [i["id"] for i in found["items"]] == [sid]
    none, _ = _call(pid, query="бюджет на сервера")
    assert none["total"] == 0
    detail, _ = _call(pid, subject_id=sid)
    acts = detail["acts"]
    assert [a["act"] for a in acts] == ["proposed", "rejected"] and acts[0]["artifact_id"] == "art_mem" and acts[0]["ref"] == "M1"
    assert detail["total"] == 2 and detail["cursor"] is None and len(detail["events"]) == 2
    early, _ = _call(pid, subject_id=sid, as_of="2026-09-10")
    assert early["fold"]["state"] == "open"
    on_day, _ = _call(pid, as_of="2026-09-12")
    assert on_day["items"] and on_day["items"][0]["id"] == sid
    bad, err = _call(pid, as_of="12.09.2026")
    assert err


def test_long_subject_feed_comes_in_pages(world, monkeypatch):  # noqa: F811
    from app.app.db import SessionLocal
    from app.app.models.speech_act import SpeechAct

    pid, sid = world
    _mode(pid, "live")
    db = SessionLocal()
    art_id = db.query(SpeechAct).filter(SpeechAct.subject_id == sid).first().artifact_id
    db.add_all([SpeechAct(project_id=pid, artifact_id=art_id, unit_ref=f"M{i}", unit_key=i, author="Исполнитель", side="executor", act="proposed",
                          quote=f"Вариант {i}: " + "подробное обоснование " * 40, said_at=f"2026-09-12T10:{i % 60:02d}:00", subject_id=sid,
                          prompt_version="v", model="m") for i in range(10, 400)])
    db.commit()
    db.close()
    seen, cursor = 0, None
    while True:
        page, err = _call(pid, subject_id=sid, **({"cursor": cursor} if cursor else {}))
        assert not err and len(json.dumps(page, ensure_ascii=False).encode("utf-8")) <= 60_000 * 1.05
        seen += len(page["acts"])
        cursor = page["cursor"]
        if not cursor:
            break
    assert seen == page["total"] == 392
