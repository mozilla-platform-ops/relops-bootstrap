# reprovision

Operator-side CLI that drives an end-to-end EACS workflow for a Mozilla CI worker.

## What it does

```bash
reprovision run macmini-m4-81
```

…walks through these steps in order:

1. **quarantine** in Taskcluster
2. **drain** — wait for the current task to finish
3. **wipe** — POST /devices/{id}/wipe with `obliteration_behavior=ObliterateWithWarning`
4. **wait_for_reenroll** — poll SimpleMDM until device status = enrolled
5. **mint** — interactive password SSH login to mint the first SecureToken (DEP skips
   Setup Assistant, so admin has no token until a PAM login; key-based ssh won't do it).
   Idempotent — skips if already ENABLED.
6. **escrow_bst** — `sudo profiles install -type bootstraptoken -user admin -password …`
   (non-interactive; requires the token from step 5)
7. **trigger_bootstrap** — SimpleMDM script_jobs API to run the bootstrap script.
   The bootstrap fetches `vault.yaml` itself over mTLS using its SCEP cert (Path C),
   so there is **no vault-delivery step** (previously a 1Password `op read` + SSH drop).
8. **wait_for_sentinel** — poll for `/var/log/m4-bootstrap-complete` over SSH

The host **stays quarantined** through the whole reprovision by default — `run` does
**not** auto-unquarantine unless you pass `--unquarantine`. Returning a host to service
needs a `queue:quarantine`-scoped credential (not wired fleet-wide yet), so the safe
default keeps it quarantined. `reprovision run <host> --unquarantine` does the final
un-quarantine; there's also a standalone `unquarantine` subcommand.

If any step fails, fix the issue and re-run the individual subcommand:

```bash
reprovision mint macmini-m4-81            # re-run just the SecureToken mint
reprovision escrow-bst macmini-m4-81      # re-run just the BST escrow
reprovision trigger-bootstrap macmini-m4-81
reprovision wait-sentinel macmini-m4-81
reprovision unquarantine macmini-m4-81
```

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
| `REPROVISION_TC_CLIENT_ID` | yes | TC client with `queue:quarantine-worker:*` scope |
| `REPROVISION_TC_ACCESS_TOKEN` | yes | TC client access token |
| `REPROVISION_SIMPLEMDM_API_KEY` | yes | SimpleMDM API key with device wipe + script_jobs perms |
| `REPROVISION_BOOTSTRAP_SCRIPT_ID` | yes | Numeric SimpleMDM script id for the bootstrap script |
| `REPROVISION_ONEPASSWORD_VAULT` | no | 1P vault name (default: "RelOps Vault") |
| `REPROVISION_SSH_ADMIN_USER` | no | Default: `admin` |

The `op` CLI must be authenticated on the operator's laptop (either `op signin`
or an `OP_SERVICE_ACCOUNT_TOKEN` env var for a service account).

## Secret delivery: today and future

Today, `deliver_vault` reads the role's vault.yaml from 1Password via the
`op` CLI on the operator's laptop and SSH-drops it to `/var/root/vault.yaml`
on the host. The bootstrap script there is already polling for that path,
so no host-side changes are needed.

Once the SCEP + broker infrastructure (the rest of this repo) is live,
`deliver_vault` becomes a no-op: the bootstrap script on the host fetches
vault.yaml from the broker directly using its SCEP-issued client cert.
A `--no-vault-drop` flag will skip the 1Password step at that point.

## Known limitations

- `claimed_task_count()` in `clients/taskcluster.py` is a placeholder. The drain
  wait currently returns 0 immediately. Operator should manually verify the
  worker isn't running a task before invoking the full workflow until this is
  wired up properly.
- Hostname → puppet role mapping uses prefix patterns in `role_map.py`. Edit
  there or load a file via `REPROVISION_ROLE_MAP_PATH` for fleet-wide overrides.
- Single-worker mode only. A `reprovision batch <pool>` driver for whole-pool
  re-image is a future addition.
