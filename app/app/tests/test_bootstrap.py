import pytest

from app.app import bootstrap


@pytest.fixture()
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    monkeypatch.setattr(bootstrap, "ENV_PATH", path)
    monkeypatch.setattr(bootstrap, "ENV_EXAMPLE", tmp_path / ".env.example")
    return path


def test_ensure_env_creates_secret_key(env_file):
    created = bootstrap.ensure_env()
    assert created == {}
    assert env_file.is_file()
    secret = bootstrap._get(env_file.read_text(encoding="utf-8"), "SECRET_KEY")
    assert len(secret) >= 8
    assert secret != "change-me"


def test_secret_key_placeholder_is_replaced(env_file):
    env_file.write_text("SECRET_KEY=change-me\n", encoding="utf-8")
    bootstrap.ensure_env()
    secret = bootstrap._get(env_file.read_text(encoding="utf-8"), "SECRET_KEY")
    assert secret != "change-me"
    assert len(secret) >= 8


def test_ensure_env_keeps_existing_secret(env_file):
    env_file.write_text("SECRET_KEY=already-set-secret-key!!\n", encoding="utf-8")
    assert bootstrap.ensure_env() == {}
    secret = bootstrap._get(env_file.read_text(encoding="utf-8"), "SECRET_KEY")
    assert secret == "already-set-secret-key!!"


def test_bootstrap_runs_before_settings_are_cached():
    """Регрессия: `.env` должен готовиться до импорта конфигурации."""
    source = (bootstrap.ROOT / "app" / "app" / "main.py").read_text(encoding="utf-8")
    bootstrap_call = source.index("ensure_env()")
    config_import = source.index("from app.app.config import")
    db_import = source.index("from app.app.db import")
    assert bootstrap_call < config_import
    assert bootstrap_call < db_import
