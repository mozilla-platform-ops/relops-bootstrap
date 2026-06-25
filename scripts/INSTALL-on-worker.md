# Installing the relops-bootstrap LaunchDaemon on a worker

Eventually this is Puppet's job. For first-deploy validation, manual steps:

```bash
# 1. Confirm the SCEP profile is pushed and a cert is in the System keychain.
#    SimpleMDM "Dev - SCEP" profile assigned to the device group. mdmclient
#    enrolls automatically; check with:
sudo security find-identity -v /Library/Keychains/System.keychain
# Expect: "Mac mini" identity listed.

# 2. Install the bootstrap script
sudo install -m 0755 -o root -g wheel \
    relops-bootstrap.sh /usr/local/bin/relops-bootstrap.sh

# 3. Tell the script which role this worker plays. Puppet writes this in prod;
#    manual:
echo gecko_t_osx_1500_m4_no_sip | sudo tee /var/root/.relops-role
sudo chmod 0600 /var/root/.relops-role

# 4. Install + load the LaunchDaemon
sudo install -m 0644 -o root -g wheel \
    com.mozilla.relops.bootstrap.plist \
    /Library/LaunchDaemons/com.mozilla.relops.bootstrap.plist
sudo launchctl bootstrap system \
    /Library/LaunchDaemons/com.mozilla.relops.bootstrap.plist

# 5. Trigger it immediately (don't wait for boot)
sudo launchctl kickstart -k system/com.mozilla.relops.bootstrap

# 6. Confirm
sudo tail -30 /var/log/relops-bootstrap.log
sudo ls -la /var/root/vault.yaml
```

## How it works

The worker's MDM-installed SCEP cert lives in the System keychain. Its private
key is ACL-restricted: only network-stack processes (`configd`, `nehelper`,
`NEIKEv2Provider`, etc.) can sign with it — our own LaunchDaemon code can't
call `SecKeyCreateSignature` on it without hitting `errSecInteractionNotAllowed`.

But curl on macOS 15+ has SecureTransport as a TLS backend (alongside LibreSSL).
SecureTransport IS the OS network stack — it has sign access via that same
ACL entry. With `CURL_SSL_BACKEND=securetransport curl --cert "Mac mini"`,
curl finds the SCEP identity in System.keychain by Subject CN, the TLS
handshake's CertificateVerify signature is generated inside SecureTransport
(allowed), and the LB validates the cert chain via its Trust Config.

The broker then receives the leaf cert in `X-Client-Cert-Leaf` (forwarded
by the LB), parses the SPIFFE URI from the cert SAN, and serves the
role-scoped vault.yaml from Secret Manager.

No PKCS12 extraction. No private key on disk. The cert never leaves the
keychain.

## Threat model summary

| Compromise | Pre-bootstrap state | Post-bootstrap state |
|---|---|---|
| Root on worker | Reads existing `/var/root/vault.yaml` | Same — can also use the keychain cert via SecureTransport curl. Steady-state risk identical: root can already read the resulting `vault.yaml` directly. |
| Network attacker on the MDC1 worker network | Blocked at Cloud Armor (MDC1 CIDR) + TLS | Same + LB validates client cert chain to step-ca, so even from inside MDC1 an attacker needs a step-ca-issued cert to fetch a vault.yaml |
| Lost step-ca SCEP challenge secret | N/A | Limited: attacker also needs to be on the MDC1 source-IP allowlist to use the cert they enroll |
| Compromised broker SA | Could read all vault-* Secret Manager entries | Same (unchanged by this work) |

The cert is an *enrollment credential* that authorizes one operation —
fetching the role-scoped vault.yaml — that root could already perform by
reading `/var/root/vault.yaml` directly. The new design adds a cryptographic
identity binding to a fleet-wide operation that was previously unauthenticated.

## What changed during investigation (for posterity)

We pursued three dead ends before landing on SecureTransport curl:

1. **JWT-signed-by-cert** (original design): blocked because non-Apple
   processes can't call `SecKeyCreateSignature` on the SCEP key.
2. **ACME enrollment with device attestation**: the cert ended up in the
   `com.apple.identities` data-protection access group, which requires an
   Apple-only entitlement to access.
3. **SCEP with `KeyIsExtractable=true` + PKCS12 export**: `SecItemExport`
   also requires the same network-stack ACL grant; returned `-25308`
   `errSecInteractionNotAllowed` from a root LaunchDaemon context.

The realization was that **curl-with-SecureTransport routes signing through
the same network-stack helpers that the keychain ACL trusts**. We don't
need to extract or export anything; we just need the right TLS backend.
