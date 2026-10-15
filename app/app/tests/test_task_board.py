"""Задачи под управлением менеджера (PORTAL-ACCURACY-MANAGER-PLAN, Ш4): все задачи, вкладки, правка поверх извлечённого,
ручные задачи, перенос правки при переизвлечении, права."""
from __future__ import annotations

import json
import uuid
from datetime import date

from app.app.services import task_board as tb

TODAY = date(2026, 10, 14)


def test_normalize_status_and_effective():
    assert tb.normalize_status("unknown") == "open" and tb.normalize_status("closed") == "done"
    assert tb.normalize_status("cancelled") == "cancelled" and tb.normalize_status(None) == "open"
    value = {"description": "Выгрузить остатки", "assignee": "Спикер 2", "deadline": "до пятницы", "status": "open",
             "human_edit": {"status": "done", "deadline_at": "2026-10-20"}}
    eff = tb.effective(value)
    assert eff["status"] == "done" and eff["deadline_at"] == "2026-10-20" and eff["deadline"] == "20.10.2026"
    assert eff["assignee"] == "Спикер 2"  # не правили — как извлечено
    assert tb.hidden({"relevance": {"state": "superseded"}}) and not tb.hidden({"relevance": {"state": "superseded", "undone": {"by": "x"}}})


def _seed(client, manager_token):
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.entity import Entity

    h = {"Authorization": f"Bearer {manager_token}"}
    pid = client.post("/projects", json={"name": "Задачи проекта"}, headers=h).json()["id"]
    db = SessionLocal()
    tag = uuid.uuid4().hex[:8]
    art = Artifact(artifact_id=f"art_2026-10-01_100000_{tag}", project_id=pid, user_id=1, original_filename="созвон.webm",
                   stored_filename="c.webm", file_path="c.webm", file_type="audio", source_type="call", sha256=tag.ljust(64, "0"),
                   status="done")
    db.add(art)
    db.flush()

    def task(**value):
        e = Entity(artifact_id=art.id, entity_type="task", value_json=json.dumps(value, ensure_ascii=False), confidence=0.6)
        db.add(e)
        return e

    ents = {
        "overdue": task(description="Прислать выгрузку остатков", assignee="Анна Петрова", deadline="01.10.2026", status="open"),
        "week": task(description="Настроить обмен заказами", deadline="16.10.2026", status="unknown"),
        "nodeadline": task(description="Проверить права в тестовой базе", status="open"),
        "done": task(description="Поставить коннектор", deadline="20.09.2026", status="done"),
        "hidden": task(description="Старая задача про склад", status="open", relevance={"state": "superseded"}),
    }
    db.commit()
    return db, pid, art, {k: e.id for k, e in ents.items()}


def test_views_counts_and_hidden(client, manager_token):
    db, pid, _art, ids = _seed(client, manager_token)
    out = tb.list_project_tasks(db, pid, view="all", today=TODAY)
    texts = {t["description"] for t in out["items"]}
    assert "Старая задача про склад" not in texts and len(texts) == 4
    assert out["counts"] == {"open": 3, "overdue": 1, "week": 1, "no_deadline": 1, "reference": 0, "done": 1, "all": 4}
    assert [t["entity_id"] for t in tb.list_project_tasks(db, pid, view="overdue", today=TODAY)["items"]] == [ids["overdue"]]
    assert [t["entity_id"] for t in tb.list_project_tasks(db, pid, view="no_deadline", today=TODAY)["items"]] == [ids["nodeadline"]]
    assert tb.list_project_tasks(db, pid, view="open", assignee="анна петрова", today=TODAY)["total"] == 1
    db.close()


def test_edit_extracted_keeps_before_and_does_not_confirm(client, manager_token):
    from app.app.models.entity import Entity

    db, pid, _art, ids = _seed(client, manager_token)
    user = type("U", (), {"name": "Менеджер", "id": 1})()
    tb.edit_extracted(db, pid, ids["overdue"], {"status": "in_progress"}, user=user)
    tb.edit_extracted(db, pid, ids["overdue"], {"deadline_at": "2026-10-30", "assignee": "Олег Смирнов", "reason": "перенесли"}, user=user)
    ent = db.get(Entity, ids["overdue"])
    value = json.loads(ent.value_json)
    edit = value["human_edit"]
    assert edit["before"] == {"status": "open", "deadline_at": None, "assignee": "Анна Петрова"}
    assert edit["fields"] == ["assignee", "deadline_at", "status"] and edit["reason"] == "перенесли"
    assert "human_review" not in value and ent.needs_review is True  # правка задачи — не подтверждение
    row = next(t for t in tb.list_project_tasks(db, pid, view="all", today=TODAY)["items"] if t["entity_id"] == ids["overdue"])
    assert row["status"] == "in_progress" and row["deadline_date"] == "2026-10-30" and not row["overdue"] and row["edited"]
    db.close()


def test_edit_validation_and_manual_tasks(client, manager_token):
    db, pid, _art, ids = _seed(client, manager_token)
    user = type("U", (), {"name": "Менеджер", "id": 1})()
    for bad in ({"status": "почти"}, {"deadline_at": "завтра"}, {}):
        try:
            tb.edit_extracted(db, pid, ids["week"], bad, user=user)
        except tb.TaskError:
            pass
        else:
            raise AssertionError(bad)
    made = tb.create_manual(db, pid, {"description": "Созвониться с бухгалтерией", "deadline_at": "2026-10-15"}, user=user)
    row = next(t for t in tb.list_project_tasks(db, pid, view="week", today=TODAY)["items"] if t["id"] == made["id"])
    assert row["source_kind"] == "manual" and row["deadline"] == "15.10.2026"
    tb.edit_manual(db, pid, made["manual_id"], {"status": "done"})
    assert made["id"] in {t["id"] for t in tb.list_project_tasks(db, pid, view="done", today=TODAY)["items"]}
    tb.delete_manual(db, pid, made["manual_id"])
    assert made["id"] not in {t["id"] for t in tb.list_project_tasks(db, pid, view="all", today=TODAY)["items"]}
    from app.app.services.project_deadlines import list_project_deadlines

    assert all(i.get("description") != "Старая задача про склад" for i in list_project_deadlines(db, pid)["items"])
    db.close()


def test_edit_survives_reextract_transfer(client, manager_token):
    from app.app.models.entity import Entity
    from app.app.services.entities import _human_snapshot, _transfer_human

    db, pid, art, ids = _seed(client, manager_token)
    user = type("U", (), {"name": "Менеджер", "id": 1})()
    tb.edit_extracted(db, pid, ids["week"], {"status": "done"}, user=user)
    snap = _human_snapshot(db, art)
    assert [s["id"] for s in snap] == [ids["week"]] and snap[0]["human_review"] is None
    new = Entity(artifact_id=art.id, entity_type="task", value_json=json.dumps({"description": "Настроить обмен заказами клиентов"}, ensure_ascii=False))
    db.add(new)
    db.flush()
    moved, missing = _transfer_human([new], snap)
    assert moved == 1 and missing == []
    value = json.loads(new.value_json)
    assert value["human_edit"]["status"] == "done" and value["human_edit"]["transferred_from"] == ids["week"]
    assert "human_review" not in value and new.needs_review is not False
    db.close()


def test_routes_roles(client, manager_token, user_token, admin_token):
    db, pid, _art, ids = _seed(client, manager_token)
    db.close()
    hm, hu = {"Authorization": f"Bearer {manager_token}"}, {"Authorization": f"Bearer {user_token}"}
    lst = client.get(f"/projects/{pid}/tasks?view=all", headers=hm)
    assert lst.status_code == 200 and lst.json()["can_edit"] is True and lst.json()["counts"]["all"] == 4
    assert client.get(f"/projects/{pid}/tasks?view=bogus", headers=hm).status_code == 422
    assert client.patch(f"/projects/{pid}/tasks/{ids['week']}", json={"status": "done"}, headers=hu).status_code in (403, 404)
    assert client.patch(f"/projects/{pid}/tasks/{ids['week']}", json={"status": "maybe"}, headers=hm).status_code == 422
    assert client.patch(f"/projects/{pid}/tasks/{ids['week']}", json={"status": "done"}, headers=hm).status_code == 200
    made = client.post(f"/projects/{pid}/tasks", json={"description": "Ручная задача"}, headers=hm).json()
    tid = made["manual_id"]
    assert client.patch(f"/projects/{pid}/manual-tasks/{tid}", json={"assignee": "Анна Петрова"}, headers=hm).status_code == 200
    assert client.delete(f"/projects/{pid}/manual-tasks/{tid}", headers=hu).status_code in (403, 404)
    assert client.delete(f"/projects/{pid}/manual-tasks/{tid}", headers=hm).status_code == 200
    assert client.delete(f"/projects/{pid}/manual-tasks/{tid}", headers=hm).status_code == 404
    other = client.post("/projects", json={"name": "Чужой"}, headers={"Authorization": f"Bearer {admin_token}"}).json()["id"]
    assert client.patch(f"/projects/{other}/tasks/{ids['week']}", json={"status": "done"}, headers={"Authorization": f"Bearer {admin_token}"}).status_code == 422


def test_tz_and_plan_tasks_go_to_reference_tab(client, manager_token):
    """Положения ТЗ и этапы плана — не поручения: своя вкладка, не просрочены, не в «Открытых» (паспорт документа)."""
    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.entity import Entity
    from app.app.services.project_deadlines import list_project_deadlines
    from app.app.services.storage import parsed_dir

    pid = client.post("/projects", json={"name": "ТЗ и план"}, headers={"Authorization": f"Bearer {manager_token}"}).json()["id"]
    settings = get_settings()
    db = SessionLocal()
    arts = {}
    for name, kind in (("tz", "tz"), ("plan", "plan"), ("report", "status_report")):
        tag = uuid.uuid4().hex[:8]
        art = Artifact(artifact_id=f"art_2026-10-01_100000_{tag}", project_id=pid, user_id=1, original_filename=f"{name}.pdf",
                       stored_filename=f"{name}.pdf", file_path="x", file_type="pdf", source_type="document", sha256=tag.ljust(64, "0"), status="done")
        db.add(art)
        db.flush()
        folder = parsed_dir(settings, pid, "document")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{art.artifact_id}_passport.json").write_text(json.dumps({"kind": kind}), encoding="utf-8")
        db.add(Entity(artifact_id=art.id, entity_type="task", value_json=json.dumps(
            {"description": f"Пункт из {name}", "deadline": "01.03.2025", "status": "open"}, ensure_ascii=False)))
        arts[name] = art
    db.commit()
    out = tb.list_project_tasks(db, pid, view="all", today=TODAY)
    by = {t["description"]: t for t in out["items"]}
    assert by["Пункт из tz"]["origin"] == "spec" and by["Пункт из plan"]["origin"] == "plan" and by["Пункт из report"]["origin"] == "document"
    assert out["counts"]["reference"] == 2 and out["counts"]["overdue"] == 1 and out["counts"]["open"] == 1
    assert [t["description"] for t in tb.list_project_tasks(db, pid, view="overdue", today=TODAY)["items"]] == ["Пункт из report"]
    assert by["Пункт из plan"]["origin_label"] == "из плана"
    cal = {i["description"]: i["reference"] for i in list_project_deadlines(db, pid)["items"]}
    assert cal == {"Пункт из tz": True, "Пункт из plan": True, "Пункт из report": False}
    db.close()
