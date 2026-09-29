"""Этап 2 автопроверки: проверка требований моделью по источнику (docs/plans/AUTO-REVIEW-PLAN.md)."""
from __future__ import annotations

import json

import pytest

from app.app import db as db_mod
from app.app.config import get_settings

FINAL = (
    "[00:10 - 00:20] Елена Соколова: Предлагаю длину листа вынести в реквизит строки заказа.\n"
    "[00:21 - 00:24] Павел Пилот: Да, согласен, так и делаем.\n"
    "[00:25 - 00:40] Елена Соколова: Тогда я подготовлю описание реквизита к пятнице.\n"
    "[00:41 - 00:50] Павел Пилот: Может быть стоит подумать про склад, но это потом обсудим.\n"
)


def _setup(db, settings):
    from app.app.models.artifact import Artifact
    from app.app.models.entity import Entity
    from app.app.models.project import Project
    from app.app.models.user import User
    from app.app.services.storage import create_project_tree, parsed_dir

    user = db.query(User).first()
    project = Project(name="ReviewReq", created_by=user.id, is_active=True)
    db.add(project)
    db.flush()
    create_project_tree(settings, project.id)
    art = Artifact(artifact_id="art_rr", project_id=project.id, user_id=user.id, original_filename="созвон 21.09.webm",
                   stored_filename="x.webm", file_path="/x", file_type="audio", source_type="call", sha256="rr", status="done")
    db.add(art)
    db.flush()
    folder = parsed_dir(settings, project.id, "call")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "art_rr_final.txt").write_text(FINAL, encoding="utf-8")

    def ent(kind, value):
        row = Entity(artifact_id=art.id, entity_type=kind, value_json=json.dumps(value, ensure_ascii=False), confidence=0.6, is_active=True, needs_review=True)
        db.add(row)
        db.flush()
        return row.id

    ids = {
        "decision": ent("decision", {"text": "Длину листа вынести в реквизит строки заказа", "start_sec": 10.0, "speaker": "Елена Соколова"}),
        "task": ent("task", {"description": "Подготовить описание реквизита", "assignee": "Елена Соколова", "deadline": "пятница"}),
        "noise": ent("decision", {"text": "Учитывать склад в будущем", "start_sec": 41.0}),
        "nosource": ent("decision", {"text": "Бюджет утверждён директором компании"}),
        "dup": ent("decision", {"text": "Длину листа вынести в реквизит строки", "start_sec": 10.0}),
    }
    db.commit()
    return project, art, ids


def _fake_llm(calls, held, replies):
    def fake(settings, *, system, user, **_k):
        held.append(db_mod.thread_held_connections())
        calls.append(user)
        return json.dumps(replies(user), ensure_ascii=False)

    return fake


def _reply(user):
    items = []
    for line in user.splitlines():
        if not line.startswith("E"):
            continue
        key = line.split()[0]
        if "Длину листа" in line:
            items.append({"id": key, "verdict": "confirm", "confidence": 0.95, "reason": "T1: Павел соглашается",
                          "decided_by": [{"name": "Павел", "turn": 1}, {"name": "Елена Соколова", "turn": 1}]})
        elif "склад" in line:
            items.append({"id": key, "verdict": "reject", "reject_kind": "not_decision", "confidence": 0.9, "reason": "T3: только мысль, итога нет"})
        else:
            items.append({"id": key, "verdict": "confirm", "confidence": 0.6, "reason": "похоже, но не уверен"})
    return {"items": items}


@pytest.fixture
def patched_llm(monkeypatch):
    calls: list[str] = []
    held: list[list[str]] = []
    monkeypatch.setattr("app.app.services.llm.chat_completion_no_think", _fake_llm(calls, held, _reply))
    monkeypatch.setattr("app.app.services.gpu_phase.prepare_for_llm", lambda *_a, **_k: None)
    return calls, held


def _values(db, ids):
    from app.app.models.entity import Entity

    out = {}
    for key, ent_id in ids.items():
        ent = db.get(Entity, ent_id)
        out[key] = (ent, json.loads(ent.value_json))
    return out


def test_shadow_records_verdicts_without_effect(client, admin_token, tmp_data, patched_llm):
    from app.app.services.review_requirements import review_artifact_requirements

    calls, held = patched_llm
    settings = get_settings()
    db = db_mod.SessionLocal()
    try:
        baseline = len(db_mod.thread_held_connections())
        _project, art, ids = _setup(db, settings)
        stats = review_artifact_requirements(db, settings, art, "shadow")
        db.commit()
        assert stats["no_source"] == 1 and stats["duplicate"] == 1 and stats["llm_calls"] == 1
        # не больше, чем до начала: чужие сессии прошлых тестов могут закрыться сборщиком мусора в любой момент
        assert held and all(len(h) <= baseline for h in held), "проверка держала соединение БД во время ответа модели"
        vals = _values(db, ids)
        assert vals["decision"][1]["model_review"]["verdict"] == "confirm"
        assert vals["noise"][1]["model_review"]["verdict"] == "reject" and vals["noise"][0].is_active is True  # тень: без последствий
        assert vals["task"][1]["model_review"]["verdict"] == "unsure"  # 0.6 < порога подтверждения
        assert vals["nosource"][1]["model_review"]["rule"] == "no_source"
        assert vals["dup"][1]["model_review"]["rule"] == "duplicate"
        assert "decided_by" not in vals["decision"][1]  # в тени в само значение не пишется
        assert vals["decision"][1]["model_review"]["decided_by"] == [{"name": "Павел Пилот", "start_sec": 21.0}]
        # повторный прогон той же версии промпта модель не зовёт; «источник не найден» — не вердикт, его ищут снова
        again = review_artifact_requirements(db, settings, art, "shadow")
        assert again["total"] == 1 and again["no_source"] == 1 and len(calls) == 1
    finally:
        db.close()


def test_live_applies_confirm_reject_and_decided_by(client, admin_token, tmp_data, patched_llm):
    from app.app.services.auto_review import verification_for
    from app.app.services.review_requirements import review_artifact_requirements

    settings = get_settings()
    db = db_mod.SessionLocal()
    try:
        _project, art, ids = _setup(db, settings)
        review_artifact_requirements(db, settings, art, "live")
        db.commit()
        vals = _values(db, ids)
        dec_ent, dec = vals["decision"]
        assert verification_for(dec_ent.needs_review, dec) == "model_confirmed"
        assert dec["decided_by"] == [{"name": "Павел Пилот", "start_sec": 21.0}]
        assert vals["noise"][0].is_active is False and vals["noise"][1]["model_review"]["deactivated"] is True
        assert vals["dup"][0].is_active is False
        assert vals["task"][0].is_active is True and verification_for(True, vals["task"][1]) == "extracted"
    finally:
        db.close()


def test_llm_failure_leaves_batch_for_next_run(client, admin_token, tmp_data, monkeypatch):
    from app.app.services.review_requirements import review_artifact_requirements

    def broken(*_a, **_k):
        return "не json"

    monkeypatch.setattr("app.app.services.llm.chat_completion_no_think", broken)
    monkeypatch.setattr("app.app.services.gpu_phase.prepare_for_llm", lambda *_a, **_k: None)
    settings = get_settings()
    db = db_mod.SessionLocal()
    try:
        _project, art, ids = _setup(db, settings)
        stats = review_artifact_requirements(db, settings, art, "shadow")
        db.commit()
        assert stats["llm_errors"] == 1 and stats["llm_calls"] == 0
        assert "model_review" not in _values(db, ids)["decision"][1]
    finally:
        db.close()


def test_review_queue_filters_and_undo(client, admin_token, tmp_data, patched_llm):
    from app.app.services.review_requirements import review_artifact_requirements

    settings = get_settings()
    db = db_mod.SessionLocal()
    try:
        project, art, ids = _setup(db, settings)
        review_artifact_requirements(db, settings, art, "live")
        db.commit()
        pid = project.id
    finally:
        db.close()
    headers = {"Authorization": f"Bearer {admin_token}"}
    body = client.get(f"/projects/{pid}/requirements/review?verification=model_rejected", headers=headers).json()
    assert body["counts"]["model_rejected"] >= 1
    rejected = next(r for r in body["items"] if r["source_entity_id"] == ids["noise"])
    assert rejected["model_review"]["verdict"] == "reject"
    r = client.post(f"/projects/{pid}/requirements/entity/{ids['noise']}/undo-model", headers=headers)
    assert r.status_code == 200 and r.json()["is_active"] is True
    after = client.get(f"/projects/{pid}/requirements/review?verification=model_rejected", headers=headers).json()
    assert all(x["source_entity_id"] != ids["noise"] for x in after["items"])
    again = client.post(f"/projects/{pid}/requirements/entity/{ids['noise']}/undo-model", headers=headers)
    assert again.status_code == 400
    needs = client.get(f"/projects/{pid}/requirements/review?verification=needs_human", headers=headers).json()
    assert all(x["needs_human"] for x in needs["items"])
