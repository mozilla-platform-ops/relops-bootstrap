# m4-bootstrap pkg

Signed Developer ID Installer package that delivers the M4 Mac Mini zero-touch
bootstrap flow via SimpleMDM (or any MDM) as a Custom App, replacing the
SimpleMDM Script Job (id 14716) delivery path.

## Why a pkg instead of a script-job

- **Signed & MDM-native.** Custom Apps on SimpleMDM install from an assignment
  group without a script-job trigger. Cleaner state machine, retry semantics,
  and the pkg is Developer-ID-Installer signed so Gatekeeper is happy.
- **One-shot install, self-detached.** Postinstall bootstraps a one-shot
  LaunchDaemon and returns immediately, so the installer doesn't hang for the
  ~30-min bootstrap. The daemon fires `/usr/local/sbin/m4-bootstrap.sh`, which
  in turn installs the reboot-survivable `com.mozilla.m4-bootstrap` driver.
- **Idempotent.** The script's `$SENTINEL` guard means re-installing on an
  already-bootstrapped host is a fast no-op. Reinstall via MDM to re-trigger.

## Layout

```
pkg/
├── build.sh                              # builder — pkgbuild + productbuild + productsign
├── README.md                             # this file
├── payload/                              # laid down on target at install
│   ├── usr/local/sbin/m4-bootstrap.sh    # the bootstrap script
│   └── Library/LaunchDaemons/
│       └── com.mozilla.m4-bootstrap-entrypoint.plist   # one-shot RunAtLoad kickstart
├── scripts/
│   └── postinstall                       # launchctl bootstrap the entrypoint plist
├── build/                                # intermediate output (gitignored)
└── dist/                                 # signed pkg output (gitignored)
```

## Build

```bash
cd ~/git/relops-bootstrap/pkg
./build.sh
```

Environment overrides:

- `SIGN_IDENTITY="Developer ID Installer: Mozilla Corporation (43AQ936H96)"`
  — default is Ryan Curran's personal cert; set this to sign with the org cert.
- `PKG_VERSION=2026.07.08` — default is `YYYY.MM.DD.HHMM`.
- `NOTARIZE=1` — submit + staple via `xcrun notarytool` using keychain profile
  `relops-notarize`. MDM install works without notarization, but this makes
  the pkg Gatekeeper-pass for direct-double-click too.

Output: `dist/m4-bootstrap-<version>.pkg`.

## Deploy via SimpleMDM

1. **Apps → Add App → macOS App**, upload `dist/m4-bootstrap-<version>.pkg`.
2. Set **Deploy to** = `gecko-t-osx-1500-m4-no-sip` (assignment_group 2377013).
3. On enrollment, SimpleMDM pushes it as `InstallApplication` MDM command. The
   host reboots into DEP → SCEP profile lands → this pkg installs → entrypoint
   LaunchDaemon fires → bootstrap runs → puppet takes over.

**Re-fire on an existing host:** in SimpleMDM, delete `Bootstrap Complete`
custom attribute (or just push the app again) — the postinstall calls
`launchctl bootout` before `bootstrap`, so re-install re-fires cleanly.

## What lands on the host

| Path | Owner | Mode | Purpose |
|---|---|---|---|
| `/usr/local/sbin/m4-bootstrap.sh` | root:wheel | 0755 | bootstrap logic |
| `/Library/LaunchDaemons/com.mozilla.m4-bootstrap-entrypoint.plist` | root:wheel | 0644 | one-shot kickstart at install + every boot (no-op after sentinel) |
| `/var/log/m4-bootstrap.log` | root:wheel | 0644 | script stdout+stderr |
| `/var/log/m4-bootstrap-entrypoint.{out,err}` | root:wheel | 0644 | launchd capture |
| `/var/log/m4-bootstrap-pkg-postinstall.log` | root:wheel | 0644 | pkg postinstall log |
| `/var/log/m4-bootstrap-complete` | root:wheel | 0644 | sentinel; presence = done |

The bootstrap script drops additional artifacts during its run (see the
inline comments) and self-cleans the reboot-survivable driver LaunchDaemon
(`com.mozilla.m4-bootstrap`) once puppet + safari + TCC semaphores are all
in place.

## Verify signature after build

```bash
pkgutil --check-signature dist/m4-bootstrap-<version>.pkg
spctl --assess --type install dist/m4-bootstrap-<version>.pkg   # requires notarized
```

## Rebuild-and-reupload workflow

1. Edit `payload/usr/local/sbin/m4-bootstrap.sh` (or any other payload file).
2. `./build.sh` — new pkg with a fresh timestamp version.
3. Upload the new `dist/*.pkg` to SimpleMDM (or use their API):
   ```
   curl -u "$SIMPLEMDM_API_KEY:" -X POST \
     -F "binary=@dist/m4-bootstrap-<version>.pkg" \
     https://a.simplemdm.com/api/v1/apps
   ```
