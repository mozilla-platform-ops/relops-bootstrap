"""
Synthetic CA + cert fixtures for testing the mTLS-header auth flow.

The broker doesn't validate the cert chain itself (the LB does that), so
tests only need a cert that parses cleanly and has the right SPIFFE URI
in the SAN. The test harness builds a self-signed cert and base64-encodes
the PEM into the X-Client-Cert-Leaf header that the LB would forward.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


@dataclass
class TestKeyAndCert:
    key: ec.EllipticCurvePrivateKey
    cert: x509.Certificate

    def pem(self) -> bytes:
        return self.cert.public_bytes(serialization.Encoding.PEM)

    def pem_b64(self) -> str:
        return base64.b64encode(self.pem()).decode()


def _make_root() -> TestKeyAndCert:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test RelOps Root CA")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return TestKeyAndCert(key, cert)


@pytest.fixture
def root_ca() -> TestKeyAndCert:
    return _make_root()


def make_leaf_cert(
    root: TestKeyAndCert,
    hostname: str,
    role: str,
    *,
    trust_domain: str = "relops.mozilla",
) -> TestKeyAndCert:
    """Build a leaf cert with the SPIFFE URI the broker extracts from the SAN."""
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    spiffe = f"spiffe://{trust_domain}/host/{hostname}/role/{role}"

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(root.cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(hours=24))
        .add_extension(
            x509.SubjectAlternativeName([x509.UniformResourceIdentifier(spiffe)]),
            critical=False,
        )
        .sign(root.key, hashes.SHA256())
    )
    return TestKeyAndCert(leaf_key, cert)


def mtls_headers(leaf: TestKeyAndCert, **overrides) -> dict:
    """Build the X-Client-Cert-* headers the LB forwards on a happy-path mTLS connection."""
    headers = {
        "X-Client-Cert-Present": "true",
        "X-Client-Cert-Chain-Verified": "true",
        "X-Client-Cert-Error": "",
        "X-Client-Cert-Leaf": leaf.pem_b64(),
        "X-Client-Cert-Serial-Number": f"{leaf.cert.serial_number:x}",
    }
    headers.update(overrides)
    return headers


@pytest.fixture
def auth_settings(monkeypatch):
    """Reset settings between tests + set the test trust domain."""
    monkeypatch.setenv("GCP_PROJECT_ID", "test-project")
    monkeypatch.setenv("SPIFFE_TRUST_DOMAIN", "relops.mozilla")
    monkeypatch.setenv("RATE_LIMIT_REQUESTS_PER_MINUTE", "600")
    monkeypatch.setenv("RATE_LIMIT_BURST", "20")
    from app.config import get_settings

    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
def rate_limiter(auth_settings):
    from app.replay_cache import RateLimiter

    return RateLimiter(
        requests_per_minute=auth_settings.rate_limit_requests_per_minute,
        burst=auth_settings.rate_limit_burst,
    )
