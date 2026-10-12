"""
On-network macOS SBOM collector.

ronin_puppet's macos_sbom module writes two files on every macOS worker:

  /var/sbom/fingerprint.json   ~500 bytes, rewritten each run: role, OS, and the
                               sbom_sha256 of the host's software inventory
  /var/sbom/sbom.cdx.json      ~250 KB CycloneDX 1.6, rewritten only on drift

The hash covers component identity only, so every host built from the same role
lands on the same hash and the fleet collapses to a handful of distinct documents.
This agent leans on that: it reads every host's fingerprint (one small ssh round
trip each), then fetches a full document only for hashes Hangar has never seen,
and only from one host per hash. Steady state moves a few hundred bytes per host.

Cloud Run cannot reach MDC1, so this runs on-network for the same reason as the
tart health and screen agents, and reuses their ssh identity, mTLS push, and
URL handling. Like them it is a dumb collector: fingerprints are passed through
unchanged and every judgement (drift vs the role's modal hash, staleness) lives
in Hangar.

Usage:
    # production, as the LaunchDaemon runs it (RUNNER_CLIENT_CERT/KEY from the env)
    hangar-sbom-agent --loop --interval 3600

    # one sweep, printing the payload instead of pushing
    hangar-sbom-agent --hosts-file hosts.txt --dry-run
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .tart_health_agent import _api_base, _ssh, _ssl_context

FINGERPRINT = "/var/sbom/fingerprint.json"
DOCUMENT = "/var/sbom/sbom.cdx.json"

# Both files are written 0644 by puppet, so neither read needs sudo.
_NO_FP = "__NO_FINGERPRINT__"
FP_PROBE = f"if [ -f {FINGERPRINT} ]; then cat {FINGERPRINT}; else echo {_NO_FP}; fi"
DOC_PROBE = f"gzip -c {DOCUMENT} | base64"


def _headers(args: argparse.Namespace, json_body: bool = False) -> dict[str, str]:
    headers = {"Content-Type": "application/json"} if json_body else {}
    token = os.environ.get(args.token_env, "")
    if token and not args.client_cert:
        headers["X-Reprovision-Runner-Token"] = token
    return headers


def get_targets(args: argparse.Namespace) -> dict[str, Any]:
    req = urllib.request.Request(
        f"{_api_base(args.hangar_url)}/sbom/agent/targets",
        method="GET", headers=_headers(args),
    )
    ctx = _ssl_context(args.client_cert, args.client_key)
    with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
        return json.load(r)


def collect_fingerprint(host: str) -> dict[str, Any]:
    raw, why = _ssh(host, FP_PROBE, timeout=60)
    if not raw:
        time.sleep(3)
        raw, why2 = _ssh(host, FP_PROBE, timeout=60)
        why = why or why2
    if not raw:
        return {"hostname": host, "fingerprint": None, "agent_error": f"ssh failed twice: {why}"}
    if raw.strip() == _NO_FP:
        return {"hostname": host, "fingerprint": None, "agent_error": "no fingerprint (macos_sbom not applied?)"}
    try:
        fp = json.loads(raw)
    except ValueError as e:
        return {"hostname": host, "fingerprint": None, "agent_error": f"unparseable fingerprint: {e}"}
    if not isinstance(fp, dict):
        return {"hostname": host, "fingerprint": None, "agent_error": "fingerprint is not a JSON object"}
    return {"hostname": host, "fingerprint": fp, "agent_error": None}


def decode_document(b64: str) -> dict[str, Any]:
    """Undo `gzip -c | base64` and parse. Raises on anything that isn't a JSON object."""
    doc = json.loads(gzip.decompress(base64.b64decode("".join(b64.split()))))
    if not isinstance(doc, dict):
        raise ValueError("document is not a JSON object")
    return doc


def fetch_document(host: str) -> tuple[dict[str, Any] | None, str]:
    raw, why = _ssh(host, DOC_PROBE, timeout=120)
    if not raw:
        return None, why
    try:
        return decode_document(raw), ""
    except Exception as e:  # noqa: BLE001 — a bad document is skipped, not fatal
        return None, f"bad document: {e}"


def plan_fetches(results: list[dict[str, Any]], known: set[str], limit: int) -> dict[str, str]:
    """sha256 -> one host to fetch it from, for hashes Hangar lacks. At most `limit`."""
    plan: dict[str, str] = {}
    for r in results:
        sha = (r.get("fingerprint") or {}).get("sbom_sha256")
        if not sha or sha in known or sha in plan:
            continue
        if len(plan) >= limit:
            break
        plan[sha] = r["hostname"]
    return plan


def sweep(hosts: list[str], known: set[str], args: argparse.Namespace) -> dict[str, Any]:
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        results = list(ex.map(collect_fingerprint, hosts))

    documents: dict[str, Any] = {}
    for sha, host in plan_fetches(results, known, args.max_documents).items():
        doc, why = fetch_document(host)
        if doc is None:
            print(f"{host}: could not fetch {sha[:12]}: {why}", file=sys.stderr)
            continue
        documents[sha] = doc

    payload = {"hosts": results, "documents": documents}
    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return payload

    req = urllib.request.Request(
        f"{_api_base(args.hangar_url)}/sbom/agent/push",
        data=json.dumps(payload).encode(), method="POST", headers=_headers(args, json_body=True),
    )
    ctx = _ssl_context(args.client_cert, args.client_key)
    with urllib.request.urlopen(req, timeout=120, context=ctx) as r:
        print(r.read().decode(), file=sys.stderr)
    return payload


def resolve(args: argparse.Namespace) -> tuple[list[str], set[str]]:
    """Hosts to sweep and the hashes Hangar already holds."""
    try:
        targets = get_targets(args)
    except Exception as e:  # noqa: BLE001
        # Without the targets call a dry run can still be useful against a hosts file;
        # anything else has no host list and nowhere to push.
        if not (args.dry_run and args.hosts_file):
            raise
        print(f"targets unavailable ({e}); treating known hashes as empty", file=sys.stderr)
        targets = {}
    if args.hosts_file:
        hosts = [
            ln.strip() for ln in open(args.hosts_file)
            if ln.strip() and not ln.strip().startswith("#")
        ]
    else:
        hosts = list(targets.get("hosts") or [])
    return hosts, set(targets.get("known_sha256") or [])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Collect macOS SBOM fingerprints and push them to Hangar.")
    ap.add_argument("--hosts-file", help="override Hangar's host list; one FQDN per line, # comments ignored")
    # Only /api/sbom/agent/* is routed to the non-IAP backend on the runner hostname.
    ap.add_argument("--hangar-url", default=os.environ.get("HANGAR_API_URL", "https://hangar-runner.relops.mozilla.com"))
    ap.add_argument("--client-cert", default=os.environ.get("RUNNER_CLIENT_CERT", ""),
                    help="mTLS client cert (required in production; same pair reprovision-runner uses)")
    ap.add_argument("--client-key", default=os.environ.get("RUNNER_CLIENT_KEY", ""))
    ap.add_argument("--token-env", default="HANGAR_RUNNER_TOKEN",
                    help="env var holding the runner token (local dev only; production requires the cert)")
    ap.add_argument("--concurrency", type=int, default=8,
                    help="fingerprint reads are tiny; document fetches stay sequential")
    ap.add_argument("--max-documents", type=int, default=10,
                    help="cap on full documents fetched per sweep; the rest come next sweep")
    ap.add_argument("--dry-run", action="store_true", help="print the payload instead of pushing")
    ap.add_argument("--loop", action="store_true",
                    help="run forever, sweeping every --interval seconds (how the LaunchDaemon runs it)")
    ap.add_argument("--interval", type=int, default=int(os.environ.get("SBOM_INTERVAL_SECONDS", "3600")),
                    help="seconds between sweeps in --loop mode; puppet regenerates at most daily")
    args = ap.parse_args(argv)

    auth = "mTLS cert" if args.client_cert else "shared token"
    if not args.loop:
        hosts, known = resolve(args)
        if not hosts:
            print("no hosts", file=sys.stderr)
            return 2
        print(f"collecting {len(hosts)} hosts ({len(known)} hashes already known)", file=sys.stderr)
        sweep(hosts, known, args)
        return 0

    print(f"hangar-sbom-agent → {args.hangar_url} (auth: {auth}, every {args.interval}s)", file=sys.stderr)
    while True:
        started = time.time()
        try:
            # Re-resolved every sweep so new hosts and newly stored hashes are picked up.
            hosts, known = resolve(args)
            p = sweep(hosts, known, args)
            print(f"swept {len(hosts)} hosts, pushed {len(p['documents'])} new documents "
                  f"in {time.time() - started:.0f}s", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 — a failed sweep must never kill the daemon
            print(f"sweep failed: {e}", file=sys.stderr)
        time.sleep(max(5, args.interval - (time.time() - started)))


if __name__ == "__main__":
    raise SystemExit(main())
