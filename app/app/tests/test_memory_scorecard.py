"""Табло памяти решений и чек-лист приёмки (PLATFORM-FOUNDATION-PLAN, ПФ10). Все имена вымышленные."""
from __future__ import annotations

import csv

import pytest

from app.app.services import memory_scorecard as sc
from app.app.services import people


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def env(db_engine, tmp_data):
    """База, каталог эталона во временном DATA_DIR и проект с автором-админом."""
    import app.app.db as db_mod
    from app.app.config import get_settings
    from app.app.models.project import Project
    from app.app.models.user import User

    db = db_mod.SessionLocal()
    admin = User(name="score-admin", telegram_id="tg-score", role="admin", password_hash="x")
    db.add(admin)
    db.commit()
    project = Project(name="Табло", created_by=admin.id, is_active=True)
    db.add(project)
    db.commit()
    settings = get_settings()
    (settings.data_dir / "eval").mkdir(parents=True, exist_ok=True)
    yield db, settings, project.id
    db.close()


def _write_gold(settings, rows):
    path = settings.data_dir / "eval" / "memory_gold.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["предмет", "название", "дата", "источник", "автор", "сторона", "акт", "цитата", "цитата найдена кодом", "верно"])
        for r in rows:
            w.writerow(r)
        w.writerow(["S1", "т", "", "", "", "", "ИТОГ", "итог", "", ""])  # строки «ИТОГ» событиями не считаются


def test_gold_and_people_sheet_parsing(env):
    db, settings, pid = env
    _write_gold(settings, [["S1", "т", "2026-01-01", "a1:M5", "Олег Образцов", "", "просит", "ц", "да", ""]])
    assert [e["автор"] for e in sc.gold_events(settings)] == ["Олег Образцов"]
    (settings.data_dir / "eval" / "memory_people.csv").write_text(
        "человек;сторона;компания;написания\nОлег Образцов;customer;ООО Пример;Олег|Oleg O.\nПётр Мусорный;неизвестно;;\n", encoding="utf-8-sig")
    sheet = sc.people_sheet(settings)
    assert sheet == {"олег образцов": "customer", "олег": "customer", "oleg o": "customer"}  # без стороны строка не берётся


def test_export_people_sheet_from_registry_roundtrip(env):
    db, settings, pid = env
    a = people.create_person(db, pid, "Анна Исполнителева", side="executor", side_source="human", company_name="ИП Образец")
    people.add_alias(db, pid, a.id, "Anna I.")
    people.create_person(db, pid, "Неясный Человек")  # сторона неизвестна — в лист не попадает
    db.commit()
    assert sc.export_people_sheet(db, settings, pid) == 1
    assert sc.people_sheet(settings) == {"анна исполнителева": "executor", "anna i": "executor"}


def test_m7_counts_authors_found_with_known_side(env):
    db, settings, pid = env
    people.create_person(db, pid, "Олег Образцов", side="customer", side_source="human")
    people.create_person(db, pid, "Лена Безстороны")
    db.commit()
    gold = [{"автор": "Олег Образцов"}, {"автор": "Лена Безстороны"}, {"автор": "Совсем Чужой"}, {"автор": "Олег Образцов"}]
    value, missing = sc._m7(db, pid, gold)
    assert round(value, 2) == 0.33 and missing == ["Лена Безстороны", "Совсем Чужой"]
    assert sc._m7(db, pid, []) == (None, [])


def test_m8_compares_contract_side_with_sheet(env, monkeypatch):
    from app.app.models.artifact import Artifact

    db, settings, pid = env
    art = Artifact(artifact_id="art_m8", project_id=pid, user_id=1, original_filename="чат.html", stored_filename="x", file_path="/x",
                   file_type="document", source_type="document", sha256="m8", status="done")
    db.add(art)
    db.commit()
    (settings.data_dir / "eval" / "memory_people.csv").write_text("человек;сторона;компания;написания\nОлег Образцов;customer;;\nАнна Исполнителева;executor;;\n", encoding="utf-8-sig")
    gold = [{"источник": f"a{art.id}:M1", "автор": "Олег Образцов"}, {"источник": f"a{art.id}:M2", "автор": "Анна Исполнителева"},
            {"источник": f"a{art.id}:M3", "автор": "Анна Исполнителева"}, {"источник": f"a{art.id}:M4", "автор": "Незнакомец"}]
    monkeypatch.setattr(sc, "_evidence_sides", lambda *_a, **_k: [("art_m8", "M", 1, "customer"), ("art_m8", "M", 2, "customer"), ("art_m8", "M", 3, "vendor")])
    got = sc._m8(db, settings, pid, gold)
    assert got["checked"] == 3 and got["value"] == pytest.approx(2 / 3)  # M4 — автора нет в листе, пропущен
    assert got["wrong"] == [{"source": f"a{art.id}:M2", "author": "Анна Исполнителева", "expected": "vendor", "got": ["customer"]}]
    assert sc._m8(db, settings, pid, [])["value"] is None


def test_board_record_history_and_old_header_migration(env):
    db, settings, pid = env
    board = settings.data_dir / "eval" / "memory_scorecard.csv"
    board.write_text("дата;этап;проект;промпт;модель;M1_цитата;M2_итог;M3_грубо;M2_подробно\n2026-09-30 13:14;E0;18;p-old;m-old;0.99;0.88;0.42;S01 OK\n", encoding="utf-8-sig")
    old = sc.history(settings)  # старый файл переписывается с новыми столбцами, строки сохраняются
    assert old[0]["M1"] == "0.99" and old[0]["M2"] == "0.88" and old[0]["M3"] == "0.42" and old[0]["M7"] == "" and old[0]["M9"] == "" and old[0]["A1"] == "" and old[0]["детали"] == "S01 OK"
    result = sc.run(db, settings, pid, stage="тест")
    rows = sc.history(settings, pid)
    assert len(rows) == 1 and rows[0]["этап"] == "тест" and rows[0]["проект"] == str(pid) and rows[0]["M1"] == "1.00"
    assert len(sc.history(settings)) == 2 and result["row"]["этап"] == "тест"


def test_changed_since_last_run_follows_prompt_and_model(env):
    db, settings, pid = env
    assert sc.changed_since_last_run(settings, pid) is True  # прогонов ещё нет
    sc.run(db, settings, pid, stage="первый")
    assert sc.changed_since_last_run(settings, pid) is False
    assert sc.changed_since_last_run(settings.model_copy(update={"lmstudio_model": "другая-модель"}), pid) is True


def test_checklist_reflects_registry_state(env):
    db, settings, pid = env
    items = {c["id"]: c for c in sc.compute(db, settings, pid)["checklist"]}
    assert items["people"]["status"] == "fail" and items["platform"]["status"] == "fail"  # реестр пуст, флаг выключен
    assert items["m8"]["status"] == "warn" and items["wizard"]["status"] == "manual"
    people.create_person(db, pid, "Олег Образцов", side="customer", side_source="human")
    people.create_person(db, pid, "Лена Безстороны")
    people.note_unknown(db, pid, "chat_author", "Кто-то")
    db.commit()
    items = {c["id"]: c for c in sc.compute(db, settings, pid)["checklist"]}
    assert items["people"]["status"] == "warn" and "без стороны: 1" in items["people"]["detail"] and "«Кто это?»: 1" in items["people"]["detail"]


def test_turn_at_prefers_turn_that_starts_at_boundary():
    from app.app.services.transcript_turns import KIND_SPEECH, Turn, turn_at

    first = Turn(index=19, kind=KIND_SPEECH, speaker="Первый", start_sec=565.0, end_sec=567.0, text="Отлично.", line="")
    second = Turn(index=20, kind=KIND_SPEECH, speaker="Второй", start_sec=567.0, end_sec=585.0, text="Длина листа.", line="")
    assert turn_at([first, second], 567.0).index == 20  # стык: слова следующего говорящего не приписываются предыдущему
    assert turn_at([first, second], 566.0).index == 19 and turn_at([first, second], 570.0).index == 20


# ---------------------------------------------------------------- доступ и страница

@pytest.fixture()
def panel_file(tmp_path, monkeypatch):
    from app.app.config import get_settings

    path = tmp_path / "Panel_users.txt"
    monkeypatch.setenv("PANEL_USERS_PATH", str(path))
    get_settings.cache_clear()
    yield path
    get_settings.cache_clear()


def test_memory_pages_only_for_panel_owner(client, admin_token, manager_token, panel_file):
    pid = client.post("/projects", json={"name": "Для табло"}, headers=_auth(manager_token)).json()["id"]
    for path in ("/admin/memory", "/admin/memory/data", f"/admin/memory/live?project_id={pid}"):
        assert client.get(path, headers=_auth(admin_token)).status_code == 404  # файла списка нет — доступа нет ни у кого
    assert client.post("/admin/memory/run", json={"project_id": pid}, headers=_auth(admin_token)).status_code == 404
    panel_file.write_text("Admin\n", encoding="utf-8")
    assert client.get("/admin/memory", headers=_auth(admin_token)).status_code == 200
    assert client.get("/admin/memory", headers=_auth(manager_token)).status_code == 404
    assert client.get(f"/admin/memory/live?project_id={pid}", headers=_auth(manager_token)).status_code == 404


def test_memory_api_live_run_history_and_auto_run(client, admin_token, panel_file, monkeypatch):
    from app.app.services import entities

    panel_file.write_text("Admin\n", encoding="utf-8")
    h = _auth(admin_token)
    plain = client.post("/projects", json={"name": "Без платформы"}, headers=h).json()["id"]
    on = client.post("/projects", json={"name": "С платформой"}, headers=h).json()["id"]
    client.put(f"/projects/{on}/people-platform", json={"enabled": True}, headers=h)
    client.put(f"/projects/{plain}/people-platform", json={"enabled": False}, headers=h)  # новый проект — с платформой (Р4)

    data = client.get("/admin/memory/data", headers=h).json()
    assert data["project_id"] == on and data["auto_ran"] is True  # выбран проект с платформой; прогонов не было — записан сам
    assert {p["id"]: p["platform"] for p in data["projects"]} == {plain: False, on: True}
    assert len(data["history"]) == 1 and data["history"][0]["этап"].startswith("авто")
    again = client.get(f"/admin/memory/data?project_id={on}", headers=h).json()
    assert again["auto_ran"] is False and len(again["history"]) == 1  # без смены промпта и модели повторно не пишет
    monkeypatch.setattr(entities, "PROMPT_VERSION", "extract_entities-v99")
    assert client.get(f"/admin/memory/data?project_id={on}", headers=h).json()["auto_ran"] is True
    assert client.get(f"/admin/memory/data?project_id={plain}", headers=h).json()["auto_ran"] is False  # без платформы — не считаем сами

    live = client.get(f"/admin/memory/live?project_id={on}", headers=h)
    assert live.status_code == 200
    body = live.json()
    assert set(body["metrics"]) == {"M1", "M2", "M3", "M7", "M8", "M9", "A1", "A2"} and body["pending"] == ["M4", "M5", "M6"]
    assert {c["id"] for c in body["checklist"]} >= {"people", "platform", "m8", "voices", "times", "companies", "wizard", "tests", "scan", "docs"}
    assert len(client.get(f"/admin/memory/data?project_id={on}", headers=h).json()["history"]) == 2  # «live» ничего не записывает
    ran = client.post("/admin/memory/run", json={"project_id": on}, headers=h)
    assert ran.status_code == 200 and ran.json()["row"]["этап"] == "кнопка"
    assert len(client.get(f"/admin/memory/data?project_id={on}", headers=h).json()["history"]) == 3
    assert client.get("/admin/memory/live?project_id=999999", headers=h).status_code == 404


def test_memory_page_markup(client, admin_token, panel_file):
    panel_file.write_text("Admin\n", encoding="utf-8")
    html = client.get("/admin/memory", headers=_auth(admin_token)).text
    assert "Приёмка платформы" in html and "История прогонов" in html and "/admin/memory/live" in html


def test_gold_act_classes_map_human_labels_to_system_acts():
    assert sc.gold_act_classes("предложила") == ("proposed",) and sc.gold_act_classes("Просит") == ("proposed",)
    assert sc.gold_act_classes("согласился с отказом") == ("agreed",) and sc.gold_act_classes("согласился") == ("agreed",)
    assert "rejected" in sc.gold_act_classes("отклонил и предложил другое") and "proposed" in sc.gold_act_classes("отклонил и предложил другое")
    assert sc.gold_act_classes("сообщил о выполненном") == ("done",) and sc.gold_act_classes("сообщила о выполненном") == ("done",)
    assert sc.gold_act_classes("отложила") == ("deferred",) and sc.gold_act_classes("сообщил о препятствии") == ("blocked",)
    assert sc.gold_act_classes("что-то своё") == ()  # неизвестный вид эталона не проверяется


def test_acts_metrics_coverage_and_kind(env):
    from app.app.models.artifact import Artifact
    from app.app.models.speech_act import SpeechAct

    db, settings, pid = env
    art = Artifact(artifact_id="art_a1a2", project_id=pid, user_id=1, original_filename="чат.html", stored_filename="x", file_path="/x",
                   file_type="html", source_type="document", sha256="a1a2", status="done")
    db.add(art)
    db.commit()
    gold = [{"источник": f"a{art.id}:M{i}", "акт": label} for i, label in ((1, "предложила"), (2, "согласился"), (3, "отклонил"), (4, "спросил"))]
    assert sc._acts_metrics(db, pid, gold)["a1"] is None  # актов в проекте ещё нет
    for ref, act in (("M1", "proposed"), ("M2", "agreed"), ("M3", "agreed")):
        db.add(SpeechAct(project_id=pid, artifact_id=art.id, unit_ref=ref, unit_key=int(ref[1:]), act=act, quote="цитата", prompt_version="v", model="m"))
    db.commit()
    got = sc._acts_metrics(db, pid, gold)
    assert got["found"] == 3 and got["total"] == 4 and got["right"] == 2  # M4 — акта нет; M3 — вид не тот
    assert got["a1"] == pytest.approx(0.75) and got["a2"] == pytest.approx(2 / 3)
    assert got["wrong"] == [{"source": f"a{art.id}:M3", "gold": "отклонил", "got": ["agreed"]}]


def test_people_flow_tiles_on_board(client, admin_token, manager_token, panel_file):
    """ЛГ8: блок «Люди и голоса» — только владельцу табло, только числа (без имён), цели плана."""
    from app.app.db import SessionLocal
    from app.app.services import people

    panel_file.write_text("Admin\n", encoding="utf-8")
    h = _auth(admin_token)
    assert client.get("/admin/memory/people", headers=_auth(manager_token)).status_code == 404
    pid = client.post("/projects", json={"name": "Путь людей", "internal": False, "customer_company": "Заказчик Тест"},
                      headers=h).json()["id"]
    db = SessionLocal()
    try:
        people.create_person(db, pid, "Ольга Пример", company_name="Заказчик Тест", side="customer", side_source="human")
        fresh = people.create_person(db, pid, "Ник Пример")
        fresh.needs_review = True
        db.commit()
    finally:
        db.close()
    r = client.get("/admin/memory/people", headers=h)
    assert r.status_code == 200 and "Ольга" not in r.text
    tiles = {t["key"]: t for t in r.json()["tiles"]}
    assert tiles["parties"]["value"] == 1.0 and tiles["parties"]["ok"] is True
    assert tiles["people"]["value"] < 1 and tiles["people"]["ok"] is False  # «Ник Пример» без компании и стороны
    assert tiles["review"]["value"] == 1 and tiles["review"]["ok"] is None
