"""Карточка голоса: имя, компания, сторона → реестр проекта и голос (services/speaker_cards.py). Имена вымышленные."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.app.services import people
from app.app.tests.test_lab_enroll import _lab_match, _make_run, _seed_artifact


@pytest.fixture()
def setup(client, db_engine, tmp_data: Path, admin_token, monkeypatch):
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.services import speaker_profiles as sp

    project_id, artifact_id = _seed_artifact("art_cards")
    run = _make_run(tmp_data, project_id, artifact_id)
    match = _lab_match(run, project_id=project_id, artifact_id=artifact_id)
    monkeypatch.setattr(sp, "match_json_path", lambda *_a, **_k: match)
    monkeypatch.setattr(sp, "_safe_rebuild_artifact_views", lambda *_a, **_k: None)
    monkeypatch.setattr(sp, "_maybe_reaudit_voices_after_names", lambda *_a, **_k: None)
    db = SessionLocal()
    art = db.query(Artifact).filter(Artifact.artifact_id == artifact_id).one()
    yield db, art, match
    db.close()


def test_card_creates_person_party_and_consent(setup):
    from app.app.models.person import Person
    from app.app.services.speaker_cards import confirm_card

    db, art, _match = setup
    from app.app.models.project import Project

    db.get(Project, art.project_id).set_settings({"memory_platform": True})  # с реестром голос внешнего — в проекте (Д5)
    db.commit()
    person, profile = confirm_card(db, art, diar_label="speaker_0", name="Елена Соколова", company="Пример Заказчик",
                                   side="customer", job_title="главный бухгалтер")
    row = db.get(Person, person.id)
    assert (row.company_name, row.side, row.side_source, row.job_title) == ("Пример Заказчик", "customer", "human", "главный бухгалтер")
    assert row.voice_consent_at is not None  # внешний человек: согласие по договору ставится само
    assert people.party_side(db, art.project_id, "пример заказчик") == "customer"
    assert profile.display_name == "Елена Соколова" and profile.scope == "project" and profile.person_id == person.id


def test_card_updates_existing_person_and_requires_company_and_side(setup):
    from app.app.services.speaker_cards import confirm_card

    db, art, _match = setup
    person = people.create_person(db, art.project_id, "Олег Петров", side="unknown")
    db.commit()
    with pytest.raises(people.PeopleError, match="компанию"):
        confirm_card(db, art, diar_label="speaker_1", name="Олег Петров", company=" ", side="executor")
    with pytest.raises(people.PeopleError, match="сторону"):
        confirm_card(db, art, diar_label="speaker_1", name="Олег Петров", company="Пример Исполнитель", side="")
    got, _profile = confirm_card(db, art, diar_label="speaker_1", name="Олег Петров", company="Пример Исполнитель", side="executor")
    assert got.id == person.id and got.side == "executor" and got.company_name == "Пример Исполнитель"


def test_withdrawn_consent_is_not_given_again(setup):
    from app.app.services.speaker_cards import confirm_card

    db, art, _match = setup
    person = people.create_person(db, art.project_id, "Анна Пример", side="customer", company_name="Пример Заказчик")
    people.give_voice_consent(db, art.project_id, person.id)
    people.withdraw_voice_consent(db, art.project_id, person.id)
    db.commit()
    with pytest.raises(people.PeopleError, match="отозвано"):
        confirm_card(db, art, diar_label="speaker_0", name="Анна Пример", company="Пример Заказчик", side="customer")


def test_auto_voice_waits_for_human_click(setup, monkeypatch):
    from app.app.models.speaker_profile import ArtifactSpeakerLink, SpeakerProfile
    from app.app.services.speaker_cards import confirm_card, page_context
    from app.app.services.speaker_profiles import speakers_confirm_status

    db, art, match = setup
    confirm_card(db, art, diar_label="speaker_0", name="Елена Соколова", company="Пример Заказчик", side="customer")
    # speaker_1 «узнан автоматически»: имя и связь есть, но человек не проверял
    prof = SpeakerProfile(scope="project", project_id=art.project_id, display_name="Олег Петров", embedding=bytes(12), embedding_dim=3)
    db.add(prof)
    db.flush()
    db.add(ArtifactSpeakerLink(artifact_id=art.artifact_id, project_id=art.project_id, diar_label="speaker_1",
                               profile_id=prof.id, status="confirmed"))
    db.commit()
    payload = json.loads(match.read_text(encoding="utf-8"))
    payload["matches"]["speaker_1"].update(status="confirmed", profile_id=prof.id, profile_name="Олег Петров",
                                           auto={"score": 0.9, "margin": 0.2}, needs_human=True)
    match.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    status = speakers_confirm_status(db, project_id=art.project_id, artifact_id=art.artifact_id)
    assert status["pending"] == ["speaker_1"] and not status["ready"]
    assert page_context(db, art)["cards"]["speaker_1"]["needs_human"] is True

    confirm_card(db, art, diar_label="speaker_1", name="Олег Петров", company="Пример Исполнитель", side="executor")
    assert "needs_human" not in json.loads(match.read_text(encoding="utf-8"))["matches"]["speaker_1"]
    assert speakers_confirm_status(db, project_id=art.project_id, artifact_id=art.artifact_id)["ready"]


def test_apply_auto_accept_marks_needs_human():
    from types import SimpleNamespace

    from app.app.services.speaker_auto import apply_auto_accept

    settings = SimpleNamespace(voice_auto_accept_min=0.85, voice_auto_accept_margin=0.10)
    payload = {"matches": {"speaker_0": {"status": "new"}},
               "profile_candidates": {"speaker_0": [{"profile_id": 7, "display_name": "Олег Петров", "cosine": 0.93}]}}
    assert apply_auto_accept(payload, settings) == ["speaker_0"]
    assert payload["matches"]["speaker_0"]["needs_human"] is True


def test_page_suggests_registry_person_and_company_sides(setup):
    from app.app.services.speaker_cards import page_context, suggest

    db, art, _match = setup
    people.create_person(db, art.project_id, "Елена Соколова", side="customer", side_source="human", company_name="Пример Заказчик")
    people.set_party(db, art.project_id, "Пример Заказчик", "customer")
    db.commit()
    hint = suggest(db, art.project_id, "Елена Соколова")
    assert (hint["company"], hint["side"], hint["source"]) == ("Пример Заказчик", "customer", "Люди и стороны")
    ctx = page_context(db, art)
    assert "Пример Заказчик" in ctx["companies"]
    assert ctx["company_sides"][people.alias_key("Пример Заказчик")] == "customer"
    assert set(ctx["cards"]) == {"speaker_0", "speaker_1"}


def test_card_api_requires_staff(client, setup, user_token):
    _db, art, _match = setup
    r = client.post(f"/api/artifacts/{art.artifact_id}/speaker-card",
                    json={"diar_label": "speaker_0", "display_name": "Елена Соколова", "company": "Пример", "side": "customer"},
                    headers={"Authorization": f"Bearer {user_token}"})
    assert r.status_code in (403, 404)
