from pathlib import Path

from app.app.services.livekit_env import (
    build_livekit_yaml,
    ensure_livekit_files,
    is_rfc1918,
    pick_advertise_ip,
    preferred_lan_ip,
)


def test_build_livekit_yaml_uses_lan_host_and_public_turn():
    text = build_livekit_yaml(
        node_ip="192.168.0.50",
        turn_domain="203.0.113.10",
        api_key="k",
        api_secret="s",
    )
    assert "node_ip: 203.0.113.10" in text
    assert "stun_servers: []" in text
    assert "- 192.168.0.50/32" in text
    assert "- 127.0.0.1/32" in text
    assert "domain: 203.0.113.10" in text
    assert "use_external_ip: false" in text
    assert "enable_loopback_candidate: true" in text
    assert "udp_port: 50000" in text
    assert "port_range_start" not in text
    assert "udp_port: 3478" in text
    assert "relay_range_start: 50001" in text
    assert "relay_range_end: 50011" in text


def test_build_livekit_yaml_skips_public_node_ip_when_turn_is_lan():
    text = build_livekit_yaml(
        node_ip="192.168.0.50",
        turn_domain="192.168.0.50",
        api_key="k",
        api_secret="s",
    )
    assert "node_ip:" not in text


def test_pick_advertise_ip_prefers_lan_over_public():
    assert is_rfc1918("192.168.0.50")
    assert is_rfc1918("10.1.2.3")
    assert not is_rfc1918("203.0.113.10")
    assert preferred_lan_ip(["10.73.168.177", "192.168.0.50"]) == "192.168.0.50"
    assert pick_advertise_ip(lan_env="192.168.0.50", public_host="203.0.113.10", detected="") == "192.168.0.50"
    assert pick_advertise_ip(lan_env="10.73.168.177", public_host="203.0.113.10", detected="192.168.0.50") == "192.168.0.50"
    assert pick_advertise_ip(lan_env="", public_host="203.0.113.10", detected="192.168.0.10") == "192.168.0.10"
    assert pick_advertise_ip(lan_env="", public_host="203.0.113.10", detected="") == "203.0.113.10"


def test_ensure_livekit_files_writes_lan_node_ip(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "\n".join(
            [
                "SKKKK_PUBLIC_HOST=203.0.113.10",
                "LIVEKIT_API_KEY=devkey",
                "LIVEKIT_API_SECRET=devsecret",
                "LIVEKIT_URL=wss://203.0.113.10:777/livekit",
                "LIVEKIT_NODE_IP=203.0.113.10",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    yaml_path = tmp_path / "livekit.yaml"
    monkeypatch.setattr("app.app.services.livekit_env.ENV_PATH", env)
    monkeypatch.setattr("app.app.services.livekit_env.YAML_PATH", yaml_path)
    monkeypatch.setattr("app.app.services.livekit_env.detect_lan_ip", lambda: "192.168.0.50")
    ensure_livekit_files()
    text = yaml_path.read_text(encoding="utf-8")
    assert "- 192.168.0.50/32" in text
    assert "node_ip: 203.0.113.10" in text
    assert "domain: 203.0.113.10" in text
    assert "devsecret" in text
    assert "LIVEKIT_LAN_IP=192.168.0.50" in env.read_text(encoding="utf-8")
