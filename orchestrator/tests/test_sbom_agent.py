"""hangar-sbom-agent — the on-network collector for macOS SBOMs.

The point of the design is that the fleet holds a handful of distinct documents,
not one per host, so most of these pin down that a document is fetched at most
once per hash and never for a hash Hangar already has.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import io
import json
from unittest.mock import patch

from orchestrator import sbom_agent as agent


def _args(**kw) -> argparse.Namespace:
    base = dict(
        hosts_file=None, hangar_url="http://localhost:8000", client_cert="", client_key="",
        token_env="HANGAR_RUNNER_TOKEN", concurrency=2, max_documents=10, dry_run=True,
        loop=False, interval=3600,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def _fp(sha: str, host: str = "h") -> str:
    return json.dumps({"schema_version": 1, "hostname": host, "puppet_role": "gecko_t_osx_1500_m4",
                       "sbom_sha256": sha, "component_count": 3})


def _b64doc(doc: dict) -> str:
    raw = base64.b64encode(gzip.compress(json.dumps(doc).encode())).decode()
    # `base64` on macOS wraps at 76 columns by default only with -b; be robust either way.
    return "\n".join(raw[i:i + 76] for i in range(0, len(raw), 76)) + "\n"


class FakeSSH:
    """Maps host -> fingerprint text; serves documents and records fetches."""

    def __init__(self, fps: dict[str, str], docs: dict[str, dict] | None = None):
        self.fps = fps
        self.docs = docs or {}
        self.fetched: list[str] = []

    def __call__(self, host, script, timeout=120):
        if script == agent.DOC_PROBE:
            self.fetched.append(host)
            return _b64doc(self.docs.get(host, {"bomFormat": "CycloneDX", "host": host})), ""
        v = self.fps.get(host)
        if v is None:
            return "", "ssh: connect to host port 22: Operation timed out"
        return v, ""


# ---- decode ---------------------------------------------------------------------

def test_decode_document_roundtrip():
    doc = {"bomFormat": "CycloneDX", "specVersion": "1.6", "components": [{"name": "x"}]}
    assert agent.decode_document(_b64doc(doc)) == doc


def test_decode_document_rejects_non_object():
    raw = base64.b64encode(gzip.compress(b"[1,2]")).decode()
    try:
        agent.decode_document(raw)
    except ValueError:
        return
    raise AssertionError("expected ValueError")


# ---- fingerprints ---------------------------------------------------------------

def test_missing_fingerprint_is_reported():
    with patch.object(agent, "_ssh", return_value=(agent._NO_FP + "\n", "")):
        r = agent.collect_fingerprint("m4-1.example")
    assert r == {"hostname": "m4-1.example", "fingerprint": None,
                 "agent_error": "no fingerprint (macos_sbom not applied?)"}


def test_ssh_failure_retries_once_and_keeps_reason():
    calls = []

    def fake(host, script, timeout=120):
        calls.append(host)
        return "", "Permission denied (publickey)"

    with patch.object(agent, "_ssh", side_effect=fake), patch.object(agent.time, "sleep"):
        r = agent.collect_fingerprint("m4-1.example")
    assert len(calls) == 2
    assert r["fingerprint"] is None
    assert "Permission denied" in r["agent_error"]


def test_fingerprint_passed_through_unchanged():
    raw = _fp("a" * 64, "m4-1")
    with patch.object(agent, "_ssh", return_value=(raw, "")):
        r = agent.collect_fingerprint("m4-1.example")
    assert r["fingerprint"] == json.loads(raw)
    assert r["agent_error"] is None


# ---- planning / sweep -----------------------------------------------------------

def test_fetches_deduped_by_sha():
    ssh = FakeSSH({"a": _fp("s1"), "b": _fp("s1"), "c": _fp("s2")})
    with patch.object(agent, "_ssh", side_effect=ssh):
        p = agent.sweep(["a", "b", "c"], set(), _args())
    assert sorted(ssh.fetched) == ["a", "c"]
    assert set(p["documents"]) == {"s1", "s2"}


def test_known_sha_not_fetched():
    ssh = FakeSSH({"a": _fp("s1"), "b": _fp("s2")})
    with patch.object(agent, "_ssh", side_effect=ssh):
        p = agent.sweep(["a", "b"], {"s1"}, _args())
    assert ssh.fetched == ["b"]
    assert set(p["documents"]) == {"s2"}


def test_max_documents_cap():
    fps = {f"h{i}": _fp(f"s{i}") for i in range(5)}
    ssh = FakeSSH(fps)
    with patch.object(agent, "_ssh", side_effect=ssh):
        p = agent.sweep(list(fps), set(), _args(max_documents=2))
    assert len(ssh.fetched) == 2
    assert len(p["documents"]) == 2
    # Fingerprints for every host still go up; only document fetches are capped.
    assert len(p["hosts"]) == 5


def test_failed_hosts_never_planned():
    results = [{"hostname": "x", "fingerprint": None, "agent_error": "boom"}]
    assert agent.plan_fetches(results, set(), 10) == {}


def test_push_payload_shape_and_runner_token():
    ssh = FakeSSH({"a.example": _fp("s1", "a"), "b.example": None})
    sent = {}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None, context=None):
        sent["url"] = req.full_url
        sent["method"] = req.get_method()
        sent["headers"] = dict(req.header_items())
        sent["body"] = json.loads(req.data)
        return Resp(b'{"accepted": 2}')

    with patch.object(agent, "_ssh", side_effect=ssh), patch.object(agent.time, "sleep"), \
            patch.object(agent.urllib.request, "urlopen", side_effect=fake_urlopen), \
            patch.dict("os.environ", {"HANGAR_RUNNER_TOKEN": "tok"}):
        agent.sweep(["a.example", "b.example"], set(), _args(dry_run=False, hangar_url="http://h/api"))

    assert sent["url"] == "http://h/api/sbom/agent/push"
    assert sent["method"] == "POST"
    assert sent["headers"]["X-reprovision-runner-token"] == "tok"
    body = sent["body"]
    assert set(body) == {"hosts", "documents"}
    by_host = {h["hostname"]: h for h in body["hosts"]}
    assert by_host["a.example"]["fingerprint"]["sbom_sha256"] == "s1"
    assert by_host["a.example"]["agent_error"] is None
    assert by_host["b.example"]["fingerprint"] is None
    assert by_host["b.example"]["agent_error"].startswith("ssh failed twice")
    assert set(body["documents"]) == {"s1"}


def test_resolve_hosts_file_overrides_targets_and_tolerates_dry_run_failure(tmp_path):
    hf = tmp_path / "hosts.txt"
    hf.write_text("# comment\nm4-1.example\n\nm4-2.example\n")
    with patch.object(agent, "get_targets", side_effect=OSError("no route")):
        hosts, known = agent.resolve(_args(hosts_file=str(hf), dry_run=True))
    assert hosts == ["m4-1.example", "m4-2.example"]
    assert known == set()


def test_resolve_uses_targets():
    with patch.object(agent, "get_targets", return_value={"hosts": ["a"], "known_sha256": ["s1"]}):
        hosts, known = agent.resolve(_args())
    assert hosts == ["a"]
    assert known == {"s1"}


def test_plan_fetches_prefers_newest_generator_then_most_hosts():
    def r(host, sha, gen):
        return {"hostname": host, "fingerprint": {"sbom_sha256": sha, "generator_version": gen}}

    results = [
        r("h1", "old", "2.1.0"),
        r("h2", "new-one", "2.2.0"),
        r("h3", "new-many", "2.2.0"),
        r("h4", "new-many", "2.2.0"),
        r("h5", "known", "2.2.0"),
    ]
    plan = agent.plan_fetches(results, known={"known"}, limit=2)
    assert list(plan) == ["new-many", "new-one"]
    assert plan["new-many"] == "h3"
