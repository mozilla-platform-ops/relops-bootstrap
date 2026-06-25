# Installing the relops-bootstrap LaunchDaemon on a worker

Eventually this is Puppet's job. For first-deploy validation, manual steps:

```bash
# 1. Push the updated SCEP profile from SimpleMDM (KeyIsExtractable=true,
#    AllowAllAppsAccess=true). Wait for mdmclient to re-issue the cert.
#    Confirm with: security find-identity -v /Library/Keychains/System.keychain

# 2. Install the swift helper + script
sudo mkdir -p /usr/local/lib/relops-bootstrap
sudo install -m 0644 -o root -g wheel \
    extract-identity.swift /usr/local/lib/relops-bootstrap/extract-identity.swift
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
sudo ls -la /var/root/vault.yaml /var/root/.relops-bootstrap/
```

## Validation milestone before committing to B-1

The unknown step is whether `SecItemExport` works from a root LaunchDaemon
context with `KeyIsExtractable=true + AllowAllAppsAccess=true`. The keychain
ACL we dumped earlier shows export_clear/export_wrapped are restricted to
network-stack helpers — but those are the legacy *ACL* entries, not the
`KeyIsExtractable` flag. Apple's docs imply that flag relaxes export, but the
interaction with non-interactive contexts isn't documented.

**Validate by:** re-enroll m4-81 with the updated profile, run
`relops-bootstrap.sh` from a `launchctl kickstart`, and confirm
`extract-identity.swift` exits 0. If it returns
`errSecInteractionNotAllowed (-25308)` or similar, B-1 is unworkable in this
shape and the fallback is B-2 (worker-side step-ca enrollment with
disk-resident keys from the start).

## Threat model summary

| Compromise | Pre-bootstrap state | Post-bootstrap state |
|---|---|---|
| Root on worker | Reads existing `/var/root/vault.yaml` | Same (+ also reads `/var/root/.relops-bootstrap/key.pem`) |
| Network attacker between worker and broker | Blocked at Cloud Armor (MDC1 CIDR) + TLS | Same + LB validates client cert chain to step-ca |
| Lost step-ca SCEP challenge secret | N/A | Limited blast radius: attacker also needs to be on the MDC1 source-IP allowlist to use the cert they enroll |
| Compromised broker SA | Could read all vault-* Secret Manager entries | Same (unchanged by B-1) |

The cert is an *enrollment credential* — its only privilege is fetching the
role-scoped vault.yaml that root could already read directly off the worker.
