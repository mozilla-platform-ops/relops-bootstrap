from functools import lru_cache

from google.cloud import secretmanager

from .config import get_settings


@lru_cache
def _client() -> secretmanager.SecretManagerServiceClient:
    return secretmanager.SecretManagerServiceClient()


def read_secret(secret_id: str, version: str = "latest") -> bytes:
    settings = get_settings()
    name = f"projects/{settings.gcp_project_id}/secrets/{secret_id}/versions/{version}"
    resp = _client().access_secret_version(request={"name": name})
    return resp.payload.data
