from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # GCP
    gcp_project_id: str = Field(default="", description="GCP project ID; auto-detected on Cloud Run")
    step_ca_root_cert_secret: str = Field(
        default="step-ca-root-cert",
        description="Secret Manager secret holding the step-ca root PEM",
    )

    # JWT
    jwt_audience: str = Field(
        default="",
        description="Expected JWT aud claim. Defaults to the broker's own URL at startup.",
    )
    jwt_leeway_seconds: int = Field(default=10, description="Clock-skew tolerance on exp/iat checks")
    jwt_max_lifetime_seconds: int = Field(
        default=300,
        description="Reject JWTs whose exp-iat exceeds this. Keeps replay window bounded.",
    )

    # SPIFFE URI structure that step-ca puts in cert SANs.
    # Format: spiffe://relops.mozilla/host/<hostname>/role/<puppet_role>
    spiffe_trust_domain: str = Field(default="relops.mozilla")

    # Logging
    log_json: bool = Field(default=True)
    log_level: str = Field(default="INFO")


@lru_cache
def get_settings() -> Settings:
    return Settings()
