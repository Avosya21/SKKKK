"""Настройки обмена с агентами: контракт не привязан к «Пилоту» и 1С/Битрикс24."""
from __future__ import annotations

import pytest

from app.app.services import agent_settings as st
from app.app.services import contract_text as ct


def test_old_projects_keep_legacy_behavior_new_projects_get_none(client, manager_token):
    from app.app.db import SessionLocal
    from app.app.models.project import Project

    h = {"Authorization": f"Bearer {manager_token}"}
    pid = client.post("/projects", json={"name": "Сайт для клиента"}, headers=h).json()["id"]
    db = SessionLocal()
    p = db.get(Project, pid)
    assert st.get_settings_for(p)["domain_pack"] == "none"
    old = Project(name="Старый", created_by=p.created_by, settings_json="{}")
    assert st.get_settings_for(old)["domain_pack"] == "1c_bitrix24" and st.get_settings_for(old)["side_hints"]["customer"] == ["эталон"]
    db.close()


def test_anchor_packs_none_gives_no_1c_anchors():
    text = "поле в номенклатуре и печатных формах заказа клиента, обмен с Битрикс"
    assert ct.anchors_for(text, st.load_pack_rules("none")) == []
    assert {a["object"] for a in ct.anchors_for(text, st.load_pack_rules("1c_bitrix24"))} >= {"Справочник.Номенклатура", "Документ.ЗаказКлиента"}
    assert ct.anchors_for(text) == ct.anchors_for(text, st.load_pack_rules("1c_bitrix24"))  # без пакета — прежнее поведение


def test_side_hints_from_settings_replace_company_names():
    assert ct.side_from_role("сотрудник Ромашки") is None
    assert ct.side_from_role("сотрудник Ромашки", {"customer": ["ромашк"]}) == "customer"
    assert ct.side_from_role("клиент (компания Пилот)") == "customer"  # общие слова работают без настроек


def test_normalize_validates():
    ok = st.normalize({"participants": [{"name": "Иван", "side": "customer"}], "timezone": "+05:00", "domain_pack": "none", "auto_answer": 1})
    assert ok["participants"] == [{"name": "Иван", "side": "customer"}] and ok["auto_answer"] is True
    for bad in ({"participants": [{"name": "X", "side": "boss"}]}, {"timezone": "МСК"}, {"timezone": "+25:00"}, {"domain_pack": "../etc"}, {"domain_pack": "unknown"}):
        with pytest.raises(st.SettingsError):
            st.normalize(bad)


def test_settings_endpoints_roles(client, manager_token, admin_token):
    h_m = {"Authorization": f"Bearer {manager_token}"}
    h_a = {"Authorization": f"Bearer {admin_token}"}
    pid = client.post("/projects", json={"name": "Настройки"}, headers=h_m).json()["id"]
    got = client.get(f"/projects/{pid}/agent-settings", headers=h_m).json()
    assert got["settings"]["domain_pack"] == "none" and got["can_edit"] is False and {p["name"] for p in got["packs"]} >= {"none", "1c_bitrix24"}
    assert client.put(f"/projects/{pid}/agent-settings", json={"domain_pack": "1c_bitrix24"}, headers=h_m).status_code == 403  # менеджер только читает
    r = client.put(f"/projects/{pid}/agent-settings", json={"domain_pack": "1c_bitrix24", "timezone": "+05:00", "participants": [{"name": "Пётр", "side": "vendor"}]}, headers=h_a)
    assert r.status_code == 200 and r.json()["settings"]["timezone"] == "+05:00"
    assert client.put(f"/projects/{pid}/agent-settings", json={"timezone": "плохо"}, headers=h_a).status_code == 400
    assert client.get(f"/projects/{pid}/agent-settings", headers=h_m).json()["settings"]["participants"] == [{"name": "Пётр", "side": "vendor"}]


def test_contract_follows_project_settings(client, manager_token, tmp_data):
    """Новый проект (пакет none): нет якорей 1С. Настройки меняют якоря, пояс и стороны; контракт пересобирается."""
    from app.app.config import get_settings
    from app.app.models.project import Project
    from app.app.services import contract as ctr
    from app.app.tests.test_contract import _seed

    db, pid, _art, _ents = _seed(client, manager_token, tmp_data)
    settings = get_settings()
    reqs = ctr.build_contract(db, settings, pid).files["requirements"]["items"]
    assert reqs and all(r["code_anchors"] == [] for r in reqs)
    cover = next(r for r in reqs if "Покрытие" in r["statement"])
    assert cover["evidence"][0]["said_at"].endswith("+03:00") and cover["evidence"][0]["side"] == "vendor"

    project = db.get(Project, pid)
    st.save(project, {"domain_pack": "1c_bitrix24", "timezone": "+05:00", "participants": [{"name": "Елена", "side": "customer"}]})
    db.commit()
    ctr._cache.clear()
    after = ctr.get_contract(db, settings, pid).files
    cover2 = next(r for r in after["requirements"]["items"] if "Покрытие" in r["statement"])
    assert cover2["evidence"][0]["said_at"].endswith("+05:00")  # пояс из настроек
    assert cover2["evidence"][0]["side"] == "customer"  # явное указание важнее роли «консультант»
    assert any(r["code_anchors"] for r in after["requirements"]["items"])  # пакет 1С включён
    assert "+05:00" in after["state"]["gaps"]["timezone"]


def test_exchange_can_be_disabled_per_project(client, manager_token, tmp_data):
    import json

    from app.app.mcp.project_protocol import call_tool
    from app.app.config import get_settings
    from app.app.models.project import Project
    from app.app.services.project_mcp_tokens import create_project_mcp_token
    from app.app.tests.test_contract import _seed

    db, pid, art, _ = _seed(client, manager_token, tmp_data)
    tok, _plain = create_project_mcp_token(db, project_id=pid, label="t", user_id=art.user_id, can_submit=True)
    st.save(db.get(Project, pid), {"exchange_enabled": False})
    db.commit()
    out = json.loads(call_tool(db, get_settings(), pid, "submit_question", {"question": "Привет?"}, token=tok)["content"][0]["text"])
    assert out["status"] == "rejected" and "выключен" in out["reason"]


def test_project_page_renders_agent_cards_for_staff(client, manager_token, user_token):
    h = {"Authorization": f"Bearer {manager_token}"}
    pid = client.post("/projects", json={"name": "Страница карточек"}, headers=h).json()["id"]
    r = client.get(f"/projects/{pid}", headers=h)
    assert r.status_code == 200
    for needle in ("px-card-review", "px-card-exchange", "px-card-settings", "px_agent.js", "px_agent.css", "window.PX"):
        assert needle in r.text, needle
