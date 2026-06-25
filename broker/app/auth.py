"""
mTLS-forwarded-header authentication.

The GCP HTTPS Load Balancer terminates mTLS, validates the client cert chain
against the Trust Config (step-ca root + intermediate), and forwards parsed
cert info as request headers. This module reads those headers and produces a
VerifiedIdentity for the broker to authorize against.

We do NOT validate the cert chain ourselves — the LB already did that with
its trust-anchor pool, and we cannot redo it without the raw cert (the LB
forwards the SPIFFE URI and SHA-256, not the leaf PEM, by default).

Why mTLS at the LB instead of JWT-signed-by-cert? macOS keychain ACL gates
SecKeyCreateSignature to a fixed list of network-stack helpers (configd,
nehelper, NEIKEv2Provider, racoon, ...). Non-Apple binaries can't sign,
even as root from a LaunchDaemon. mTLS routes the signing through those
helpers automatically during URLSession's TLS handshake, sidestepping the
keychain ACL wall.

Headers injected by the LB (configured in terraform/lb.tf):
  X-Client-Cert-Present         "true" / "false"
  X-Client-Cert-Chain-Verified  "true" / "false"
  X-Client-Cert-Error           empty or error code
  X-Client-Cert-SPIFFE          spiffe://<trust_domain>/host/<host>/role/<role>
  X-Client-Cert-URI-SANs        comma-separated URI SANs
  X-Client-Cert-Subject-DN      subject DN
  X-Client-Cert-Serial-Number   hex serial
  X-Client-Cert-SHA256          SHA-256 fingerprint
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import get_settings


class AuthError(Exception):
    pass


@dataclass(frozen=True)
class VerifiedIdentity:
    hostname: str
    role: str
    cert_serial: str  # hex string from the LB header — used for audit + rate-limit keying


def _extract_spiffe(spiffe_uri: str, trust_domain: str) -> tuple[str, str]:
    prefix = f"spiffe://{trust_domain}/host/"
    if not spiffe_uri.startswith(prefix):
        raise AuthError(
            f"SPIFFE URI {spiffe_uri!r} doesn't start with spiffe://{trust_domain}/host/"
        )
    rest = spiffe_uri[len(prefix):]
    try:
        hostname, role_marker, role = rest.split("/", 2)
    except ValueError as e:
        raise AuthError(f"malformed SPIFFE URI: {spiffe_uri!r}") from e
    if role_marker != "role" or not hostname or not role:
        raise AuthError(f"malformed SPIFFE URI: {spiffe_uri!r}")
    return hostname, role


def verify_mtls_headers(
    *,
    cert_present: str,
    chain_verified: str,
    cert_error: str,
    spiffe: str,
    serial_number: str,
) -> VerifiedIdentity:
    """
    Validate the LB-forwarded mTLS headers. Returns the verified identity.
    Raises AuthError on any failure.
    """
    settings = get_settings()

    if cert_present.lower() != "true":
        raise AuthError("no client cert presented")
    if chain_verified.lower() != "true":
        raise AuthError(f"chain not verified (lb error: {cert_error or 'none'})")
    if not spiffe:
        raise AuthError("X-Client-Cert-SPIFFE header missing")

    hostname, role = _extract_spiffe(spiffe, settings.spiffe_trust_domain)
    return VerifiedIdentity(
        hostname=hostname,
        role=role,
        cert_serial=serial_number or "unknown",
    )
