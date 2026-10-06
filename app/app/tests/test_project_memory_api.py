"""API памяти решений и блок для чата (docs/plans/DECISION-MEMORY-PLAN.md, Э6). Имена вымышленные."""
from __future__ import annotations

from datetime import date

import pytest

from app.app.services import memory_chat as mc


@pytest.fixture()
def world(client, auth_headers):
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.decision_memory import Subject
    from app.app.models.project import Project
    from app.app.models.speech_act import SpeechAct
    from app.app.models.user import User
    from app.app.services import decision_log as dl

    db = SessionLocal()
    admin = db.query(User).filter(User.telegram_id == "tg-admin").one()
    project = Project(name="Память", created_by=admin.id, is_active=True)
    db.add(project)
    db.commit()
    art = Artifact(artifact_id="art_mem", project_id=project.id, user_id=admin.id, original_filename="чат.html", stored_filename="c", file_path="/x",
                   file_type="html", source_type="document", sha256="mem", status="done")
    db.add(art)
    db.commit()
    subject = Subject(project_id=project.id, title="Подпапки каталога по толщине")
    db.add(subject)
    db.commit()
    spec = [("customer", "Заказчик", "proposed", "А мы можем их по толщине разбить?", "2026-09-11T12:30:00"),
            ("executor", "Исполнитель", "rejected", "Сделать папки по толщине — неправильно", "2026-09-11T13:14:00")]
    for i, (side, author, act, quote, said) in enumerate(spec, 1):
        row = SpeechAct(project_id=project.id, artifact_id=art.id, unit_ref=f"M{i}", unit_key=i, author=author, side=side, act=act, quote=quote,
                        said_at=said, subject_id=subject.id, prompt_version="v", model="m")
        db.add(row)
        db.flush()
        dl.add_event(db, subject, dl.ACT_TO_EVENT[act], said, actor_side=side, anchor=f"M{i}", speech_act_id=row.id)
    dl.refresh_subject(db, subject)
    db.commit()
    ids = (project.id, subject.id)
    db.close()
    return ids


def test_list_and_detail(client, auth_headers, world):
    pid, sid = world
    r = client.get(f"/projects/{pid}/memory/subjects", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1 and body["items"][0]["acts"] == 2 and body["items"][0]["title"].startswith("Подпапки")
    assert client.get(f"/projects/{pid}/memory/subjects?q=толщине", headers=auth_headers).json()["total"] == 1
    assert client.get(f"/projects/{pid}/memory/subjects?q=нет-такого", headers=auth_headers).json()["total"] == 0
    d = client.get(f"/projects/{pid}/memory/subjects/{sid}", headers=auth_headers).json()
    assert [a["act"] for a in d["acts"]] == ["proposed", "rejected"] and d["acts"][0]["quote"].startswith("А мы")
    assert d["fold"]["state"] in ("open", "rejected") and len(d["events"]) == 2
    early = client.get(f"/projects/{pid}/memory/subjects/{sid}?as_of=2026-09-10", headers=auth_headers).json()
    assert early["fold"]["state"] == "open"


def test_state_at_date(client, auth_headers, world):
    pid, _sid = world
    r = client.get(f"/projects/{pid}/memory/state-at?date=2026-09-11", headers=auth_headers)
    assert r.status_code == 200 and r.json()["items"]
    assert client.get(f"/projects/{pid}/memory/state-at?date=вчера", headers=auth_headers).status_code == 422


def test_human_decision_and_release(client, auth_headers, world):
    pid, sid = world
    r = client.post(f"/projects/{pid}/memory/subjects/{sid}/decide", headers=auth_headers, json={"kind": "agreed", "note": "решили на встрече"})
    assert r.status_code == 200 and r.json()["fold"]["state"] == "agreed"
    assert client.post(f"/projects/{pid}/memory/subjects/{sid}/decide", headers=auth_headers, json={"kind": "proposed"}).status_code == 400
    rel = client.post(f"/projects/{pid}/memory/subjects/{sid}/release", headers=auth_headers).json()
    assert rel["released"] == 1 and rel["fold"]["state"] != "agreed"


def test_no_access_without_login_or_other_project(client, user_token, world):
    pid, sid = world
    assert client.get(f"/projects/{pid}/memory/subjects", headers={"Authorization": f"Bearer {user_token}"}).status_code in (403, 404)
    assert client.get(f"/projects/{pid}/memory/subjects/{sid + 99}", headers={"Authorization": f"Bearer {user_token}"}).status_code in (403, 404)


def test_date_in_question():
    today = date(2026, 10, 6)
    assert mc.date_in_question("что было на 15.09.2026?", today=today) == "2026-09-15"
    assert mc.date_in_question("а на 15 сентября?", today=today) == "2026-09-15"
    assert mc.date_in_question("на 20 ноября", today=today) == "2025-11-20"  # позже сегодняшнего — прошлый год
    assert mc.date_in_question("31.02.2026", today=today) is None
    assert mc.date_in_question("без даты", today=today) is None


def test_decision_block_by_words_and_date(client, world):
    from app.app.db import SessionLocal

    pid, _sid = world
    db = SessionLocal()
    try:
        text = mc.decision_block(db, pid, None, "что решили по подпапкам каталога по толщине на 11.09.2026?", today=date(2026, 10, 6))
        assert "Подпапки каталога по толщине" in text and "Лента:" in text and "Состояние на 2026-09-11" in text and "«Сделать папки" in text
        assert mc.decision_block(db, pid, None, "какая погода сегодня", today=date(2026, 10, 6)) == ""
    finally:
        db.close()
