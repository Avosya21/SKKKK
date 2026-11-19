"""Разбор Login_roles.txt."""

from __future__ import annotations

from app.app.services import login_roles


def test_default_manager(tmp_path):
    path = tmp_path / "roles.txt"
    path.write_text("# empty\n", encoding="utf-8")
    assert login_roles.roles_for("anyone@ex.ru", path) == ["manager"]


def test_multi_role(tmp_path):
    path = tmp_path / "roles.txt"
    path.write_text("alice@ex.ru | admin, manager\n", encoding="utf-8")
    assert login_roles.roles_for("Alice@ex.ru", path) == ["admin", "manager"]
    assert login_roles.resolve_role("alice@ex.ru", "admin", path) == "admin"
    assert login_roles.resolve_role("alice@ex.ru", "user", path) is None


def test_rejects_mixed_customer_staff(tmp_path):
    path = tmp_path / "roles.txt"
    path.write_text("bob@ex.ru | user,admin\n", encoding="utf-8")
    assert login_roles.roles_for("bob@ex.ru", path) == ["manager"]


def test_customer_only(tmp_path):
    path = tmp_path / "roles.txt"
    path.write_text("client@co.ru | user\n", encoding="utf-8")
    assert login_roles.roles_for("client@co.ru", path) == ["user"]
    assert login_roles.is_customer_role("user")
