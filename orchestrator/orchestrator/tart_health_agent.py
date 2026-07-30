"""
On-network tart slot health collector.

Cloud Run cannot reach MDC1, so Hangar cannot SSH a tart host itself — the same
constraint that produced the reprovision runner and the screen agent. This runs
on-network, SSHes each tart host, collects per-slot facts, and pushes them to
Hangar's /api/tart-health/agent/push. It is deliberately a dumb collector: all
severity thresholds live server-side so they can be tuned without redeploying
anything on-network.

Every fact gathered here is one that was invisible during the 2026-07-27..29
incident, where five of 26 slots in gecko-t-osx-1500-m-vms were out of production
and nothing noticed:

  guest uptime vs `tart run` uptime  three slots rebooted every ~84s for weeks
                                     while `tart run` stayed up 11+ days, so the
                                     host looked healthy from every angle
  guest disk free                    the cause of that loop: generic-worker needs
                                     20 GiB and panics (exit 69) without it
  configured workerId vs MAC         a clone came up impersonating another host's
                                     live worker; quarantine cannot drain that
  guest clock skew                   a clone inherits the image RTC and can sit
                                     indefinitely failing TLS, never registering
  cert expiry AND owner              injection needs the cert readable by the tart
                                     user; step writes it 0600 root:wheel
  clone refspec                      a single-branch pin makes a host structurally
                                     unable to follow master

Usage:
    python -m orchestrator.tart_health_agent --hosts-file hosts.txt \\
        --hangar-url https://hangar.relops.mozilla.com --token-env HANGAR_RUNNER_TOKEN

    # local dev against a Hangar running in docker compose
    python -m orchestrator.tart_health_agent --hosts-file hosts.txt \\
        --hangar-url http://localhost:8000 --token-env HANGAR_RUNNER_TOKEN --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

TC_ROOT = "https://firefox-ci-tc.services.mozilla.com"
POOL = ("releng-hardware", "gecko-t-osx-1500-m-vms")
PUPPET_REPO = "/opt/puppet_environments/mozilla-platform-ops/ronin_puppet"
CERT = "/etc/step-cert/tart-client.crt"
KEY = "/etc/step-cert/tart-client.key"
SSH_OPTS = ["-o", "ConnectTimeout=10", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new"]

# Collected in ONE ssh round trip per host. Emits `key=value` lines; anything the
# host cannot answer is simply omitted rather than guessed at.
HOST_PROBE = r"""
set -u
T=/usr/local/bin/tart
D=%(repo)s
echo "checkout_sha=$(sudo git -C $D rev-parse --short HEAD 2>/dev/null)"
# A single-branch refspec means this host can never see master, however often it fetches.
fetch=$(sudo git -C $D config --get-all remote.origin.fetch 2>/dev/null | head -1)
case "$fetch" in
  '+refs/heads/*:refs/remotes/origin/*') echo "refspec_pinned=false" ;;
  '') ;;
  *) echo "refspec_pinned=true" ;;
esac
echo "inject_vault=$(sudo grep -cE '^  inject_vault: true' $D/data/roles/tart_worker.yaml 2>/dev/null)"
if sudo test -f %(cert)s; then
  echo "cert_expiry=$(sudo openssl x509 -in %(cert)s -noout -enddate 2>/dev/null | cut -d= -f2)"
  # notBefore too, so severity can be judged against this cert's own lifetime. A
  # 168h cert is always "expiring within 7 days"; only the remaining FRACTION says
  # whether the renew daemon has actually stopped doing its job.
  echo "cert_not_before=$(sudo openssl x509 -in %(cert)s -noout -startdate 2>/dev/null | cut -d= -f2)"
  # Owner matters as much as expiry: tart-run-vm.sh runs as the tart user and cannot
  # read a root-owned key, which silently disables injection.
  echo "cert_owner=$(sudo stat -f '%%Su' %(key)s 2>/dev/null)"
fi
echo "tart_user=$(sudo /usr/libexec/PlistBuddy -c 'Print :UserName' /Library/LaunchDaemons/com.mozilla.tartworker-1.plist 2>/dev/null)"
for i in 1 2; do
  vm=$($T list 2>/dev/null | awk -v n="$i" '$1=="local" && $2 ~ ("-" n "$") {print $2; exit}')
  [ -z "$vm" ] && continue
  echo "slot${i}_vm=$vm"
  echo "slot${i}_state=$($T list 2>/dev/null | awk -v v="$vm" '$1=="local" && $2==v {print $NF}')"
  mac=$(/usr/bin/python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("macAddress",""))' "$HOME/.tart/vms/$vm/config.json" 2>/dev/null)
  [ -n "$mac" ] && echo "slot${i}_worker=mac-$(printf '%%s' "$mac" | awk -F: '{print $4$5$6}' | tr 'A-F' 'a-f')"
  pid=$(pgrep -f "[t]art run --no-graphics $vm" | head -1)
  [ -n "$pid" ] && echo "slot${i}_etime=$(ps -o etime= -p "$pid" 2>/dev/null | tr -d ' ')"
  echo "slot${i}_vault=$(sudo test -s "$HOME/.tart-vault/$vm/vault.yaml" && echo 1 || echo 0)"
  ip=$($T ip "$vm" --wait 5 2>/dev/null | head -1)
  [ -n "$ip" ] && echo "slot${i}_ip=$ip"
done
"""



def _openssl_date(v: str | None) -> datetime | None:
    """Parse an `openssl x509 -dates` value, e.g. `Aug  3 17:21:45 2026 GMT`.

    Single-digit days are space-padded, which %d tolerates only when the separator
    collapses, so try both spacings.
    """
    if not v:
        return None
    for fmt in ("%b %d %H:%M:%S %Y %Z", "%b  %d %H:%M:%S %Y %Z"):
        try:
            return datetime.strptime(v.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _etime_s(v: str) -> int | None:
    """Parse BSD ps etime ([[dd-]hh:]mm:ss) to seconds. macOS has no `etimes`."""
    if not v:
        return None
    days = 0
    if "-" in v:
        d, _, v = v.partition("-")
        if not d.isdigit():
            return None
        days = int(d)
    parts = v.split(":")
    if not all(p.isdigit() for p in parts) or not 2 <= len(parts) <= 3:
        return None
    parts = [int(p) for p in parts]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, sec = parts
    return days * 86400 + h * 3600 + m * 60 + sec


def _ssh(host: str, script: str, timeout: int = 120) -> tuple[str, str]:
    """Run `script` on `host`. Returns (stdout, failure_reason).

    The reason is returned rather than dropped: a health checker that can only say
    "unreachable" makes its own failures indistinguishable from the host's, which
    sent me chasing two live hosts (m4-237, m4-239) that were fine all along.
    """
    try:
        r = subprocess.run(["ssh", *SSH_OPTS, host, script], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "", f"ssh timed out after {timeout}s"
    except OSError as e:
        return "", f"could not run ssh: {e}"
    if r.stdout.strip():
        return r.stdout, ""
    # No output: surface ssh's own complaint, which is the whole diagnosis.
    err = " ".join(r.stderr.split())[:200] or f"no output, ssh exit {r.returncode}"
    return "", err


def _kv(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            v = v.strip()
            if v:
                out[k.strip()] = v
    return out


# Guest facts need a password login: these images have no key auth and no tart guest
# agent. `expect` is used the same way the operator runbook does.
GUEST_PROBE = r"""
uptime | sed -n 's/.*up \([^,]*\),.*/uptime_raw=\1/p'
echo "guest_epoch=$(date -u +%s)"
echo "guest_disk_gib=$(df -g / | awk 'NR==2{print $4}')"
echo "cfg_worker=$(sudo grep -m1 -oE 'mac-[0-9a-f]+' /opt/worker/generic-worker.conf.yaml 2>/dev/null)"
echo "guest_up_s=$(( $(date -u +%s) - $(sysctl -n kern.boottime | sed -n 's/.*sec = \([0-9]*\).*/\1/p') ))"
"""


def _guest(host: str, ip: str) -> dict[str, str]:
    """Collect from inside the guest via the host, using expect for the password login."""
    exp = (
        "log_user 1\nset timeout 60\n"
        f'spawn ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR admin@{ip} '
        f'"{GUEST_PROBE.strip()}"\n'
        'expect { -re "(P|p)assword:" { send "admin\\r"; exp_continue } timeout { } eof { } }\n'
    )
    payload = "cat > /tmp/_gp.exp <<'XEOF'\n" + exp + "XEOF\n/usr/bin/expect /tmp/_gp.exp; rm -f /tmp/_gp.exp"
    return _kv(_ssh(host, payload, timeout=150)[0])


def tc_workers() -> dict[str, dict[str, Any]]:
    prov, wt = POOL
    url = f"{TC_ROOT}/api/queue/v1/provisioners/{prov}/worker-types/{wt}/workers"
    out: dict[str, dict[str, Any]] = {}
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            data = json.load(r)
    except Exception:
        return out
    for w in data.get("workers", []):
        out[w["workerId"]] = w
    return out


def task_state(task_id: str) -> str | None:
    try:
        with urllib.request.urlopen(f"{TC_ROOT}/api/queue/v1/task/{task_id}/status", timeout=20) as r:
            runs = json.load(r).get("status", {}).get("runs", [])
        return runs[-1].get("state") if runs else None
    except Exception:
        return None


def collect_host(host: str, tc: dict[str, dict[str, Any]], probe_guests: bool) -> list[dict[str, Any]]:
    short = host.split(".")[0]
    probe = HOST_PROBE % {"repo": PUPPET_REPO, "cert": CERT, "key": KEY}
    # Retry once, with a short pause. A single ssh failure is usually transient —
    # observed 2026-07-30, when m4-237 and then m4-239 each reported unreachable in a
    # sweep and answered fine seconds later, both up 13 days. Monitoring that cries
    # wolf on one dropped connection gets ignored, which defeats the point.
    raw, why = _ssh(host, probe)
    if not raw:
        time.sleep(3)
        raw, why2 = _ssh(host, probe)
        why = why or why2
    if not raw:
        # Report ssh's actual complaint, not a guess about the host being down.
        return [
            {"hostname": short, "slot": i, "agent_error": f"ssh failed twice: {why}"}
            for i in (1, 2)
        ]
    h = _kv(raw)

    cert_expiry = _openssl_date(h.get("cert_expiry"))
    cert_not_before = _openssl_date(h.get("cert_not_before"))

    tart_user = h.get("tart_user", "admin")
    slots: list[dict[str, Any]] = []
    for i in (1, 2):
        vm = h.get(f"slot{i}_vm")
        if not vm:
            slots.append({"hostname": short, "slot": i, "vm_state": "missing"})
            continue
        wid = h.get(f"slot{i}_worker")
        w = tc.get(wid or "", {})
        latest = (w.get("latestTask") or {}).get("taskId")
        s: dict[str, Any] = {
            "hostname": short,
            "slot": i,
            "vm_name": vm,
            "worker_id": wid,
            "vm_state": h.get(f"slot{i}_state"),
            "tart_run_uptime_s": _etime_s(h.get(f"slot{i}_etime", "")),
            "vault_present": h.get(f"slot{i}_vault") == "1",
            "registered": bool(w),
            "quarantined": bool(w.get("quarantineUntil")) if w else None,
            "last_task_state": task_state(latest) if latest else None,
            "inject_vault": h.get("inject_vault") == "1",
            "cert_expiry": cert_expiry.replace(tzinfo=None).isoformat() if cert_expiry else None,
            "cert_not_before": cert_not_before.replace(tzinfo=None).isoformat() if cert_not_before else None,
            "cert_owner_ok": (h.get("cert_owner") == tart_user) if h.get("cert_owner") else None,
            "checkout_sha": h.get("checkout_sha"),
            "refspec_pinned": h.get("refspec_pinned") == "true",
        }
        ip = h.get(f"slot{i}_ip")
        if probe_guests and ip:
            g = _guest(host, ip)
            if g.get("guest_up_s", "").lstrip("-").isdigit():
                s["guest_reachable"] = True
                s["guest_uptime_s"] = int(g["guest_up_s"])
            if g.get("guest_disk_gib", "").isdigit():
                s["guest_disk_free_gib"] = int(g["guest_disk_gib"])
            if g.get("cfg_worker"):
                s["configured_worker_id"] = g["cfg_worker"]
            if g.get("guest_epoch", "").isdigit():
                s["clock_skew_s"] = int(g["guest_epoch"]) - int(datetime.now(timezone.utc).timestamp())
            if not g:
                s["guest_reachable"] = False
        elif ip is None:
            s["guest_reachable"] = False
        slots.append(s)
    return slots


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Collect tart slot health and push it to Hangar.")
    ap.add_argument("--hosts-file", required=True, help="one FQDN per line; blank lines and # comments ignored")
    ap.add_argument("--hangar-url", default="https://hangar.relops.mozilla.com")
    ap.add_argument("--token-env", default="HANGAR_RUNNER_TOKEN",
                    help="env var holding the runner token (local dev; production uses the mTLS client cert)")
    ap.add_argument("--concurrency", type=int, default=4,
                    help="MDC1 bandwidth is the constraint; keep this low")
    ap.add_argument("--no-guests", action="store_true",
                    help="skip in-guest probes (much faster; loses disk/clock/identity checks)")
    ap.add_argument("--dry-run", action="store_true", help="print the payload instead of pushing")
    args = ap.parse_args(argv)

    hosts = [
        ln.strip() for ln in open(args.hosts_file)
        if ln.strip() and not ln.strip().startswith("#")
    ]
    if not hosts:
        print("no hosts", file=sys.stderr)
        return 2

    tc = tc_workers()
    print(f"collecting {len(hosts)} hosts (guest probes: {not args.no_guests})", file=sys.stderr)
    slots: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        for res in ex.map(lambda hh: collect_host(hh, tc, not args.no_guests), hosts):
            slots.extend(res)

    payload = {"slots": slots}
    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return 0

    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{args.hangar_url.rstrip('/')}/api/tart-health/agent/push",
        data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "X-Reprovision-Runner-Token": os.environ.get(args.token_env, "")},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        print(r.read().decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
