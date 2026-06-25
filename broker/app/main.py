from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import PlainTextResponse, Response

from .auth import AuthError, ReplayError, RevokedCertError, verify
from .config import get_settings
from .logging_setup import configure_logging, log
from .replay_cache import JtiCache, RateLimiter
from .revocation import (
    RevocationCache,
    start_background_refresh,
    stop_background_refresh,
)
from .secrets import read_secret


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = get_settings()

    # Pre-load the step-ca root cert.
    root_pem = read_secret(settings.step_ca_root_cert_secret)
    app.state.root_cert_pem = root_pem

    # In-memory state shared across requests.
    app.state.jti_cache = JtiCache(max_size=settings.jti_cache_max_size)
    app.state.rate_limiter = RateLimiter(
        requests_per_minute=settings.rate_limit_requests_per_minute,
        burst=settings.rate_limit_burst,
    )
    app.state.revocation_cache = RevocationCache()

    # CRL polling — no-op if CRL_URL not configured.
    app.state.crl_task = start_background_refresh(
        app.state.revocation_cache,
        settings.crl_url,
        settings.crl_refresh_seconds,
        settings.crl_fetch_timeout_seconds,
    )

    log.info(
        "startup",
        root_cert_bytes=len(root_pem),
        project=settings.gcp_project_id,
        crl_enabled=bool(settings.crl_url),
        rate_limit_rpm=settings.rate_limit_requests_per_minute,
    )

    try:
        yield
    finally:
        await stop_background_refresh(app.state.crl_task)


app = FastAPI(title="vault-broker", version="0.1.0", lifespan=lifespan)


@app.get("/healthz", response_class=PlainTextResponse)
async def healthz() -> str:
    return "ok"


@app.get("/secret/{role}")
async def get_secret(
    role: str,
    request: Request,
    authorization: str = Header(default=""),
) -> Response:
    state = request.app.state
    remote = request.client.host if request.client else None

    if not authorization:
        log.warning("auth.missing", role=role, remote=remote)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing Authorization")

    try:
        identity = verify(
            authorization,
            state.root_cert_pem,
            jti_cache=state.jti_cache,
            revocation_cache=state.revocation_cache,
        )
    except RevokedCertError as e:
        log.warning("auth.revoked", role=role, reason=str(e), remote=remote)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="cert revoked")
    except ReplayError as e:
        log.warning("auth.replay", role=role, reason=str(e), remote=remote)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="replay detected")
    except AuthError as e:
        log.warning("auth.failed", role=role, reason=str(e), remote=remote)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")

    # Rate limit *after* auth so unauthenticated probes don't fill the bucket-table.
    if not state.rate_limiter.consume(identity.cert_serial):
        log.warning(
            "auth.rate_limited",
            role=role,
            hostname=identity.hostname,
            cert_serial=identity.cert_serial,
            remote=remote,
        )
        # 429 with a generic message; never leak rate-limit internals.
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="rate limit exceeded",
            headers={"Retry-After": "60"},
        )

    if identity.role != role:
        log.warning(
            "auth.role_mismatch",
            url_role=role,
            cert_role=identity.role,
            hostname=identity.hostname,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"cert is authorized for role '{identity.role}', not '{role}'",
        )

    secret_id = f"vault-{role}"
    try:
        content = read_secret(secret_id)
    except Exception as e:
        log.error("secret.read_failed", secret_id=secret_id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="secret unavailable",
        )

    log.info(
        "secret.served",
        role=role,
        hostname=identity.hostname,
        cert_serial=identity.cert_serial,
        bytes=len(content),
    )

    return Response(content=content, media_type="text/yaml")
