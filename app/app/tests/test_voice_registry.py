"""Голос -> человек из реестра (PLATFORM-FOUNDATION-PLAN, ПФ5). Все имена вымышленные."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.app.services import people
from app.app.services.speaker_profiles import enroll_speaker
from app.app.tests.test_lab_enroll import _lab_match, _make_run, _seed_artifact


def _enable(db, project_id):
    from app.app.models.project import Project

    project = db.get(Project, project_id)
    project.set_settings({"memory_platform": True})
    db.commit()


def _enroll(db, project_id, artifact_id, lab_path, label, name):
    return enroll_speaker(db, project_id=project_id, artifact_id=artifact_id, diar_label=label, display_name=name,
                          match_path=lab_path, rebuild_views=False)


@pytest.fixture()
def setup(client, db_engine, tmp_data: Path, admin_token, monkeypatch):
    from app.app.db import SessionLocal
    from app.app.services import speaker_profiles as sp

    project_id, artifact_id = _seed_artifact()
    run = _make_run(tmp_data, project_id, artifact_id)
    lab_path = _lab_match(run, project_id=project_id, artifact_id=artifact_id)
    prod_path = tmp_data / "prod_match.json"
    prod_path.write_text('{"embeddings": {}}', encoding="utf-8")
    monkeypatch.setattr(sp, "match_json_path", lambda *_a, **_k: prod_path)
    db = SessionLocal()
    yield db, project_id, artifact_id, lab_path
    db.close()


def test_unknown_name_is_refused_and_queued_when_platform_on(setup):
    from app.app.models.speaker_profile import SpeakerProfile

    db, pid, aid, lab = setup
    _enable(db, pid)
    with pytest.raises(ValueError, match="нет в списке"):
        _enroll(db, pid, aid, lab, "speaker_0", "Незнакомец Иванов")
    assert db.query(SpeakerProfile).count() == 0
    queue = people.open_unknown(db, pid)
    assert [q.label for q in queue] == ["Незнакомец Иванов"] and queue[0].kind == "speaker_label"


def test_external_person_needs_consent_and_stays_in_project(setup):
    from app.app.models.speaker_profile import SpeakerProfile

    db, pid, aid, lab = setup
    _enable(db, pid)
    person = people.create_person(db, pid, "Елена Соколова", side="customer", side_source="human")
    people.add_alias(db, pid, person.id, "Елена")
    db.commit()
    with pytest.raises(ValueError, match="согласие на запись голоса"):
        _enroll(db, pid, aid, lab, "speaker_0", "Елена")
    assert db.query(SpeakerProfile).count() == 0

    people.give_voice_consent(db, pid, person.id)
    db.commit()
    profile = _enroll(db, pid, aid, lab, "speaker_0", "Елена")  # короткое имя приводится к полному
    assert profile.display_name == "Елена Соколова"
    assert profile.scope == "project" and profile.person_id == person.id  # в общие голоса внешний человек не уходит
    assert db.query(SpeakerProfile).filter(SpeakerProfile.scope == "global").count() == 0


def test_employee_voice_does_not_need_external_consent(setup):
    from app.app.models.speaker_profile import SpeakerProfile
    from app.app.models.user import User

    db, pid, aid, lab = setup
    _enable(db, pid)
    account = User(name="staff-example", telegram_id="tg-staff", role="manager", password_hash="x")
    db.add(account)
    db.commit()
    people.create_person(db, pid, "Пётр Сотрудников", user_id=account.id, side="executor", side_source="card")
    db.commit()
    profile = _enroll(db, pid, aid, lab, "speaker_1", "Пётр Сотрудников")
    assert profile.display_name == "Пётр Сотрудников"
    assert db.query(SpeakerProfile).filter(SpeakerProfile.scope == "global").count() >= 1  # сотрудник — как прежде


def test_platform_off_keeps_old_behaviour(setup):
    db, pid, aid, lab = setup
    profile = _enroll(db, pid, aid, lab, "speaker_0", "Кто Угодно")  # флаг не включён: реестр не спрашивают
    assert profile.display_name == "Кто Угодно" and profile.person_id is None


def test_withdraw_consent_deletes_person_voice_only_in_console(setup):
    from app.app.models.speaker_profile import ArtifactSpeakerLink, SpeakerProfile

    db, pid, aid, lab = setup
    _enable(db, pid)
    person = people.create_person(db, pid, "Ольга Заказчикова", side="customer", side_source="human")
    people.give_voice_consent(db, pid, person.id)
    db.commit()
    _enroll(db, pid, aid, lab, "speaker_0", "Ольга Заказчикова")
    assert db.query(SpeakerProfile).filter(SpeakerProfile.person_id == person.id).count() == 1
    assert people.withdraw_voice_consent(db, pid, person.id) == 1
    db.commit()
    assert db.query(SpeakerProfile).filter(SpeakerProfile.person_id == person.id).count() == 0
    db.refresh(person)
    assert person.voice_consent_at is None
    link = db.query(ArtifactSpeakerLink).filter(ArtifactSpeakerLink.artifact_id == aid).one()
    assert link.profile_id is None and link.status == "pending"


def test_consent_api_marks_external_only_and_refuses_withdrawal(client, manager_token):
    def auth(t):
        return {"Authorization": f"Bearer {t}"}

    pid = client.post("/projects", json={"name": "Согласие"}, headers=auth(manager_token)).json()["id"]
    ext = client.post(f"/projects/{pid}/people", json={"display_name": "Гость Внешний", "side": "customer"}, headers=auth(manager_token)).json()["id"]
    ok = client.put(f"/projects/{pid}/people/{ext}/voice-consent", json={"given": True}, headers=auth(manager_token))
    assert ok.status_code == 200
    row = next(p for p in ok.json()["people"] if p["id"] == ext)
    assert row["voice_consent_at"] and row["external"] is True
    assert client.put(f"/projects/{pid}/people/{ext}/voice-consent", json={"given": False}, headers=auth(manager_token)).status_code == 400
    employee = next(p for p in ok.json()["people"] if p["user"])  # автор проекта — сотрудник
    assert client.put(f"/projects/{pid}/people/{employee['id']}/voice-consent", json={"given": True}, headers=auth(manager_token)).status_code == 400


def test_speaker_pages_get_people_suggestions(setup):
    """На странице подтверждения спикеров поля имён получают подсказку из реестра людей проекта."""
    from app.app.models.user import User
    from app.app.routers import pages

    db, pid, _aid, _lab = setup
    assert pages._people_datalist(db, pid) == ""  # реестр пуст — ничего не добавляем
    people.create_person(db, pid, 'Пётр "Кавычкин" <Тест>')
    db.commit()
    block = pages._people_datalist(db, pid)
    assert "pp-people" in block and "&lt;Тест&gt;" in block and "&quot;Кавычкин&quot;" in block  # экранирование
    html = "<html><body><input class='name-inp'></body></html>"
    served = pages._serve_report(html, User(role="admin"), embed=False, kind="audio", role="admin", people_html=block).body.decode("utf-8")
    assert served.count("pp-people") >= 2 and "list" in served
    plain = pages._serve_report("<html><body><p>без полей имён</p></body></html>", User(role="admin"), embed=False, kind="audio", role="admin", people_html=block)
    assert "pp-people" not in plain.body.decode("utf-8")


def test_link_voice_to_employee_account_by_click(client, manager_token, db_engine):
    from app.app.db import SessionLocal
    from app.app.models.speaker_profile import SpeakerProfile
    from app.app.tests.test_voice_link import _person, _voice

    def auth(t):
        return {"Authorization": f"Bearer {t}"}

    pid = client.post("/projects", json={"name": "Голос"}, headers=auth(manager_token)).json()["id"]
    db = SessionLocal()
    try:
        account = _person(db, "voice-owner", "Иван", "Иванов")  # карточка с согласием на голос
        people.create_person(db, pid, "Иван Иванов", user_id=account.id, side="executor", side_source="card")
        matching, other = _voice(db, "Иван Иванов"), _voice(db, "Совсем Другой")
        db.commit()
        person_id = people.resolve(db, pid, "Иван Иванов").person.id
        matching_id, other_id, account_id = matching.id, other.id, account.id
    finally:
        db.close()
    h = auth(manager_token)
    row = next(p for p in client.get(f"/projects/{pid}/people", headers=h).json()["people"] if p["id"] == person_id)
    assert [v["id"] for v in row["voice_suggestions"]] == [matching_id]
    assert client.post(f"/projects/{pid}/people/{person_id}/voices/{other_id}/link", headers=h).status_code == 400  # не предлагался
    ok = client.post(f"/projects/{pid}/people/{person_id}/voices/{matching_id}/link", headers=h)
    assert ok.status_code == 200
    row = next(p for p in ok.json()["people"] if p["id"] == person_id)
    assert row["voice_suggestions"] == [] and row["voices"] == 1
    db = SessionLocal()
    try:
        assert db.get(SpeakerProfile, matching_id).user_id == account_id
    finally:
        db.close()
