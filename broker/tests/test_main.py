from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from .conftest import make_leaf_cert, mint_jwt


@pytest.fixture
def client(auth_settings, root_ca, monkeypatch):
    """
    FastAPI TestClient with the lifespan stubbed so we don't hit real Secret Manager
    for the root cert at startup.
    """
    # Stub read_secret to return the test root PEM when asked for step-ca-root-cert,
    # and a fake YAML payload when asked for vault-<role>.
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


def test_get_secret_happy_path(client, root_ca):
    leaf = make_leaf_cert(root_ca, "macmini-m4-81", "gecko_t_osx_1500_m4_no_sip")
    token = mint_jwt(leaf)
    r = client.get(
        "/secret/gecko_t_osx_1500_m4_no_sip",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert b"# vault.yaml for gecko_t_osx_1500_m4_no_sip" in r.content


def test_missing_auth_returns_401(client):
    r = client.get("/secret/gecko_t_osx_1500_m4_no_sip")
    assert r.status_code == 401


def test_url_role_mismatching_cert_role_returns_403(client, root_ca):
    """Host has a cert authorized for role A but asks for role B."""
    leaf = make_leaf_cert(root_ca, "macmini-m4-81", "gecko_t_osx_1500_m4_no_sip")
    token = mint_jwt(leaf)
    r = client.get(
        "/secret/gecko_t_osx_1400_r8",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


def test_invalid_jwt_returns_401(client, root_ca, other_root_ca):
    """Cert signed by an unrelated CA; chain fails."""
    leaf = make_leaf_cert(other_root_ca, "macmini-m4-81", "gecko_t_osx_1500_m4_no_sip")
    token = mint_jwt(leaf)
    r = client.get(
        "/secret/gecko_t_osx_1500_m4_no_sip",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 401
