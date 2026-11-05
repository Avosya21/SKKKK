from __future__ import annotations

from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
from pathlib import Path
from types import SimpleNamespace

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app.app.services import tls_cert as tc


PUBLIC_HOST = "203.0.113.10"
PRODUCTION_CA = "https://acme-v02.api.letsencrypt.org/directory"


def _settings(tmp_path: Path, **overrides):
    base = dict(
        primary_analitics_public_host=PUBLIC_HOST,
        primary_analitics_acme_ca="",
        primary_analitics_tls_cert="",
        primary_analitics_tls_key="",
        primary_analitics_tls_renew_hours=48,
        primary_analitics_tls_issue_timeout_sec=1,
        primary_analitics_acme_tls_port=8443,
        data_dir=tmp_path,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _write_cert(
    path: Path,
    *,
    host: str,
    hours: int,
    issuer_cn: str,
    subject_cn: str | None = None,
) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    after = now + timedelta(hours=hours)
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer_cn)])
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject_cn or host)])
    try:
        san = x509.IPAddress(ip_address(host))
    except ValueError:
        san = x509.DNSName(host)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(after - timedelta(hours=24))
        .not_valid_after(after)
        .add_extension(x509.SubjectAlternativeName([san]), critical=False)
        .sign(key, hashes.SHA256())
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    path.with_suffix(".key").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )


def test_tls_paths_use_caddy_storage_for_production_ca(tmp_path):
    cert, key = tc.tls_paths(_settings(tmp_path))
    folder = tmp_path / "caddy" / "certificates" / "acme-v02.api.letsencrypt.org-directory" / PUBLIC_HOST
    assert cert == folder / f"{PUBLIC_HOST}.crt"
    assert key == folder / f"{PUBLIC_HOST}.key"


def test_tls_paths_honor_explicit_files(tmp_path):
    cert = tmp_path / "custom.crt"
    key = tmp_path / "custom.key"
    _write_cert(cert, host=PUBLIC_HOST, hours=80, issuer_cn="Let's Encrypt")
    settings = _settings(tmp_path, primary_analitics_tls_cert=str(cert), primary_analitics_tls_key=str(key))
    assert tc.tls_paths(settings) == (cert, key)


def test_tls_paths_ignore_staging_override_when_ca_is_production(tmp_path):
    staging = tmp_path / "staging.crt"
    _write_cert(staging, host=PUBLIC_HOST, hours=80, issuer_cn="(STAGING) Artificial Amaranth")
    settings = _settings(tmp_path, primary_analitics_tls_cert=str(staging), primary_analitics_tls_key=str(tmp_path / "staging.key"))
    cert, _key = tc.tls_paths(settings)
    assert cert != staging
    assert cert.name == f"{PUBLIC_HOST}.crt"


def test_inspect_missing_cert(tmp_path):
    info = tc.inspect_cert(tmp_path / "gone.crt")
    assert info.reason == tc.REASON_MISSING


def test_valid_production_cert_does_not_need_issue(tmp_path):
    path = tmp_path / "ok.crt"
    _write_cert(path, host=PUBLIC_HOST, hours=80, issuer_cn="Let's Encrypt")
    info = tc.inspect_cert(path)
    assert tc.cert_issue_reason(info, _settings(tmp_path)) == ""
    assert PUBLIC_HOST in info.names


def test_expired_and_expiring_and_staging_need_issue(tmp_path):
    settings = _settings(tmp_path)
    expired = tmp_path / "expired.crt"
    _write_cert(expired, host=PUBLIC_HOST, hours=-2, issuer_cn="Let's Encrypt")
    assert tc.cert_issue_reason(tc.inspect_cert(expired), settings) == tc.REASON_EXPIRED

    expiring = tmp_path / "expiring.crt"
    _write_cert(expiring, host=PUBLIC_HOST, hours=10, issuer_cn="Let's Encrypt")
    assert tc.cert_issue_reason(tc.inspect_cert(expiring), settings) == tc.REASON_EXPIRING

    staging = tmp_path / "staging.crt"
    _write_cert(staging, host=PUBLIC_HOST, hours=80, issuer_cn="(STAGING) Artificial Amaranth")
    assert tc.cert_issue_reason(tc.inspect_cert(staging), settings) == tc.REASON_STAGING


def test_host_mismatch_needs_issue(tmp_path):
    path = tmp_path / "other.crt"
    _write_cert(path, host="10.0.0.1", hours=80, issuer_cn="Let's Encrypt")
    assert tc.cert_issue_reason(tc.inspect_cert(path), _settings(tmp_path)) == tc.REASON_HOST


def test_ensure_tls_skips_without_public_host(tmp_path):
    assert tc.ensure_tls_cert(_settings(tmp_path, primary_analitics_public_host="")) == "skip"


def test_ensure_tls_ok_does_not_touch_caddy(tmp_path):
    settings = _settings(tmp_path)
    cert, _key = tc.tls_paths(settings)
    _write_cert(cert, host=PUBLIC_HOST, hours=80, issuer_cn="Let's Encrypt")
    calls: list[str] = []
    status = tc.ensure_tls_cert(
        settings,
        start_caddy=lambda _s: calls.append("start"),
        reload_caddy=lambda _s: calls.append("reload"),
        trigger=lambda _s: calls.append("trigger"),
    )
    assert status == "ok"
    assert calls == ["start"]


def test_ensure_tls_reissues_when_missing(tmp_path):
    settings = _settings(tmp_path)
    cert, _key = tc.tls_paths(settings)
    calls: list[str] = []

    def _trigger(_settings):
        calls.append("trigger")
        _write_cert(cert, host=PUBLIC_HOST, hours=80, issuer_cn="Let's Encrypt")

    status = tc.ensure_tls_cert(
        settings,
        start_caddy=lambda _s: calls.append("start"),
        reload_caddy=lambda _s: calls.append("reload"),
        trigger=_trigger,
    )
    assert status == "renewed"
    assert calls == ["start", "trigger", "reload"]
    assert tc.cert_issue_reason(tc.inspect_cert(cert), settings) == ""


def test_ensure_tls_failed_issue_keeps_portal(tmp_path):
    settings = _settings(tmp_path)
    status = tc.ensure_tls_cert(
        settings,
        start_caddy=lambda _s: None,
        reload_caddy=lambda _s: None,
        trigger=lambda _s: None,
    )
    assert status == "failed"
