from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # GCP
    gcp_project_id: str = Field(
        default="",
        description="GCP project ID; auto-detected on Cloud Run",
    )

    # SPIFFE URI structure that step-ca puts in cert SANs. The broker parses
    # this out of the leaf cert itself (see auth.py) — it does NOT rely on the
    # LB's X-Client-Cert-SPIFFE header, which silently drops URL-encoded URIs.
    # Format: spiffe://relops.mozilla/host/<hostname>/role/<puppet_role>
    spiffe_trust_domain: str = Field(default="relops.mozilla")

    # Per-cert-serial rate limit. Token bucket, refills 1/sec, burst configurable.
    rate_limit_requests_per_minute: int = Field(default=60)
    rate_limit_burst: int = Field(default=10)

    # Logging
    log_json: bool = Field(default=True)
    log_level: str = Field(default="INFO")


@lru_cache
def get_settings() -> Settings:
    return Settings()
