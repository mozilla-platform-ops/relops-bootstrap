"""Tiny Secret Manager helper. Used for the SimpleMDM API token,
the allowlist file, and the kill switch flag."""

from __future__ import annotations

from functools import lru_cache

from google.cloud import secretmanager

from .config import get_settings


@lru_cache
def _client() -> secretmanager.SecretManagerServiceClient:
    return secretmanager.SecretManagerServiceClient()


def read_secret(secret_id: str, version: str = "latest") -> bytes:
    project = get_settings().gcp_project_id
    name = f"projects/{project}/secrets/{secret_id}/versions/{version}"
    response = _client().access_secret_version(request={"name": name})
    return response.payload.data
