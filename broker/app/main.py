from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import PlainTextResponse, Response

from .auth import AuthError, verify
from .config import get_settings
from .logging_setup import configure_logging, log
from .secrets import read_secret


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = get_settings()

    # Pre-load the step-ca root cert so we don't hit Secret Manager on every request.
    # Cache lives on app.state; refresh is a process restart (Cloud Run rolling deploy).
    root_pem = read_secret(settings.step_ca_root_cert_secret)
    app.state.root_cert_pem = root_pem
    log.info("startup", root_cert_bytes=len(root_pem), project=settings.gcp_project_id)
    yield


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
    settings = get_settings()

    if not authorization:
        log.warning("auth.missing", role=role, remote=request.client.host if request.client else None)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing Authorization")

    try:
        identity = verify(authorization, request.app.state.root_cert_pem)
    except AuthError as e:
        log.warning(
            "auth.failed",
            role=role,
            reason=str(e),
            remote=request.client.host if request.client else None,
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")

    # The cert SAN is authoritative for the host's role. The URL role parameter
    # must match — protects against a host with a valid cert for role X trying
    # to fetch role Y.
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

    # Audit log line; Cloud Logging picks it up and the Audit Logs trail mirrors it
    # via Secret Manager's own audit logging (access_secret_version is logged automatically).
    return Response(content=content, media_type="text/yaml")
