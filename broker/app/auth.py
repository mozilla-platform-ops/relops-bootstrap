"""
JWT-signed-by-cert validation.

The host signs a short-lived JWT with the private key of its SCEP-issued client cert
and embeds the cert in the JWT header as `x5c`. We verify:

  1. The x5c cert chains up to the step-ca root cert.
  2. The leaf cert is not in the step-ca CRL (if revocation polling is enabled).
  3. The JWT signature is valid under the cert's public key.
  4. JWT claims: aud matches us, exp is in the future, lifetime is bounded.
  5. JWT jti has not been seen recently (replay protection).
  6. Cert SAN encodes a SPIFFE URI of the form
       spiffe://<trust_domain>/host/<hostname>/role/<puppet_role>
     and we return the parsed (hostname, role) tuple as the verified identity.

The (hostname, role) extracted from the *cert SAN* (not the JWT body) is what we
authorize against. The cert is signed by step-ca and we trust step-ca's issuance
policy. Anything in the JWT body that isn't a standard claim is informational only.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.x509.oid import ExtensionOID

from .config import get_settings
from .replay_cache import JtiCache
from .revocation import RevocationCache


class AuthError(Exception):
    pass


class RevokedCertError(AuthError):
    pass


class ReplayError(AuthError):
    pass


@dataclass(frozen=True)
class VerifiedIdentity:
    hostname: str
    role: str
    cert_serial: int  # for audit logging + rate-limit keying


def _decode_x5c_entry(entry: str) -> x509.Certificate:
    der = base64.b64decode(entry)
    return x509.load_der_x509_certificate(der)


def _verify_chain(
    leaf: x509.Certificate,
    intermediates: list[x509.Certificate],
    root: x509.Certificate,
) -> None:
    """
    Minimal chain verification: each cert is signed by the next, terminating in root.
    """
    chain = [leaf] + intermediates + [root]
    for i in range(len(chain) - 1):
        cert, issuer = chain[i], chain[i + 1]
        if cert.issuer != issuer.subject:
            raise AuthError(f"chain link {i} subject/issuer mismatch")

        issuer_pubkey = issuer.public_key()
        sig_alg_oid = cert.signature_hash_algorithm
        if isinstance(issuer_pubkey, ec.EllipticCurvePublicKey):
            issuer_pubkey.verify(
                cert.signature,
                cert.tbs_certificate_bytes,
                ec.ECDSA(sig_alg_oid),
            )
        elif isinstance(issuer_pubkey, rsa.RSAPublicKey):
            issuer_pubkey.verify(
                cert.signature,
                cert.tbs_certificate_bytes,
                padding.PKCS1v15(),
                sig_alg_oid,
            )
        else:
            raise AuthError(f"unsupported issuer key type: {type(issuer_pubkey).__name__}")


def _extract_spiffe_identity(cert: x509.Certificate, trust_domain: str) -> tuple[str, str]:
    try:
        san_ext = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
    except x509.ExtensionNotFound as e:
        raise AuthError("cert missing SAN extension") from e

    uri_sans = san_ext.value.get_values_for_type(x509.UniformResourceIdentifier)
    prefix = f"spiffe://{trust_domain}/host/"
    for uri in uri_sans:
        if not uri.startswith(prefix):
            continue
        rest = uri[len(prefix) :]
        try:
            hostname, role_marker, role = rest.split("/", 2)
        except ValueError:
            continue
        if role_marker != "role":
            continue
        if not hostname or not role:
            continue
        return hostname, role

    raise AuthError(f"no SAN URI matching spiffe://{trust_domain}/host/<host>/role/<role>")


def verify(
    authorization_header: str,
    root_cert_pem: bytes,
    *,
    jti_cache: JtiCache,
    revocation_cache: RevocationCache,
) -> VerifiedIdentity:
    """
    Validate the Authorization: Bearer <jwt> header. Returns the verified host identity.

    Raises AuthError on any failure. Catch + map to HTTP status in the FastAPI layer:
      - RevokedCertError → 401 (specific subclass, useful for audit log differentiation)
      - ReplayError      → 401
      - AuthError        → 401
    """
    settings = get_settings()

    if not authorization_header.lower().startswith("bearer "):
        raise AuthError("missing Bearer prefix")
    token = authorization_header.split(" ", 1)[1].strip()

    # 1. Peek at the JWT header to pull the x5c chain *without* verifying yet.
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as e:
        raise AuthError(f"malformed JWT header: {e}") from e

    x5c = header.get("x5c")
    if not x5c or not isinstance(x5c, list) or len(x5c) == 0:
        raise AuthError("JWT header missing x5c cert chain")

    try:
        leaf = _decode_x5c_entry(x5c[0])
        intermediates = [_decode_x5c_entry(c) for c in x5c[1:]]
    except Exception as e:
        raise AuthError(f"x5c parse failed: {e}") from e

    # 2. Chain-verify against the configured step-ca root.
    try:
        root = x509.load_pem_x509_certificate(root_cert_pem)
    except Exception as e:
        raise AuthError(f"root cert load failed: {e}") from e

    try:
        _verify_chain(leaf, intermediates, root)
    except (AuthError, Exception) as e:
        if isinstance(e, AuthError):
            raise
        raise AuthError(f"cert chain verify failed: {e}") from e

    # 3. Reject revoked certs.
    if revocation_cache.is_revoked(leaf.serial_number):
        raise RevokedCertError(f"cert serial {leaf.serial_number} is revoked")

    # 4. Cert validity window.
    now = datetime.now(timezone.utc)
    if now < leaf.not_valid_before_utc or now > leaf.not_valid_after_utc:
        raise AuthError("leaf cert outside validity window")

    # 5. JWT signature + claims.
    pubkey = leaf.public_key()
    pubkey_pem = pubkey.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    alg = header.get("alg")
    if alg not in settings.jwt_allowed_algs:
        raise AuthError(f"unsupported JWT alg: {alg}")

    required_claims = ["exp", "iat", "aud"]
    if settings.jwt_require_jti:
        required_claims.append("jti")

    try:
        claims = jwt.decode(
            token,
            pubkey_pem,
            algorithms=settings.jwt_allowed_algs,
            audience=settings.jwt_audience,
            leeway=settings.jwt_leeway_seconds,
            options={"require": required_claims},
        )
    except jwt.InvalidTokenError as e:
        raise AuthError(f"JWT verify failed: {e}") from e

    # 6. Bound the JWT lifetime.
    if claims["exp"] - claims["iat"] > settings.jwt_max_lifetime_seconds:
        raise AuthError(f"JWT lifetime exceeds {settings.jwt_max_lifetime_seconds}s")

    # 7. Replay protection via jti cache.
    if settings.jwt_require_jti:
        jti = claims.get("jti")
        if not isinstance(jti, str) or not jti:
            raise AuthError("jti must be a non-empty string")
        if jti_cache.seen_recently(jti, settings.jti_cache_ttl_seconds):
            raise ReplayError(f"jti replay: {jti}")

    # 8. Extract the verified identity from the cert SAN.
    hostname, role = _extract_spiffe_identity(leaf, settings.spiffe_trust_domain)
    return VerifiedIdentity(hostname=hostname, role=role, cert_serial=leaf.serial_number)
