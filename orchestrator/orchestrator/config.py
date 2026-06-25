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

    # 1Password (where per-role vault.yaml content lives)
    # The CLI shells out to `op` rather than using the SDK; the operator's
    # local `op` config provides auth (Service Account token or operator SSO).
    onepassword_vault: str = Field(default="RelOps Vault")

    # SSH to host
    ssh_admin_user: str = Field(default="admin")
    ssh_command_timeout_seconds: int = Field(default=120)

    # Polling cadence
    drain_poll_seconds: int = Field(default=15)
    drain_max_wait_seconds: int = Field(default=3600)
    wipe_poll_seconds: int = Field(default=30)
    wipe_max_wait_seconds: int = Field(default=1800)
    bootstrap_poll_seconds: int = Field(default=30)
    bootstrap_max_wait_seconds: int = Field(default=3600)

    # Secret bootstrap script ID in SimpleMDM
    bootstrap_script_id: int = Field(
        default=0,
        description="SimpleMDM script ID to run on the device post-DEP. Find via the SimpleMDM API or web UI.",
    )

    # Hostname -> puppet role mapping
    # Loaded from a per-fleet JSON/YAML file outside this code so it can be edited
    # without redeploying the CLI. For now we default to pattern-matching on hostname.
    role_map_path: str = Field(default="")


@lru_cache
def get_settings() -> Settings:
    return Settings()
