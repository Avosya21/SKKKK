from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.app.services import edge_runtime as er


def test_ensure_caddy_skips_without_binary(monkeypatch, tmp_path):
    monkeypatch.setattr(er, "CADDY_EXE", tmp_path / "missing.exe")
    assert er.ensure_caddy(_settings()) == "skip"


def test_ensure_caddy_reloads_when_running(monkeypatch, tmp_path):
    exe = tmp_path / "caddy.exe"
    exe.write_text("", encoding="utf-8")
    cfg = tmp_path / "Caddyfile"
    cfg.write_text("# test\n", encoding="utf-8")
    monkeypatch.setattr(er, "CADDY_EXE", exe)
    monkeypatch.setattr(er, "CADDYFILE", cfg)
    monkeypatch.setattr(er, "CADDY_DIR", tmp_path)
    monkeypatch.setattr(er.subprocess, "run", lambda *a, **k: MagicMock(returncode=0))
    assert er.ensure_caddy(_settings()) == "reloaded"


def test_ensure_livekit_starts_compose(monkeypatch, tmp_path):
    calls: list[str] = []
    monkeypatch.setattr(er, "LIVEKIT_EXE", tmp_path / "missing.exe")
    monkeypatch.setattr(er, "ensure_livekit_binary", lambda: None)
    monkeypatch.setattr(er, "ensure_livekit_files", lambda: calls.append("files"))
    monkeypatch.setattr(er, "_open_livekit_firewall", lambda: None)
    monkeypatch.setattr("app.app.services.docker_runtime.docker_daemon_running", lambda: True)
    monkeypatch.setattr(
        er.subprocess,
        "run",
        lambda *a, **k: (calls.append("compose"), MagicMock(returncode=0))[1],
    )
    assert er.ensure_livekit() == "started"
    assert calls == ["files", "compose"]


def test_ensure_livekit_prefers_native_binary(monkeypatch, tmp_path):
    calls: list[str] = []
    exe = tmp_path / "livekit-server.exe"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(er, "LIVEKIT_EXE", exe)
    monkeypatch.setattr(er, "ensure_livekit_files", lambda: None)
    monkeypatch.setattr(er, "_open_livekit_firewall", lambda: None)
    monkeypatch.setattr(er, "ensure_livekit_binary", lambda: exe)
    monkeypatch.setattr(er, "_stop_docker_livekit", lambda: calls.append("stop-docker"))
    monkeypatch.setattr(er, "_start_native_livekit", lambda path: calls.append(path.name) or "started")
    assert er.ensure_livekit() == "started"
    assert calls == ["stop-docker", "livekit-server.exe"]


def test_start_native_reloads_when_yaml_changes(monkeypatch, tmp_path):
    calls: list[str] = []
    monkeypatch.setattr(er, "_port_open", lambda _port: True)
    monkeypatch.setattr(er, "_yaml_stamp_matches", lambda: False)
    monkeypatch.setattr(er, "_stop_native_livekit", lambda: calls.append("stop"))
    monkeypatch.setattr(er, "_spawn_native_livekit", lambda _exe: calls.append("spawn"))
    assert er._start_native_livekit(tmp_path / "livekit-server.exe") == "reloaded"
    assert calls == ["stop", "spawn"]


def test_pid_running_alive_and_dead():
    assert er._pid_running(os.getpid()) is True
    assert er._pid_running(0) is False
    assert er._pid_running(999_999_999) is False


def test_ensure_screen_recorder_skips_without_script(monkeypatch, tmp_path):
    monkeypatch.setattr(er, "edge_disabled", lambda: False)  # эти тесты проверяют сам запуск (с подменой Popen)
    monkeypatch.setattr(er, "VOICE_PY", tmp_path / "python.exe")
    monkeypatch.setattr(er, "SCREEN_RECORDER", tmp_path / "missing.py")
    monkeypatch.setattr(er, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path))
    assert er.ensure_screen_recorder() == "skip"


def test_ensure_voice_cleaner_skips_without_venv(monkeypatch, tmp_path):
    monkeypatch.setattr(er, "edge_disabled", lambda: False)  # эти тесты проверяют сам запуск (с подменой Popen)
    monkeypatch.setattr(er, "VOICE_PY", tmp_path / "missing.exe")
    monkeypatch.setattr(er, "VOICE_CLEANER", tmp_path / "voice_cleaner.py")
    monkeypatch.setattr(er, "get_settings", lambda: SimpleNamespace(meeting_voice_server=True))
    assert er.ensure_voice_cleaner() == "skip"


def test_ensure_voice_cleaner_reports_failed_if_process_exits(monkeypatch, tmp_path):
    monkeypatch.setattr(er, "edge_disabled", lambda: False)  # эти тесты проверяют сам запуск (с подменой Popen)
    py = tmp_path / "python.exe"
    py.write_text("", encoding="utf-8")
    script = tmp_path / "voice_cleaner.py"
    script.write_text("", encoding="utf-8")
    monkeypatch.setattr(er, "VOICE_PY", py)
    monkeypatch.setattr(er, "VOICE_CLEANER", script)
    monkeypatch.setattr(er, "_wait_port", lambda *a, **k: True)
    monkeypatch.setattr(er, "_pid_running", lambda _pid: False)
    monkeypatch.setattr(er.time, "sleep", lambda *_: None)

    class _Dead:
        pid = 11
        returncode = 1

        def poll(self):
            return 1

    monkeypatch.setattr(er.subprocess, "Popen", lambda *a, **k: _Dead())
    monkeypatch.setattr(
        er,
        "get_settings",
        lambda: SimpleNamespace(
            meeting_voice_server=True,
            livekit_rtc_internal="ws://127.0.0.1:7880",
            data_dir=tmp_path,
        ),
    )
    assert er.ensure_voice_cleaner() == "failed"


def test_ensure_edge_starts_voice_after_livekit(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(er, "ensure_tls_cert", lambda _s: calls.append("tls") or "ok")
    monkeypatch.setattr(er, "ensure_caddy", lambda _s: calls.append("caddy") or "ok")
    monkeypatch.setattr(er, "ensure_livekit", lambda: calls.append("livekit") or "started")
    monkeypatch.setattr(er, "ensure_voice_cleaner", lambda: calls.append("voice") or "started")
    monkeypatch.setattr(er, "ensure_screen_recorder", lambda: calls.append("screen") or "started")
    er.ensure_edge_services(_settings())
    assert calls == ["tls", "caddy", "livekit", "voice", "screen"]


def test_ensure_edge_services_keeps_portal_if_livekit_fails(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(er, "ensure_tls_cert", lambda _s: calls.append("tls") or "ok")
    monkeypatch.setattr(er, "ensure_caddy", lambda _s: calls.append("caddy") or "reloaded")
    monkeypatch.setattr(er, "ensure_livekit", lambda: (_ for _ in ()).throw(RuntimeError("docker down")))
    er.ensure_edge_services(_settings())
    assert calls == ["tls", "caddy"]


def test_caddyfile_issues_shortlived_ip_cert():
    text = (Path(__file__).resolve().parents[3] / "tools" / "caddy" / "Caddyfile").read_text(
        encoding="utf-8"
    )
    assert "profile shortlived" in text
    assert "disable_tlsalpn_challenge" in text
    assert "acme-challenge" in text
    assert "SKKKK_ACME_TLS_PORT" in text
    assert "force_automate" in text


def _settings():
    return SimpleNamespace(
        skkkk_public_host="203.0.113.10",
        skkkk_https_port=777,
        skkkk_acme_http_port=8088,
        skkkk_acme_email="ops@example.test",
        skkkk_acme_ca="",
        skkkk_acme_tls_port=8443,
        skkkk_tls_cert="",
        skkkk_tls_key="",
        web_port=1777,
        data_dir=Path("C:/1C_NEW_SKKKK/data"),
    )


def test_check_served_certificate_reloads_only_on_mismatch(monkeypatch, tmp_path):
    settings = SimpleNamespace(skkkk_public_host="1.2.3.4", skkkk_https_port=777)
    monkeypatch.setattr(er, "tls_paths", lambda _s: (tmp_path / "c.crt", tmp_path / "c.key"))
    monkeypatch.setattr(er, "_file_cert_serial", lambda _p: 200)
    reloads: list[int] = []
    monkeypatch.setattr(er, "ensure_caddy", lambda _s: reloads.append(1) or "reloaded")
    monkeypatch.setattr(er.time, "sleep", lambda _s: None)

    def serve(*values):
        seq = iter(values)
        monkeypatch.setattr(er, "_served_cert_serial", lambda _h, _p: next(seq))

    serve(200)  # порт отдаёт то же, что на диске
    assert er.check_served_certificate(settings) == "ok" and reloads == []
    serve(100, 200)  # отдавал старый, после перезагрузки новый
    assert er.check_served_certificate(settings) == "reloaded" and reloads == [1]
    serve(100, 100)  # перезагрузка не помогла: сообщаем, а не молчим
    assert er.check_served_certificate(settings) == "failed"


def test_check_served_certificate_skips_without_public_host_or_file(monkeypatch, tmp_path):
    assert er.check_served_certificate(SimpleNamespace(skkkk_public_host="", skkkk_https_port=777)) == "skip"
    monkeypatch.setattr(er, "tls_paths", lambda _s: (tmp_path / "missing.crt", tmp_path / "k"))
    assert er.check_served_certificate(SimpleNamespace(skkkk_public_host="h", skkkk_https_port=777)) == "skip"


def test_recorders_are_never_started_under_tests(monkeypatch, tmp_path):
    """Регрессия: сторожевой цикл в тестовом приложении запускал НАСТОЯЩИЕ рекордеры (в пустой временной папке «уже запущенных» нет),
    и после каждого прогона тестов оставалась ещё одна копия; копии выбивали друг друга из комнаты и рвали запись созвона."""
    started: list[int] = []
    monkeypatch.setattr(er.subprocess, "Popen", lambda *a, **k: started.append(1))
    assert er.edge_disabled() is True  # идут тесты
    assert er.ensure_voice_cleaner() == "off"
    assert er.ensure_screen_recorder() == "off"
    assert started == []


def test_voice_venv_ready_checks_like_the_cleaner(monkeypatch, tmp_path):
    """Готовность шумодава проверяется с той же подменой torchaudio.backend, что делает voice_cleaner: голый
    ``import df.enhance`` падает на torchaudio 2.9+ и давал ложное «чистильщик не стартует»."""
    py = tmp_path / "python.exe"
    py.write_text("")
    monkeypatch.setattr(er, "VOICE_PY", py)
    calls = []
    monkeypatch.setattr(er.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or SimpleNamespace(returncode=0))
    assert er.voice_venv_ready() is True
    cmd = calls[0]
    assert cmd[0] == str(py) and cmd[1] == "-c"
    assert "voice_cleaner._ensure_df_io_compat()" in cmd[2] and "import df.enhance" in cmd[2]
    assert cmd[3] == str(er.VOICE_CLEANER.parent)
