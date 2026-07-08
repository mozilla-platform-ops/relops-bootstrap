# reprovision

Operator-side CLI that drives an end-to-end EACS workflow for a Mozilla CI worker.

## What it does

```bash
reprovision run macmini-m4-81
```

…walks through these steps in order:

1. **quarantine** in Taskcluster
2. **drain** — wait for the current task to finish
3. **wipe** — POST /devices/{id}/wipe with `obliteration_behavior=DoNotObliterate`
   (**EACS-only**: if Erase All Content & Settings can't run, the erase *fails* rather than
   doing a full obliterate). `step_wipe` first verifies a **Bootstrap Token is escrowed** —
   EACS requires it, and wiping a BST-less box would otherwise fail or full-obliterate into a
   long headless macOS reinstall (see **Prerequisites**).
4. **wait_for_reenroll** — poll SimpleMDM for a *fresh* `enrolled_at` (status alone is
   unreliable — it stays `enrolled` until the erase actually executes)
5. **mint** — interactive password SSH login to mint the first SecureToken (DEP skips
   Setup Assistant, so admin has no token until a PAM login; key-based ssh won't do it).
   Idempotent — skips if already ENABLED.
6. **escrow_bst** — `sudo profiles install -type bootstraptoken -user admin -password …`
   (non-interactive; requires the token from step 5)
7. **wait_for_sentinel** — poll for `/var/log/m4-bootstrap-complete` over SSH

> **The bootstrap itself is not a workflow step.** It's a **signed PKG** (managed install)
> assigned to the group that lands during DEP convergence — once admin logs in (the mint) the
> managed pkgs start installing — and runs on its own. It fetches `vault.yaml` over mTLS using
> its SCEP cert (Path C), so there's no vault-delivery step either. (Previously the bootstrap
> was a triggered SimpleMDM *script-job* + the vault came from a 1Password `op read` — both gone.)

The host **stays quarantined** through the whole reprovision by default — `run` does
**not** auto-unquarantine unless you pass `--unquarantine`. Returning a host to service
needs a `queue:quarantine`-scoped credential (not wired fleet-wide yet), so the safe
default keeps it quarantined. `reprovision run <host> --unquarantine` does the final
un-quarantine; there's also a standalone `unquarantine` subcommand.

If any step fails, fix the issue and re-run the individual subcommand:

```bash
reprovision mint macmini-m4-81            # re-run just the SecureToken mint
reprovision escrow-bst macmini-m4-81      # re-run just the BST escrow
reprovision wait-sentinel macmini-m4-81
reprovision unquarantine macmini-m4-81
```

## Two flows: fresh vs reprovision

Both share the same core — **mint → escrow BST → signed-PKG bootstrap → sentinel**. They
differ only at the front. `reprovision run` is the *reprovision* driver; a fresh machine uses
the individual steps (no EACS).

### Reprovision an existing host → back to prod  *(what `run` does; proven on m4-80/m4-81)*

Preconditions: host already in the SimpleMDM group (SCEP / CLT / `relops_key_admin` / bootstrap
PKG / DEP fixed-pw), **BST escrowed** (`sudo profiles status -type bootstraptoken` → `YES`),
quarantined.

```bash
reprovision run macmini-m4-XX
# = quarantine → drain → wipe (EACS) → wait_for_reenroll → mint → escrow_bst → wait_for_sentinel
```

Stays quarantined. If TC creds are unavailable, run the steps from `wipe` onward — the box is
already quarantined, so `quarantine`/`drain` are skippable.

### Provision a fresh host → prod

No EACS (factory-clean). Standard DEP front-end, then the same core:

1. **Assign** the machine (by serial) to DEP/ADE **and** the SimpleMDM group.
2. **Power on** → DEP enrolls (Setup Assistant skipped) → profiles/pkgs begin.
3. `reprovision mint macmini-m4-XX` — mints the *first* SecureToken + triggers pkg delivery.
4. `reprovision escrow-bst macmini-m4-XX` — establishes BST custody (so it's EACS-able later).
5. The signed **bootstrap PKG** runs → SCEP vault → puppet → sentinel;
   confirm with `reprovision wait-sentinel macmini-m4-XX`.
6. Quarantine during bring-up; **un-quarantine into service** when validated (needs a
   `queue:quarantine`-scoped credential).

## Prerequisites

The target host must:

- Be in a SimpleMDM assignment group that carries the **Dev - SCEP** profile (vault mTLS
  cert), **Command Line Tools**, the **relops_key_admin** pkg (installs the operator ssh key),
  the **bootstrap PKG** (signed managed install), and a DEP account-setup that creates `admin`
  with a **fixed** password.
- Have a **Bootstrap Token escrowed** (`sudo profiles status -type bootstraptoken` →
  `escrowed to server: YES`). EACS requires it; `step_wipe` aborts without it rather than risk
  a full obliterate. A normally-provisioned host already has one; a host that never minted a
  SecureToken (so never escrowed a BST) can't be EACS'd until it does.
- Be **quarantined** in Taskcluster (it stays quarantined through the whole reprovision).

## Install

```bash
cd orchestrator
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
```

## Config

All settings come from environment variables prefixed `REPROVISION_`. Drop a
`.env` file in the working directory or export them.

| Var | Required | Purpose |
|---|---|---|
| `REPROVISION_SIMPLEMDM_API_KEY` | yes | SimpleMDM API key: device wipe + status |
| `REPROVISION_TC_CLIENT_ID` | quarantine steps only | TC client with `queue:quarantine-worker:*` scope |
| `REPROVISION_TC_ACCESS_TOKEN` | quarantine steps only | TC client access token |
| `REPROVISION_SSH_ADMIN_USER` | no | Default: `admin` |
| `REPROVISION_SSH_ADMIN_PASSWORD` | yes (prod) | The DEP account-setup admin password, used for the mint login. Set to your strong DEP password (from a secret); defaults to `admin` only for lab use. |

TC credentials are only needed for the `quarantine` / `drain` / `unquarantine`
steps. The core `wipe → reenroll → mint → escrow → wait-sentinel` sequence
runs without them.

## Secret delivery (Path C)

There is no vault-delivery step. The bootstrap script on the host fetches
`vault.yaml` itself over mTLS from the broker using its SCEP-issued client cert
(the rest of this repo provisions that broker + CA). Previously this was a
1Password `op read` + SSH drop; that `deliver_vault` step and its `op` dependency
have been removed.

## Admin password

The DEP macOS Account Setup must create `admin` with a **fixed** password, because the
`mint` step logs in with it (`REPROVISION_SSH_ADMIN_PASSWORD`) to grant the first SecureToken.

**Hardening:** set a strong, random password in the SimpleMDM DEP account-setup and point
`REPROVISION_SSH_ADMIN_PASSWORD` at it (ideally sourced from a secret store, not committed).
That's the whole story — no rotation step is needed.

Why not SimpleMDM's `rotate_admin_password`? It only rotates an *auto-generated managed*
password and returns `"macOS Auto Admin password can not be rotated"` for a fixed one — and
SimpleMDM won't expose an auto-generated password via API for the mint to read. The two
requirements are mutually exclusive, so we harden via a strong fixed DEP password instead.

## Known limitations

- `drain` uses a best-effort heuristic (`is_currently_busy`): it inspects the worker's
  recent tasks and their run states via the TC queue. A worker between two tasks can briefly
  look idle; the step requires two consecutive idle polls to mitigate that.
- Hostname → puppet role mapping uses prefix patterns in `role_map.py`. Edit
  there or load a file via `REPROVISION_ROLE_MAP_PATH` for fleet-wide overrides.
- Single-worker mode only. A `reprovision batch <pool>` driver for whole-pool
  re-image is a future addition.
