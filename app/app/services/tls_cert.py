"""Публичный сертификат :777 — проверка при старте и перевыпуск через Caddy :8443."""

from __future__ import annotations

import logging
import socket
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlparse

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtensionOID, NameOID

from app.app.config import Settings
from app.app.logging_setup import log_event

logger = logging.getLogger("primaryanalitics.tls")

PRODUCTION_ACME_CA = "https://acme-v02.api.letsencrypt.org/directory"
TRUSTED_ISSUER_MARK = "let's encrypt"
STAGING_MARKERS = ("staging", "fake le", "artificial")
BOOTSTRAP_ISSUER_CN = "PrimaryAnalitics bootstrap"
BOOTSTRAP_HOURS = 24
ISSUE_POLL_SEC = 2
TRIGGER_ATTEMPTS = 5
TRIGGER_WAIT_SEC = 1
SOCKET_TIMEOUT_SEC = 8

REASON_MISSING = "missing"
REASON_EXPIRED = "expired"
REASON_EXPIRING = "expiring"
REASON_STAGING = "staging"
REASON_HOST = "host"
REASON_ISSUER = "issuer"
REASON_UNREADABLE = "unreadable"


@dataclass(frozen=True)
class CertInfo:
    path: Path
    not_after: datetime | None = None
    names: tuple[str, ...] = ()
    issuer: str = ""
    reason: str = ""

    @property
    def hours_left(self) -> float | None:
        if self.not_after is None:
            return None
        return (self.not_after - datetime.now(timezone.utc)).total_seconds() / 3600


def _acme_storage_name(ca_url: str) -> str:
    parsed = urlparse((ca_url or PRODUCTION_ACME_CA).rstrip("/"))
    path = parsed.path.strip("/").replace("/", "-")
    return f"{parsed.netloc}-{path}" if path else parsed.netloc


def _is_production_ca(ca_url: str) -> bool:
    ca = (ca_url or PRODUCTION_ACME_CA).lower()
    return "acme-v02.api.letsencrypt.org" in ca and "staging" not in ca


def _is_staging_issuer(issuer: str) -> bool:
    text = issuer.lower()
    return any(mark in text for mark in STAGING_MARKERS)


def _is_trusted_issuer(issuer: str) -> bool:
    return TRUSTED_ISSUER_MARK in issuer.lower() and not _is_staging_issuer(issuer)


def _storage_tls_paths(settings: Settings) -> tuple[Path, Path]:
    host = (settings.primary_analitics_public_host or "").strip()
    folder = settings.data_dir / "caddy" / "certificates" / _acme_storage_name(settings.primary_analitics_acme_ca) / host
    return folder / f"{host}.crt", folder / f"{host}.key"


def tls_paths(settings: Settings) -> tuple[Path, Path]:
    stored_cert, stored_key = _storage_tls_paths(settings)
    raw_cert = (settings.primary_analitics_tls_cert or "").strip()
    raw_key = (settings.primary_analitics_tls_key or "").strip()
    if not raw_cert:
        return stored_cert, stored_key
    explicit_cert = Path(raw_cert)
    explicit_key = Path(raw_key) if raw_key else stored_key
    if _is_production_ca(settings.primary_analitics_acme_ca):
        info = inspect_cert(explicit_cert)
        if info.reason == REASON_MISSING or _is_staging_issuer(info.issuer):
            return stored_cert, stored_key
    return explicit_cert, explicit_key


def inspect_cert(path: Path) -> CertInfo:
    if not path.is_file():
        return CertInfo(path=path, reason=REASON_MISSING)
    try:
        cert = x509.load_pem_x509_certificate(path.read_bytes())
    except Exception:
        return CertInfo(path=path, reason=REASON_UNREADABLE)
    return CertInfo(
        path=path,
        not_after=cert.not_valid_after_utc,
        names=_cert_names(cert),
        issuer=cert.issuer.rfc4514_string(),
    )


def _cert_names(cert: x509.Certificate) -> tuple[str, ...]:
    names = [item.value for item in cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)]
    try:
        san = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value
    except x509.ExtensionNotFound:
        return tuple(names)
    names.extend(str(item) for item in san.get_values_for_type(x509.DNSName))
    names.extend(str(item) for item in san.get_values_for_type(x509.IPAddress))
    return tuple(names)


def cert_issue_reason(info: CertInfo, settings: Settings) -> str:
    if info.reason:
        return info.reason
    host = (settings.primary_analitics_public_host or "").strip()
    if host and host not in info.names:
        return REASON_HOST
    if _is_production_ca(settings.primary_analitics_acme_ca) and _is_staging_issuer(info.issuer):
        return REASON_STAGING
    if _is_production_ca(settings.primary_analitics_acme_ca) and not _is_trusted_issuer(info.issuer):
        return REASON_ISSUER
    hours = info.hours_left
    if hours is None:
        return REASON_UNREADABLE
    if hours <= 0:
        return REASON_EXPIRED
    if hours <= settings.primary_analitics_tls_renew_hours:
        return REASON_EXPIRING
    return ""


def write_bootstrap_cert(cert_path: Path, key_path: Path, host: str) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host or BOOTSTRAP_ISSUER_CN)])
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, BOOTSTRAP_ISSUER_CN)])
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1))
        .not_valid_after(now + timedelta(hours=BOOTSTRAP_HOURS))
    )
    builder = builder.add_extension(x509.SubjectAlternativeName([_san_for_host(host)]), critical=False)
    cert = builder.sign(key, hashes.SHA256())
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )


def _san_for_host(host: str) -> x509.GeneralName:
    try:
        return x509.IPAddress(ip_address(host))
    except ValueError:
        return x509.DNSName(host)


def _start_caddy(settings: Settings) -> None:
    from app.app.services.edge_runtime import ensure_caddy

    ensure_caddy(settings)


def _reload_caddy(settings: Settings) -> None:
    _start_caddy(settings)


def _trigger_issue(settings: Settings) -> None:
    host = (settings.primary_analitics_public_host or "").strip()
    port = settings.primary_analitics_acme_tls_port
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    last_error = ""
    for _attempt in range(TRIGGER_ATTEMPTS):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=SOCKET_TIMEOUT_SEC) as sock:
                with ctx.wrap_socket(sock, server_hostname=host) as tls:
                    request = f"GET / HTTP/1.0\r\nHost: {host}\r\n\r\n".encode()
                    tls.sendall(request)
                    tls.recv(256)
            return
        except OSError as exc:
            last_error = str(exc)
            time.sleep(TRIGGER_WAIT_SEC)
    raise RuntimeError(f"acme :{port}: {last_error}")


def _wait_valid(settings: Settings, cert_path: Path) -> bool:
    deadline = time.time() + settings.primary_analitics_tls_issue_timeout_sec
    while time.time() <= deadline:
        if not cert_issue_reason(inspect_cert(cert_path), settings):
            return True
        time.sleep(min(ISSUE_POLL_SEC, settings.primary_analitics_tls_issue_timeout_sec))
    return False


def ensure_tls_cert(
    settings: Settings,
    *,
    start_caddy=None,
    reload_caddy=None,
    trigger=None,
) -> str:
    host = (settings.primary_analitics_public_host or "").strip()
    if not host:
        log_event(logger, "edge.tls.skip", reason="no_public_host")
        return "skip"
    start = start_caddy or _start_caddy
    reload = reload_caddy or _reload_caddy
    issue = trigger or _trigger_issue
    try:
        return _ensure_tls_cert(settings, start=start, reload=reload, issue=issue)
    except Exception as exc:
        log_event(logger, "edge.tls.failed", level=logging.WARNING, error=str(exc))
        return "failed"


def _ensure_tls_cert(settings: Settings, *, start, reload, issue) -> str:
    cert_path, key_path = tls_paths(settings)
    if not cert_path.is_file() or not key_path.is_file():
        write_bootstrap_cert(cert_path, key_path, settings.primary_analitics_public_host.strip())
        log_event(logger, "edge.tls.bootstrap", path=str(cert_path))
    start(settings)
    info = inspect_cert(cert_path)
    reason = cert_issue_reason(info, settings)
    if not reason:
        log_event(
            logger,
            "edge.tls.ok",
            not_after=info.not_after.isoformat() if info.not_after else "",
            hours_left=round(info.hours_left or 0, 1),
        )
        return "ok"
    log_event(logger, "edge.tls.reissue", reason=reason, path=str(cert_path))
    issue(settings)
    if not _wait_valid(settings, cert_path):
        log_event(logger, "edge.tls.failed", level=logging.WARNING, reason=reason)
        return "failed"
    reload(settings)
    log_event(logger, "edge.tls.renewed", reason=reason)
    return "renewed"
