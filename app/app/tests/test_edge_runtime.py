from __future__ import annotations

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


def test_ensure_voice_cleaner_skips_without_venv(monkeypatch, tmp_path):
    monkeypatch.setattr(er, "VOICE_PY", tmp_path / "missing.exe")
    monkeypatch.setattr(er, "VOICE_CLEANER", tmp_path / "voice_cleaner.py")
    monkeypatch.setattr(er, "get_settings", lambda: SimpleNamespace(meeting_voice_server=True))
    assert er.ensure_voice_cleaner() == "skip"


def test_ensure_voice_cleaner_reports_failed_if_process_exits(monkeypatch, tmp_path):
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
    er.ensure_edge_services(_settings())
    assert calls == ["tls", "caddy", "livekit", "voice"]


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
