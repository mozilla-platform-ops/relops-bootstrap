import pytest

from app.auth import AuthError, verify

from .conftest import make_leaf_cert, mint_jwt


def test_happy_path_extracts_hostname_and_role(auth_settings, root_ca):
    leaf = make_leaf_cert(root_ca, "macmini-m4-81", "gecko_t_osx_1500_m4_no_sip")
    token = mint_jwt(leaf)
    identity = verify(f"Bearer {token}", root_ca.pem())
    assert identity.hostname == "macmini-m4-81"
    assert identity.role == "gecko_t_osx_1500_m4_no_sip"
    assert identity.cert_serial == leaf.cert.serial_number


def test_missing_bearer_prefix_is_rejected(auth_settings, root_ca):
    leaf = make_leaf_cert(root_ca, "macmini-m4-81", "gecko_t_osx_1500_m4_no_sip")
    token = mint_jwt(leaf)
    with pytest.raises(AuthError, match="Bearer"):
        verify(token, root_ca.pem())


def test_cert_signed_by_unrelated_ca_is_rejected(auth_settings, root_ca, other_root_ca):
    # cert is signed by `other_root_ca` but we present root_ca's PEM as the trust anchor.
    leaf = make_leaf_cert(other_root_ca, "macmini-m4-81", "gecko_t_osx_1500_m4_no_sip")
    token = mint_jwt(leaf)
    with pytest.raises(AuthError, match="chain"):
        verify(f"Bearer {token}", root_ca.pem())


def test_expired_jwt_is_rejected(auth_settings, root_ca):
    leaf = make_leaf_cert(root_ca, "macmini-m4-81", "gecko_t_osx_1500_m4_no_sip")
    # Set iat 1000s in the past and lifetime 60s → expired.
    token = mint_jwt(leaf, iat_offset_seconds=-1000, lifetime_seconds=60)
    with pytest.raises(AuthError, match="JWT verify"):
        verify(f"Bearer {token}", root_ca.pem())


def test_wrong_audience_is_rejected(auth_settings, root_ca):
    leaf = make_leaf_cert(root_ca, "macmini-m4-81", "gecko_t_osx_1500_m4_no_sip")
    token = mint_jwt(leaf, audience="https://wrong-aud.example")
    with pytest.raises(AuthError, match="JWT verify"):
        verify(f"Bearer {token}", root_ca.pem())


def test_overlong_lifetime_is_rejected(auth_settings, root_ca):
    leaf = make_leaf_cert(root_ca, "macmini-m4-81", "gecko_t_osx_1500_m4_no_sip")
    # 1 hour lifetime > 300s ceiling.
    token = mint_jwt(leaf, lifetime_seconds=3600)
    with pytest.raises(AuthError, match="lifetime exceeds"):
        verify(f"Bearer {token}", root_ca.pem())


def test_missing_x5c_header_is_rejected(auth_settings, root_ca):
    from datetime import datetime, timedelta, timezone

    import jwt as pyjwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    # Mint a JWT with NO x5c (just a bare ES256 token).
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    now = datetime.now(timezone.utc)
    token = pyjwt.encode(
        {
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=60)).timestamp()),
            "aud": "https://vault-broker.test",
        },
        pem,
        algorithm="ES256",
    )
    with pytest.raises(AuthError, match="x5c"):
        verify(f"Bearer {token}", root_ca.pem())


def test_cert_missing_spiffe_san_is_rejected(auth_settings, root_ca):
    """Synthesize a cert that's chain-valid but has no SPIFFE SAN — should fail at identity extraction."""
    from datetime import datetime, timedelta, timezone

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "no-spiffe-host")]))
        .issuer_name(root_ca.cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(hours=1))
        # SAN with only DNSName, no URI
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("no-spiffe-host.test")]),
            critical=False,
        )
        .sign(root_ca.key, hashes.SHA256())
    )
    from .conftest import TestKeyAndCert

    leaf = TestKeyAndCert(leaf_key, leaf_cert)
    token = mint_jwt(leaf)
    with pytest.raises(AuthError, match="SAN URI"):
        verify(f"Bearer {token}", root_ca.pem())
