"""Keys and livekit.yaml for the Meeting Mind room."""

from __future__ import annotations

import re
import secrets
import socket
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ENV_PATH = ROOT / ".env"
YAML_PATH = ROOT / "tools" / "livekit" / "livekit.yaml"

URL_KEY = "LIVEKIT_URL"
API_KEY = "LIVEKIT_API_KEY"
API_SECRET = "LIVEKIT_API_SECRET"
NODE_IP_KEY = "LIVEKIT_NODE_IP"
LAN_IP_KEY = "LIVEKIT_LAN_IP"
TURN_UDP_PORT = 3478
RTC_TCP_PORT = 7881
RTC_UDP_MUX_PORT = 50000
RTC_UDP_START = 50000
RTC_UDP_END = 50011
TURN_RELAY_START = 50001
LOOPBACK_IP = "127.0.0.1"
DETECT_ROUTE_HOST = "1.1.1.1"
DETECT_ROUTE_PORT = 80


def is_rfc1918(ip: str) -> bool:
    if ip.startswith(("10.", "192.168.")):
        return True
    if ip.startswith("172."):
        try:
            second = int(ip.split(".")[1])
        except (IndexError, ValueError):
            return False
        return 16 <= second <= 31
    return False


def _lan_rank(ip: str) -> int:
    if ip.startswith("192.168."):
        return 0
    if ip.startswith("172."):
        return 1
    if ip.startswith("10."):
        return 2
    return 9


def preferred_lan_ip(candidates: list[str]) -> str:
    rfc = [ip.strip() for ip in candidates if ip and is_rfc1918(ip.strip())]
    if not rfc:
        return ""
    return sorted(rfc, key=_lan_rank)[0]


def pick_advertise_ip(*, lan_env: str, public_host: str, detected: str) -> str:
    picked = preferred_lan_ip([lan_env, detected])
    return picked or public_host.strip()


def detect_lan_ip() -> str:
    found: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if is_rfc1918(ip):
                found.append(ip)
    except OSError:
        pass
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((DETECT_ROUTE_HOST, DETECT_ROUTE_PORT))
        found.append(sock.getsockname()[0])
    except OSError:
        pass
    finally:
        sock.close()
    return preferred_lan_ip(found)


def _public_node_ip(turn_domain: str) -> str:
    host = (turn_domain or "").strip()
    if not host or is_rfc1918(host) or host == LOOPBACK_IP:
        return ""
    return host


def build_livekit_yaml(*, node_ip: str, turn_domain: str, api_key: str, api_secret: str) -> str:
    public_ip = _public_node_ip(turn_domain)
    rtc = [
        "rtc:",
        f"  tcp_port: {RTC_TCP_PORT}",
        f"  udp_port: {RTC_UDP_MUX_PORT}",
        "  use_external_ip: false",
        "  enable_loopback_candidate: true",
        "  stun_servers: []",
    ]
    if public_ip:
        rtc.append(f"  node_ip: {public_ip}")
    rtc.extend(
        [
            "  ips:",
            "    includes:",
            f"      - {node_ip}/32",
            f"      - {LOOPBACK_IP}/32",
        ]
    )
    return "\n".join(
        [
            "port: 7880",
            "bind_addresses:",
            '  - ""',
            *rtc,
            "turn:",
            "  enabled: true",
            f"  domain: {turn_domain}",
            f"  udp_port: {TURN_UDP_PORT}",
            f"  relay_range_start: {TURN_RELAY_START}",
            f"  relay_range_end: {RTC_UDP_END}",
            "keys:",
            f"  {api_key}: {api_secret}",
            "logging:",
            "  level: info",
            "",
        ]
    )


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _upsert_env(path: Path, updates: dict[str, str]) -> None:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        matched = False
        for key, value in updates.items():
            if re.match(rf"^{re.escape(key)}=", line):
                out.append(f"{key}={value}")
                seen.add(key)
                matched = True
                break
        if not matched:
            out.append(line)
    missing = [key for key in updates if key not in seen]
    if missing:
        if out and out[-1].strip():
            out.append("")
        out.append("# --- Meeting Mind / LiveKit ---")
        for key in missing:
            out.append(f"{key}={updates[key]}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def ensure_livekit_files() -> None:
    env = _read_env(ENV_PATH)
    host = env.get("PRIMARY_ANALITICS_PUBLIC_HOST") or "127.0.0.1"
    https_port = env.get("PRIMARY_ANALITICS_HTTPS_PORT") or "777"
    url = (env.get(URL_KEY) or "").strip() or f"wss://{host}:{https_port}/livekit"
    key = (env.get(API_KEY) or "").strip() or ("primaryanalitics" + secrets.token_hex(8))
    secret = (env.get(API_SECRET) or "").strip() or secrets.token_urlsafe(32)
    lan_ip = pick_advertise_ip(
        lan_env=(env.get(LAN_IP_KEY) or "").strip(),
        public_host=host,
        detected=detect_lan_ip(),
    )
    _upsert_env(
        ENV_PATH,
        {
            URL_KEY: url,
            API_KEY: key,
            API_SECRET: secret,
            NODE_IP_KEY: host,
            LAN_IP_KEY: lan_ip,
        },
    )
    YAML_PATH.parent.mkdir(parents=True, exist_ok=True)
    YAML_PATH.write_text(
        build_livekit_yaml(node_ip=lan_ip, turn_domain=host, api_key=key, api_secret=secret),
        encoding="utf-8",
    )
