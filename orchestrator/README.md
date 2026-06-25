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
5. **escrow_bst** — `ssh admin@host sudo profiles install -type bootstraptoken`
6. **deliver_vault** — `op read` the role's vault.yaml from 1Password → SSH drop to `/var/root/vault.yaml`
7. **trigger_bootstrap** — SimpleMDM script_jobs API to run the bootstrap script
8. **wait_for_sentinel** — poll for `/var/log/m4-bootstrap-complete` over SSH
9. **unquarantine** in Taskcluster

If any step fails, fix the issue and re-run the individual subcommand:

```bash
reprovision deliver-vault macmini-m4-81   # re-run just the vault drop
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

## Path A vs Path C

This CLI implements **Path A** today — operator-side 1Password fetch + SSH drop.
When Path C (broker + SCEP) is live, the `deliver_vault` step becomes a no-op
because the bootstrap script on the host fetches vault.yaml from the broker
using its SCEP cert. A future flag (`--path c`) will skip the 1Password step.

## Known limitations

- `claimed_task_count()` in `clients/taskcluster.py` is a placeholder. The drain
  wait currently returns 0 immediately. Operator should manually verify the
  worker isn't running a task before invoking the full workflow until this is
  wired up properly.
- Hostname → puppet role mapping uses prefix patterns in `role_map.py`. Edit
  there or load a file via `REPROVISION_ROLE_MAP_PATH` for fleet-wide overrides.
- Single-worker mode only. A `reprovision batch <pool>` driver for whole-pool
  re-image is a future addition.
