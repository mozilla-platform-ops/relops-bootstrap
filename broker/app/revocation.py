"""
CRL polling for cert revocation checks.

Background task fetches the configured CRL URL every CRL_REFRESH_SECONDS,
parses it, and updates an in-memory set of revoked serials. Auth path consults
that set on every request via is_revoked().

When CRL_URL is empty (default until step-ca is bootstrapped), the task does
nothing and is_revoked always returns False — fail-open while we're standing
up the PKI. Once the CRL is live we flip the URL and revocation enforcement
turns on with no other code changes.
"""

from __future__ import annotations

import asyncio
import threading
from contextlib import suppress

import httpx
import structlog
from cryptography import x509

log = structlog.get_logger("vault-broker.revocation")


class RevocationCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._revoked: set[int] = set()
        self._last_update_unix: float = 0.0

    def update(self, serials: set[int]) -> None:
        with self._lock:
            self._revoked = serials
            import time

            self._last_update_unix = time.time()

    def is_revoked(self, serial: int) -> bool:
        with self._lock:
            return serial in self._revoked

    def size(self) -> int:
        with self._lock:
            return len(self._revoked)

    def last_update_unix(self) -> float:
        with self._lock:
            return self._last_update_unix


async def _fetch_and_parse(url: str, timeout_seconds: int) -> set[int]:
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        resp = await client.get(url)
        resp.raise_for_status()
    body = resp.content

    # Try DER first (step-ca defaults to DER), fall back to PEM.
    try:
        crl = x509.load_der_x509_crl(body)
    except ValueError:
        crl = x509.load_pem_x509_crl(body)

    return {entry.serial_number for entry in crl}


async def _refresh_loop(cache: RevocationCache, url: str, refresh_seconds: int, timeout_seconds: int) -> None:
    while True:
        try:
            serials = await _fetch_and_parse(url, timeout_seconds)
            cache.update(serials)
            log.info("crl.refreshed", revoked_count=len(serials))
        except Exception as e:
            log.warning("crl.refresh_failed", error=str(e))
        # Even on failure: wait a full interval, then retry. Don't tight-loop on a broken CRL endpoint.
        await asyncio.sleep(refresh_seconds)


def start_background_refresh(
    cache: RevocationCache,
    url: str,
    refresh_seconds: int,
    timeout_seconds: int,
) -> asyncio.Task | None:
    """Returns the running Task so the lifespan handler can cancel it on shutdown."""
    if not url:
        log.info("crl.disabled", reason="no CRL_URL configured")
        return None

    task = asyncio.create_task(
        _refresh_loop(cache, url, refresh_seconds, timeout_seconds),
        name="crl-refresh",
    )
    log.info("crl.started", url=url, refresh_seconds=refresh_seconds)
    return task


async def stop_background_refresh(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
