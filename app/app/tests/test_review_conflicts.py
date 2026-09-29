"""Этап 4 автопроверки: конфликты решений (docs/plans/AUTO-REVIEW-PLAN.md)."""
from __future__ import annotations

import json

import pytest

from app.app import db as db_mod
from app.app.config import get_settings


NEW_PLAIN = "[00:05 - 00:15] Елена: Мастер-система номенклатуры — 1С.\n[00:16 - 00:17] Иван: Согласен.\n"
NEW_CANCEL = "[00:05 - 00:15] Елена: Передумали: мастер-система номенклатуры — 1С.\n[00:16 - 00:17] Иван: Согласен.\n"


def _setup(db, settings, *, human_on_older=False, new_final=NEW_PLAIN, older_extra=None, newer_text="Мастер-система номенклатуры — 1С"):
    from app.app.models.artifact import Artifact
    from app.app.models.entity import Entity, Link
    from app.app.models.project import Project
    from app.app.models.user import User
    from app.app.services.storage import create_project_tree, parsed_dir

    user = db.query(User).first()
    project = Project(name="Conf", created_by=user.id, is_active=True)
    db.add(project)
    db.flush()
    create_project_tree(settings, project.id)
    folder = parsed_dir(settings, project.id, "call")
    folder.mkdir(parents=True, exist_ok=True)

    def art(aid, name, final):
        row = Artifact(artifact_id=aid, project_id=project.id, user_id=user.id, original_filename=name, stored_filename=f"{aid}.webm",
                       file_path="/x", file_type="audio", source_type="call", sha256=aid, status="done")
        db.add(row)
        db.flush()
        (folder / f"{aid}_final.txt").write_text(final, encoding="utf-8")
        return row

    old_art = art("art_c_old", "созвон 19.08.2026.webm", "[00:10 - 00:20] Елена: Мастер-система номенклатуры — Битрикс.\n[00:21 - 00:22] Иван: Да.\n")
    new_art = art("art_c_new", "созвон 21.09.2026.webm", new_final)
    older_value = {"text": "Мастер-система номенклатуры — Битрикс", "start_sec": 10.0, "speaker": "Елена"}
    if human_on_older:
        older_value["human_review"] = {"by": "Admin", "action": "confirm"}
    older_value.update(older_extra or {})
    older = Entity(artifact_id=old_art.id, entity_type="decision", value_json=json.dumps(older_value, ensure_ascii=False), is_active=True, needs_review=True)
    newer = Entity(artifact_id=new_art.id, entity_type="decision", value_json=json.dumps({"text": newer_text, "start_sec": 5.0}, ensure_ascii=False), is_active=True, needs_review=True)
    db.add_all([older, newer])
    db.flush()
    link = Link(project_id=project.id, source_entity_id=newer.id, target_entity_id=older.id, link_type="POTENTIAL_CONFLICT", similarity_score=0.9, is_automatic=True)
    db.add(link)
    db.commit()
    return project, new_art, older.id, newer.id, link.id


@pytest.fixture
def llm(monkeypatch):
    state = {"reply": {"relation": "contradicts", "which": None, "confidence": 0.9, "reason": "B: 1С вместо Битрикс"}, "users": [], "held": []}

    def fake(settings, *, system, user, **_k):
        state["held"].append(db_mod.thread_held_connections())
        state["users"].append(user)
        return json.dumps(state["reply"], ensure_ascii=False)

    monkeypatch.setattr("app.app.services.llm.chat_completion_no_think", fake)
    monkeypatch.setattr("app.app.services.gpu_phase.prepare_for_llm", lambda *_a, **_k: None)
    return state


def _state(db, older_id, newer_id, link_id):
    from sqlalchemy import select

    from app.app.models.entity import Entity, Link, Resolution

    db.expire_all()
    return (db.get(Entity, older_id), db.get(Entity, newer_id), db.get(Link, link_id),
            db.scalar(select(Resolution).where(Resolution.link_id == link_id)))


def test_live_newer_wins_and_undo(client, admin_token, tmp_data, llm):
    from app.app.services.review_conflicts import review_artifact_conflicts

    settings = get_settings()
    db = db_mod.SessionLocal()
    try:
        baseline = len(db_mod.thread_held_connections())
        project, new_art, older_id, newer_id, link_id = _setup(db, settings)
        stats = review_artifact_conflicts(db, settings, new_art, "live")
        assert stats["newer_wins"] == 1 and stats["llm_calls"] == 1
        # не больше, чем до начала: чужие сессии прошлых тестов могут закрыться сборщиком мусора в любой момент
        assert all(len(h) <= baseline for h in llm["held"]), "держали соединение БД во время ответа модели"
        prompt = llm["users"][0]
        assert prompt.index("Битрикс") < prompt.index("Пункт B")  # A — раньше (19.08), B — позже (21.09)
        assert "Вес источника: высказывание" in prompt
        older, newer, link, res = _state(db, older_id, newer_id, link_id)
        assert res.status == "model_resolved" and res.resolved_by is None and res.winning_entity_id == newer_id
        assert older.is_active is False and newer.is_active is True
        assert json.loads(older.value_json)["model_review"]["conflict_link"] == link_id
        pid = project.id
    finally:
        db.close()
    headers = {"Authorization": f"Bearer {admin_token}"}
    assert [c["link_id"] for c in client.get(f"/projects/{pid}/conflicts?view=model", headers=headers).json()] == [link_id]
    assert client.get(f"/projects/{pid}/conflicts", headers=headers).json() == []
    r = client.post(f"/conflicts/{link_id}/undo-model", headers=headers)
    assert r.status_code == 200 and r.json()["restored_entity_id"] == older_id
    pending = client.get(f"/projects/{pid}/conflicts", headers=headers).json()
    assert [c["link_id"] for c in pending] == [link_id] and pending[0]["model_review"]["undone"]
    db = db_mod.SessionLocal()
    try:
        older, _newer, _link, res = _state(db, older_id, newer_id, link_id)
        assert older.is_active is True and res is None
        older.is_active = False  # сторону выключили иначе (дубль, устарело по теме) — спорить не с чем
        db.commit()
    finally:
        db.close()
    assert client.get(f"/projects/{pid}/conflicts", headers=headers).json() == []


def test_shadow_only_notes_verdict(client, admin_token, tmp_data, llm):
    from app.app.services.review_conflicts import review_artifact_conflicts

    settings = get_settings()
    db = db_mod.SessionLocal()
    try:
        _project, new_art, older_id, newer_id, link_id = _setup(db, settings)
        review_artifact_conflicts(db, settings, new_art, "shadow")
        older, _newer, link, res = _state(db, older_id, newer_id, link_id)
        assert res is None and older.is_active is True
        assert json.loads(link.model_review)["verdict"] == "newer_wins"
        assert review_artifact_conflicts(db, settings, new_art, "shadow")["total"] == 0  # та же версия промпта — повторно не спрашиваем
    finally:
        db.close()


@pytest.mark.parametrize("reply,expected", [
    ({"relation": "contradicts", "which": None, "confidence": 0.5, "reason": "?"}, "unsure"),
    ({"relation": "refines", "which": None, "confidence": 0.9, "reason": "B уточняет A"}, "not_conflict"),
    ({"verdict": "not_conflict", "confidence": 0.9, "reason": "ответ старого формата v2"}, "not_conflict"),
])
def test_threshold_and_not_conflict(client, admin_token, tmp_data, llm, reply, expected):
    from app.app.services.review_conflicts import review_artifact_conflicts

    llm["reply"] = reply
    settings = get_settings()
    db = db_mod.SessionLocal()
    try:
        _project, new_art, older_id, newer_id, link_id = _setup(db, settings)
        stats = review_artifact_conflicts(db, settings, new_art, "live")
        assert stats[expected] == 1
        older, newer, _link, res = _state(db, older_id, newer_id, link_id)
        assert older.is_active and newer.is_active
        if expected == "not_conflict":
            assert res.status == "model_dismissed"
        else:
            assert res is None
    finally:
        db.close()


def test_human_confirmed_side_is_not_overridden(client, admin_token, tmp_data, llm):
    from app.app.services.review_conflicts import review_artifact_conflicts

    settings = get_settings()
    db = db_mod.SessionLocal()
    try:
        _project, new_art, older_id, newer_id, link_id = _setup(db, settings, human_on_older=True)
        stats = review_artifact_conflicts(db, settings, new_art, "live")
        assert stats["unsure"] == 1
        older, _newer, _link, res = _state(db, older_id, newer_id, link_id)
        assert older.is_active is True and res is None
    finally:
        db.close()


def test_winner_without_source_support_is_unsure(client, admin_token, tmp_data, llm):
    """Замер 2026-09-24: «24 слайда» из «видеокарта 24 ГБ» — неверно извлечённый пункт не может выключить верный."""
    from app.app.models.entity import Entity
    from app.app.services.review_conflicts import review_artifact_conflicts

    settings = get_settings()
    db = db_mod.SessionLocal()
    try:
        _project, new_art, older_id, newer_id, link_id = _setup(db, settings)
        ent = db.get(Entity, newer_id)
        ent.value_json = json.dumps({"text": "Количество слайдов презентации двадцать четыре", "start_sec": 5.0}, ensure_ascii=False)
        db.commit()
        stats = review_artifact_conflicts(db, settings, new_art, "live")
        assert stats["unsure"] == 1
        older, _newer, link, res = _state(db, older_id, newer_id, link_id)
        assert older.is_active is True and res is None
        assert "не подтверждает" in json.loads(link.model_review)["reason"]
    finally:
        db.close()


def test_participants_conflict_closed_by_rule_without_llm(client, admin_token, tmp_data, llm):
    from app.app.models.entity import Entity
    from app.app.services.review_conflicts import review_artifact_conflicts

    settings = get_settings()
    db = db_mod.SessionLocal()
    try:
        _project, new_art, older_id, newer_id, link_id = _setup(db, settings)
        for ent_id, name in ((older_id, "Компания-2"), (newer_id, "Компания-7")):
            ent = db.get(Entity, ent_id)
            ent.entity_type, ent.value_json = "participant", json.dumps({"name": name, "role": None}, ensure_ascii=False)
        db.commit()
        stats = review_artifact_conflicts(db, settings, new_art, "live")
        assert stats["rule_not_material"] == 1 and stats["llm_calls"] == 0 and llm["users"] == []
        older, newer, link, res = _state(db, older_id, newer_id, link_id)
        assert res.status == "model_dismissed" and older.is_active and newer.is_active
        assert json.loads(link.model_review)["model"] == "rule"
    finally:
        db.close()


def test_requirement_undo_releases_conflict(client, admin_token, tmp_data, llm):
    from app.app.services.review_conflicts import review_artifact_conflicts

    settings = get_settings()
    db = db_mod.SessionLocal()
    try:
        project, new_art, older_id, newer_id, link_id = _setup(db, settings)
        review_artifact_conflicts(db, settings, new_art, "live")
        pid = project.id
    finally:
        db.close()
    r = client.post(f"/projects/{pid}/requirements/entity/{older_id}/undo-model", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200, r.text
    db = db_mod.SessionLocal()
    try:
        older, _newer, link, res = _state(db, older_id, newer_id, link_id)
        assert older.is_active is True and res is None and json.loads(link.model_review)["undone"]
    finally:
        db.close()


# --- CONFLICT-AUTONOMY-PLAN: правила Р1, промпт v3, сводка важного, автооткат ---


def test_explicit_cancel_decided_by_rule_without_llm(client, admin_token, tmp_data, llm):
    from app.app.services.review_conflicts import review_artifact_conflicts

    settings = get_settings()
    db = db_mod.SessionLocal()
    try:
        _project, new_art, older_id, newer_id, link_id = _setup(db, settings, new_final=NEW_CANCEL)
        stats = review_artifact_conflicts(db, settings, new_art, "live")
        assert stats["rule_explicit_cancel"] == 1 and stats["llm_calls"] == 0 and llm["users"] == []
        older, newer, link, res = _state(db, older_id, newer_id, link_id)
        assert res.winning_entity_id == newer_id and older.is_active is False and newer.is_active
        review = json.loads(link.model_review)
        assert review["rule"] == "rule:explicit_cancel" and review["model"] == "rule:explicit_cancel"
        assert res.reason.startswith("правило explicit_cancel")
    finally:
        db.close()


AGREED = {"decided_by": [{"name": "Иван", "turn": 2}]}


@pytest.mark.parametrize("reply,older_extra,expected,reject_kind", [
    # противоречие без отмены: утверждённое раньше сильнее простого высказывания позже
    ({"relation": "contradicts", "which": None, "confidence": 0.9, "reason": "разные системы"}, AGREED, "older_wins", "superseded"),
    # явная отмена из более слабого источника не выключает утверждённое решение
    ({"relation": "replaces", "which": "B", "confidence": 0.9, "reason": "B меняет"}, AGREED, "unsure", None),
    # «раннее отменяет позднее» — ошибка модели, не решение
    ({"relation": "replaces", "which": "A", "confidence": 0.9, "reason": "A меняет"}, None, "unsure", None),
    ({"relation": "replaces", "which": "B", "confidence": 0.9, "reason": "B меняет"}, None, "newer_wins", "superseded"),
    ({"relation": "not_decision", "which": "B", "confidence": 0.9, "reason": "B — только вопрос"}, None, "older_wins", "not_decision"),
    ({"relation": "different_objects", "which": None, "confidence": 0.9, "reason": "разные справочники"}, None, "not_conflict", None),
    # модель считает B извлечённым неверно, но источник B подтверждает — не верим
    ({"relation": "misextracted", "which": "B", "confidence": 0.9, "reason": "?"}, None, "unsure", None),
])
def test_v3_relation_to_verdict_by_rules(client, admin_token, tmp_data, llm, reply, older_extra, expected, reject_kind):
    from app.app.models.entity import Entity
    from app.app.services.review_conflicts import review_artifact_conflicts

    llm["reply"] = reply
    settings = get_settings()
    db = db_mod.SessionLocal()
    try:
        _project, new_art, older_id, newer_id, link_id = _setup(db, settings, older_extra=older_extra)
        stats = review_artifact_conflicts(db, settings, new_art, "live")
        assert stats[expected] == 1 and stats["llm_calls"] == 1
        older, newer, link, res = _state(db, older_id, newer_id, link_id)
        review = json.loads(link.model_review)
        assert review["relation"] == reply["relation"] and review["reject_kind"] == reject_kind
        if expected in ("newer_wins", "older_wins"):
            loser = older if expected == "newer_wins" else newer
            assert loser.is_active is False
            assert json.loads(db.get(Entity, loser.id).value_json)["model_review"]["reject_kind"] == reject_kind
        else:
            assert older.is_active and newer.is_active
    finally:
        db.close()


def test_misextracted_side_without_source_support_loses(client, admin_token, tmp_data, llm):
    from app.app.models.entity import Entity
    from app.app.services.review_conflicts import review_artifact_conflicts

    llm["reply"] = {"relation": "misextracted", "which": "B", "confidence": 0.9, "reason": "в источнике B о другом"}
    settings = get_settings()
    db = db_mod.SessionLocal()
    try:
        _project, new_art, older_id, newer_id, link_id = _setup(db, settings, newer_text="Количество слайдов презентации двадцать четыре")
        stats = review_artifact_conflicts(db, settings, new_art, "live")
        assert stats["older_wins"] == 1
        older, newer, _link, res = _state(db, older_id, newer_id, link_id)
        assert older.is_active and newer.is_active is False and res.winning_entity_id == older_id
        assert json.loads(db.get(Entity, newer_id).value_json)["model_review"]["reject_kind"] == "not_said"
    finally:
        db.close()


def test_money_conflict_goes_to_important_summary(client, admin_token, tmp_data, llm):
    from app.app.services.review_conflicts import review_artifact_conflicts

    settings = get_settings()
    db = db_mod.SessionLocal()
    try:
        new_final = "[00:05 - 00:15] Елена: Бюджет внедрения — 4 млн.\n[00:16 - 00:17] Иван: Согласен.\n"
        project, new_art, older_id, newer_id, link_id = _setup(db, settings, new_final=new_final, newer_text="Бюджет внедрения — 4 млн")
        review_artifact_conflicts(db, settings, new_art, "live")
        pid = project.id
    finally:
        db.close()
    headers = {"Authorization": f"Bearer {admin_token}"}
    important = client.get(f"/projects/{pid}/conflicts?view=important", headers=headers).json()
    assert [c["link_id"] for c in important] == [link_id] and important[0]["model_review"]["important"] is True
    assert client.post(f"/conflicts/{link_id}/undo-model", headers=headers).status_code == 200
    assert client.get(f"/projects/{pid}/conflicts?view=important", headers=headers).json() == []


def test_many_undos_roll_project_back_to_shadow(client, admin_token, tmp_data, llm):
    from app.app.models.entity import Link
    from app.app.models.project import Project
    from app.app.services.review_conflicts import (
        UNDO_MIN_DECISIONS,
        _now,
        check_undo_rate,
        effective_mode,
        review_artifact_conflicts,
    )

    settings = get_settings()
    db = db_mod.SessionLocal()
    try:
        project, new_art, older_id, newer_id, _link_id = _setup(db, settings)
        for i in range(UNDO_MIN_DECISIONS):
            review = {"verdict": "newer_wins", "mode": "live", "at": _now()}
            if i == 0:
                review["undone"] = {"by": "Admin", "at": _now()}
            db.add(Link(project_id=project.id, source_entity_id=newer_id, target_entity_id=older_id, link_type="POTENTIAL_CONFLICT",
                        is_automatic=True, model_review=json.dumps(review)))
        db.commit()
        assert check_undo_rate(db, project.id) is True  # 1 из 10 = 10 % > 5 %
        db.commit()
        db.expire_all()
        project = db.get(Project, project.id)
        assert project.settings()["conflict_review_mode"] == "shadow"
        assert effective_mode(project, "live") == "shadow"
        stats = review_artifact_conflicts(db, settings, new_art, "live")
        assert stats["newer_wins"] >= 1
        older, _newer, _link, res = _state(db, older_id, newer_id, _link_id)
        assert older.is_active is True and res is None  # в тени — без последствий
        assert check_undo_rate(db, project.id) is False  # уже в тени — повторно не переключаем
    finally:
        db.close()