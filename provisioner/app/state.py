"""Per-device 'last script-job fired at' state, backed by a tiny GCS bucket.

GCS chosen over Firestore because: (1) we have ~80 devices, GCS is trivially
sufficient for that scale, (2) one less GCP service to provision, (3) state
is dumb-simple key→timestamp so a flat bucket works.

Each device hostname becomes one tiny object: <prefix>/<hostname>.txt
containing an ISO-8601 timestamp.
"""

from __future__ import annotations

from datetime import datetime, timezone

from google.cloud import storage


class RateLimitState:
    def __init__(self, bucket: str, prefix: str = "rate-limit/") -> None:
        self._client = storage.Client()
        self._bucket = self._client.bucket(bucket)
        self._prefix = prefix.rstrip("/") + "/"

    def last_fired(self, hostname: str) -> datetime | None:
        blob = self._bucket.blob(self._prefix + hostname + ".txt")
        if not blob.exists():
            return None
        raw = blob.download_as_text().strip()
        try:
            return datetime.fromisoformat(raw).astimezone(timezone.utc)
        except ValueError:
            return None

    def record_fire(self, hostname: str) -> None:
        blob = self._bucket.blob(self._prefix + hostname + ".txt")
        blob.upload_from_string(datetime.now(timezone.utc).isoformat())
