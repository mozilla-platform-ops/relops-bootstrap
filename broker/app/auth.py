"""
mTLS-forwarded-header authentication.

The GCP HTTPS Load Balancer terminates mTLS, validates the client cert chain
against the Trust Config (step-ca root + intermediate), and forwards parsed
cert info as request headers. We use:

  X-Client-Cert-Present         "true" / "false"
  X-Client-Cert-Chain-Verified  "true" / "false"
  X-Client-Cert-Error           empty or LB error code
  X-Client-Cert-Serial-Number   hex serial (audit + rate-limit key)
  X-Client-Cert-Leaf            base64-encoded leaf PEM

We parse the leaf cert ourselves and extract the SPIFFE URI from the SAN
extension because the LB's own SPIFFE parser silently drops URIs with
URL-encoded chars (e.g. `Mac%20mini` from %ComputerName%-expanded subjects).
The broker is more lenient: it URL-decodes the URI and accepts.

We do NOT re-verify the chain — the LB already did that against its
Trust Config. We DO verify the chain-verified header is "true".

Why mTLS-at-LB instead of JWT-signed-by-cert? macOS keychain ACL gates
SecKeyCreateSignature to network-stack helpers (configd, nehelper, ...).
Non-Apple binaries can't sign in-process, even as root from a LaunchDaemon.
mTLS routes the signing through SecureTransport / network framework, which
IS in that allowed-apps list.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from urllib.parse import unquote

from cryptography import x509
from cryptography.x509.oid import ExtensionOID

from .config import get_settings


class AuthError(Exception):
    pass


@dataclass(frozen=True)
class VerifiedIdentity:
    hostname: str
    role: str
    cert_serial: str  # hex; from the LB header — used for audit + rate-limit keying


def _decode_leaf(leaf_header: str) -> x509.Certificate:
    """The LB forwards the leaf as base64-encoded PEM. Strip newlines that
    some libraries insert into header values, then b64-decode."""
    cleaned = leaf_header.strip().replace("\n", "").replace("\r", "")
    try:
        der_or_pem = base64.b64decode(cleaned)
    except Exception as e:
        raise AuthError(f"X-Client-Cert-Leaf not valid base64: {e}") from e

    # GCP usually returns a PEM payload (with -----BEGIN/END----- lines)
    # base64-wrapped. Try PEM first; fall back to DER.
    try:
        return x509.load_pem_x509_certificate(der_or_pem)
    except ValueError:
        try:
            return x509.load_der_x509_certificate(der_or_pem)
        except ValueError as e:
            raise AuthError(f"X-Client-Cert-Leaf is neither PEM nor DER: {e}") from e


def _extract_spiffe(cert: x509.Certificate, trust_domain: str) -> tuple[str, str]:
    try:
        san_ext = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
    except x509.ExtensionNotFound as e:
        raise AuthError("cert has no SubjectAltName extension") from e

    uri_sans = san_ext.value.get_values_for_type(x509.UniformResourceIdentifier)
    prefix = f"spiffe://{trust_domain}/host/"
    for raw_uri in uri_sans:
        # URL-decode so `Mac%20mini` becomes `Mac mini`
        uri = unquote(raw_uri)
        if not uri.startswith(prefix):
            continue
        rest = uri[len(prefix):]
        try:
            hostname, role_marker, role = rest.split("/", 2)
        except ValueError:
            continue
        if role_marker != "role" or not hostname or not role:
            continue
        return hostname, role

    raise AuthError(
        f"no SAN URI matching spiffe://{trust_domain}/host/<host>/role/<role>"
    )


def verify_mtls_headers(
    *,
    cert_present: str,
    chain_verified: str,
    cert_error: str,
    cert_leaf: str,
    serial_number: str,
) -> VerifiedIdentity:
    """
    Validate the LB-forwarded mTLS headers + the leaf cert. Returns the
    verified identity. Raises AuthError on any failure.
    """
    settings = get_settings()

    if cert_present.lower() != "true":
        raise AuthError("no client cert presented")
    if chain_verified.lower() != "true":
        raise AuthError(f"chain not verified (lb error: {cert_error or 'none'})")
    if not cert_leaf:
        raise AuthError("X-Client-Cert-Leaf header missing")

    leaf = _decode_leaf(cert_leaf)
    hostname, role = _extract_spiffe(leaf, settings.spiffe_trust_domain)

    return VerifiedIdentity(
        hostname=hostname,
        role=role,
        cert_serial=serial_number or "unknown",
    )
