# Intel r8 builders → macOS 15: `reprovision builder-upgrade`

**Status:** design, not built. Tracks RELOPS-2594.

Moves an Intel Mac mini builder (`*_b_osx_1015` roles, macOS 10.15) to macOS 15 in place:
no wipe, same pool, same vault.yaml, new role (`*_b_osx_1500`). Every step below was done by
hand on macmini-r8-202, r8-241 and r8-242 in Sept–Oct 2026; each one exists because skipping
it broke something.

## Reused as-is

| Piece | Where |
|---|---|
| Hangar enqueue → runner claim → stream events | `ReprovisionJob(action=...)`, `runner.py` |
| quarantine, drain, quarantine-on-register, unquarantine | `workflow.py` |
| Paced fan-out, per-host logs, idempotent re-runs | `batch.py` |
| In-place upgrade launcher (pkg from releng-pxe1 → one-shot `startosinstall` LaunchDaemon) | `step_os_update`, `data/macos-upgrade.sh` |
| Drain → OS update → puppet → reboot → verify shape | `tart-host-update` (#85/#86) |

## The workflow

| # | Step | What it does | Why (what broke without it) |
|---|---|---|---|
| 1 | `preflight` | **T2 bridgeOS ≥ 18P**; `puppet config print certname` == `facter networking.fqdn`; stock `hiera.yaml` (no `hiera_vault`); free disk; record the pinned task user from `/opt/worker/current-task-user.json`; target role exists | 17P6610 T2s can't stage the 24P6005 firmware (≈67 MB short on the T2's own storage) and every install path boots straight back to 10.15 — r8-195, four attempts. Those hosts need a DFU revive, not this tool. |
| 2 | `quarantine`, `drain` | reuse | |
| 3 | `stop_worker` | `rm /var/tmp/semaphore/run-buildbot`; `launchctl bootout system/org.mozilla.worker-runner`; log out the console user with `launchctl bootout gui/$(id -u $(stat -f%Su /dev/console))` (expect the console owner to become `root`) | Otherwise worker-runner reboots mid-upgrade. Autologin leaves the pinned task user holding the console (its `loginwindow` and Aqua session), which gets in the way of the installer; `killall loginwindow` is a crash to macOS and can autologin straight back in. |
| 4 | `clear_caches` | as root: `/opt/worker/cache/*`, `/opt/worker/tasks/*`, `{}` → `directory-caches.json`; dead `/Users/cltbld/caches`; stale `macOS Install Data`; `Xcode.app.pre-*` | Installer + upgrade + Xcode need ~60 GB. r8-241 had 49 GB of cache; r8-195 had 22 GB of 2020-era caches. `/opt/worker/cache` is 0700 — a bare `sudo rm` glob deletes nothing. |
| 5 | `os_update` | Intel variant of `macos-upgrade.sh` (15.8.1 pkg; no SecureToken/`--user` on Intel) | |
| 6 | `wait_for_os` | poll for a 24-series build and T2 24P | |
| 7 | `restore_sudo` | wait for the MDM sudo-less pkg, else `sudo -S` with the relops password from 1Password | The upgrade resets `/etc/sudoers` to Apple's stock file; relops loses NOPASSWD. |
| 8 | `install_xcode` | `data/install-xcode.sh` (xip from the ronin package bucket; verifies signature + build `16F6`; `Xcode-16.4.app` + symlink; `xcode-select`; license; first launch) | **Must run before puppet.** With Xcode 12.2 still selected, `/usr/bin/git` crashes in xcodebuild's plugin loader, so `run-puppet.sh` dies silently at "Checking existing Puppet repository...". |
| 9 | `install_clt` | `softwareupdate -i "Command Line Tools for Xcode-16.4"` with no timeout | Disappears once ronin's `macos_xcode_tools` has a longer exec timeout. |
| 10 | `set_role` | `role_map`: `*_b_osx_1015` → `*_b_osx_1500` | vault.yaml's `worker:` hash already names the pool/client, so no secret changes for an in-place upgrade. |
| 11 | `puppet` ×2 | `run-puppet.sh` detached with a log; require "Compiled catalog for `<fqdn>`" and a non-trivial resource count; second run must change nothing but the always-run mercurial exec | r8-270's stale `certname = …local` applied an empty catalog in 0.04s and reported success. Disappears once ronin pins `--certname`. |
| 12 | `verify` | autologin == pinned task user and `dscl . -authonly` passes; `worker-runner-config.yaml` pool + client; Xcode 16F6; openssl Cellar 1.1.1h linked; managed `AutomaticallyInstallMacOSUpdates = 0` | r8-202 auto-installed 15.8.1 overnight without the update profile, which wiped `/var/tmp` (and the semaphore) and left it dead for 7h. |
| 13 | `return` | touch semaphore, reboot, `quarantine_on_register` unless `--unquarantine` | |

### Donor variant (tester → builder, e.g. r8-270)

Same steps, plus: upgrade via SimpleMDM's OS-update declaration (testers have `apsd`, so it
lands in minutes); strip `[main] server`/`certname` from `puppet.conf`; fetch the target
role's vault.yaml over mTLS (Path C) instead of copying a token between hosts. The
tester's `worker:` hash must not survive — it carries the tester pool, client and
`multiuser-static` engine.

## Ronin changes that shrink the tool

1. `run-puppet.sh`: pass `--certname=<facter fqdn>` (kills step 11's empty-catalog check).
2. `macos_xcode_tools`: `unless => test -x …/CommandLineTools/usr/bin/git`, `timeout => 1800` (kills step 9).
3. Later: Xcode version as role data. The first install still has to come from the tool —
   puppet can't fetch its repo until git works.

## Hangar

- **Worker action** "Upgrade builder to macOS 15": allowlist, red confirm, streamed timeline.
- **Migration board**: each builder pool's hosts as converted / eligible / needs DFU /
  unreachable, from a read-only scan agent (same shape as the screen and tart-health agents).
- **Capacity guard** in batch mode: refuse if the pool would drop below N workers or its
  queue is already over a threshold. With gecko-1 at 2 workers the L1 queue reached 74 and
  try tasks started hitting `deadline-exceeded`.
- **Per-pool cap** while a release branch is unfixed (gecko-3 ≤ 50% on macOS 15 until the
  esr153 `build-cpython.sh` backport, D335124, lands).

## Open questions

1. `startosinstall` on 18P+ Intel hosts is untested — r8-241/242 used the GUI installer.
   Try the CLI by hand on the next host (r8-139) before step 5 depends on it.
2. releng-pxe1 has the 15.3 installer for the M4s; it needs 15.8.1.
3. Old builders get MDM changes only at boot (`apsd` disabled) and some never received
   group profiles at all (r8-202). Is the sudo pkg reliable enough to drop the password
   fallback?
