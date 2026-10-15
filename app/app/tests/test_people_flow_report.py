"""Отчёт расхождений «компания → сторона → человек → учётка → голос» (PEOPLE-FLOW-PLAN, ЛГ0). Все имена вымышленные."""
from __future__ import annotations

import pytest

from app.app.services import people


@pytest.fixture()
def db(db_engine):
    import app.app.db as db_mod

    session = db_mod.SessionLocal()
    yield session
    session.close()


def _user(db, login):
    from app.app.models.user import User

    row = User(name=login, telegram_id="tg-" + login, role="manager", password_hash="x")
    db.add(row)
    db.commit()
    return row


def test_company_key_ignores_case_quotes_and_legal_form():
    from scripts.people_flow_report import company_key

    assert company_key("ООО «Пример-Альфа»") == company_key("пример альфа") == company_key("ПРИМЕР АЛЬФА ООО")
    assert company_key("Пример Альфа") != company_key("Пример Бета")


def test_report_finds_each_kind_of_gap(db):
    from app.app.config import get_settings
    from app.app.models.company import Company
    from app.app.models.project import Project
    from app.app.models.project_member import ProjectMember
    from app.app.models.speaker_profile import ArtifactSpeakerLink, SpeakerProfile
    from scripts.people_flow_report import collect, render_md, totals

    admin, member = _user(db, "flow-admin"), _user(db, "flow-member")
    db.add(Company(name="Пример Исполнитель", name_key="пример исполнитель", side="executor", active=True))
    project = Project(name="Пример ЛГ0", created_by=admin.id, is_active=True)
    bare = Project(name="Пример без сторон", created_by=admin.id, is_active=True)
    db.add_all([project, bare])
    db.commit()
    people.set_party(db, project.id, "пример исполнитель", "executor")  # есть в справочнике, но без company_id
    people.set_party(db, project.id, "ООО «Заказчик Пример»", "customer")  # нет в справочнике
    people.create_person(db, project.id, "Анна Пример", company_name="ООО «Заказчик Пример»", side="executor", side_source="human")
    people.create_person(db, project.id, "Борис Пример")
    db.add(ProjectMember(project_id=project.id, user_id=member.id, role="user", granted_by=admin.id))
    voice = SpeakerProfile(scope="global", display_name="Голос Пример", embedding=b"\0" * 8, embedding_dim=2)
    db.add(voice)
    db.flush()
    db.add(ArtifactSpeakerLink(artifact_id="art-example", project_id=project.id, diar_label="speaker_0", profile_id=voice.id,
                               status="confirmed"))
    db.commit()

    report = collect(db, get_settings())
    row = next(r for r in report["projects"] if r["id"] == project.id)
    assert row["no_company"] == ["Борис Пример"] and row["side_unknown"] == ["Борис Пример"]
    assert len(row["side_conflict"]) == 1 and "Анна Пример" in row["side_conflict"][0]
    assert row["members_without_person"] == [member.id]
    assert sorted(row["parties_without_company_id"]) == ["ООО «Заказчик Пример»", "пример исполнитель"]
    assert next(r for r in report["projects"] if r["id"] == bare.id)["parties"] == []
    portal = report["portal"]
    assert ["Пример Исполнитель", "пример исполнитель"] in portal["company_variants"]
    assert "ООО «Заказчик Пример»" in portal["parties_outside_directory"]
    assert "Голос Пример" in portal["voices_confirmed_without_owner"]
    assert "flow-member" in portal["users_without_card"]
    numbers = totals(report)
    assert numbers["людей со стороной против компании"] >= 1 and numbers["без сторон"] >= 1
    assert "Анна Пример" in render_md(report)
