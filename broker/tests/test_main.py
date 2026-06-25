import pytest
from fastapi.testclient import TestClient

from .conftest import make_leaf_cert, mint_jwt


@pytest.fixture
def client(auth_settings, root_ca, monkeypatch):
    """TestClient with read_secret stubbed."""

    def fake_read_secret(secret_id: str, version: str = "latest") -> bytes:
        if secret_id == "step-ca-root-cert":
            return root_ca.pem()
        if secret_id.startswith("vault-"):
            role = secret_id[len("vault-") :]
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
    leaf = make_leaf_cert(root_ca, "macmini-m4-81", "gecko_t_osx_1500_m4_no_sip")
    token = mint_jwt(leaf)
    r = client.get(
        "/secret/gecko_t_osx_1500_m4_no_sip",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert b"# vault.yaml for gecko_t_osx_1500_m4_no_sip" in r.content


def test_missing_auth_401(client):
    r = client.get("/secret/gecko_t_osx_1500_m4_no_sip")
    assert r.status_code == 401


def test_role_mismatch_403(client, root_ca):
    leaf = make_leaf_cert(root_ca, "macmini-m4-81", "gecko_t_osx_1500_m4_no_sip")
    token = mint_jwt(leaf)
    r = client.get("/secret/gecko_t_osx_1400_r8", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


def test_invalid_jwt_401(client, root_ca, other_root_ca):
    leaf = make_leaf_cert(other_root_ca, "macmini-m4-81", "gecko_t_osx_1500_m4_no_sip")
    token = mint_jwt(leaf)
    r = client.get(
        "/secret/gecko_t_osx_1500_m4_no_sip",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 401


def test_jti_replay_returns_401(client, root_ca):
    leaf = make_leaf_cert(root_ca, "macmini-m4-81", "gecko_t_osx_1500_m4_no_sip")
    fixed_jti = "22222222-2222-2222-2222-222222222222"

    token1 = mint_jwt(leaf, jti=fixed_jti)
    r1 = client.get(
        "/secret/gecko_t_osx_1500_m4_no_sip", headers={"Authorization": f"Bearer {token1}"}
    )
    assert r1.status_code == 200

    token2 = mint_jwt(leaf, jti=fixed_jti)
    r2 = client.get(
        "/secret/gecko_t_osx_1500_m4_no_sip", headers={"Authorization": f"Bearer {token2}"}
    )
    assert r2.status_code == 401


def test_revoked_cert_returns_401(client, root_ca):
    leaf = make_leaf_cert(root_ca, "macmini-m4-81", "gecko_t_osx_1500_m4_no_sip")

    # Directly mark this leaf serial as revoked in the live cache.
    from app.main import app

    app.state.revocation_cache.update({leaf.cert.serial_number})

    token = mint_jwt(leaf)
    r = client.get(
        "/secret/gecko_t_osx_1500_m4_no_sip", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 401


def test_rate_limit_returns_429(client, root_ca, auth_settings):
    """Burst is 5 per auth_settings; the 6th request from the same cert must 429."""
    leaf = make_leaf_cert(root_ca, "macmini-m4-81", "gecko_t_osx_1500_m4_no_sip")

    # Burn through the burst with distinct jti values (each call must have a unique jti).
    for _ in range(auth_settings.rate_limit_burst):
        token = mint_jwt(leaf)
        r = client.get(
            "/secret/gecko_t_osx_1500_m4_no_sip", headers={"Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 200

    # Next one should be rate-limited.
    token = mint_jwt(leaf)
    r = client.get(
        "/secret/gecko_t_osx_1500_m4_no_sip", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 429
    assert r.headers.get("Retry-After") == "60"
