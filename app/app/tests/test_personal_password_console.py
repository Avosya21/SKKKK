"""Сброс личного пароля из консоли: пункт 7 меню запуска и scripts/personal_password.py."""

from __future__ import annotations

import pytest

GOOD = "correct-horse-battery"


@pytest.fixture()
def allow(tmp_path, monkeypatch):
    from app.app.config import get_settings
    from app.app.services import personal_password_console as console

    path = tmp_path / "Login_up.txt"
    path.write_text("Admin\nIvan.Test\n", encoding="utf-8")
    monkeypatch.setenv("LOGIN_ALLOWLIST_PATH", str(path))
    get_settings.cache_clear()
    monkeypatch.setattr(console, "_last_logins", lambda: {})  # журнал входов в app.db — не для этих тестов
    yield
    get_settings.cache_clear()


def _run(answers):
    from app.app.services import personal_password_console as console

    it = iter(answers)
    lines: list[str] = []
    done = console.interactive_reset(ask=lambda _p: next(it), out=lines.append)
    return done, "\n".join(lines)


def test_list_shows_allowlist_and_state(allow):
    from app.app.services import personal_password, personal_password_console as console

    personal_password.set_initial("ivan.test", GOOD, GOOD)
    personal_password.set_initial("gone.user", GOOD, GOOD)  # был, но убран из Login_up.txt
    rows = console.login_rows()
    assert [(r.login, r.has_password, r.in_allowlist) for r in rows] == [
        ("Admin", False, True),
        ("Ivan.Test", True, True),
        ("gone.user", True, False),
    ]
    lines: list[str] = []
    console.print_rows(rows, lines.append)
    assert "есть" in lines[2] and "нет в Login_up.txt" in lines[3]


@pytest.mark.parametrize("choice", ["2", "IVAN.TEST", " ivan.test "])
def test_reset_by_number_or_login(allow, choice):
    from app.app.services import personal_password

    personal_password.set_initial("ivan.test", GOOD, GOOD)
    done, out = _run([choice, "да"])
    assert done and "сброшен" in out
    info = personal_password.info("ivan.test")
    assert not info.has_password and info.reset_pending and info.version == 2


def test_refusal_changes_nothing(allow):
    from app.app.services import personal_password

    personal_password.set_initial("ivan.test", GOOD, GOOD)
    done, out = _run(["2", ""])
    assert not done and "Отменено" in out
    assert personal_password.verify("ivan.test", GOOD)


def test_unknown_and_empty_choice(allow):
    assert _run(["nobody"])[0] is False
    assert "не найден" in _run(["nobody"])[1]
    assert _run(["99"])[0] is False
    assert _run([""])[0] is False


def test_nothing_to_reset(allow):
    done, out = _run(["1"])
    assert not done and "сбрасывать нечего" in out


def test_script_reset_yes_and_unknown(allow, capsys):
    import importlib.util
    from pathlib import Path

    from app.app.services import personal_password

    path = Path(__file__).resolve().parents[3] / "scripts" / "personal_password.py"
    spec = importlib.util.spec_from_file_location("personal_password_script", path)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)

    personal_password.set_initial("ivan.test", GOOD, GOOD)
    assert script.main(["list"]) == 0
    assert "Ivan.Test" in capsys.readouterr().out
    assert script.main(["reset", "nobody", "--yes"]) == 1
    assert script.main(["reset", "ivan.test", "--yes"]) == 0
    assert not personal_password.has_password("ivan.test")
