import bcrypt
import pytest
from pydantic_settings import SettingsConfigDict

from app.app import bootstrap


@pytest.fixture()
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    monkeypatch.setattr(bootstrap, "ENV_PATH", path)
    monkeypatch.setattr(bootstrap, "ENV_EXAMPLE", tmp_path / ".env.example")
    return path


def test_ensure_env_creates_file_and_all_role_passwords(env_file):
    created = bootstrap.ensure_env()

    assert set(created) == {"admin", "manager", "user"}
    assert env_file.is_file()
    text = env_file.read_text(encoding="utf-8")
    for role in bootstrap.ROLES:
        assert f"{bootstrap.hash_key(role)}=" in text


def test_generated_password_verifies_against_stored_hash(env_file):
    created = bootstrap.ensure_env()
    hashes = bootstrap.role_hashes()

    for role, password in created.items():
        assert bcrypt.checkpw(password.encode(), hashes[role].encode())


def test_secret_key_placeholder_is_replaced(env_file):
    env_file.write_text("SECRET_KEY=change-me\n", encoding="utf-8")

    bootstrap.ensure_env()

    secret = bootstrap._get(env_file.read_text(encoding="utf-8"), "SECRET_KEY")
    assert secret != "change-me"
    assert len(secret) >= 8


def test_ensure_env_keeps_existing_hashes(env_file):
    first = bootstrap.ensure_env()
    before = bootstrap.role_hashes()

    second = bootstrap.ensure_env()

    assert second == {}
    assert bootstrap.role_hashes() == before
    assert first


def test_reset_role_passwords_replaces_every_hash(env_file):
    bootstrap.ensure_env()
    before = bootstrap.role_hashes()

    passwords = bootstrap.reset_role_passwords()
    after = bootstrap.role_hashes()

    assert set(passwords) == set(bootstrap.ROLES)
    for role in bootstrap.ROLES:
        assert after[role] != before[role]
        assert bcrypt.checkpw(passwords[role].encode(), after[role].encode())


@pytest.mark.parametrize("role", ["admin", "manager", "user"])
def test_login_accepts_password_generated_by_bootstrap(client, env_file, monkeypatch, role):
    """Сквозная проверка: пароль из `.env` доходит до `/auth/login` как есть."""
    import app.app.main as main_mod
    from app.app.config import Settings, get_settings

    env_file.write_text("SECRET_KEY=test-secret-key-32chars!!\n", encoding="utf-8")
    created = bootstrap.ensure_env()

    # Переменные окружения имеют приоритет над .env, а conftest их уже задал.
    for name in bootstrap.ROLES:
        monkeypatch.delenv(bootstrap.hash_key(name), raising=False)

    class _EnvFileSettings(Settings):
        model_config = SettingsConfigDict(
            env_file=str(env_file),
            env_file_encoding="utf-8",
            extra="ignore",
        )

    env_settings = _EnvFileSettings()
    main_mod.app.dependency_overrides[get_settings] = lambda: env_settings
    try:
        response = client.post(
            "/auth/login",
            json={
                "name": f"Bootstrap {role}",
                "telegram_id": f"tg-bootstrap-{role}",
                "password": created[role],
            },
        )
    finally:
        main_mod.app.dependency_overrides.pop(get_settings, None)

    assert response.status_code == 200, response.text
    assert response.json()["role"] == role


def test_bootstrap_runs_before_settings_are_cached():
    """Регрессия: `.env` должен готовиться до импорта конфигурации.

    Раньше подготовка вызывалась из `_run_api`, то есть уже после того, как
    `app.app.db` закешировал Settings с пустыми хешами, и вход всегда давал 401.
    """
    source = (bootstrap.ROOT / "app" / "app" / "main.py").read_text(encoding="utf-8")

    bootstrap_call = source.index("ensure_env()")
    config_import = source.index("from app.app.config import")
    db_import = source.index("from app.app.db import")

    assert bootstrap_call < config_import
    assert bootstrap_call < db_import


def test_authenticate_new_user_reads_fresh_hashes_after_reset(env_file, monkeypatch):
    """После `credentials --reset` API не должен требовать перезапуска."""
    from app.app.config import Settings
    from app.app.dependencies import authenticate_new_user

    env_file.write_text("SECRET_KEY=test-secret-key-32chars!!\n", encoding="utf-8")
    created = bootstrap.ensure_env()

    for name in bootstrap.ROLES:
        monkeypatch.delenv(bootstrap.hash_key(name), raising=False)

    stale_settings = Settings()
    old_admin_hash = stale_settings.admin_password_hash

    rotated = bootstrap.reset_role_passwords()
    assert rotated["admin"] != created["admin"]

    matched = authenticate_new_user(stale_settings, rotated["admin"])

    assert matched is not None
    role, fresh_hash = matched
    assert role == "admin"
    assert fresh_hash != old_admin_hash
    assert bcrypt.checkpw(rotated["admin"].encode(), fresh_hash.encode())
