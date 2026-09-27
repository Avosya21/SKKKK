"""CONFLICT-AUTONOMY-PLAN: ошибки графа О1–О5, правила Р1, хронология противоречий для чата/ТЗ (Р4)."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from app.app import db as db_mod
from app.app.config import get_settings


def _project(db, name="Auto"):
    from app.app.models.project import Project
    from app.app.models.user import User

    user = db.query(User).first()
    project = Project(name=name, created_by=user.id, is_active=True)
    db.add(project)
    db.flush()
    return project, user


def _call(db, settings, project, user, aid, name, final):
    from app.app.models.artifact import Artifact
    from app.app.services.storage import create_project_tree, parsed_dir

    create_project_tree(settings, project.id)
    folder = parsed_dir(settings, project.id, "call")
    folder.mkdir(parents=True, exist_ok=True)
    row = Artifact(artifact_id=aid, project_id=project.id, user_id=user.id, original_filename=name, stored_filename=f"{aid}.webm",
                   file_path="/x", file_type="audio", source_type="call", sha256=aid, status="done")
    db.add(row)
    db.flush()
    (folder / f"{aid}_final.txt").write_text(final, encoding="utf-8")
    return row


def _decision(db, art, text, start, **extra):
    from app.app.models.entity import Entity

    ent = Entity(artifact_id=art.id, entity_type="decision", value_json=json.dumps({"text": text, "start_sec": start, **extra}, ensure_ascii=False),
                 is_active=True, needs_review=True)
    db.add(ent)
    db.flush()
    return ent


def _link(db, project, src, tgt):
    from app.app.models.entity import Link

    link = Link(project_id=project.id, source_entity_id=src.id, target_entity_id=tgt.id, link_type="POTENTIAL_CONFLICT",
                similarity_score=0.9, is_automatic=True)
    db.add(link)
    db.flush()
    return link


def _res(db, link_id):
    from sqlalchemy import select

    from app.app.models.entity import Resolution

    return db.scalar(select(Resolution).where(Resolution.link_id == link_id))


OLD = "[00:10 - 00:20] Елена: Мастер-система номенклатуры — Битрикс.\n[00:21 - 00:22] Иван: Да.\n"
CANCEL = "[00:05 - 00:15] Елена: Передумали: мастер-система номенклатуры — 1С.\n[00:16 - 00:17] Иван: Согласен.\n"


def test_cancel_phrase_does_not_touch_other_artifacts_conflicts(client, admin_token, tmp_data):
    """О1: «отменяем» в артефакте B раньше закрывало конфликт C↔D всего проекта."""
    from app.app.services.graph import _try_auto_resolve

    settings = get_settings()
    db = db_mod.SessionLocal()
    try:
        project, user = _project(db)
        c = _call(db, settings, project, user, "art_o1_c", "созвон 19.08.2026.webm", OLD)
        d = _call(db, settings, project, user, "art_o1_d", "созвон 20.08.2026.webm", "[00:05 - 00:15] Елена: Мастер-система номенклатуры — 1С.\n")
        b = _call(db, settings, project, user, "art_o1_b", "созвон 21.09.2026.webm", "[00:01 - 00:05] Елена: Отменяем прежний отчёт.\n")
        link = _link(db, project, _decision(db, d, "Мастер-система номенклатуры — 1С", 5.0), _decision(db, c, "Мастер-система номенклатуры — Битрикс", 10.0))
        db.commit()
        _try_auto_resolve(db, settings, b, "Отменяем прежний отчёт.")
        db.commit()
        assert _res(db, link.id) is None
    finally:
        db.close()


def test_cancel_rule_picks_newer_by_date_not_link_direction(client, admin_token, tmp_data):
    """О2: при пересборке связь бывает «старый → новый»; побеждать должен более новый по дате материала."""
    from app.app.models.entity import Entity
    from app.app.services.graph import _try_auto_resolve

    settings = get_settings()
    db = db_mod.SessionLocal()
    try:
        project, user = _project(db)
        old = _call(db, settings, project, user, "art_o2_old", "созвон 19.08.2026.webm", OLD)
        new = _call(db, settings, project, user, "art_o2_new", "созвон 21.09.2026.webm", CANCEL)
        older = _decision(db, old, "Мастер-система номенклатуры — Битрикс", 10.0)
        newer = _decision(db, new, "Мастер-система номенклатуры — 1С", 5.0)
        link = _link(db, project, older, newer)  # направление «наоборот»
        db.commit()
        _try_auto_resolve(db, settings, new, CANCEL)
        db.commit()
        db.expire_all()
        res = _res(db, link.id)
        assert res.winning_entity_id == newer.id and res.resolved_by is None
        assert db.get(Entity, older.id).is_active is False and db.get(Entity, newer.id).is_active is True
        # и срабатывает только из артефакта, где звучит отмена: старый артефакт с той же фразой в тексте не решает
    finally:
        db.close()


def test_cancel_rule_skips_human_confirmed_side(client, admin_token, tmp_data):
    """О3: подтверждённое человеком не выключается фразой отмены."""
    from app.app.models.entity import Entity
    from app.app.services.graph import _try_auto_resolve

    settings = get_settings()
    db = db_mod.SessionLocal()
    try:
        project, user = _project(db)
        old = _call(db, settings, project, user, "art_o3_old", "созвон 19.08.2026.webm", OLD)
        new = _call(db, settings, project, user, "art_o3_new", "созвон 21.09.2026.webm", CANCEL)
        older = _decision(db, old, "Мастер-система номенклатуры — Битрикс", 10.0, human_review={"by": "Admin", "action": "confirm"})
        newer = _decision(db, new, "Мастер-система номенклатуры — 1С", 5.0)
        link = _link(db, project, newer, older)
        db.commit()
        _try_auto_resolve(db, settings, new, CANCEL)
        db.commit()
        db.expire_all()
        assert _res(db, link.id) is None and db.get(Entity, older.id).is_active is True
    finally:
        db.close()


def test_cancel_phrase_far_from_item_is_ignored(client, admin_token, tmp_data):
    """Фраза отмены должна быть в источнике нового пункта, а не где угодно в тексте созвона."""
    from app.app.services.graph import _try_auto_resolve

    settings = get_settings()
    db = db_mod.SessionLocal()
    try:
        project, user = _project(db)
        old = _call(db, settings, project, user, "art_far_old", "созвон 19.08.2026.webm", OLD)
        lines = ["[00:01 - 00:02] Олег: Отменяем планёрку в пятницу."]
        lines += [f"[{m:02d}:00 - {m:02d}:05] Олег: Реплика {m} про отчёты и печатные формы." for m in range(1, 20)]
        lines += ["[20:00 - 20:10] Елена: Мастер-система номенклатуры — 1С."]
        final = "\n".join(lines) + "\n"
        new = _call(db, settings, project, user, "art_far_new", "созвон 21.09.2026.webm", final)
        link = _link(db, project, _decision(db, new, "Мастер-система номенклатуры — 1С", 1200.0),
                     _decision(db, old, "Мастер-система номенклатуры — Битрикс", 10.0))
        db.commit()
        _try_auto_resolve(db, settings, new, final)
        db.commit()
        assert _res(db, link.id) is None
    finally:
        db.close()


def _docs_with_conflict(db, settings, monkeypatch, engine):
    from app.app.models.artifact import Artifact
    from app.app.services.entities import EntitiesPayload, persist_entities
    from app.app.tests.test_entity_vectors import _fake_embed, _prepare_vec

    _prepare_vec(engine)
    fake = _fake_embed([])
    monkeypatch.setattr("app.app.services.entities.embed_texts", fake)
    monkeypatch.setattr("app.app.services.graph.embed_texts", fake)
    project, user = _project(db, "Rebuild")
    arts = []
    for i, text in enumerate(("мастер-система Битрикс", "мастер-система 1С")):
        art = Artifact(artifact_id=f"art_rb_{i}", project_id=project.id, user_id=user.id, original_filename=f"d{i}.docx",
                       stored_filename=f"d{i}.docx", file_path=f"d{i}.docx", file_type="docx", source_type="document",
                       status="done", sha256=f"rb{i}")
        db.add(art)
        db.flush()
        persist_entities(db, settings, art, EntitiesPayload(decisions=[text]))
        arts.append(art)
    db.commit()
    return project, user


def _conflicts(db, project_id):
    from sqlalchemy import select

    from app.app.models.entity import Link

    return db.scalars(select(Link).where(Link.project_id == project_id, Link.link_type == "POTENTIAL_CONFLICT")).all()


def test_rebuild_makes_one_link_per_pair_and_keeps_human_resolution(client, admin_token, tmp_data, db_engine, monkeypatch):
    """О4: пересборка давала A→B и B→A; О5: пересборка стирала решение человека."""
    from app.app.models.entity import Entity, Resolution
    from app.app.services.graph import rebuild_project_graph
    from app.app.tests.test_entity_vectors import _settings

    settings = _settings(tmp_data, dry_run=False)
    db = db_mod.SessionLocal()
    try:
        project, user = _docs_with_conflict(db, settings, monkeypatch, db_engine)
        rebuild_project_graph(db, settings, project.id)
        links = _conflicts(db, project.id)
        assert len(links) == 1
        link = links[0]
        db.add(Resolution(link_id=link.id, winning_entity_id=link.source_entity_id, losing_entity_id=link.target_entity_id,
                          reason="так решили на встрече", resolved_by=user.id, status="resolved"))
        db.get(Entity, link.target_entity_id).is_active = False
        db.commit()
        rebuild_project_graph(db, settings, project.id)
        links = _conflicts(db, project.id)
        assert [lk.id for lk in links] == [link.id]
        res = _res(db, link.id)
        assert res is not None and res.reason == "так решили на встрече"
    finally:
        db.close()


def test_repair_collapses_reverse_duplicates_and_releases_legacy(client, admin_token, tmp_data):
    from app.app.models.entity import Entity, Resolution
    from app.app.services.graph import LEGACY_AUTO_REASON, repair_conflict_links

    settings = get_settings()
    db = db_mod.SessionLocal()
    try:
        project, user = _project(db)
        old = _call(db, settings, project, user, "art_rep_old", "созвон 19.08.2026.webm", OLD)
        new = _call(db, settings, project, user, "art_rep_new", "созвон 21.09.2026.webm", CANCEL)
        a = _decision(db, old, "Мастер-система номенклатуры — Битрикс", 10.0)
        b = _decision(db, new, "Мастер-система номенклатуры — 1С", 5.0)
        first, second = _link(db, project, b, a), _link(db, project, a, b)
        a.is_active = False
        db.add(Resolution(link_id=second.id, winning_entity_id=b.id, losing_entity_id=a.id, reason=LEGACY_AUTO_REASON,
                          resolved_by=None, status="resolved"))
        db.commit()
        report = repair_conflict_links(db, project.id)  # только отчёт
        assert report["duplicate_links_removed"] == 1 and len(report["legacy_auto"]) == 1
        db.rollback()
        assert len(_conflicts(db, project.id)) == 2
        report = repair_conflict_links(db, project.id, apply=True, release_legacy=True)
        db.commit()
        db.expire_all()
        links = _conflicts(db, project.id)
        # остаётся связь с решением, затем решение снято, проигравший включён — конфликт снова ждёт проверки
        assert [lk.id for lk in links] == [second.id] and _res(db, second.id) is None
        assert db.get(Entity, a.id).is_active is True and first.id not in {lk.id for lk in links}
    finally:
        db.close()


# --- правила Р1 ---


@pytest.mark.parametrize("source_type,file_type,priority,etype,value,text,weight", [
    ("document", "docx", 1.0, "decision", {}, "Бюджет 3 млн", 3),
    ("document", "docx", 0.8, "fact", {}, "Бюджет 3 млн", 2),
    ("call", "audio", 0.5, "decision", {"decided_by": [{"name": "Иван"}]}, "Бюджет 4 млн", 2),
    ("call", "audio", 0.5, "decision", {}, "Бюджет 4 млн", 1),
    ("call", "audio", 0.5, "decision", {"decided_by": [{"name": "Иван"}]}, "Может, бюджет 4 млн?", 0),
])
def test_source_weight(source_type, file_type, priority, etype, value, text, weight):
    from app.app.models.artifact import Artifact
    from app.app.services.conflict_rules import source_weight

    art = Artifact(source_type=source_type, file_type=file_type, priority_weight=priority)
    assert source_weight(etype, value, art, text) == weight


def test_cancel_allowed_and_weight_winner():
    from app.app.services.conflict_rules import cancel_allowed, winner_by_weight

    doc, agreed, said, question = ({"weight": w} for w in (3, 2, 1, 0))
    assert cancel_allowed(agreed, doc)  # утверждённое на созвоне решение может явно отменить документ
    assert not cancel_allowed(said, agreed)  # простое высказывание — нет
    assert cancel_allowed(said, said)
    assert not cancel_allowed(question, said)
    assert winner_by_weight(doc, said) == ("older_wins", "rule:source_weight")
    assert winner_by_weight(said, said) == ("newer_wins", "rule:later_equal_weight")


def test_material_topic():
    from app.app.services.conflict_rules import is_material_topic

    assert is_material_topic("Бюджет 3 млн")
    assert is_material_topic("Запуск до 15.10")
    assert is_material_topic("Сдать 01.12.2026")
    assert not is_material_topic("Платформа 8.3, конфигурация УТ")


# --- Р4: чат не останавливается, получает хронологию ---


def test_chat_open_conflict_answers_with_both_versions(client, admin_token, tmp_data):
    from app.app.models.artifact import Artifact
    from app.app.models.entity import Resolution
    from app.app.services.chat import answer_chat

    settings = get_settings()
    db = db_mod.SessionLocal()
    try:
        project, user = _project(db, "ChatConflict")
        doc = Artifact(artifact_id="art_cc_doc", project_id=project.id, user_id=user.id, original_filename="ТЗ 12.09.2026.docx",
                       stored_filename="t.docx", file_path="/tmp/t.docx", file_type="docx", source_type="document", status="done",
                       sha256="cc1", priority_weight=1.0)
        call = Artifact(artifact_id="art_cc_call", project_id=project.id, user_id=user.id, original_filename="созвон 19.09.2026.webm",
                        stored_filename="c.webm", file_path="/tmp/c.webm", file_type="audio", source_type="call", status="done",
                        sha256="cc2", priority_weight=0.5)
        db.add_all([doc, call])
        db.flush()
        _link(db, project, _decision(db, call, "Бюджет внедрения 4 млн", 5.0), _decision(db, doc, "Бюджет внедрения 3 млн", 0.0))
        win, lose = _decision(db, call, "Срок запуска 01.12.2026", 9.0), _decision(db, doc, "Срок запуска 01.11.2026", 0.0)
        done = _link(db, project, win, lose)
        lose.is_active = False
        db.add(Resolution(link_id=done.id, winning_entity_id=win.id, losing_entity_id=lose.id, resolved_by=None,
                          status="model_resolved", reason="правило explicit_cancel: явная отмена «передумали»"))
        db.commit()
        pid, uid, doc_id, call_id = project.id, user.id, doc.id, call.id
    finally:
        db.close()

    captured: dict = {}

    def fake_completion(_settings, *, system, user, **_k):
        captured["user"] = user
        return "В документе 3 млн, на созвоне позже — 4 млн."

    hits = [{"chunk_id": 1, "artifact_id": call_id, "chunk_index": 0, "text": "бюджет 4 млн", "speaker": "Елена", "score": 0.9},
            {"chunk_id": 2, "artifact_id": doc_id, "chunk_index": 0, "text": "бюджет 3 млн", "speaker": None, "score": 0.88}]
    settings.dry_run = False
    with patch("app.app.services.chat.embed_texts", return_value=[[0.1] * 1024]), \
            patch("app.app.services.chat.search_vectors", return_value=hits), \
            patch("app.app.services.chat._document_context_blocks", return_value=[]), \
            patch("app.app.services.chat.chat_completion_no_think", side_effect=fake_completion):
        db2 = db_mod.SessionLocal()
        try:
            out = answer_chat(db2, settings, pid, uid, "какой бюджет верный? противоречие")
        finally:
            db2.close()
    assert out.get("clarification") is not True
    prompt = captured["user"]
    assert "Нерешённые противоречия" in prompt and "Бюджет внедрения 3 млн" in prompt and "Бюджет внедрения 4 млн" in prompt
    assert "В силе: [decision] Срок запуска 01.12.2026" in prompt and "Срок запуска 01.11.2026" in prompt
    assert '{"text"' not in prompt.split("Дополнительно из БД проекта:")[-1]


def test_conflict_lines_filter_and_dismissed(client, admin_token, tmp_data):
    from app.app.models.entity import Resolution
    from app.app.services.conflict_context import conflict_lines

    settings = get_settings()
    db = db_mod.SessionLocal()
    try:
        project, user = _project(db, "Lines")
        a = _call(db, settings, project, user, "art_ln_a", "созвон 19.08.2026.webm", OLD)
        b = _call(db, settings, project, user, "art_ln_b", "созвон 21.09.2026.webm", CANCEL)
        c = _call(db, settings, project, user, "art_ln_c", "созвон 22.09.2026.webm", OLD)
        _link(db, project, _decision(db, b, "Склад — адресное хранение", 1.0), _decision(db, a, "Склад — без адресного хранения", 1.0))
        x, y = _decision(db, c, "Отчёт А", 1.0), _decision(db, a, "Отчёт Б", 2.0)
        dismissed = _link(db, project, x, y)
        db.add(Resolution(link_id=dismissed.id, winning_entity_id=x.id, losing_entity_id=y.id, resolved_by=None,
                          status="model_dismissed", reason="модель: не конфликт — разные отчёты"))
        db.commit()
        resolved, open_ = conflict_lines(db, project.id, artifact_ids={b.id})
        assert resolved == [] and len(open_) == 1 and "адресное хранение" in open_[0]
        resolved, open_ = conflict_lines(db, project.id)
        assert len(open_) == 1  # «не конфликт» не выводится
        assert conflict_lines(db, project.id, artifact_ids=set(), entity_ids=set()) == ([], [])
    finally:
        db.close()


def test_docx_uploaded_as_call_weighs_as_document():
    """REQUIREMENTS-PLAN этап 1: «Нормализация_НСИ…docx» загружен как созвон (вес 0.5) — по правилам это документ."""
    from app.app.models.artifact import Artifact
    from app.app.services.conflict_rules import WEIGHT_AGREED, source_weight

    art = Artifact(source_type="call", file_type="docx", priority_weight=0.5)
    assert source_weight("task", {}, art, "Нормализовать справочник профнастила") == WEIGHT_AGREED