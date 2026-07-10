"""
hangar-screen-agent — pushes on-demand worker VNC frames to Hangar.

Companion to reprovision-runner. Hangar (Cloud Run) can't VNC into MDC1, so when someone
opens a worker's live view Hangar just marks that host "watched". This agent lives on the VPN,
polls those requests over the same **mTLS** transport (outbound only), grabs ONE **passive**
VNC frame per watched host (admin account, ~3s hold, *no input injected* — safe on a worker
mid-test), and pushes the JPEG back. It only ever touches hosts that are actively being watched.

Config (env), shared with the runner:
  HANGAR_API_URL, RUNNER_CLIENT_CERT + RUNNER_CLIENT_KEY (mTLS) or REPROVISION_RUNNER_TOKEN
  REPROVISION_SSH_ADMIN_USER (default 'admin') + REPROVISION_SSH_ADMIN_PASSWORD  (VNC auth)
  SCREEN_POLL_SECONDS (6) · SCREEN_HOLD_SECONDS (3) · SCREEN_MAX_WIDTH (1280) · SCREEN_JPEG_QUALITY (55)

Run:  hangar-screen-agent
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
import time

import httpx

from .hostnames import validate_fqdn
from .runner import Config

VNC_PORT = 5900
POLL = int(os.environ.get("SCREEN_POLL_SECONDS", "6"))
HOLD_SECONDS = float(os.environ.get("SCREEN_HOLD_SECONDS", "3"))
MAX_WIDTH = int(os.environ.get("SCREEN_MAX_WIDTH", "1280"))
JPEG_QUALITY = int(os.environ.get("SCREEN_JPEG_QUALITY", "55"))
ADMIN_USER = os.environ.get("REPROVISION_SSH_ADMIN_USER", "admin")


def _admin_password() -> str:
    pw = os.environ.get("REPROVISION_SSH_ADMIN_PASSWORD", "")
    if not pw:
        sys.exit("set REPROVISION_SSH_ADMIN_PASSWORD (VNC auth for the admin account)")
    return pw


async def _grab(fqdn: str, password: str) -> bytes:
    """Connect, hold the session ~3s so a headless mini composites, grab one frame → JPEG.

    The hold (not any input) is what makes the framebuffer real; we never move the mouse or
    send keys, so a running test is undisturbed."""
    import asyncvnc  # imported lazily so the module loads even where asyncvnc isn't installed
    from PIL import Image

    async with asyncvnc.connect(fqdn, port=VNC_PORT, username=ADMIN_USER, password=password) as client:
        await asyncio.sleep(HOLD_SECONDS)
        pixels = await client.screenshot()  # numpy (H, W, 4) RGBA
    img = Image.fromarray(pixels[..., :3])
    if img.width > MAX_WIDTH:
        img.thumbnail((MAX_WIDTH, MAX_WIDTH * 10))  # cap width, keep aspect
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


def _pending(client: httpx.Client, cfg: Config) -> list[str]:
    r = client.get(f"{cfg.api}/screen/agent/requests", headers=cfg.headers, timeout=30)
    r.raise_for_status()
    return r.json().get("hosts", [])


def _push(client: httpx.Client, cfg: Config, fqdn: str, jpeg: bytes) -> None:
    # /screen/agent/* is routed to the mTLS backend; host is a query param so the path stays fixed.
    r = client.post(
        f"{cfg.api}/screen/agent/frame",
        params={"host": fqdn},
        headers={**cfg.headers, "Content-Type": "image/jpeg"},
        content=jpeg,
        timeout=30,
    )
    r.raise_for_status()


def main() -> None:
    cfg = Config()
    password = _admin_password()
    auth = "mTLS cert" if cfg.client_cert else "shared token"
    print(f"hangar-screen-agent [{cfg.runner_id}] → {cfg.api} (auth: {auth}, poll {POLL}s)")
    with httpx.Client(**cfg.client_kwargs) as client:
        while True:
            try:
                hosts = _pending(client, cfg)
            except httpx.HTTPError as e:
                print(f"poll failed: {e}")
                time.sleep(POLL)
                continue
            for fqdn in hosts:
                try:
                    # The admin VNC password is handed to whatever host we connect to,
                    # so never connect to a host the control plane names without
                    # confirming it's an in-fleet worker on the fixed MDC1 domain.
                    fqdn = validate_fqdn(fqdn)
                    jpeg = asyncio.run(_grab(fqdn, password))
                    _push(client, cfg, fqdn, jpeg)
                    print(f"pushed {fqdn} ({len(jpeg)} bytes)")
                except Exception as e:  # noqa: BLE001 — one bad host must not stop the loop
                    print(f"capture {fqdn} failed: {e}")
            time.sleep(POLL)


if __name__ == "__main__":
    main()
