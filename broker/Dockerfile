# Multi-stage: builder layer installs deps into a venv, runtime layer is minimal.
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build
COPY pyproject.toml /build/
RUN pip install --target=/install fastapi 'uvicorn[standard]' pydantic pydantic-settings \
                                  cryptography 'pyjwt[crypto]' google-cloud-secret-manager structlog


FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app:/deps

WORKDIR /app

COPY --from=builder /install /deps
COPY app /app/app

# Cloud Run injects PORT.
EXPOSE 8080

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
