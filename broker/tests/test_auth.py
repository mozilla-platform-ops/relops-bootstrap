import pytest

from app.auth import AuthError, ReplayError, RevokedCertError, verify

from .conftest import make_leaf_cert, mint_jwt


def _verify(authorization, root_pem, jti_cache, revocation_cache):
    return verify(authorization, root_pem, jti_cache=jti_cache, revocation_cache=revocation_cache)


def test_happy_path(auth_settings, root_ca, jti_cache, revocation_cache):
    leaf = make_leaf_cert(root_ca, "macmini-m4-81", "gecko_t_osx_1500_m4_no_sip")
    token = mint_jwt(leaf)
    identity = _verify(f"Bearer {token}", root_ca.pem(), jti_cache, revocation_cache)
    assert identity.hostname == "macmini-m4-81"
    assert identity.role == "gecko_t_osx_1500_m4_no_sip"
    assert identity.cert_serial == leaf.cert.serial_number


def test_missing_bearer_prefix(auth_settings, root_ca, jti_cache, revocation_cache):
    leaf = make_leaf_cert(root_ca, "macmini-m4-81", "gecko_t_osx_1500_m4_no_sip")
    token = mint_jwt(leaf)
    with pytest.raises(AuthError, match="Bearer"):
        _verify(token, root_ca.pem(), jti_cache, revocation_cache)


def test_unrelated_ca_rejected(auth_settings, root_ca, other_root_ca, jti_cache, revocation_cache):
    leaf = make_leaf_cert(other_root_ca, "macmini-m4-81", "gecko_t_osx_1500_m4_no_sip")
    token = mint_jwt(leaf)
    with pytest.raises(AuthError, match="chain"):
        _verify(f"Bearer {token}", root_ca.pem(), jti_cache, revocation_cache)


def test_expired_jwt(auth_settings, root_ca, jti_cache, revocation_cache):
    leaf = make_leaf_cert(root_ca, "macmini-m4-81", "gecko_t_osx_1500_m4_no_sip")
    token = mint_jwt(leaf, iat_offset_seconds=-1000, lifetime_seconds=60)
    with pytest.raises(AuthError, match="JWT verify"):
        _verify(f"Bearer {token}", root_ca.pem(), jti_cache, revocation_cache)


def test_wrong_audience(auth_settings, root_ca, jti_cache, revocation_cache):
    leaf = make_leaf_cert(root_ca, "macmini-m4-81", "gecko_t_osx_1500_m4_no_sip")
    token = mint_jwt(leaf, audience="https://wrong-aud.example")
    with pytest.raises(AuthError, match="JWT verify"):
        _verify(f"Bearer {token}", root_ca.pem(), jti_cache, revocation_cache)


def test_overlong_lifetime(auth_settings, root_ca, jti_cache, revocation_cache):
    leaf = make_leaf_cert(root_ca, "macmini-m4-81", "gecko_t_osx_1500_m4_no_sip")
    token = mint_jwt(leaf, lifetime_seconds=3600)
    with pytest.raises(AuthError, match="lifetime exceeds"):
        _verify(f"Bearer {token}", root_ca.pem(), jti_cache, revocation_cache)


def test_missing_x5c(auth_settings, root_ca, jti_cache, revocation_cache):
    from datetime import datetime, timedelta, timezone

    import jwt as pyjwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

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
        _verify(f"Bearer {token}", root_ca.pem(), jti_cache, revocation_cache)


def test_missing_spiffe_san(auth_settings, root_ca, jti_cache, revocation_cache):
    from datetime import datetime, timedelta, timezone

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    from .conftest import TestKeyAndCert

    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "no-spiffe-host")]))
        .issuer_name(root_ca.cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("no-spiffe-host.test")]),
            critical=False,
        )
        .sign(root_ca.key, hashes.SHA256())
    )

    leaf = TestKeyAndCert(leaf_key, leaf_cert)
    token = mint_jwt(leaf)
    with pytest.raises(AuthError, match="SAN URI"):
        _verify(f"Bearer {token}", root_ca.pem(), jti_cache, revocation_cache)


# --- new tests for the 4 hardening features ---


def test_jti_required(auth_settings, root_ca, jti_cache, revocation_cache):
    leaf = make_leaf_cert(root_ca, "macmini-m4-81", "gecko_t_osx_1500_m4_no_sip")
    token = mint_jwt(leaf, include_jti=False)
    with pytest.raises(AuthError, match="JWT verify"):
        _verify(f"Bearer {token}", root_ca.pem(), jti_cache, revocation_cache)


def test_jti_replay_rejected(auth_settings, root_ca, jti_cache, revocation_cache):
    leaf = make_leaf_cert(root_ca, "macmini-m4-81", "gecko_t_osx_1500_m4_no_sip")
    fixed_jti = "11111111-1111-1111-1111-111111111111"

    # First use succeeds.
    token1 = mint_jwt(leaf, jti=fixed_jti)
    _verify(f"Bearer {token1}", root_ca.pem(), jti_cache, revocation_cache)

    # Replay with the same jti — even via a fresh token with that jti — must fail.
    token2 = mint_jwt(leaf, jti=fixed_jti)
    with pytest.raises(ReplayError):
        _verify(f"Bearer {token2}", root_ca.pem(), jti_cache, revocation_cache)


def test_revoked_cert_rejected(auth_settings, root_ca, jti_cache, revocation_cache):
    leaf = make_leaf_cert(root_ca, "macmini-m4-81", "gecko_t_osx_1500_m4_no_sip")
    revocation_cache.update({leaf.cert.serial_number})
    token = mint_jwt(leaf)
    with pytest.raises(RevokedCertError):
        _verify(f"Bearer {token}", root_ca.pem(), jti_cache, revocation_cache)


def test_unsupported_alg_rejected(auth_settings, root_ca, jti_cache, revocation_cache, monkeypatch):
    """ES384 isn't in the default allowed_algs list."""
    monkeypatch.setenv("JWT_ALLOWED_ALGS_CSV", "RS256,ES256")  # explicitly drop ES384
    from app.config import get_settings

    get_settings.cache_clear()

    # Generate a P-384 cert + JWT signed ES384 — should reject.
    from datetime import datetime, timedelta, timezone

    import jwt as pyjwt
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    from .conftest import TestKeyAndCert

    leaf_key = ec.generate_private_key(ec.SECP384R1())
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "macmini-m4-81")]))
        .issuer_name(root_ca.cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.UniformResourceIdentifier(
                        "spiffe://relops.mozilla/host/macmini-m4-81/role/gecko_t_osx_1500_m4_no_sip"
                    )
                ]
            ),
            critical=False,
        )
        .sign(root_ca.key, hashes.SHA256())
    )
    leaf = TestKeyAndCert(leaf_key, leaf_cert)

    from datetime import datetime as dt
    from uuid import uuid4

    now = dt.now(timezone.utc)
    pem_key = leaf_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    token = pyjwt.encode(
        {
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=60)).timestamp()),
            "aud": "https://vault-broker.test",
            "jti": str(uuid4()),
        },
        pem_key,
        algorithm="ES384",
        headers={"x5c": [leaf.der_b64()]},
    )
    with pytest.raises(AuthError, match="unsupported JWT alg"):
        _verify(f"Bearer {token}", root_ca.pem(), jti_cache, revocation_cache)
