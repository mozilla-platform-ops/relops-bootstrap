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

### Provision a fresh host → prod

No EACS (factory-clean). Standard DEP front-end, then the same core:

1. **Assign** the machine (by serial) to DEP/ADE **and** the SimpleMDM group.
2. **Power on** → DEP enrolls (Setup Assistant skipped) → profiles/PKGs begin.
3. `reprovision mint macmini-m4-XX` — mints the *first* SecureToken + kicks off PKG delivery.
4. `reprovision escrow-bst macmini-m4-XX` — establishes BST custody (so it's EACS-able later).
5. Signed **bootstrap PKG** runs → SCEP vault → puppet → sentinel;
   confirm with `reprovision wait-sentinel macmini-m4-XX`.
6. Quarantine during bring-up; **un-quarantine into service** when validated.

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
| `reprovision run <host>` | Full pipeline. `--unquarantine` returns it to service at the end. |
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
