# `reprovision`

**One command to take a Mozilla CI worker from "in service" to "erased, re-enrolled, re-bootstrapped, and back in the pool" — no console, no Setup Assistant, no pasted secrets.**

```bash
reprovision run macmini-m4-80
```

Apple Silicon makes remote reprovisioning genuinely hard: DEP skips Setup Assistant, so a
freshly-enrolled mini has **no SecureToken and no Bootstrap Token** — and without those you
can't Erase-All-Content-and-Settings it again, can't escrow trust, and can't bootstrap
headless. `reprovision` automates the whole dance, including the one step everyone assumes
needs a human at the keyboard: **minting the first SecureToken**.

---

## The pipeline

```
  reprovision run macmini-m4-80
        │
        ▼
  ┌───────────────┐   TC (Hawk)
  │  quarantine   │   stop new tasks landing on the worker
  └───────┬───────┘
          ▼
  ┌───────────────┐   TC (Hawk)
  │    drain      │   wait out the in-flight task (2 idle polls)
  └───────┬───────┘
          ▼
  ┌───────────────┐   SimpleMDM
  │     wipe      │   EACS — obliteration_behavior=DoNotObliterate.
  │  ⚠ BST guard  │   Aborts unless a Bootstrap Token is escrowed, so a
  └───────┬───────┘   BST-less box never falls back to a full obliterate.
          ▼
  ┌───────────────┐   SimpleMDM
  │ wait-reenroll │   poll for a *fresh* enrolled_at (status lags the erase)
  └───────┬───────┘
          ▼
  ┌───────────────┐   SSH (expect)
  │     mint      │   interactive password login → grants admin's FIRST
  └───────┬───────┘   SecureToken. Key-based ssh can't do this. Idempotent.
          ▼
  ┌───────────────┐   SSH
  │  escrow-bst   │   profiles install -type bootstraptoken → EACS-able again
  └───────┬───────┘
          ▼
  ┌───────────────┐   (no step — signed PKG does it)
  │  bootstrap    │   managed-install PKG fetches vault.yaml over mTLS using its
  │  (mTLS/SCEP)  │   SCEP cert, runs puppet, registers in Taskcluster
  └───────┬───────┘
          ▼
  ┌───────────────┐   SSH
  │ wait-sentinel │   poll for /var/log/m4-bootstrap-complete
  └───────┬───────┘
          ▼
     still quarantined  (pass --unquarantine to return to service)
```

The **bootstrap itself is not a workflow step** — it's a **signed PKG** (managed install)
scoped to the SimpleMDM group. Once `admin` logs in (the mint), the managed PKGs start
installing and the bootstrap runs on its own. It fetches `vault.yaml` over mTLS using its
SCEP-issued client cert, so there's no vault-delivery step and no `op read` + SSH-drop anymore.

---

## Runner — driven from Hangar (one-click, no terminal)

`reprovision` also runs unattended behind the [hangar](https://github.com/mozilla-platform-ops/hangar)
dashboard's one-click **Reprovision** button. Hangar (Cloud Run) can't reach MDC1, so it only
*queues* a job; a small **on-network runner** (`reprovision-runner`, this package) polls Hangar
over **mTLS — outbound only, no inbound to the datacenter** — claims the job, runs `reprovision run`,
and streams every stdout line back as a job event that renders live in the Hangar cockpit.

The runner is **Puppet-managed** (ronin_puppet role `gecko_t_osx_1500_m4_reprovision_runner`): a
LaunchDaemon runs it, its creds come from the host's `vault.yaml`, and its mTLS cert is step-ca-issued.
It's the only component that holds SSH/admin creds — Hangar holds none. Proven in prod: a Hangar
click reprovisioned `macmini-m4-80` via the managed runner on `macmini-m4-81`.

```bash
reprovision-runner            # HANGAR_API_URL + RUNNER_CLIENT_CERT/KEY (mTLS) or REPROVISION_RUNNER_TOKEN
```

### Companion agents on the same host

The runner host carries two more Puppet-managed LaunchDaemons, sharing its venv, env file and
mTLS cert. Same reason in both cases: Cloud Run can't reach MDC1, so anything needing to touch
a worker has to run on-network and push results outbound.

| Daemon | Purpose |
|---|---|
| `hangar-screen-agent` | On-demand VNC frames for Hangar's live worker view. |
| `hangar-tart-health-agent` | Sweeps the tart VM hosts and pushes per-slot health to Hangar. |

```bash
hangar-tart-health-agent --hosts-file hosts.txt --loop --interval 600   # as the LaunchDaemon runs it
hangar-tart-health-agent --hosts-file hosts.txt --dry-run               # one sweep, print, don't push
```

**`hangar-tart-health-agent`** exists because Taskcluster can't see inside a tart VM. In July 2026
five of 26 slots in `gecko-t-osx-1500-m-vms` were out of production for weeks: three guests were
crash-rebooting every ~84 s while `tart run` on the host stayed up 11+ days, so every host-level
signal read green. Nor could it surface as a TC worker error — hardware pools have no
worker-manager representation, so `reportWorkerError` returns `ResourceNotFound` whatever scopes
the caller holds.

It is deliberately a **dumb collector**: every severity threshold lives server-side in Hangar, so
what counts as `crit` can be retuned by deploying Hangar alone, with nothing changed on-network.

Per host it collects, in one ssh round trip: guest uptime vs `tart run` uptime (the crash-loop
signature), guest disk free, configured workerId vs the MAC-derived one (a clone impersonating
another host's worker), guest clock skew (a clone inheriting the image RTC never registers),
client-cert expiry/notBefore/owner, and the puppet clone's refspec.

Four things that only bite in production, all now handled — worth knowing if you extend it:

- **Prod is cert-only.** Hangar sets `REPROVISION_RUNNER_HOSTS` but no `REPROVISION_RUNNER_TOKEN`,
  so `require_runner`'s token fallback is dead there. The token path is for local dev.
- **Push at the runner hostname, not the human one.** Only `/api/tart-health/agent/*` is routed to
  the non-IAP backend. A push at `hangar.relops.mozilla.com` is 301'd to a login page.
- **`HANGAR_API_URL` already ends in `/api`** — as the sibling agents read it. `_api_base()`
  normalises, so both that and a bare site URL work.
- **ssh must go as the admin user.** Under launchd as root, a bare `ssh <host>` is `root@` and
  every worker refuses it; identity comes from `clients/ssh.py`, so a rotated admin key needs no
  change here.

Guest-level probes (disk, clock, identity) need an `expect` password login per VM and are off by
default (`--no-guests` in the LaunchDaemon argv). They are also the checks that catch a
crash-looping guest inside a healthy host, so a green Hangar page without them means "the hosts
look fine", not "the fleet is fine".

Running it by hand on the runner host **requires sourcing the env file first**, or secret
resolution falls through to an `op://` ref and the 1Password CLI isn't installed there:

```bash
sudo bash -c 'set -a; . /var/root/reprovision-runner/runner.env; set +a;
  /opt/reprovision-runner/.venv/bin/hangar-tart-health-agent \
    --hosts-file /var/root/reprovision-runner/tart-hosts.txt --no-guests --dry-run'
```

## Two golden paths

Both share the same core — **mint → escrow BST → signed-PKG bootstrap → sentinel**. They
differ only at the front.

### Reprovision an existing host → back to prod  *(what `run` does; proven on m4-80/m4-81)*

Preconditions: host already in the SimpleMDM group (SCEP / CLT / `relops_key_admin` /
bootstrap PKG / DEP fixed-pw), **BST escrowed**, quarantined.

```bash
reprovision run macmini-m4-80
# quarantine → drain → wipe (EACS) → wait-reenroll → mint → escrow-bst → wait-sentinel
```

Stays quarantined by default. If TC creds are unavailable, skip the TC-dependent front and
run from `wipe` (the box is already quarantined):

```bash
reprovision wipe          macmini-m4-80
reprovision wait-reenroll macmini-m4-80
reprovision mint          macmini-m4-80
reprovision escrow-bst    macmini-m4-80
reprovision wait-sentinel macmini-m4-80
```

### Provision a fresh host → prod  *(what `provision` does)*

No EACS (factory-clean). **Nothing in this path can erase a host** — that's the point of it
being a separate command from `run`, whose second phase is a wipe.

```bash
reprovision provision macmini-m4-201
# preflight → mint → escrow-bst → bootstrap-pkg → wait-sentinel
```

For a hardware refresh where a tech racks the minis and disables SIP in the same visit,
everything after that visit is remote:

1. **Assign** the machines (by serial) to DEP/ADE and to the **intake** group — the group that
   carries DEP account-setup, `relops-ssh` and the OS-install job, but *not* the bootstrap PKG.
   Make sure DHCP reservations exist first: nothing in puppet sets the macOS hostname, so a
   worker registers under whatever name DHCP hands it.
2. **At the rack:** power on → DEP enrolls (Setup Assistant skipped) → `csrutil disable`.
3. **MDM:** in-place update to the target OS. Stagger it — the installer download is what
   saturates the link.
4. `reprovision batch hosts.txt --action preflight` — read-only; tells you which hosts actually
   came out at the target OS with SIP off, *before* you commit to anything.
5. **Move the passing hosts into the bootstrap group** — membership installs the signed PKG, so
   the move *is* the trigger. Keeping the PKG out of the intake group is what stops an OS
   install from landing on top of a converging puppet run.
6. `reprovision batch hosts.txt --action provision -j 3 --quarantine-on-register`.

> **If the tech can't disable SIP at the rack**, it's because Recovery authenticates against a
> **volume owner** and a DEP host with Setup Assistant skipped has none until an interactive
> login. Fix without a second trip: `reprovision batch hosts.txt --action mint` (remote, creates
> the volume owner), then Recovery. `mint` is idempotent, so `provision` re-running it later is
> a no-op — which is why it's safe to run early.

A fresh host was never quarantined, so by default it **starts claiming work as soon as
generic-worker registers.** To hold it out of the pool:

```bash
reprovision provision macmini-m4-201 --quarantine-on-register
```

A worker that isn't registered yet **cannot** be quarantined — `queue.quarantineWorker` 404s
on a worker that doesn't exist — so this watches for it to appear and quarantines on sight.

> **It narrows the race; it does not close it.** The driver writes the sentinel, then
> worker-runner starts generic-worker, which registers and can `claimWork` immediately —
> roughly a minute after the sentinel. We poll every 5s from the sentinel onward, so exposure
> is seconds rather than however long it takes someone to notice. A task claimed inside that
> window does run on an unvalidated host. For a hard guarantee, don't move the host into the
> bootstrap group until you can accept it in the pool.

Fails closed: without TC credentials it refuses **up front**, rather than spending the
bootstrap window to discover it can't quarantine anything. Validate, then
`reprovision unquarantine <host>`.

### Batches

```bash
reprovision batch hosts.txt --action preflight            # read-only readiness sweep
reprovision batch hosts.txt --action mint                 # before the Recovery trip
reprovision batch hosts.txt --action provision -j 3 \
    --quarantine-on-register                              # the real run
```

`hosts.txt` is one short hostname per line, `#` comments allowed. Each host runs as its own
`reprovision` subprocess with its own log under `~/.local/state/reprovision/batch-<stamp>/`,
so one bad host can't take the batch down.

Hosts that aren't ready yet come back **skipped**, not failed, and re-running the same command
picks them up — every action is idempotent:

```
  ✓ macmini-m4-201  ok       12:44
  ▲ macmini-m4-202  skipped   0:04   macOS 26.1, expected 15.3 — let the MDM in-place update…
  ✗ macmini-m4-203  failed    0:918  password login denied (wrong admin password?)

  ████████  38 ok · 15 skipped · 2 failed · 71:20 wall clock
```

Concurrency defaults to **3**, matching the runner's `RUNNER_MAX_CONCURRENT`: the ceiling is
MDC1 network and imaging throughput, not local CPU. Pushing past it is what took ~12 of 25
hosts offline simultaneously on the 2026-05-12 batch.

---

## Secrets: nothing touches your shell

**Every secret is resolved at run time from a vault — you never `export` or paste one.**

For each secret the resolution order is: **direct env var → its `_REF` → error.** A `_REF`
of `op://Vault/Item/field` is read via the **1Password CLI**; anything else is treated as a
**GCP Secret Manager** secret id and read via `gcloud` (reusing your existing login).

```
baked-in REFERENCE (a shared pointer):        the secret lives in the vault:
  ssh_admin_password_ref =                     op://RelOps/DEP …SSH/password
        │                                              │
        └────────── secrets.ssh_admin_password() ──────┘  ← op read, at run time
```

The *references* are shared, non-secret pointers into the team `RelOps` 1Password vault and
ship as config defaults — the secrets themselves stay in the vault, gated by vault access.
One `op signin` per session and **no secret ever lands in your shell history**. You only
touch config to *override* a default (see `.env.example`).

| Var | Required | Default |
|---|---|---|
| `REPROVISION_SIMPLEMDM_API_KEY` / `_REF` | yes | `op://RelOps/SimpleMDM API admin/password` |
| `REPROVISION_TC_CLIENT_ID` / `_REF` | quarantine steps only | `op://RelOps/Taskcluster Quarantine/username` |
| `REPROVISION_TC_ACCESS_TOKEN` / `_REF` | quarantine steps only | `op://RelOps/Taskcluster Quarantine/password` |
| `REPROVISION_SSH_ADMIN_PASSWORD` / `_REF` | yes | `op://RelOps/DEP Provisioned Mac Admin Account SimpleMDM SSH/password` |
| `REPROVISION_SSH_ADMIN_KEY` / `_REF` | no | `op://RelOps/RelOps Worker Admin Key/notesPlain` (drives `admin@`; set the ref empty to use ssh-agent / `~/.ssh` instead) |
| `REPROVISION_SSH_ADMIN_USER` | no | `admin` |
| `REPROVISION_GCP_PROJECT` | no | `relops-bootstrap` (only if a `_REF` is a Secret Manager id) |

Any `_REF` can instead be a **GCP Secret Manager** secret id (then `gcloud auth login`); a
direct `REPROVISION_*` value always wins over its `_REF`.

> TC credentials are **only** needed for `quarantine` / `drain` / `unquarantine`. The core
> `wipe → reenroll → mint → escrow → wait-sentinel` sequence runs without them.

---

## Commands

| Command | What it does |
|---|---|
| `reprovision run <host>` | Full pipeline, **including an EACS wipe**. `--unquarantine` returns it to service at the end. |
| `reprovision provision <host>` | Fresh DEP host → prod. **No wipe in this path.** `--no-wait` stops after the BST escrow. |
| `reprovision preflight <host>` | Read-only readiness check (OS version, SIP, SecureToken, BST). Needs no SimpleMDM/TC credential. |
| `reprovision quarantine-on-register <host>` | Watch for a fresh worker to register, then quarantine it on sight. |
| `reprovision wait-bootstrap-pkg <host>` | Confirm the signed PKG landed — i.e. the host is in the bootstrap group. |
| `reprovision batch <file>` | Run `--action preflight\|mint\|provision` across a host list, `-j` at a time, one log per host. |
| `reprovision quarantine <host>` | Quarantine in Taskcluster. |
| `reprovision drain <host>` | Wait for the current task to finish. |
| `reprovision wipe <host>` | EACS via SimpleMDM (`DoNotObliterate`, BST-guarded). |
| `reprovision wait-reenroll <host>` | Block until a fresh `enrolled_at` after the erase. |
| `reprovision mint <host>` | Mint the admin SecureToken via interactive password login (idempotent). |
| `reprovision escrow-bst <host>` | `profiles install -type bootstraptoken` (needs `mint` first). |
| `reprovision wait-sentinel <host>` | Poll for `/var/log/m4-bootstrap-complete`. |
| `reprovision unquarantine <host>` | Return to service (needs a `queue:quarantine` scope). |

Any step is independently re-runnable — if the pipeline fails partway, fix the issue and
re-run just that subcommand.

**Exit codes:** `0` success · **`2` host not ready** (wrong OS, SIP still enabled, box not up
yet — go fix it or come back later) · `1` everything else. `batch` reads the `2` to report
skipped separately from failed, which is what makes "38 ok, 15 not ready, 2 broken" possible.

| Gate override | Default |
|---|---|
| `--expected-os` / `REPROVISION_PROVISION_EXPECTED_OS` | `15.3` (accepts point releases: 15.3.1 ✓, 15.4 ✗) |
| `--allow-sip-enabled` | off — `provision`/`preflight` require SIP **disabled** |
| `--quarantine-on-register` | off — a fresh host serves as soon as it registers |
| `-j` / `REPROVISION_BATCH_MAX_CONCURRENT` | `3` |
| `REPROVISION_PREFLIGHT_SSHD_WAIT_SECONDS` | `60` |
| `REPROVISION_QUARANTINE_ON_REGISTER_POLL_SECONDS` | `5` (this interval *is* the exposure window) |
| `REPROVISION_QUARANTINE_ON_REGISTER_MAX_WAIT_SECONDS` | `900` |
| `REPROVISION_BOOTSTRAP_PKG_MAX_WAIT_SECONDS` | `300` — how long to wait for the PKG before calling it a group problem |

### Wrong-group detection

The PKG is a managed install triggered by **group membership**, so a host in the wrong group
never bootstraps. Without a check, `wait-sentinel` polls the full hour and then reports
"sentinel did not appear in time" — which points at the bootstrap when the real answer is the
group assignment. Across 55 hosts that's an expensive way to find a typo.

`provision` therefore confirms `/usr/local/sbin/m4-bootstrap.sh` (the PKG payload) is present
before committing to the sentinel wait, and reports it as a **skip** with
`bootstrap pkg hasn't landed — is this host in the bootstrap group?`

It runs **after** mint/escrow, not before: PKG delivery has been observed to land during
convergence once admin logs in, so gating earlier could fail a host that was going to be fine.
`preflight` only *reports* PKG presence — the readiness sweep runs before the group move, so
gating there would fail every host in the sweep.

### Demos

```bash
reprovision demo --flow reprovision   # EACS an existing host and bring it back
reprovision demo --flow provision     # a fresh DEP host, rack to prod
reprovision demo --flow batch         # the refresh rollout: sweep, then the provision wave
```

Safe replays — no ssh, no SimpleMDM, no Taskcluster. They drive the same `ui` layer as the real
run, so the screen looks like production, just faster. `--flow batch` is the one to show for a
refresh: it runs a deliberately mixed host set so the skipped-vs-failed reporting is visible.

> **The host stays quarantined through the whole reprovision by default.** `run` does *not*
> auto-unquarantine unless you pass `--unquarantine`, because returning a host to service
> needs a `queue:quarantine`-scoped credential that isn't wired fleet-wide yet. The safe
> default keeps it out of the pool.

---

## Install & first-run setup

Everything an operator needs before their first `reprovision`. Any RelOps engineer with
the same SimpleMDM / Taskcluster / vault permissions can run this.

**1. Be on the VPN.** Targets are `*.test.releng.mdc1.mozilla.com`, reachable only over the
Mozilla VPN. (An `ssh` **exit 255** almost always means you're off-VPN.)

**2. Get the code.** From wherever you keep checkouts (this is the *only* `cd`):
```bash
git clone git@github.com:mozilla-platform-ops/relops-bootstrap.git
cd relops-bootstrap/orchestrator
```
> Already have the repo? Instead of the two lines above, jump to it from anywhere inside your
> checkout with `cd "$(git rev-parse --show-toplevel)/orchestrator"` (never double-`cd`s).

**3. Install** — pip:
```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest -q                       # sanity check — should be all green
```
…or [`uv`](https://docs.astral.sh/uv/) (drop-in — same `.venv`, same `reprovision`):
```bash
uv venv --python 3.11 && source .venv/bin/activate   # >=3.11 (3.12/3.13/3.14 also fine)
uv pip install -e '.[dev]'
uv run pytest -q
```
(No `uv.lock` is committed, so uv resolves from `pyproject.toml` — pip users are unaffected.
With uv you can also skip the activate: `uv run reprovision …`.)

**4. Sign in to 1Password (one-time per shell session):**
```bash
op signin
```
No config needed — the secret *references* (shared `op://` pointers into the team `RelOps`
1Password vault) are baked into the defaults, so `op signin` is the whole auth story and
**`gcloud` is not required.** (Only create a `.env` from `.env.example` to *override* a
default — a different vault, or GCP Secret Manager as the backend, which then needs
`gcloud auth login`.)
> First time on a new machine, the 1Password CLI needs the account added once:
> `op account add --address mozilla.1password.com --email <you>@mozilla.com`, then
> `eval $(op signin)`. Or enable **1Password app → Settings → Developer → Integrate with
> 1Password CLI**.

**5. Verify your access** before touching a host (read-only — confirms every credential resolves
from the vault, makes no changes):
```bash
reprovision check
```
Each line shows `✓` or a plain fix-it message. A `403 / not authorized` means you're signed in but
your 1Password account isn't in the **`RelOps`** vault yet — ask a RelOps admin to add you.

---

## Why the mint is the hard part

DEP skips Setup Assistant, so at enrollment **no account is a volume owner** and `admin` has
**no SecureToken**. Apple only grants the first token on an *interactive* (PAM) authentication —
a real password login. Key-based SSH does **not** trigger it. `mint` reproduces the operator's
manual `ssh admin@host` + typed password using `expect`, driving `keyboard-interactive`
against macOS (where `sshpass` is unreliable). The authentication *is* the mint; the remote
command is irrelevant. Once `admin` holds a token, `escrow-bst` can escrow a Bootstrap Token —
which is exactly what the next EACS requires, closing the loop.

## Admin password

The DEP macOS Account Setup must create `admin` with a **fixed** password, because `mint`
logs in with it. **Hardening:** set a strong, random password in the SimpleMDM DEP
account-setup and point `REPROVISION_SSH_ADMIN_PASSWORD_REF` at it in a vault. That's the
whole story — no rotation step.

Why not SimpleMDM's `rotate_admin_password`? It only rotates an *auto-generated managed*
password (`"macOS Auto Admin password can not be rotated"` for a fixed one), and SimpleMDM
won't expose an auto-generated password via API for the mint to read. The two requirements
are mutually exclusive, so we harden via a strong fixed DEP password instead.

## Secret delivery

There is no vault-delivery step. The bootstrap PKG on the host fetches `vault.yaml` itself
over mTLS from the broker using its SCEP-issued client cert (the rest of this repo provisions
that broker + CA). The old 1Password `op read` + SSH-drop `deliver_vault` step is gone.

## Known limitations

- `drain` is a best-effort heuristic (`is_currently_busy`) over the worker's recent tasks and
  their run states via the TC queue. A worker between two tasks can briefly look idle, so the
  step requires **two consecutive idle polls**.
- Hostname → puppet role mapping uses prefix patterns in `role_map.py`; override fleet-wide
  via `REPROVISION_ROLE_MAP_PATH`.
- Single-worker only. A `reprovision batch <pool>` whole-pool driver is a future addition.
