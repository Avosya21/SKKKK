"""Контракт обмена с агентом анализа: разбор переписки, дословные цитаты, стабильные id, порции, дельта, list_artifacts."""
from __future__ import annotations

import json
import uuid

from app.app.services import contract as ctr
from app.app.services import contract_text as ct

import pytest


@pytest.fixture(autouse=True)
def _fresh_contract_cache():
    ctr._cache.clear()  # номера проектов повторяются между тестами — кэш прошлого теста не должен просачиваться
    yield
    ctr._cache.clear()


CHAT = """**ПИЛОТ | АКМЕ**

**15 сентября 2026**

**13:37 Елена:**
Добавить еще поле "Покрытие".

**13:44 Елена:**
Если все устроит - добавить еще одно поле для покрытия - будет по дооценке (в текущем ТЗ четко фиксировали доработку двух полей), будет в районе 5-8 часов, я думаю.

**13:49 Ilya Abramov:**
Будем делать, потому что это позволяет сократить до 205 строк номенклатуру по профнастилу.

**13:50 Елена:**
Хорошо, тогда согласуем перечень значений.

**13:51 Ilya Abramov:**
Да, пришлю список. Документ: https://docs.google.com/document/d/abc123.

**16 сентября 2026**

**09:00 Елена:**
Доброе утро!
"""


def test_parse_chat_dates_speakers_and_bodies():
    msgs = ct.parse_chat(CHAT)
    assert [m.speaker for m in msgs][:3] == ["Елена", "Елена", "Ilya Abramov"]
    assert msgs[1].at().isoformat() == "2026-09-15T13:44:00"
    assert msgs[-1].at().isoformat() == "2026-09-16T09:00:00"
    assert "5-8 часов" in msgs[1].text and ct.looks_like_chat(CHAT)


def test_quote_is_verbatim_slice_of_source():
    text = ct.parse_chat(CHAT)[1].text
    quote, score = ct.best_quote("Добавление поля «Покрытие» по дооценке 5–8 часов", text)
    assert quote in text and score > 0.4  # срез исходной строки, ничего не переписано


def test_hours_scope_urls_and_anchors():
    assert ct.find_hours("будет в районе 5-8 часов") == (5.0, 8.0)
    assert ct.find_hours("около 3 часов") == (3.0, 3.0)
    assert ct.SCOPE_RE.search("это в текущем ТЗ не было") and not ct.SCOPE_RE.search("не входит в смету")
    assert ct.find_urls(CHAT) == ["https://docs.google.com/document/d/abc123"]
    objs = {a["object"] for a in ct.anchors_for("поле в номенклатуре и печатных формах заказа клиента")}
    assert {"Справочник.Номенклатура", "печатные формы документов", "Документ.ЗаказКлиента"} <= objs


def test_side_from_role_and_slug():
    assert ct.side_from_role("консультант/интегратор") == "vendor"
    assert ct.side_from_role("клиент (компания Пилот)") == "customer"
    assert ct.side_from_role(None) is None
    assert ctr.project_slug("ПИЛОТ (обкатка ИИ)", 18) == "pilot"
    assert ctr.project_slug("!!!", 7) == "p7"


def _seed(client, manager_token, tmp_data):
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.entity import Chunk, Entity

    h = {"Authorization": f"Bearer {manager_token}"}
    pid = client.post("/projects", json={"name": "Пилот контракт"}, headers=h).json()["id"]
    db = SessionLocal()
    tag = uuid.uuid4().hex[:8]
    art = Artifact(artifact_id=f"art_2026-09-17_123428_{tag}", project_id=pid, user_id=1, original_filename="messages.html",
                   stored_filename="m.html", file_path="m.html", file_type="html", source_type="document", sha256=tag.ljust(64, "0"), status="done")
    db.add(art)
    db.flush()
    parsed = tmp_data / "projects" / str(pid) / "parsed" / "documents"
    parsed.mkdir(parents=True, exist_ok=True)
    (parsed / f"{art.artifact_id}_clean.txt").write_text(CHAT, encoding="utf-8")
    db.add(Chunk(artifact_id=art.id, project_id=pid, chunk_index=0, text=CHAT, speaker=None, token_estimate=100))
    db.add(Entity(artifact_id=art.id, entity_type="participant", value_json=json.dumps({"name": "Елена Соколова", "role": "консультант/интегратор"}, ensure_ascii=False)))
    ents = []
    for stmt in ("Добавление поля «Покрытие» по дооценке 5–8 часов, после тестирования функционала",
                 "Сократить номенклатуру по профнастилу до 205 строк"):
        e = Entity(artifact_id=art.id, entity_type="decision", value_json=json.dumps({"text": stmt}, ensure_ascii=False), confidence=0.6)
        db.add(e)
        ents.append(e)
    db.commit()
    return db, pid, art, ents


def test_contract_evidence_sides_estimate_and_stable_ids(client, manager_token, tmp_data):
    from app.app.config import get_settings
    from app.app.models.entity import Entity

    db, pid, art, ents = _seed(client, manager_token, tmp_data)
    settings = get_settings()
    first = ctr.build_contract(db, settings, pid).files
    reqs = {r["statement"]: r for r in first["requirements"]["items"]}
    cover = next(r for s, r in reqs.items() if "Покрытие" in s)
    ev = cover["evidence"][0]
    assert ev["speaker"] == "Елена" and ev["side"] == "vendor" and ev["said_at"] == "2026-09-15T13:44:00+03:00"
    assert ev["quote"] in CHAT and ev["chunk_id"]  # цитата дословная и привязана к фрагменту
    assert cover["estimate"]["hours_min"] == 5 and cover["estimate"]["hours_max"] == 8
    assert cover["verification"] == "extracted" and cover["in_current_tz"] is False
    other = next(r for s, r in reqs.items() if "205" in s)
    assert other["evidence"][0]["side"] == "customer"  # двое в переписке: второй — заказчик
    assert first["conflicts"]["total"] == 0  # ТЗ проекта нет — правило «вне ТЗ» молчит, а не выдумывает
    reg = first["artifacts"]["items"][0]
    assert reg["type"] == "chat" and reg["occurred_at"] == "2026-09-16T09:00:00+03:00"
    assert reg["external_links"] == ["https://docs.google.com/document/d/abc123"]
    assert first["state"]["counts"]["requirements"] == 2 and "open_questions" in first["state"]["gaps"]

    # переизвлечение: старые сущности стёрты, созданы новые с другими числовыми id — id требований прежние
    old_ids = {r["statement"]: r["id"] for r in first["requirements"]["items"]}
    for e in db.query(Entity).filter(Entity.entity_type == "decision", Entity.artifact_id == art.id).all():
        db.delete(e)
    db.commit()
    for stmt in old_ids:
        db.add(Entity(artifact_id=art.id, entity_type="decision", value_json=json.dumps({"text": stmt}, ensure_ascii=False), confidence=0.7))
    db.commit()
    again = {r["statement"]: r["id"] for r in ctr.build_contract(db, settings, pid).files["requirements"]["items"]}
    assert again == old_ids


def test_scope_conflict_when_tz_exists_and_paging(client, manager_token, tmp_data):
    from app.app.config import get_settings
    from app.app.services.tz import tz_paths

    db, pid, art, _ = _seed(client, manager_token, tmp_data)
    settings = get_settings()
    tz_path, _v = tz_paths(settings, pid)
    tz_path.write_text("## Объём\n- Доработка двух полей в печатных формах заказа\n", encoding="utf-8")
    files = ctr.build_contract(db, settings, pid).files
    conflict = files["conflicts"]["items"][0]
    assert conflict["kind"] == "scope" and conflict["left"]["speaker"] == "Елена" and conflict["left"]["quote"] in CHAT
    assert conflict["severity"] == "high" and files["tz"]["sections"][0]["points"]
    # порции: срез укладывается в лимит, курсор доходит до конца
    page = ctr.paginate(files["requirements"], limit=1)
    assert len(page["items"]) == 1 and page["cursor"] == "1" and page["total"] == 2
    tiny = ctr.paginate({"items": [{"x": "я" * 400} for _ in range(50)]}, max_bytes=3000)
    assert 1 <= len(tiny["items"]) < 50 and tiny["cursor"]


def test_mcp_list_artifacts_no_longer_crashes_and_serves_contract(client, manager_token, tmp_data):
    from app.app.config import get_settings
    from app.app.services import project_mcp_contract as tools
    from app.app.services import project_mcp_data as data

    db, pid, art, _ = _seed(client, manager_token, tmp_data)
    settings = get_settings()
    rows = data.mcp_list_artifacts(db, pid, settings=settings)  # раньше: AttributeError: 'Artifact' has no attribute 'created_at'
    real = next(r for r in rows if r["artifact_id"] == art.artifact_id)
    assert real["type"] == "chat" and real["occurred_at"] and real["ingested_at"]
    only_contract = data.mcp_list_artifacts(db, pid, settings=settings, prefix="contract/")
    assert {r["filename"] for r in only_contract} == {f"contract/{n}.json" for n in ctr.FILES} | {"contract/GUIDE.md"}
    body = data.mcp_get_artifact_content(db, settings, pid, artifact_id="contract/requirements.json", limit=1)
    assert body["content"]["items"] and body["content"]["cursor"] == "1"
    ents = data.mcp_list_entities(db, pid, entity_type="decision")
    assert ents and ents[0]["verification"] == "extracted"
    req = ctr.get_contract(db, settings, pid).files["requirements"]["items"][0]
    assert tools.get_quote(db, settings, pid, requirement_id=req["id"])["evidence"]
    assert tools.get_artifact_meta(db, settings, pid, artifact_id=art.artifact_id)["participants"]
    assert tools.get_code_anchors(db, settings, pid, requirement_ids=[req["id"]])["total"] == 1


def test_get_changes_added_and_removed(client, manager_token, tmp_data):
    from app.app.config import get_settings
    from app.app.models.entity import Entity
    from app.app.services import project_mcp_contract as tools

    db, pid, art, ents = _seed(client, manager_token, tmp_data)
    settings = get_settings()
    first = tools.get_changes(db, settings, pid)
    assert first["total"] >= 2 and all(c["change"] == "added" for c in first["items"] if c["id"].startswith("req-"))
    assert tools.get_changes(db, settings, pid, since="2999-01-01T00:00:00+00:00")["total"] == 0
    db.delete(db.get(Entity, ents[1].id))
    db.commit()
    ctr._cache.clear()
    after = tools.get_changes(db, settings, pid, since="2000-01-01T00:00:00+00:00")
    assert any(c["change"] == "removed" for c in after["items"])


def test_call_evidence_who_and_when_from_bound_turn(client, manager_token, tmp_data):
    """REQUIREMENTS-PLAN А9: кто сказал и кто утвердил — из привязки пункта к реплике; дата неизвестна — не дата загрузки."""
    from datetime import datetime

    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.entity import Chunk, Entity

    h = {"Authorization": f"Bearer {manager_token}"}
    pid = client.post("/projects", json={"name": "Кто и когда"}, headers=h).json()["id"]
    db = SessionLocal()
    tag = uuid.uuid4().hex[:8]
    final = ("[00:10 - 00:20] Елена: Мастер-система номенклатуры — Битрикс, так удобнее менеджерам.\n"
             "[12:34 - 12:50] Елена: Мастер-система номенклатуры будет 1С, справочник ведём там.\n"
             "[12:51 - 12:55] Павел: Да, согласен, так и делаем.\n")
    art = Artifact(artifact_id=f"art_2026-09-01_000000_{tag}", project_id=pid, user_id=1, original_filename="созвон.webm",
                   stored_filename="c.webm", file_path="c.webm", file_type="audio", source_type="call", sha256=tag.ljust(64, "1"),
                   status="done", upload_date=datetime(2026, 8, 27, 10, 0))
    db.add(art)
    db.flush()
    calls = tmp_data / "projects" / str(pid) / "parsed" / "calls"
    calls.mkdir(parents=True, exist_ok=True)
    (calls / f"{art.artifact_id}_final.txt").write_text(final, encoding="utf-8")
    db.add(Chunk(artifact_id=art.id, project_id=pid, chunk_index=0, text=final, speaker=None, token_estimate=50, start_sec=0.0, end_sec=900.0))
    bound = {"text": "Мастер-система номенклатуры — 1С", "speaker": "Елена", "speaker_source": "voice", "start_sec": 754.0,
             "decided_by": [{"name": "Павел", "start_sec": 771.0}]}
    db.add(Entity(artifact_id=art.id, entity_type="decision", value_json=json.dumps(bound, ensure_ascii=False), confidence=0.7))
    db.add(Entity(artifact_id=art.id, entity_type="task", value_json=json.dumps({"text": "Вести справочник номенклатуры в 1С"}, ensure_ascii=False), confidence=0.6))
    db.commit()
    files = ctr.build_contract(db, get_settings(), pid).files
    db.close()
    reqs = {r["statement"]: r for r in files["requirements"]["items"]}
    ev = reqs["Мастер-система номенклатуры — 1С"]["evidence"][0] if "Мастер-система номенклатуры — 1С" in reqs else None
    decs = {d["statement"]: d for d in files["decisions"]["items"]} if "decisions" in files else {}
    ev = ev or decs["Мастер-система номенклатуры — 1С"]["evidence"][0]
    assert ev["speaker"] == "Елена" and ev["speaker_basis"] == "voice" and ev["timecode"] == "12:34"
    assert ev["agreed_by"] == [{"name": "Павел", "side": "unknown", "timecode": "12:51"}]
    assert "1С" in ev["quote"] and ev["material"] == "созвон.webm"
    assert ev["said_at"] is None and ev["said_date"] is None and ev["date_basis"] == "unknown"  # дата загрузки 27.08 — не дата записи
    task_ev = reqs["Вести справочник номенклатуры в 1С"]["evidence"]
    assert task_ev and task_ev[0]["speaker_basis"] == "words"  # не привязан к реплике: говорящий по совпадению слов