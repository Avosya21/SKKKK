import socket

import pytest

from app.app import preflight
from app.app.config import Settings
from app.app.preflight import FAIL, OK, WARN, CheckResult


def _settings(tmp_path, **overrides):
    base = {
        "secret_key": "test-secret-key-32chars!!",
        "data_dir": tmp_path / "data",
        "database_url": f"sqlite:///{(tmp_path / 'check.db').as_posix()}",
        "dry_run": True,
    }
    base.update(overrides)
    return Settings(**base)


def test_python_check_passes_on_supported_version():
    assert preflight.check_python().status == OK


def test_data_dir_check_creates_and_reports_free_space(tmp_path):
    settings = _settings(tmp_path)
    result = preflight.check_data_dir(settings)
    assert result.status in {OK, WARN}
    assert settings.data_dir.is_dir()


def test_env_check_fails_without_role_passwords(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("SECRET_KEY=test-secret-key-32chars!!\n", encoding="utf-8")

    from app.app import bootstrap

    monkeypatch.setattr(bootstrap, "ENV_PATH", env_path)
    monkeypatch.setattr(bootstrap, "ENV_EXAMPLE", tmp_path / "absent")

    result = preflight.check_env(_settings(tmp_path))
    assert result.status == FAIL
    assert "credentials --reset" in result.hint


def test_port_check_detects_occupied_port(tmp_path):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        settings = _settings(tmp_path, web_host="127.0.0.1", web_port=port)
        assert preflight.check_port(settings).status == FAIL

    settings = _settings(tmp_path, web_host="127.0.0.1", web_port=port)
    assert preflight.check_port(settings).status == OK


def test_ollama_check_ignores_model_name_case(tmp_path, monkeypatch):
    """Ollama отдаёт `BGE-M3:latest`, а в настройках указано `bge-m3`."""
    settings = _settings(tmp_path, dry_run=False, ollama_embed_model="bge-m3")
    monkeypatch.setattr(
        "app.app.services.ollama_client.ollama_tags",
        lambda *a, **k: ["BGE-M3:latest"],
    )
    assert preflight.check_ollama(settings).status == OK


def test_ollama_check_fails_when_model_absent(tmp_path, monkeypatch):
    settings = _settings(tmp_path, dry_run=False, ollama_embed_model="bge-m3")
    monkeypatch.setattr(
        "app.app.services.ollama_client.ollama_tags",
        lambda *a, **k: ["llama3:latest"],
    )
    result = preflight.check_ollama(settings)
    assert result.status == FAIL
    assert "ollama pull" in result.hint


def test_broken_check_becomes_failure_not_crash(tmp_path, monkeypatch):
    def _explode(_settings, _mode):
        def _boom():
            raise RuntimeError("сломалось")

        return [("database", _boom)]

    monkeypatch.setattr(preflight, "_plan", _explode)
    results = preflight.run_preflight(_settings(tmp_path), mode="all")

    assert len(results) == 1
    assert results[0].status == FAIL
    assert "сломалось" in results[0].detail


def test_summarize_counts_each_status():
    results = [
        CheckResult("a", OK),
        CheckResult("b", WARN),
        CheckResult("c", FAIL),
        CheckResult("d", FAIL),
    ]
    failed, warned = preflight.summarize(results)
    assert (failed, warned) == (2, 1)


def test_dry_run_skips_external_services(tmp_path):
    plan = dict(preflight._plan(_settings(tmp_path, dry_run=True), "all"))
    assert "dry_run" in plan
    assert "llm" not in plan
    assert "ollama" not in plan


def test_worker_mode_checks_the_queue_slot_but_not_the_port(tmp_path):
    plan = dict(preflight._plan(_settings(tmp_path), "worker"))
    assert "worker_slot" in plan
    assert "port" not in plan


@pytest.mark.parametrize("mode", ["all", "api"])
def test_api_modes_check_the_port(tmp_path, mode):
    plan = dict(preflight._plan(_settings(tmp_path), mode))
    assert "port" in plan
    assert "meeting_voice" in plan


def test_worker_mode_skips_meeting_voice(tmp_path):
    plan = dict(preflight._plan(_settings(tmp_path), "worker"))
    assert "meeting_voice" not in plan


def test_meeting_voice_ok_when_venv_exists(tmp_path, monkeypatch):
    py = tmp_path / "python.exe"
    py.write_text("", encoding="utf-8")
    monkeypatch.setattr("app.app.services.edge_runtime.VOICE_PY", py)
    monkeypatch.setattr("app.app.services.edge_runtime.voice_venv_ready", lambda: True)
    result = preflight.check_meeting_voice(_settings(tmp_path, meeting_voice_server=True, dry_run=False))
    assert result.status == OK


def test_meeting_voice_warns_without_venv(tmp_path, monkeypatch):
    monkeypatch.setattr("app.app.services.edge_runtime.VOICE_PY", tmp_path / "missing.exe")
    result = preflight.check_meeting_voice(_settings(tmp_path, meeting_voice_server=True, dry_run=False))
    assert result.status == WARN


def test_meeting_voice_warns_if_deps_missing(tmp_path, monkeypatch):
    py = tmp_path / "python.exe"
    py.write_text("", encoding="utf-8")
    monkeypatch.setattr("app.app.services.edge_runtime.VOICE_PY", py)
    monkeypatch.setattr("app.app.services.edge_runtime.voice_venv_ready", lambda: False)
    result = preflight.check_meeting_voice(_settings(tmp_path, meeting_voice_server=True, dry_run=False))
    assert result.status == WARN


def test_personal_passwords_check(monkeypatch):
    from app.app.config import Settings
    from app.app.preflight import OK, check_personal_passwords
    from app.app.services import personal_password

    off = check_personal_passwords(Settings(personal_password="off"))
    assert off.status == OK and "выключены" in off.detail
    personal_password.set_initial("admin", "correct-horse-battery", "correct-horse-battery")
    on = check_personal_passwords(Settings(personal_password="required"))
    assert on.status == OK and "задано: 1" in on.detail and "required" in on.detail
