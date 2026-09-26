"""Слепок голоса ↔ карточка: только с согласием, отзыв удаляет слепки, гости не затрагиваются."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app.app.tests.test_user_profile import jpeg


@pytest.fixture()
def db(db_engine):
    import app.app.db as db_mod

    session = db_mod.SessionLocal()
    yield session
    session.close()


def _person(db, login, first, last):
    from app.app.models.user import User
    from app.app.services import user_profile
    from app.app.services.companies import sync_companies

    user = User(name=login, telegram_id="tg-" + login, role="user", password_hash="x")
    db.add(user)
    db.commit()
    user_profile.complete_onboarding(
        db, user, last_name=last, first_name=first, job_title="Аналитик",
        company_id=sync_companies(db)[0].id, avatar_bytes=jpeg(),
    )
    return user


def _voice(db, name, scope="global"):
    from app.app.models.speaker_profile import SpeakerProfile

    row = SpeakerProfile(scope=scope, display_name=name, embedding=b"\0" * 1024, embedding_dim=256)
    db.add(row)
    db.commit()
    return row


def test_suggestions_by_exact_name_either_order(db, tmp_data):
    ivan = _person(db, "Admin", "Иван", "Иванов")
    a = _voice(db, "Иван Иванов")
    b = _voice(db, "иванов  иван")
    _voice(db, "Гость Сергей")
    _voice(db, "Иван")  # неполное имя — не предлагаем
    from app.app.services import voice_link

    found = voice_link.suggestions(db)
    assert sorted(s.profile_id for s in found) == sorted([a.id, b.id])
    assert {s.user_id for s in found} == {ivan.id}


def test_namesakes_are_not_suggested(db, tmp_data):
    _person(db, "Admin", "Иван", "Иванов")
    _person(db, "Manager", "Иван", "Иванов")
    _voice(db, "Иван Иванов")
    from app.app.services import voice_link

    assert voice_link.suggestions(db) == []


def test_link_requires_consent_and_withdraw_deletes(db, tmp_data):
    from app.app.models.speaker_profile import SpeakerProfile
    from app.app.services import user_profile, voice_link

    ivan = _person(db, "Admin", "Иван", "Иванов")
    mine = _voice(db, "Иван Иванов")
    guest = _voice(db, "Гость Сергей")
    voice_link.link_profile(db, mine.id, ivan.id)
    assert db.get(SpeakerProfile, mine.id).user_id == ivan.id

    assert user_profile.withdraw_voice_consent(db, ivan.id) == 1
    assert db.get(SpeakerProfile, mine.id) is None
    assert db.get(SpeakerProfile, guest.id) is not None  # чужие слепки не трогаем
    again = _voice(db, "Иван Иванов")
    with pytest.raises(voice_link.VoiceLinkError, match="согласия"):
        voice_link.link_profile(db, again.id, ivan.id)
    assert voice_link.suggestions(db) == []  # без согласия и не предлагаем


def test_merge_users_moves_voice_link(db, tmp_data):
    from app.app.models.speaker_profile import SpeakerProfile
    from app.app.models.user import User
    from app.app.services import voice_link
    from app.app.services.user_identity import merge_users_into

    loser = _person(db, "Admin", "Иван", "Иванов")
    winner = User(name="admin", telegram_id="tg-2", role="user", password_hash="x")
    db.add(winner)
    db.commit()
    voice = _voice(db, "Иван Иванов")
    voice_link.link_profile(db, voice.id, loser.id)
    merge_users_into(db, winner, [loser])
    db.expire_all()
    assert db.get(SpeakerProfile, voice.id).user_id == winner.id


def test_migration_adds_column_to_old_table(tmp_path, monkeypatch):
    import sqlite3

    from sqlalchemy import create_engine

    import app.app.db as db_mod

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE speaker_profiles (id INTEGER PRIMARY KEY, display_name TEXT)")
    con.commit()
    con.close()
    monkeypatch.setattr(db_mod, "engine", create_engine(f"sqlite:///{path.as_posix()}"))
    monkeypatch.setattr(db_mod.settings, "database_url", f"sqlite:///{path.as_posix()}")
    db_mod.apply_schema_migrations()
    cols = [r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(speaker_profiles)")]
    assert "user_id" in cols


def test_script_link_asks_each(db, tmp_data, monkeypatch, capsys):
    from app.app.models.speaker_profile import SpeakerProfile
    import app.app.db as db_mod

    ivan = _person(db, "Admin", "Иван", "Иванов")
    a = _voice(db, "Иван Иванов")
    b = _voice(db, "Иванов Иван", scope="project")
    path = Path(__file__).resolve().parents[3] / "scripts" / "link_voice_profiles.py"
    spec = importlib.util.spec_from_file_location("link_voice_script", path)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    monkeypatch.setattr(script, "SessionLocal", db_mod.SessionLocal)
    answers = iter(["да", ""])
    assert script.main(["link"], ask=lambda _p: next(answers)) == 0
    db.expire_all()
    assert [db.get(SpeakerProfile, a.id).user_id, db.get(SpeakerProfile, b.id).user_id] == [ivan.id, None]
    assert script.main(["unlink", str(a.id)]) == 0
    db.expire_all()
    assert db.get(SpeakerProfile, a.id).user_id is None
