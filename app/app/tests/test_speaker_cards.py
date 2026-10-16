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
    assert (row.company_name, row.side, row.side_source, row.job_title) == ("Пример Заказчик", "customer", "project_party", "главный бухгалтер")
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


def _global_voice(db, name, vec=(1.0, 0.0, 0.0)):
    from app.app.models.speaker_profile import SpeakerProfile
    from app.app.services.speaker_profiles import pack_embedding

    prof = SpeakerProfile(scope="global", display_name=name, embedding=pack_embedding(list(vec)), embedding_dim=3)
    db.add(prof)
    db.commit()
    return prof


def test_chosen_voice_links_to_that_profile_not_by_spelling(setup):
    from app.app.models.speaker_profile import ArtifactSpeakerLink
    from app.app.services.speaker_cards import confirm_card

    db, art, _match = setup
    voice = _global_voice(db, "Игорь Пилот")
    person, profile = confirm_card(db, art, diar_label="speaker_0", name="Игорь", company="Пример Заказчик", side="customer",
                                   profile_id=voice.id)
    assert profile.id == voice.id and profile.display_name == "Игорь Пилот"
    assert person.display_name == "Игорь Пилот"
    link = db.query(ArtifactSpeakerLink).filter(ArtifactSpeakerLink.diar_label == "speaker_0").one()
    assert link.profile_id == voice.id


def test_other_name_rebinds_without_renaming_old_voice(setup):
    from app.app.models.speaker_profile import ArtifactSpeakerLink, SpeakerProfile
    from app.app.services.speaker_cards import confirm_card

    db, art, _match = setup
    first = _global_voice(db, "Игорь Пилот")
    confirm_card(db, art, diar_label="speaker_0", name="Игорь Пилот", company="Пример Заказчик", side="customer", profile_id=first.id)
    second = _global_voice(db, "Олег Петров", vec=(0.0, 1.0, 0.0))
    confirm_card(db, art, diar_label="speaker_0", name="Олег Петров", company="Пример Заказчик", side="customer", profile_id=second.id)
    assert db.get(SpeakerProfile, first.id).display_name == "Игорь Пилот"  # прежний голос не переименован
    link = db.query(ArtifactSpeakerLink).filter(ArtifactSpeakerLink.diar_label == "speaker_0").one()
    assert link.profile_id == second.id


def test_new_person_refuses_known_voice_name(setup):
    from app.app.services.speaker_cards import confirm_card

    db, art, _match = setup
    _global_voice(db, "Игорь Пилот")
    with pytest.raises(people.PeopleError, match="уже есть в базе"):
        confirm_card(db, art, diar_label="speaker_1", name="Игорь (Пилот)", company="Пример", side="customer", new_person=True)


def test_new_person_does_not_merge_into_similar_voice(setup):
    from app.app.services.speaker_cards import confirm_card

    db, art, _match = setup
    _global_voice(db, "Игорь Пилот", vec=(1.0, 0.0, 0.0))  # голос speaker_0 в фикстуре — тот же вектор
    person, profile = confirm_card(db, art, diar_label="speaker_0", name="Олег Петров", company="Пример", side="customer",
                                   new_person=True)
    assert profile.display_name == "Олег Петров"


def test_rename_is_explicit(setup):
    from app.app.models.speaker_profile import SpeakerProfile
    from app.app.services.speaker_cards import confirm_card

    db, art, _match = setup
    voice = _global_voice(db, "Игорь Пилотт")
    confirm_card(db, art, diar_label="speaker_0", name="Игорь Пилотт", company="Пример", side="customer", profile_id=voice.id)
    _p, profile = confirm_card(db, art, diar_label="speaker_0", name="Игорь Пилот", company="Пример", side="customer",
                               profile_id=voice.id, rename=True)
    assert profile.id == voice.id and db.get(SpeakerProfile, voice.id).display_name == "Игорь Пилот"
    assert people.resolve(db, art.project_id, "Игорь Пилот") is not None


def test_page_lists_voice_candidates(setup):
    from app.app.services.speaker_cards import page_context

    db, art, match = setup
    voice = _global_voice(db, "Игорь Пилот")
    payload = json.loads(match.read_text(encoding="utf-8"))
    payload["profile_candidates"] = {"speaker_0": [{"profile_id": voice.id, "display_name": "Игорь Пилот", "cosine": 0.873}]}
    match.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    cands = page_context(db, art)["cards"]["speaker_0"]["candidates"]
    assert cands[0]["profile_id"] == voice.id and cands[0]["pct"] == 87


def test_staff_voice_links_to_account_and_party_companies_come_first(setup):
    """ЛГ4 (Р5): подтверждённый голос сотрудника с согласием в карточке сам привязывается к его учётке;
    в карточке голоса компании проекта идут первыми, есть стороны проекта для подстановки компании."""
    from datetime import datetime, timezone

    from app.app.models.user import User
    from app.app.services import user_profile as profiles
    from app.app.services.companies import canonical_company, sync_companies
    from app.app.services.speaker_cards import confirm_card, page_context

    db, art, _match = setup
    sync_companies(db)
    worker = User(name="lg4-worker", telegram_id="tg-lg4-worker", role="user", password_hash="x")
    db.add(worker)
    db.commit()
    card = profiles.fill_card_for_member(db, worker.id, last_name="Пилотова", first_name="Ирина",
                                         company_id=canonical_company(db, "ACME").id)
    card.voice_consent_at = datetime.now(timezone.utc)
    db.commit()
    people.set_party(db, art.project_id, "Заказчик Тест", "customer")
    people.member_person(db, art.project_id, worker.id)
    db.commit()
    person, profile = confirm_card(db, art, diar_label="speaker_0", name="Ирина Пилотова", company="ACME", side="executor")
    db.refresh(profile)
    assert person.user_id == worker.id and profile.scope == "global" and profile.user_id == worker.id
    ctx = page_context(db, art)
    parties = {p["company"] for p in ctx["project_parties"]}
    assert parties == {"ACME", "Заказчик Тест"} and set(ctx["companies"][:2]) == parties


def test_merging_speakers_merges_their_people(setup):
    """Вкладка «Голоса»: две метки созвона подписали разными людьми, а это один человек — «Объединить» сводит и людей
    проекта в одну карточку (написания, голоса, реплики — у оставшегося)."""
    from app.app.services.speaker_cards import confirm_card
    from app.app.services.speaker_profiles import merge_speakers

    db, art, _match = setup
    first, _ = confirm_card(db, art, diar_label="speaker_0", name="Анна Пример", company="Пример Заказчик", side="customer")
    second, _ = confirm_card(db, art, diar_label="speaker_1", name="Аня Ник", company="Пример Заказчик", side="customer",
                             new_person=True)
    assert first.id != second.id
    result = merge_speakers(db, project_id=art.project_id, artifact_id=art.artifact_id,
                            source_diar_label="speaker_1", target_diar_label="speaker_0")
    db.expire_all()
    assert result["people_merged"] is True
    kept = people.resolve(db, art.project_id, "Аня Ник").person
    assert kept.id == first.id and len([p for p in people.list_people(db, art.project_id) if p.display_name in ("Анна Пример", "Аня Ник")]) == 1


def test_merged_labels_are_one_card_and_split_returns_label(setup):
    """Вкладка «Голоса»: присоединённая метка — внутри карточки основной (не отдельная карточка-дубль); «Отделить»
    возвращает её отдельным спикером без имени."""
    import json

    from app.app.models.speaker_profile import ArtifactSpeakerLink
    from app.app.services.speaker_cards import confirm_card
    from app.app.services.speaker_profiles import merge_speakers, split_speaker
    from asr.build_speaker_match_view import _speaker_card_groups

    db, art, match = setup
    confirm_card(db, art, diar_label="speaker_0", name="Анна Пример", company="Пример Заказчик", side="customer")
    merge_speakers(db, project_id=art.project_id, artifact_id=art.artifact_id, source_diar_label="speaker_1",
                   target_diar_label="speaker_0")
    payload = json.loads(match.read_text(encoding="utf-8"))
    groups = _speaker_card_groups(payload["matches"], payload["merge_map"])
    assert [g["labels"] for g in groups] == [["speaker_0", "speaker_1"]]

    split_speaker(db, project_id=art.project_id, artifact_id=art.artifact_id, diar_label="speaker_1")
    payload = json.loads(match.read_text(encoding="utf-8"))
    assert payload["merge_map"]["speaker_1"] == "speaker_1" and payload["matches"]["speaker_1"]["status"] == "new"
    assert not db.query(ArtifactSpeakerLink).filter_by(artifact_id=art.artifact_id, diar_label="speaker_1").count()
    assert [g["labels"] for g in _speaker_card_groups(payload["matches"], payload["merge_map"])] == [["speaker_0"], ["speaker_1"]]
    with pytest.raises(ValueError):
        split_speaker(db, project_id=art.project_id, artifact_id=art.artifact_id, diar_label="speaker_1")
