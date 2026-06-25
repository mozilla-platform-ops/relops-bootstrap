"""
Synthetic CA + cert fixtures.
"""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
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

    def der_b64(self) -> str:
        return base64.b64encode(self.cert.public_bytes(serialization.Encoding.DER)).decode()


def _make_root() -> TestKeyAndCert:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test RelOps Root CA")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return TestKeyAndCert(key, cert)


def _make_leaf(
    root: TestKeyAndCert,
    hostname: str,
    role: str,
    trust_domain: str = "relops.mozilla",
) -> TestKeyAndCert:
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    spiffe_uri = f"spiffe://{trust_domain}/host/{hostname}/role/{role}"

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(root.cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(hours=24))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName(f"{hostname}.test.releng.mdc1.mozilla.com"),
                    x509.UniformResourceIdentifier(spiffe_uri),
                ]
            ),
            critical=False,
        )
        .sign(root.key, hashes.SHA256())
    )
    return TestKeyAndCert(leaf_key, cert)


@pytest.fixture
def root_ca() -> TestKeyAndCert:
    return _make_root()


@pytest.fixture
def other_root_ca() -> TestKeyAndCert:
    return _make_root()


def make_leaf_cert(root_ca: TestKeyAndCert, hostname: str, role: str) -> TestKeyAndCert:
    return _make_leaf(root_ca, hostname, role)


def mint_jwt(
    leaf: TestKeyAndCert,
    *,
    audience: str = "https://vault-broker.test",
    lifetime_seconds: int = 60,
    extra_claims: dict | None = None,
    iat_offset_seconds: int = 0,
    alg: str = "ES256",
    include_jti: bool = True,
    jti: str | None = None,
) -> str:
    now = datetime.now(timezone.utc) + timedelta(seconds=iat_offset_seconds)
    claims = {
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=lifetime_seconds)).timestamp()),
        "aud": audience,
        "sub": "test-sub",
    }
    if include_jti:
        claims["jti"] = jti or str(uuid.uuid4())
    if extra_claims:
        claims.update(extra_claims)

    pem_key = leaf.key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    return jwt.encode(claims, pem_key, algorithm=alg, headers={"x5c": [leaf.der_b64()]})


@pytest.fixture
def auth_settings(monkeypatch):
    """Reset settings between tests + set the test trust domain + audience."""
    monkeypatch.setenv("GCP_PROJECT_ID", "test-project")
    monkeypatch.setenv("JWT_AUDIENCE", "https://vault-broker.test")
    monkeypatch.setenv("SPIFFE_TRUST_DOMAIN", "relops.mozilla")
    monkeypatch.setenv("JWT_ALLOWED_ALGS_CSV", "ES256,RS256")
    monkeypatch.setenv("RATE_LIMIT_REQUESTS_PER_MINUTE", "60")
    monkeypatch.setenv("RATE_LIMIT_BURST", "5")
    from app.config import get_settings

    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
def jti_cache():
    from app.replay_cache import JtiCache

    return JtiCache(max_size=1000)


@pytest.fixture
def revocation_cache():
    from app.revocation import RevocationCache

    return RevocationCache()


@pytest.fixture
def rate_limiter(auth_settings):
    from app.replay_cache import RateLimiter

    return RateLimiter(
        requests_per_minute=auth_settings.rate_limit_requests_per_minute,
        burst=auth_settings.rate_limit_burst,
    )
