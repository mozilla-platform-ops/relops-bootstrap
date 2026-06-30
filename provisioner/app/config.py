"""Settings for the relops-provisioner. All operator-tunable knobs go here."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # GCP
    gcp_project_id: str = Field(default="relops-bootstrap")

    # Safety mode
    dry_run: bool = Field(
        default=True,
        description="When true, log 'would_fire' decisions but never call the SimpleMDM API. Default safe.",
    )

    # Secret Manager refs
    simplemdm_api_token_secret: str = Field(default="simplemdm-api-token")
    allowlist_secret: str = Field(
        default="provisioner-allowlist",
        description="Newline-separated list of hostnames eligible for auto-provisioning. # for comments.",
    )
    kill_switch_secret: str = Field(
        default="provisioner-enabled",
        description="Value of 'true' enables firing; anything else (incl. read failure) means disabled.",
    )

    # Taskcluster
    tc_root_url: str = Field(default="https://firefox-ci-tc.services.mozilla.com")
    tc_alive_threshold_minutes: int = Field(default=10)
    tc_recent_task_threshold_hours: int = Field(default=6)

    # Rate limit
    rate_limit_per_device_hours: int = Field(default=24)
    rate_limit_state_bucket: str = Field(
        default="relops-bootstrap-provisioner-state",
        description="GCS bucket for per-device last-fired timestamps.",
    )
    rate_limit_state_prefix: str = Field(default="rate-limit/")

    # The fleet — (SimpleMDM assignment_group_id) → {puppet role, SimpleMDM script id}
    # Keys are SimpleMDM ASSIGNMENT_GROUP ids, not device_group ids. RelOps
    # fleet membership for puppet roles lives in assignment_groups.
    # Edit here when adding new roles to auto-provisioning. Keeping this in
    # code (rather than a secret) is intentional: changes need code review.
    target_groups: dict[str, dict] = Field(
        default={
            # 2377013 = SimpleMDM assignment_group gecko-t-osx-1500-m4-no-sip,
            # m4-81's MDM grouping during initial testing. The TC worker pool
            # is the production gecko-t-osx-1500-m4 — TC pool membership is
            # independent of SimpleMDM assignment-group membership.
            # 14716 = "Dev - CC- Bootstrap" script.
            "2377013": {
                "role": "gecko_t_osx_1500_m4",
                "script_id": 14716,
            },
            # Add other (assignment_group, role, script) tuples as more roles
            # come online. Verified assignment_group ids:
            # "2017918": gecko-t-osx-1500-m4 (m4 prod)
            # "2017910": gecko-t-osx-1500-m4-staging
            # "2017911": gecko-t-osx-1400-r8 (r8 prod)
            # "2017895": gecko-t-osx-1015-r8 (1015 prod)
        }
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
