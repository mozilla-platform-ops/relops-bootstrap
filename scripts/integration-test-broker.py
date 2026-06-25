#!/usr/bin/env python3
"""
Integration test driver for the live vault-broker.

Mints a JWT signed by a real step-ca-issued client cert (passed in on the
command line), embeds the cert chain in x5c, and curls the broker. Proves
the full auth path works against the deployed service.

Usage:
  integration-test-broker.py \\
    --cert /tmp/test.crt \\
    --key  /tmp/test.key \\
    --intermediates /tmp/intermediates.crt \\
    --broker-url https://forge.relops.mozilla.com \\
    --role gecko_t_osx_1500_m4_no_sip

What this exercises (each layer fails closed if the prior succeeds):
  1. Network path: curl reaches the broker via LB
  2. TLS: LB cert validates against system roots
  3. Broker auth: cert chain validates against the step-ca root in Secret Manager
  4. Broker auth: JWT signature verifies under cert's pubkey
  5. Broker auth: jti / aud / exp / lifetime checks pass
  6. Broker auth: SPIFFE URI extracted from cert SAN matches the URL role
  7. Broker: Secret Manager read returns the vault.yaml (or 500 if secret empty)
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import uuid
from pathlib import Path

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa


def load_cert_chain(leaf_path: Path, intermediates_path: Path | None) -> list[bytes]:
    """Returns DER-encoded certs in chain order: leaf first, intermediates after."""
    chain = []
    leaf = x509.load_pem_x509_certificate(leaf_path.read_bytes())
    chain.append(leaf.public_bytes(serialization.Encoding.DER))
    if intermediates_path and intermediates_path.exists():
        text = intermediates_path.read_text()
        # split on PEM markers
        cur: list[str] = []
        for line in text.splitlines():
            cur.append(line)
            if "-----END CERTIFICATE-----" in line:
                pem = "\n".join(cur).encode()
                chain.append(x509.load_pem_x509_certificate(pem).public_bytes(serialization.Encoding.DER))
                cur = []
    return chain


def x5c_header(chain_der: list[bytes]) -> list[str]:
    return [base64.b64encode(d).decode() for d in chain_der]


def jwt_alg_for_key(key) -> str:
    if isinstance(key, ec.EllipticCurvePrivateKey):
        return "ES256"
    if isinstance(key, rsa.RSAPrivateKey):
        return "RS256"
    raise ValueError(f"unsupported key type: {type(key).__name__}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--cert", required=True, type=Path, help="leaf client cert PEM")
    p.add_argument("--key", required=True, type=Path, help="leaf client cert private key PEM")
    p.add_argument("--intermediates", type=Path, help="optional intermediate cert chain PEM (concatenated)")
    p.add_argument("--broker-url", required=True, help="e.g. https://forge.relops.mozilla.com")
    p.add_argument("--role", required=True, help="e.g. gecko_t_osx_1500_m4_no_sip")
    p.add_argument("--lifetime", type=int, default=60, help="JWT lifetime seconds")
    args = p.parse_args()

    cert_pem = args.cert.read_bytes()
    key = serialization.load_pem_private_key(args.key.read_bytes(), password=None)
    chain = load_cert_chain(args.cert, args.intermediates)
    alg = jwt_alg_for_key(key)

    now = int(time.time())
    claims = {
        "iat": now,
        "exp": now + args.lifetime,
        "aud": args.broker_url,
        "sub": "integration-test",
        "jti": str(uuid.uuid4()),
    }
    token = jwt.encode(
        claims,
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
        algorithm=alg,
        headers={"x5c": x5c_header(chain)},
    )

    print(f"alg:    {alg}")
    print(f"aud:    {args.broker_url}")
    print(f"role:   {args.role}")
    print(f"jti:    {claims['jti']}")
    print(f"lifetime: {args.lifetime}s")
    print()

    # Use httpx for parity with the broker's own deps; could be `requests`.
    import httpx

    url = f"{args.broker_url}/secret/{args.role}"
    print(f"GET {url}")
    r = httpx.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=15)
    print(f"  status: {r.status_code}")
    print(f"  body:   {r.text[:400]}")

    if r.status_code == 200:
        print("\n✅ broker returned the secret — full auth chain works")
        return 0
    if r.status_code == 500 and "secret unavailable" in r.text.lower():
        print("\n✅ auth passed, Secret Manager lookup failed (secret not populated)")
        print("    Auth path is fully working; populate the secret to test the rest.")
        return 0
    print(f"\n❌ unexpected response code {r.status_code}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
