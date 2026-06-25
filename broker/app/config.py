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
    # Stored as CSV string because pydantic-settings JSON-parses list-typed env vars before
    # field validators run, so `JWT_ALLOWED_ALGS=ES256,RS256` would fail JSON decode.
    # Access via the .jwt_allowed_algs property below.
    jwt_allowed_algs_csv: str = Field(
        default="RS256,ES256",
        description="Comma-separated JWT algs to accept. RS256 is the Apple SCEP default; "
        "ES256 if the SCEP profile is configured for ECC keys. "
        "Env var: JWT_ALLOWED_ALGS_CSV.",
    )
    jwt_require_jti: bool = Field(
        default=True,
        description="Reject JWTs without a jti claim. Required for replay cache to function.",
    )

    @property
    def jwt_allowed_algs(self) -> list[str]:
        return [a.strip() for a in self.jwt_allowed_algs_csv.split(",") if a.strip()]

    # SPIFFE URI structure that step-ca puts in cert SANs.
    # Format: spiffe://relops.mozilla/host/<hostname>/role/<puppet_role>
    spiffe_trust_domain: str = Field(default="relops.mozilla")

    # Replay protection: in-memory jti cache. TTL slightly longer than the max JWT lifetime
    # so a JWT can't be replayed within its own validity window.
    jti_cache_ttl_seconds: int = Field(default=360)
    jti_cache_max_size: int = Field(default=10_000)

    # Per-cert-serial rate limit. Token bucket, refills 1/sec, burst configurable.
    # Defends against credential abuse if a host is compromised.
    rate_limit_requests_per_minute: int = Field(default=60)
    rate_limit_burst: int = Field(default=10)

    # CRL revocation check (optional; off until step-ca is bootstrapped and the URL is known).
    crl_url: str = Field(default="", description="URL of step-ca's CRL endpoint. Empty = revocation check disabled.")
    crl_refresh_seconds: int = Field(default=300, description="Re-fetch CRL every N seconds in a background task.")
    crl_fetch_timeout_seconds: int = Field(default=15)

    # Logging
    log_json: bool = Field(default=True)
    log_level: str = Field(default="INFO")


@lru_cache
def get_settings() -> Settings:
    return Settings()
