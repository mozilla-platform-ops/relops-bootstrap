from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_prefix="REPROVISION_")

    # --- Secrets ---
    # Each secret resolves as: the direct value below (env var) → its *_ref → error.
    # A *_ref of "op://Vault/Item/field" is read via the 1Password CLI; anything else is
    # a GCP Secret Manager secret id read via gcloud. Keep refs in .env; secrets stay in
    # the vault, fetched at run time (see secrets.py). One-time gcloud/op auth, no exports.
    gcp_project: str = Field(default="relops-bootstrap")

    # The *_ref defaults below are shared, NON-secret pointers into the team "RelOps"
    # 1Password vault — the actual secrets stay in the vault, gated by vault access. Anyone
    # with the same permissions gets a zero-config setup: just `op signin`, no .env needed.
    # Override via env / .env only if your vault or item names differ.

    # Taskcluster
    tc_root_url: str = Field(default="https://firefox-ci-tc.services.mozilla.com")
    tc_client_id: str = Field(default="")
    tc_client_id_ref: str = Field(default="op://RelOps/Taskcluster Quarantine/username")
    tc_access_token: str = Field(default="")
    tc_access_token_ref: str = Field(default="op://RelOps/Taskcluster Quarantine/password")

    # SimpleMDM
    simplemdm_api_key: str = Field(default="")
    # Shared op:// ref (no gcloud). Override with a Secret Manager id if you prefer that backend.
    # NB: the item is "SimpleMDM API admin VNC", not "SimpleMDM API admin". The shorter name
    # was the default here and no longer exists in the vault, so `reprovision check` failed for
    # every operator with `isn't an item in the "RelOps" vault`. Verified 2026-08-10: this ref
    # returns a key that GETs /api/v1/account with HTTP 200. (GCP Secret Manager id
    # `simplemdm-api-token` also works, if you'd rather resolve it through gcloud.)
    simplemdm_api_key_ref: str = Field(default="op://RelOps/SimpleMDM API admin VNC/password")

    # SSH to host
    ssh_admin_user: str = Field(default="admin")
    # Operator private key for the admin account — the counterpart to the public key that
    # relops_key_admin installs on every worker. Resolved at run time and used via `ssh -i`
    # (written to a 0600 temp file), so any operator with vault access drives admin@ without
    # placing a key on disk. Shared op:// ref by default; empty → ssh-agent / default identities.
    ssh_admin_key: str = Field(default="")
    ssh_admin_key_ref: str = Field(default="op://RelOps/RelOps Worker Admin Key/notesPlain")
    # Password for the SimpleMDM-managed admin account — used ONLY for the interactive
    # password login that mints the first SecureToken (workflow.step_mint) and the
    # non-interactive BST escrow. Key-based ssh handles everything else. Prefer the *_ref
    # (e.g. an op:// reference to the fixed DEP admin password) over a raw value.
    ssh_admin_password: str = Field(default="")
    ssh_admin_password_ref: str = Field(
        default="op://RelOps/DEP Provisioned Mac Admin Account SimpleMDM SSH/password"
    )
    ssh_command_timeout_seconds: int = Field(default=120)

    # Polling cadence
    drain_poll_seconds: int = Field(default=15)
    drain_max_wait_seconds: int = Field(default=3600)
    wipe_poll_seconds: int = Field(default=30)
    wipe_max_wait_seconds: int = Field(default=1800)
    bootstrap_poll_seconds: int = Field(default=30)
    bootstrap_max_wait_seconds: int = Field(default=3600)

    # --- Fresh-host provisioning (no EACS) ---
    # Target OS for a fresh DEP host. The MDM installs it as an in-place update *before* the
    # host is moved into the bootstrap group; step_preflight refuses to provision a host that
    # isn't there yet, because a mid-bootstrap OS install takes the box down for 25-45 min
    # (seen on ~12 of 25 hosts during the 2026-05-12 batch). Matched as an exact version or a
    # prefix, so "15.3" accepts 15.3 and 15.3.1 but not 15.30 / 15.4.
    provision_expected_os: str = Field(default="15.3")
    # How long preflight waits for sshd before calling a host "not up yet" (and skipping it,
    # rather than blocking a batch slot for the full 15-minute DEP-convergence window).
    preflight_sshd_wait_seconds: int = Field(default=60)
    # Default fan-out for `reprovision batch`. 3 matches the reprovision runner's
    # RUNNER_MAX_CONCURRENT: the ceiling here is MDC1 network/imaging, not local CPU.
    batch_max_concurrent: int = Field(default=3)
    # `provision --quarantine-on-register`: a fresh worker can't be quarantined before it
    # exists in TC (quarantineWorker 404s), so we watch for it and quarantine on sight. Poll
    # tightly — generic-worker can claim a task within about a minute of the sentinel, and
    # this interval IS the exposure window. Registration itself trails the sentinel, so the
    # wait only needs to cover worker-runner starting generic-worker, not a puppet run.
    quarantine_on_register_poll_seconds: int = Field(default=5)
    quarantine_on_register_max_wait_seconds: int = Field(default=900)
    # How long to wait for the bootstrap pkg to land before concluding the host is in the wrong
    # SimpleMDM group. Short by design: the point is to fail in minutes instead of burning the
    # full bootstrap_max_wait_seconds on a sentinel that was never going to appear. Long enough
    # to absorb a pending MDM check-in right after a group move (check-in on these boxes is
    # often boot-only).
    bootstrap_pkg_poll_seconds: int = Field(default=10)
    bootstrap_pkg_max_wait_seconds: int = Field(default=300)

    # (The bootstrap is delivered as a signed PKG / managed install, not a triggered
    # script-job, so there's no bootstrap_script_id anymore.)

    # The SimpleMDM assignment group `add-to-group` puts hosts into. Membership here IS the
    # bootstrap trigger, so this is the one number that decides what a wave does. Default is
    # gecko-t-osx-1500-m4-bootstrap (an exact clone of 2377013: 7 apps + 10 profiles incl.
    # Dev - SCEP). Configurable because the next fleet will have its own group; the production
    # groups are separately blocked in clients.simplemdm.PROTECTED_GROUP_IDS, which this cannot
    # override.
    bootstrap_group_id: int = Field(default=2417981)

    # `validate` refuses a host whose main display isn't at this refresh rate. 60.0 because that is
    # what mozharness's own pre-test check enforces — matching it means validate agrees with what CI
    # will decide, rather than inventing a second standard. A KVM presenting 75Hz made m4-242 fail
    # 15 consecutive production tasks in ~43s each (2026-08-14) without ever running a test.
    # Resolution is deliberately NOT gated: it varies legitimately across the fleet, so validate
    # reports it and only hard-fails on the thing CI actually rejects.
    validate_expected_refresh_hz: float = Field(default=60.0)

    # Hostname -> puppet role mapping
    # Loaded from a per-fleet JSON/YAML file outside this code so it can be edited
    # without redeploying the CLI. For now we default to pattern-matching on hostname.
    role_map_path: str = Field(default="")


@lru_cache
def get_settings() -> Settings:
    return Settings()
