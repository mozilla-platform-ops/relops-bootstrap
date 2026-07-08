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
    simplemdm_api_key_ref: str = Field(default="op://RelOps/SimpleMDM API admin/password")

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

    # (The bootstrap is delivered as a signed PKG / managed install, not a triggered
    # script-job, so there's no bootstrap_script_id anymore.)

    # Hostname -> puppet role mapping
    # Loaded from a per-fleet JSON/YAML file outside this code so it can be edited
    # without redeploying the CLI. For now we default to pattern-matching on hostname.
    role_map_path: str = Field(default="")


@lru_cache
def get_settings() -> Settings:
    return Settings()
