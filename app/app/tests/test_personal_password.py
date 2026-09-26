"""Личный пароль: хранилище, правила, версии (сервис без HTTP)."""

from __future__ import annotations

import pytest

from app.app.tests.conftest import ADMIN_PASS

GOOD = "correct-horse-battery"


@pytest.fixture()
def pp():
    from app.app.services import personal_password

    return personal_password


def test_store_is_isolated_temp_file(pp, tmp_path):
    from app.app import credentials_db

    pp.set_initial("Admin", GOOD, GOOD)
    assert credentials_db.db_path() == tmp_path / "credentials.db"
    assert (tmp_path / "credentials.db").is_file()


def test_set_and_verify_case_insensitive_login(pp):
    assert not pp.has_password("Admin")
    version = pp.set_initial("Admin", GOOD, GOOD)
    assert version == 1
    assert pp.has_password("admin")
    assert pp.verify("ADMIN", GOOD)
    assert not pp.verify("Admin", GOOD + "x")


def test_hash_is_argon2_and_not_plain(pp):
    from app.app.credentials_db import PersonalPassword, credentials_session

    pp.set_initial("Admin", GOOD, GOOD)
    with credentials_session() as db:
        row = db.get(PersonalPassword, "admin")
        assert row.password_hash.startswith("$argon2id$")
        assert GOOD not in row.password_hash


@pytest.mark.parametrize(
    ("new", "confirm", "text"),
    [
        (GOOD, GOOD + "1", "не совпадают"),
        ("short", "short", "короче"),
        ("x" * 129, "x" * 129, "длиннее"),
        (" " * 12, " " * 12, "пробелов"),
        ("  admin  ", "  admin  ", None),  # короче 10 → правило длины раньше
    ],
)
def test_rules(pp, new, confirm, text):
    with pytest.raises(pp.PasswordRuleError) as exc:
        pp.check_rules(new, confirm, login="admin")
    if text:
        assert text in str(exc.value)


def test_rule_not_equal_to_login(pp):
    with pytest.raises(pp.PasswordRuleError, match="логином"):
        pp.check_rules("Long.Login.Name", "Long.Login.Name", login="long.login.name")


def test_rule_not_equal_to_role_password(pp, monkeypatch):
    monkeypatch.setattr(pp, "min_length", lambda: 4)
    with pytest.raises(pp.PasswordRuleError, match="паролем роли"):
        pp.check_rules(ADMIN_PASS, ADMIN_PASS, login="someone")


def test_set_initial_twice_is_refused(pp):
    pp.set_initial("Admin", GOOD, GOOD)
    with pytest.raises(pp.PasswordStateError):
        pp.set_initial("Admin", GOOD + "2", GOOD + "2")


def test_change_bumps_version_and_needs_current(pp):
    pp.set_initial("Admin", GOOD, GOOD)
    with pytest.raises(pp.PasswordStateError):
        pp.change("Admin", "wrong-current", GOOD + "2", GOOD + "2")
    with pytest.raises(pp.PasswordRuleError):
        pp.change("Admin", GOOD, GOOD, GOOD)
    assert pp.change("Admin", GOOD, GOOD + "2", GOOD + "2") == 2
    assert pp.verify("Admin", GOOD + "2")
    assert not pp.verify("Admin", GOOD)


def test_reset_removes_hash_and_never_lowers_version(pp):
    pp.set_initial("Admin", GOOD, GOOD)
    pp.change("Admin", GOOD, GOOD + "2", GOOD + "2")
    assert pp.reset("Admin") == 3
    state = pp.info("Admin")
    assert not state.has_password and state.reset_pending and state.version == 3
    assert not pp.verify("Admin", GOOD + "2")
    # После сброса создаётся заново, версия не возвращается к 1.
    assert pp.set_initial("Admin", GOOD, GOOD) == 3
    assert not pp.info("Admin").reset_pending


def test_reset_of_login_without_password(pp):
    assert pp.reset("Nobody") == 1
    assert pp.current_version("nobody") == 1


def test_current_version_cache_sees_own_changes(pp):
    assert pp.current_version("Admin") == 0
    pp.set_initial("Admin", GOOD, GOOD)
    assert pp.current_version("Admin") == 1
    pp.reset("Admin")
    assert pp.current_version("Admin") == 2


def test_mode_validation(monkeypatch):
    from pydantic import ValidationError

    from app.app.config import Settings

    assert Settings(personal_password="Required").personal_password == "required"
    with pytest.raises(ValidationError):
        Settings(personal_password="requird")
