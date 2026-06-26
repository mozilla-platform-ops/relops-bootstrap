import pytest
from fastapi.testclient import TestClient

from .conftest import make_leaf_cert, mtls_headers


@pytest.fixture
def client(auth_settings, root_ca, monkeypatch):
    """TestClient with read_secret stubbed."""

    def fake_read_secret(secret_id: str, version: str = "latest") -> bytes:
        if secret_id.startswith("vault-"):
            role = secret_id[len("vault-"):]
            return f"# vault.yaml for {role}\nworker:\n  worker_id: test\n".encode()
        raise KeyError(secret_id)

    monkeypatch.setattr("app.main.read_secret", fake_read_secret)
    monkeypatch.setattr("app.secrets.read_secret", fake_read_secret)

    from app.main import app

    with TestClient(app) as c:
        yield c


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.text == "ok"


def test_happy_path(client, root_ca):
    leaf = make_leaf_cert(root_ca, "macmini-m4-81", "gecko_t_osx_1500_m4")
    r = client.get("/secret/gecko_t_osx_1500_m4", headers=mtls_headers(leaf))
    assert r.status_code == 200
    assert b"vault.yaml for gecko_t_osx_1500_m4" in r.content


def test_missing_cert_present_header_401(client, root_ca):
    leaf = make_leaf_cert(root_ca, "macmini-m4-81", "gecko_t_osx_1500_m4")
    r = client.get(
        "/secret/gecko_t_osx_1500_m4",
        headers=mtls_headers(leaf, **{"X-Client-Cert-Present": "false"}),
    )
    assert r.status_code == 401


def test_chain_not_verified_401(client, root_ca):
    leaf = make_leaf_cert(root_ca, "macmini-m4-81", "gecko_t_osx_1500_m4")
    r = client.get(
        "/secret/gecko_t_osx_1500_m4",
        headers=mtls_headers(leaf, **{"X-Client-Cert-Chain-Verified": "false"}),
    )
    assert r.status_code == 401


def test_missing_leaf_header_401(client, root_ca):
    leaf = make_leaf_cert(root_ca, "macmini-m4-81", "gecko_t_osx_1500_m4")
    r = client.get(
        "/secret/gecko_t_osx_1500_m4",
        headers=mtls_headers(leaf, **{"X-Client-Cert-Leaf": ""}),
    )
    assert r.status_code == 401


def test_role_mismatch_403(client, root_ca):
    """Cert is for role A, URL asks for role B → 403."""
    leaf = make_leaf_cert(root_ca, "macmini-m4-81", "gecko_t_osx_1500_m4_no_sip")
    r = client.get("/secret/gecko_t_osx_1400_r8", headers=mtls_headers(leaf))
    assert r.status_code == 403
    assert b"not 'gecko_t_osx_1400_r8'" in r.content


def test_spiffe_url_encoded_space_in_cn(client, root_ca):
    """GCP LB drops URL-encoded SPIFFE URIs; our broker URL-decodes and accepts."""
    leaf = make_leaf_cert(root_ca, "Mac mini", "gecko_t_osx_1500_m4")
    # The cert's SAN URI is spiffe://relops.mozilla/host/Mac mini/role/...
    # which when serialized into the cert is URL-encoded as Mac%20mini. The
    # broker URL-decodes before role-matching.
    r = client.get("/secret/gecko_t_osx_1500_m4", headers=mtls_headers(leaf))
    assert r.status_code == 200


def test_no_spiffe_in_san_401(client, root_ca):
    """Cert has no SAN URI matching the trust domain — broker rejects."""
    from datetime import UTC, datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    from .conftest import TestKeyAndCert

    leaf_key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "no-spiffe")]))
        .issuer_name(root_ca.cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.UniformResourceIdentifier("https://example.com/not-spiffe")]
            ),
            critical=False,
        )
        .sign(root_ca.key, hashes.SHA256())
    )
    leaf = TestKeyAndCert(leaf_key, cert)
    r = client.get("/secret/gecko_t_osx_1500_m4", headers=mtls_headers(leaf))
    assert r.status_code == 401


def test_rate_limit_returns_429(client, root_ca, auth_settings):
    """Burst is 20 per auth_settings; the 21st request from the same cert must 429."""
    leaf = make_leaf_cert(root_ca, "macmini-m4-81", "gecko_t_osx_1500_m4")
    headers = mtls_headers(leaf)

    for _ in range(auth_settings.rate_limit_burst):
        r = client.get("/secret/gecko_t_osx_1500_m4", headers=headers)
        assert r.status_code == 200

    r = client.get("/secret/gecko_t_osx_1500_m4", headers=headers)
    assert r.status_code == 429
    assert r.headers.get("Retry-After") == "60"
