"""Карточки из консоли: пункт 8 меню запуска и scripts/user_profile.py; сброс карточки закрывает сессии."""

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


@pytest.fixture()
def ivan(db, tmp_data):
    """Admin (из тестового Login_up.txt) с заполненной карточкой в ACME."""
    from app.app.config import get_settings
    from app.app.models.user import User
    from app.app.services import user_profile
    from app.app.services.companies import sync_companies

    get_settings.cache_clear()
    companies = {c.name: c for c in sync_companies(db)}
    user = User(name="Admin", telegram_id="tg-a", role="admin", password_hash="x")
    db.add(user)
    db.commit()
    user_profile.complete_onboarding(
        db, user, last_name="Иванов", first_name="Иван", job_title="Аналитик",
        company_id=companies["ACME"].id, avatar_bytes=jpeg(),
    )
    return user


def _run(db, answers):
    from app.app.services import user_profile_console as console

    it = iter(answers)
    lines: list[str] = []
    done = console.interactive(db, ask=lambda _p: next(it), out=lines.append)
    return done, "\n".join(lines)


def test_list_shows_state_without_company_side(db, ivan):
    from app.app.services import user_profile_console as console

    rows = console.card_rows(db)
    assert rows[0].login == "Admin" and rows[0].complete and rows[0].company == "ACME"
    assert rows[1].user_id is None  # Manager ни разу не входил
    lines: list[str] = []
    console.print_rows(rows, lines.append)
    assert "Иван Иванов" in lines[1] and "заполнена" in lines[1] and "не входил" in lines[2]
    assert "Исполнитель" not in "\n".join(lines) and "executor" not in "\n".join(lines)


def test_set_company_by_number_and_name(db, ivan):
    from app.app.services import user_profile

    done, _ = _run(db, ["1", "1", "2", "да"])  # Admin → сменить компанию → №2
    assert done
    db.expire_all()
    assert user_profile.get_profile(db, ivan.id).company_id is not None
    done, _ = _run(db, ["admin", "1", "acme", "да"])
    assert done


def test_voice_withdraw_and_restore(db, ivan):
    from app.app.services import user_profile

    assert _run(db, ["1", "2", "да"])[0]
    db.expire_all()
    assert user_profile.get_profile(db, ivan.id).voice_consent_at is None
    done, out = _run(db, ["1", "2", "да"])
    assert done and "вернуть согласие" in out
    db.expire_all()
    assert user_profile.get_profile(db, ivan.id).voice_consent_at is not None


def test_refusal_and_never_logged_in(db, ivan):
    from app.app.services import user_profile

    done, out = _run(db, ["1", "3", "нет"])
    assert not done and "Отменено" in out
    assert user_profile.is_complete(user_profile.get_profile(db, ivan.id))
    done, out = _run(db, ["Manager"])
    assert not done and "ни разу не входил" in out
    assert _run(db, ["nobody"])[0] is False
    assert _run(db, ["1", "1", "Нет такой"])[0] is False


def test_reset_card_closes_open_sessions(client, monkeypatch, db_engine):
    """Сброс карточки админом: открытая сессия — 401 сразу, вход ведёт на шаг «карточка»."""
    import app.app.db as db_mod
    from app.app.config import get_settings
    from app.app.services import user_profile_console as console
    from app.app.tests.test_login_profile import GOOD, _acme, _step1, _submit, _to_profile_step

    monkeypatch.setenv("PERSONAL_PASSWORD", "required")
    get_settings.cache_clear()
    ticket = _to_profile_step(client)
    token = _submit(client, ticket, company_id=_acme(client, ticket)).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/projects", headers=headers).status_code == 200
    with db_mod.SessionLocal() as db:
        row = next(r for r in console.card_rows(db) if r.login == "Admin")
        console.reset_card(db, row, source="test")
    assert client.get("/projects", headers=headers).status_code == 401
    step = _step1(client)
    r = client.post("/auth/login/personal", json={"ticket": step["ticket"], "password": GOOD})
    assert r.json()["step"] == "profile"


def test_script(db, ivan, capsys, monkeypatch):
    path = Path(__file__).resolve().parents[3] / "scripts" / "user_profile.py"
    spec = importlib.util.spec_from_file_location("user_profile_script", path)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    import app.app.db as db_mod

    monkeypatch.setattr(script, "SessionLocal", db_mod.SessionLocal)
    assert script.main(["list"]) == 0
    assert "Иван Иванов" in capsys.readouterr().out
    assert script.main(["set-company", "admin", "Заказчик Тест", "--yes"]) == 0
    assert script.main(["set-company", "admin", "Нет такой", "--yes"]) == 1
    assert script.main(["withdraw-voice", "1", "--yes"]) == 0
    assert script.main(["reset", "nobody", "--yes"]) == 1
    assert script.main(["reset", "Manager", "--yes"]) == 1  # не входил
