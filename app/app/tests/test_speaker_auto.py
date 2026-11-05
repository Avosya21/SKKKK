"""Этап 3 автопроверки: автопринятие голоса, мягкий шлагбаум, подсказка имени, пересчёт после имени."""
from __future__ import annotations

import json
from types import SimpleNamespace

from app.app import db as db_mod
from app.app.config import get_settings


def _settings(**over):
    base = dict(voice_auto_accept_min=0.85, voice_auto_accept_margin=0.10, voice_gate_before_asr=True, voice_gate_mode="soft")
    base.update(over)
    return SimpleNamespace(**base)


def _payload():
    return {
        "merge_map": {"speaker_0": "speaker_0", "speaker_1": "speaker_1", "speaker_2": "speaker_2", "speaker_3": "speaker_3"},
        "matches": {
            "speaker_0": {"diar_label": "speaker_0", "status": "new"},
            "speaker_1": {"diar_label": "speaker_1", "status": "new"},
            "speaker_2": {"diar_label": "speaker_2", "status": "new"},
            "speaker_3": {"diar_label": "speaker_3", "status": "confirmed", "profile_id": 9, "profile_name": "Ирина"},
        },
        "profile_candidates": {
            "speaker_0": [{"profile_id": 1, "display_name": "Елена Соколова", "cosine": 0.93}, {"profile_id": 2, "display_name": "Анна", "cosine": 0.51}],
            # один человек в двух профилях: высокое сходство, но нет отрыва — не принимаем
            "speaker_1": [{"profile_id": 3, "display_name": "Заказчик 2", "cosine": 0.905}, {"profile_id": 4, "display_name": "Сергей Орлов", "cosine": 0.892}],
            "speaker_2": [{"profile_id": 5, "display_name": "Павел", "cosine": 0.62}],
            "speaker_3": [{"profile_id": 6, "display_name": "Кто-то", "cosine": 0.99}],
        },
        "display_names": {},
    }


def test_auto_accept_rule_needs_score_and_margin():
    from app.app.services.speaker_auto import apply_auto_accept, auto_labels, unnamed_labels

    payload = _payload()
    accepted = apply_auto_accept(payload, _settings())
    assert accepted == ["speaker_0"]
    row = payload["matches"]["speaker_0"]
    assert row["status"] == "confirmed" and row["profile_name"] == "Елена Соколова" and row["auto"]["margin"] == 0.42
    assert payload["matches"]["speaker_3"]["profile_name"] == "Ирина"  # решение человека не трогаем
    assert auto_labels(payload) == {"speaker_0"}
    assert unnamed_labels(payload) == ["speaker_1", "speaker_2"]


def test_margin_counts_other_person_not_duplicate_profile():
    """Прод 2026-09-24: «Заказчик 2 Клиент Б» — второй профиль Сергея Орлова; без voice_aliases отрыва нет и голос не принимался."""
    from app.app.services.speaker_auto import auto_accept_decision

    cands = [{"profile_id": 37, "display_name": "Сергей Орлов", "cosine": 0.93},
             {"profile_id": 33, "display_name": "Заказчик 2 Клиент Б", "cosine": 0.91},
             {"profile_id": 13, "display_name": "Антон Морозов", "cosine": 0.41}]
    assert auto_accept_decision(cands, _settings()) is None
    decision = auto_accept_decision(cands, _settings(), {"Заказчик 2 Клиент Б": "Сергей Орлов"})
    assert decision["profile_name"] == "Сергей Орлов" and decision["margin"] == 0.52


def test_gate_modes():
    from app.app.services.speaker_auto import gate_mode, gate_proceeds

    assert gate_proceeds(_settings(voice_gate_mode="soft"), ["speaker_1"]) is True
    assert gate_proceeds(_settings(voice_gate_mode="strict"), ["speaker_1"]) is False
    assert gate_proceeds(_settings(voice_gate_mode="strict"), []) is True  # все голоса узнаны — не ждём
    assert gate_mode(_settings(voice_gate_before_asr=False, voice_gate_mode="strict")) == "off"
    assert gate_mode(_settings(voice_gate_mode="bogus")) == "strict"


def test_placeholder_label_is_not_a_name():
    from app.app.services.transcript_turns import parse_final_turns

    turns = parse_final_turns("[00:01 - 00:05] speaker_1: переносим релиз\n[00:06 - 00:08] SPEAKER_02: да\n")
    assert [t.known_speaker for t in turns] == [None, None]


def test_suggest_names_filters_weak_and_placeholder(monkeypatch):
    from app.app.services import speaker_auto
    from app.app.services.transcript_turns import parse_final_turns

    turns = parse_final_turns(
        "[00:01 - 00:03] Елена Соколова: Павел, вы меня слышите?\n"
        "[00:04 - 00:06] speaker_1: Да, слышу хорошо.\n"
        "[00:07 - 00:09] speaker_2: Добрый день всем.\n"
        "[00:10 - 00:12] speaker_3: Угу.\n"
    )
    seen = {}

    def fake(settings, *, system, user, **_k):
        seen["user"] = user
        return json.dumps({"speakers": [
            {"label": "speaker_1", "name": "Павел", "quote": "Павел, вы меня слышите?", "confidence": 0.9},
            {"label": "speaker_2", "name": "Павел", "quote": "", "confidence": 0.3},
            {"label": "speaker_3", "name": "speaker_3", "quote": "", "confidence": 0.9},
        ]}, ensure_ascii=False)

    monkeypatch.setattr("app.app.services.llm.chat_completion_no_think", fake)
    hints = speaker_auto.suggest_names(get_settings(), turns, ["speaker_1", "speaker_2", "speaker_3"], ["Елена Соколова"])
    assert set(hints) == {"speaker_1"} and hints["speaker_1"]["name"] == "Павел"
    assert "перед ней [00:01] Елена Соколова: Павел, вы меня слышите?" in seen["user"]


def test_write_hints_never_names_confirmed(tmp_path):
    from app.app.services.speaker_auto import write_hints

    path = tmp_path / "match.json"
    path.write_text(json.dumps(_payload(), ensure_ascii=False), encoding="utf-8")
    written = write_hints(path, {"speaker_1": {"name": "Павел"}, "speaker_3": {"name": "Чужое"}})
    data = json.loads(path.read_text(encoding="utf-8"))
    assert written == 1
    assert data["matches"]["speaker_1"]["llm_hint"]["name"] == "Павел"
    assert data["matches"]["speaker_1"]["status"] == "new"  # подсказка не имя
    assert "llm_hint" not in data["matches"]["speaker_3"]


def test_match_view_shows_auto_and_hint():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "asr"))
    from build_speaker_match_view import _speaker_cards

    payload = _payload()
    payload["matches"]["speaker_0"].update({"status": "confirmed", "profile_id": 1, "profile_name": "Елена Соколова",
                                            "auto": {"score": 0.93, "margin": 0.42}})
    payload["matches"]["speaker_1"]["llm_hint"] = {"name": "Павел", "quote": "Павел, вы меня слышите?"}
    groups = [
        {"primary": "speaker_0", "labels": ["speaker_0"], "match": payload["matches"]["speaker_0"], "profile_id": 1},
        {"primary": "speaker_1", "labels": ["speaker_1"], "match": payload["matches"]["speaker_1"], "profile_id": None},
    ]
    html = _speaker_cards(groups, {}, payload["matches"], payload["merge_map"])
    assert "Узнан автоматически по голосу (93%)." in html
    assert "отрыв" not in html
    assert "Подсказка модели:" in html and 'value="Павел"' in html


def test_refresh_job_dedup_and_worker_dispatch(db_engine, monkeypatch):
    from app.app.models.artifact import Artifact
    from app.app.models.job import Job
    from app.app.models.project import Project
    from app.app.models.user import User
    from app.app.services.queue import KIND_REFRESH, enqueue_knowledge_refresh
    from app.app.workers import review as review_mod
    from app.app.workers.gpu_worker import run_once

    db = db_mod.SessionLocal()
    try:
        user = User(name="sp", telegram_id="tg-sp", role="admin", password_hash="x")
        db.add(user)
        db.flush()
        project = Project(name="Sp", created_by=user.id, is_active=True)
        db.add(project)
        db.flush()
        art = Artifact(artifact_id="art_sp", project_id=project.id, user_id=user.id, original_filename="a.webm", stored_filename="a.webm",
                       file_path="/x", file_type="audio", source_type="call", sha256="sp", status="done")
        db.add(art)
        db.commit()
        job = enqueue_knowledge_refresh(db, art.id)
        assert job is not None and job.kind == KIND_REFRESH
        assert enqueue_knowledge_refresh(db, art.id) is None
        job_pk, art_pk = job.id, art.id
    finally:
        db.close()
    calls = []
    monkeypatch.setattr(get_settings(), "dry_run", False)
    monkeypatch.setattr(review_mod, "refresh_knowledge", lambda _db, _s, a: calls.append(a.artifact_id))
    assert run_once()
    db = db_mod.SessionLocal()
    try:
        assert calls == ["art_sp"]
        assert db.get(Job, job_pk).status == "done" and db.get(Artifact, art_pk).status == "done"
    finally:
        db.close()
