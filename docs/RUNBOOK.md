# 🧑‍🚀 Provisioning Runbook

**The operator's field guide. Copy-paste sequences up top, then the failure modes that
actually happen — each one with the symptom you'll see first, not the cause.**

Every gotcha in here cost someone a real debugging session. They're written symptom-first
because that's the order you meet them in.

---

## ⚡ TL;DR — the two sequences

### 🆕 Fresh hardware → production

```bash
cd ~/git/relops-bootstrap/orchestrator
HOSTS=~/Desktop/wave.txt          # one short hostname per line, '#' comments ok

uv run reprovision batch $HOSTS --action mint         -j3   # SecureToken (idempotent, often a no-op)
uv run reprovision batch $HOSTS --action os-update    -j3   # in-place upgrade to target OS
#   ⏳ wait ~35 min: ~14GB download, then startosinstall, then a reboot
uv run reprovision batch $HOSTS --action preflight --allow-sip-enabled -j3
uv run reprovision batch $HOSTS --action add-to-group --quarantine-on-register -j2
#   ⏳ blocks ~30 min per host: this is the bootstrap, and the watch that holds each host
uv run reprovision batch $HOSTS --action validate     -j3   # ← the gate. Do not skip.
uv run reprovision unquarantine macmini-m4-XXX              # only what validate passed
```

### 🔄 Existing host → wiped → back to production

```bash
uv run reprovision add-to-group macmini-m4-211   # ⚠️ REQUIRED FIRST — see below
uv run reprovision run          macmini-m4-211   # quarantine→drain→wipe→re-enroll→mint→BST→bootstrap
uv run reprovision validate     macmini-m4-211
uv run reprovision unquarantine macmini-m4-211
```

> ⚠️ **`run` has no group step.** It assumes the bootstrap PKG will arrive "during DEP
> convergence" — which only happens if the host is already in a group carrying that PKG.
> Production groups **do not** carry it. Skip the `add-to-group` and the wipe succeeds, then
> the host waits a full hour for a sentinel that can never be written. Always `add-to-group`
> first; it's additive and idempotent.

---

<a id="concurrency"></a>

## 🎚️ Concurrency: `-j` means something different per action

**This is the single easiest way to break a wave.** `-j` is not one knob — each action is
bound by a different resource.

| action | safe `-j` | bound by | notes |
|---|---|---|---|
| `mint` | 3–4 | SSH | fast; often a no-op when the token already exists |
| `os-update` | **irrelevant** ⚠️ | the mirror | **launch-and-return** — `-j` paces only the *launches*. The **hosts-file length is the real concurrency.** 33 hosts = 33 simultaneous 14GB pulls |
| `preflight` | 3–4 | SSH | read-only |
| `add-to-group` | **2** 🚨 | **SimpleMDM API** | 3 API calls per host. At `-j12` the 429 retry budget blew and hosts failed |
| `validate` | 3 | SSH | read-only |
| `provision` | 3 | MDC1 imaging throughput | matches the runner's `RUNNER_MAX_CONCURRENT` |

<a id="j-trap"></a>

### 🪤 The `-j` trap that bit us hardest

`add-to-group --quarantine-on-register` couples a **SimpleMDM-bound action** to a
**30-minute Taskcluster-bound wait**, so one `-j` has to serve both. Raise it for wall-clock
and you hammer SimpleMDM; lower it for SimpleMDM and 33 hosts serialize into ~5½ hours.

At `-j12` on 2026-08-14 it looked like 5 hosts failed. What actually happened: **12 hosts had
already been added to the group** (the add succeeded, the follow-up `push_apps` 429'd), so
killing the batch orphaned 12 live bootstraps with no watcher. They'd have gone into
production unvalidated.

**Workaround until this is fixed** — split it along the resource boundary:

```bash
# 1. the SimpleMDM half, gently
uv run reprovision batch $HOSTS --action add-to-group -j2

# 2. the Taskcluster half, all at once (pre-resolve creds so N processes don't hammer 1Password)
export REPROVISION_TC_CLIENT_ID=... REPROVISION_TC_ACCESS_TOKEN=...
export REPROVISION_QUARANTINE_ON_REGISTER_MAX_WAIT_SECONDS=5400   # 900s default is too short!
grep '^macmini' $HOSTS | xargs -P 33 -I{} \
  sh -c 'nohup uv run reprovision quarantine-on-register {} > /tmp/qor/{}.log 2>&1'
```

> 🧠 **Why 5400.** The default budget is sized for a watch started *after* bootstrap, when
> registration is a minute away. Started at group-add it must span the **whole** bootstrap
> (~30 min). Too short and the watch expires before there's anything to quarantine — and the
> host goes live unheld, i.e. exactly the failure the flag exists to prevent.

---

## 🔬 Never trust `ok`. Verify the thing itself.

`ok` means *the command succeeded*, which is often **not** the same as *the work happened*.

| action | what `ok` really means | how to actually verify |
|---|---|---|
| `os-update` | the upgrade **launched** | `pgrep -x curl` + watch `/private/tmp/InstallAssistant-*.zip` grow. 10 hosts at exactly `0:10` is the launch check, not progress — and it's the same signature the old self-bootout bug produced |
| `add-to-group` | this host's 3 API calls returned | **query group membership** — an add can succeed while `push_apps` fails, so the ✗ list understates what changed |
| `mint` | the step ran | `sysadminctl -secureTokenStatus admin` says `ENABLED` |
| `validate` | 🟢 this one is trustworthy | it *is* the verification |

```bash
# group membership — the authoritative answer
uv run python -c "
from orchestrator.clients import simplemdm as s
print(len(s.assignment_group_device_ids(2417981)), 'devices')"
```

---

## 🖥️ The KVM / refresh-rate trap

**Symptom:** a host takes tasks and fails **100%** of them in ~43s each, then reboots. Looks
like a boot loop or a dead unit.

**Cause:** the KVM negotiates **1280x1024@75Hz**. mozharness runs a pre-test refresh-rate
check and *fatally halts* at anything but 60Hz — before a single test runs.

```
Running pre test command verify refresh rate with '.../macosx_resolution_refreshrate.py --check'
  Refresh Rate: 75.00 Hz
  ERROR: expected refresh rate = 60.00, instead got 75.00.
  FATAL - Halting on failure
Exit Code: 3    run-tests - Wall time: 0s
```

**This is the rack default, not a one-off.** On 2026-08-14, **31 of 33** hosts came up at
75Hz. The two exceptions were the only two whose first task didn't fail.

### 😈 Why it's so easy to misdiagnose

- The worker exits **code 0** with `Host is not dynamically provisioned; exiting`. Nothing
  looks crashed.
- Disk, semaphores, puppet and the bootstrap are **all healthy** and byte-identical to a
  working host.
- `system_profiler SPDisplaysDataType` over SSH shows **only the GPU, no display section**, so
  it will neither confirm nor deny the refresh rate.
- A `TROUBLE`-style counter built on `Aborted:true` / `task exception` / `malformed-payload`
  stays at **0** — these are ordinary task *failures*.

**Fix:** set the KVM correctly, then `validate`. ❌ **Reprovisioning cannot fix it** — a wipe
does not change what the KVM negotiates.

**Triage across a pool** — grep failed task logs for `instead got`; display faults separate
from real test failures instantly. A *missing* `live_backing.log` means something else
entirely (the task never ran).

```bash
# read it straight off the host (must be inside the GUI session — see below)
ssh admin@$H 'uid=$(id -u cltbld); sudo launchctl asuser "$uid" /usr/local/bin/python3 -c "
import Quartz
d=Quartz.CGMainDisplayID(); m=Quartz.CGDisplayCopyDisplayMode(d)
print(\"%.2fHz %dx%d\" % (Quartz.CGDisplayModeGetRefreshRate(m),
      Quartz.CGDisplayModeGetPixelWidth(m), Quartz.CGDisplayModeGetPixelHeight(m)))"'
```

> 🪟 **Over a plain SSH login this returns `0.00Hz 0x0`** — not an error, just zeros, which a
> naive check sails straight past. CoreGraphics needs a window server, so you must hop into the
> console user's session with `launchctl asuser`. And it only works **after** bootstrap:
> `cltbld` doesn't exist until puppet creates it, which is why `validate` can't live in
> `preflight`.

---

<a id="safari-wedge"></a>

## 🎭 Safari remote automation: the two ways it wedges

The AppleScript UI-scripting step is the most fragile part of the bootstrap. Both failures
present identically — `safari-*-has-run` semaphores never appear and puppet retries forever.

### 1. 🧙 MiniBuddy steals focus

**Symptom:** semaphores never written; `frontmost: Setup Assistant`.

`Setup Assistant -MiniBuddy` is the **per-user first-login assistant** — and
`Skip Setup Assistant - All Screens` does **not** suppress it. That profile governs *device*
setup at first boot; MiniBuddy runs when a brand-new **user account** logs in for the first
time. On a reprovisioned host `cltbld` is recreated by puppet after the wipe, so it gets a
fresh one. It sits in front of Safari and `System Events` clicks go to the frontmost app.

```bash
sudo pkill -f "Setup Assistant"
```

### 2. 🪟 Safari running with zero windows

**Symptom:** `Can't get window 1 of process "Safari". Invalid index.`

The script assumes an already-running Safari **with a window**. It neither launches the app
nor waits for a window. If a previous attempt closed them, every retry fails forever — and
`open -a Safari` won't help, because Safari restores a windowless session.

**Fix: reboot.** A clean login gives Safari real windows (verified: 0 → 2 windows), and that's
how the ~50 successful provisions worked.

```bash
ssh admin@$H 'sudo shutdown -r now'
```

> 🐛 **Known bug, needs a ronin_puppet fix:** the AppleScript should *launch* Safari and *wait*
> for a window rather than assuming one. Until then, a disturbed session wedges permanently and
> the repair is a reboot.

**Don't "just log in again"** to fix a stalled host — `cltbld` is already logged in, and a
fresh login re-triggers MiniBuddy.

---

## 🆔 Hostname is not a device key

**Symptom:** `add-to-group` reports `no SimpleMDM device with that name — has it finished DEP
enrolling?` on a host that is enrolled, upgraded, and answering SSH.

A fresh DEP arrival is named **`Mac mini`** in SimpleMDM (`device_name` like `Mac mini (39)`).
The hostname comes from **DHCP**, and nothing in ronin_puppet runs `scutil --set`, so it is
never written back. The API answers **HTTP 200 with zero hits** — silent and confident.

Only *already-provisioned* hosts carry their hostname (every older r8 shows
`name='macmini-r8-118'`), which is why this looked correct when tested against the existing
fleet. Resolution is now by **serial**, read from the host over SSH. 🔧 Fixed in #65.

---

## 🔐 1Password will drop out mid-wave

**Symptom:** `couldn't read op://...` or `timed out reading op://... after 30s`, on many hosts
at once. **Not a host problem.**

`batch` resolves secrets **once in the parent** and passes them to children via `REPROVISION_*`
(#68) — before that, a 10-host batch fired 10+ `op read` calls in seconds and lost **9 of 10
hosts**. But the pre-warm is best-effort: if the *parent's* read fails, children fall back and
fail too.

```bash
op read "op://RelOps/RelOps Worker Admin Key/notesPlain" >/dev/null && echo primed
```

If that hangs, `op` is waiting on a biometric/desktop-app approval — approve it and retry. ⚠️
Standalone commands (not `batch`) each resolve their own secrets, so pre-resolve into env when
looping over many hosts by hand.

---

## 🧨 SimpleMDM group rules

- ✅ **ADD, never MOVE.** Assignment groups are additive. *Moving* a host out of a group strips
  every profile that group carried — on m4-214 a move silently removed **Skip Setup Assistant**
  and the **FDA SSH Keygen Wrapper**, and the host then hung on the Wi-Fi pane at first boot,
  presenting as "Safari automation is broken". Cost most of a day. There is deliberately **no
  remove/move verb** in the client, and a test asserts it stays that way.
- 🚫 **Never add the bootstrap PKG to a production group.** Every member would run the bootstrap
  mid-task. `PROTECTED_GROUP_IDS` refuses this before any HTTP call, hard-coded — a guard you
  can override with an env var is not a guard.
- ⚠️ **Membership does not prove the PKG was pushed.** The already-a-member path skips
  `push_apps`, so a host added by hand in the UI can sit in the group with nothing installed.
  `add-to-group` warns when the payload is missing.

---

## 🍏 Apple platform facts that shape everything

| fact | consequence |
|---|---|
| 🔥 **EACS re-enables SIP** | Never infer post-wipe SIP state from what was set before. Proven on m4-214. Run `csrutil status`. |
| 🎫 DEP skips Setup Assistant → **no SecureToken** | Only an interactive PAM login grants the *first* token. Key-based SSH cannot. This is why the runner must be on-network. |
| 🔑 SecureToken arrival is **inconsistent** | 240–244 needed a manual login; 245–254 arrived with tokens; of 255–288 only 2 needed minting. Don't assume either way — `mint` is idempotent, run it. |
| 🗝️ **BST escrowed ≠ BST generated** | `wipe` uses `DoNotObliterate` and **fails closed** without an escrowed Bootstrap Token — deliberately, so a BST-less box never falls back to a full obliterate. |
| 🖥️ Headless minis don't composite | Screenshots succeed but come out blank. No script fix; needs display infra. |
| 🔁 Hosts reboot **between tasks** | A `worker=down` reading often just means you caught one mid-cycle. `generic-worker`'s `Resolved N tasks in total` is **per session** and re-reads `1` forever — count distinct `Resolving task <id>` lines instead. |

---

## 🌐 Infrastructure notes

- 📦 **The `releng-pxe1` mirror is nginx now**, not the old single-threaded `python3 -m
  http.server` with a listen backlog of 5. Measured while upgrading: **107 MB/s at 4 concurrent,
  205 at 10, 314 at 12, 681 at 23**, no stalls. It is not the bottleneck.
- 🌍 **DNS: use the system resolver.** `host` and `nslookup` query DNS directly and bypass the
  VPN's split-DNS, so they `NXDOMAIN` hosts you can SSH into *right now*. Use
  `dscacheutil -q host -a name <fqdn>`. A bad DNS probe once "found" 14 nonexistent hosts.
- ⏱️ **A DNS race can break the vault fetch.** `curl: (6) Could not resolve host:
  forge.relops.mozilla.com` early in bootstrap means the network wasn't up yet. It retries;
  check whether `/var/root/vault.yaml` exists before treating it as fatal.
- 🐚 **zsh doesn't word-split.** `for n in $HOSTS` iterates **once** with the whole string,
  silently producing empty results. Use `for n in $(echo $HOSTS)` or an array. This has burned
  us repeatedly in throughput one-liners.

---

## 🚦 Quarantine discipline

**Always `validate` before releasing.** It is the only step that catches a host which is
perfect on every other signal and still fails 100% of tasks.

```
exit 0  →  fit          → safe to unquarantine
exit 2  →  not ready    → hasn't bootstrapped; nothing to judge yet
exit 1  →  UNFIT        → do not release; the output names why
```

`--quarantine-on-register` **narrows the race, it does not close it.** The worker registers
about a minute after the sentinel and can `claimWork` immediately; the watch polls every 5s, so
exposure is seconds. Expect **each host to claim exactly one task** before the hold lands.

That one task is a **free early-warning signal** — on 2026-08-14 it's what revealed the
fleet-wide 75Hz fault. The quarantine kept the damage to one task per host (31 total) instead of
~15 each (450+), which is the rate an unheld bad host burns work at.

```bash
# classify a pool's failures fast
# "instead got"  → display/KVM        no log → the task never ran        otherwise → real failure
```

---

## 🗺️ Where things live

| what | where |
|---|---|
| Per-host batch logs | `~/.local/state/reprovision/batch-<stamp>/<host>.log` |
| Bootstrap driver log (on host) | `/var/log/m4-bootstrap-driver.log` |
| Bootstrap log (on host) | `/var/log/m4-bootstrap.log` |
| Completion sentinel | `/var/log/m4-bootstrap-complete` |
| PKG postinstall log | `/var/log/m4-bootstrap-pkg-postinstall.log` |
| Worker log | `/opt/worker/logs/stderr.log` |
| Semaphores | `/var/tmp/semaphore/`, `/Users/cltbld/Library/Preferences/semaphore/` |
| Bootstrap group | SimpleMDM assignment group **2417981** (`gecko-t-osx-1500-m4-bootstrap`) |
| Production group | **2017918** — 🚫 never add the bootstrap PKG here |

`startosinstall` writes thousands of `Preparing: N.N%` lines, so `tail` is useless on the
upgrade log — filter with `grep -v 'Preparing:'`.

---

## 🧯 Quick triage table

| symptom | likely cause | first move |
|---|---|---|
| 100% task failure, ~43s each, worker exits 0 | KVM at 75Hz | read the display mode; fix the KVM |
| Safari semaphores never appear | MiniBuddy focus **or** windowless Safari | `pkill -f "Setup Assistant"`, else reboot |
| `no SimpleMDM device with that name` | hostname isn't a device key | ensure SSH works so the serial can be read |
| Many hosts fail on `op://` at once | 1Password session dropped | `op read <ref>` to prime, retry |
| Host waits forever for the sentinel | not in a group carrying the PKG | `add-to-group` |
| `NO-DNS` on a host you can SSH to | `host`/`nslookup` bypassing split-DNS | use `dscacheutil` |
| Task fails with **no** `live_backing.log` | the task never ran | check for a reboot mid-task |
| `add-to-group` 429s | `-j` too high | drop to `-j2`; re-run (idempotent) |
| Worker down on one probe | reboots between tasks | probe again before believing it |

---

## 🔗 See also

- [`orchestrator/README.md`](../orchestrator/README.md) — the pipeline, every command, secret handling
- [`../README.md`](../README.md) — architecture, mTLS/SCEP design, bringing it up
- [eacs.html](https://mozilla-platform-ops.github.io/relops-bootstrap/eacs.html) — what EACS
  actually does, button-press → re-enroll
