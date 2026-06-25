from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import PlainTextResponse, Response

from .auth import AuthError, verify_mtls_headers
from .config import get_settings
from .logging_setup import configure_logging, log
from .replay_cache import RateLimiter
from .secrets import read_secret


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = get_settings()

    app.state.rate_limiter = RateLimiter(
        requests_per_minute=settings.rate_limit_requests_per_minute,
        burst=settings.rate_limit_burst,
    )

    log.info(
        "startup",
        project=settings.gcp_project_id,
        rate_limit_rpm=settings.rate_limit_requests_per_minute,
        spiffe_trust_domain=settings.spiffe_trust_domain,
    )
    yield


app = FastAPI(title="vault-broker", version="0.2.0", lifespan=lifespan)


@app.get("/healthz", response_class=PlainTextResponse)
async def healthz() -> str:
    return "ok"


@app.get("/_debug/headers")
async def debug_headers(request: Request) -> dict:
    """Dump the request headers the broker actually receives. Used to verify
    LB-forwarded mTLS headers during bring-up. Remove after Path C is proven."""
    return {k: v for k, v in request.headers.items()}


@app.get("/secret/{role}")
async def get_secret(
    role: str,
    request: Request,
    x_client_cert_present: str = Header(default="", alias="X-Client-Cert-Present"),
    x_client_cert_chain_verified: str = Header(
        default="", alias="X-Client-Cert-Chain-Verified"
    ),
    x_client_cert_error: str = Header(default="", alias="X-Client-Cert-Error"),
    x_client_cert_spiffe: str = Header(default="", alias="X-Client-Cert-SPIFFE"),
    x_client_cert_serial_number: str = Header(
        default="", alias="X-Client-Cert-Serial-Number"
    ),
) -> Response:
    state = request.app.state
    remote = request.client.host if request.client else None

    try:
        identity = verify_mtls_headers(
            cert_present=x_client_cert_present,
            chain_verified=x_client_cert_chain_verified,
            cert_error=x_client_cert_error,
            spiffe=x_client_cert_spiffe,
            serial_number=x_client_cert_serial_number,
        )
    except AuthError as e:
        log.warning("auth.failed", role=role, reason=str(e), remote=remote)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid client cert"
        )

    if not state.rate_limiter.consume(identity.cert_serial):
        log.warning(
            "auth.rate_limited",
            role=role,
            hostname=identity.hostname,
            cert_serial=identity.cert_serial,
            remote=remote,
        )
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
