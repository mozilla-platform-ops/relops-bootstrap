from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_prefix="REPROVISION_")

    # Taskcluster
    tc_root_url: str = Field(default="https://firefox-ci-tc.services.mozilla.com")
    tc_client_id: str = Field(default="")
    tc_access_token: str = Field(default="")

    # SimpleMDM
    simplemdm_api_key: str = Field(default="")

    # SSH to host
    ssh_admin_user: str = Field(default="admin")
    # Password for the SimpleMDM-managed admin account. Used ONLY for the interactive
    # password login that mints the first SecureToken (see workflow.step_mint) and for
    # the non-interactive BST escrow. Key-based ssh handles everything else.
    # NB: SimpleMDM does not expose the auto-admin password via API, so if you enable
    # unique-per-device passwords this must be sourced another way (secret store).
    ssh_admin_password: str = Field(default="admin")
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
