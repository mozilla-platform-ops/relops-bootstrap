# Multi-stage: builder layer installs deps into a venv, runtime layer is minimal.
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# Copy pyproject + the package so `pip install .` works.
COPY pyproject.toml /build/
COPY app /build/app

# Install the project (drives deps from pyproject.toml — no need to maintain a second list here).
RUN pip install --target=/install .


FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/deps

WORKDIR /app

COPY --from=builder /install /deps

# Cloud Run injects PORT.
EXPOSE 8080

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
