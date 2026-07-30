"""hangar-tart-health-agent — the on-network collector for tart VM slot health.

Focused on the parsers, because every one of them has a real failure behind it:
macOS `ps` has no `etimes` (so uptimes silently came back null), openssl pads
single-digit days, and `_ssh` used to swallow stderr, which made two healthy hosts
look unreachable.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from orchestrator import tart_health_agent as agent


# ---- _etime_s: BSD `ps -o etime=` ----------------------------------------------

def test_etime_mm_ss():
    assert agent._etime_s("04:11") == 251


def test_etime_hh_mm_ss():
    assert agent._etime_s("20:54:56") == 75296


def test_etime_with_days():
    # The form that matters: a `tart run` up for weeks. 11d 20h 54m 56s.
    assert agent._etime_s("11-20:54:56") == 1025696


def test_etime_rejects_garbage():
    assert agent._etime_s("") is None
    assert agent._etime_s("not-a-time") is None
    assert agent._etime_s("x-01:02:03") is None


# ---- _openssl_date -------------------------------------------------------------

def test_openssl_date_space_padded_day():
    # `openssl x509 -enddate` pads single-digit days, which is the common case for
    # these short-lived certs.
    d = agent._openssl_date("Aug  3 17:21:45 2026 GMT")
    assert d is not None
    assert (d.year, d.month, d.day, d.hour) == (2026, 8, 3, 17)


def test_openssl_date_two_digit_day():
    d = agent._openssl_date("Jul 27 17:20:45 2026 GMT")
    assert d is not None
    assert (d.month, d.day) == (7, 27)


def test_openssl_date_none_and_unparseable():
    assert agent._openssl_date(None) is None
    assert agent._openssl_date("") is None
    assert agent._openssl_date("whenever") is None


# ---- _kv ------------------------------------------------------------------------

def test_kv_parses_and_drops_empty_values():
    # The probe emits a key with an empty value whenever the host cannot answer;
    # those must be dropped rather than stored as "".
    out = agent._kv("checkout_sha=7b0c9dd1\nslot1_vm=\nslot1_state=running\nnoise\n")
    assert out == {"checkout_sha": "7b0c9dd1", "slot1_state": "running"}


def test_kv_keeps_equals_in_value():
    assert agent._kv("problem=a=b")["problem"] == "a=b"


# ---- _ssh: the reason must survive ---------------------------------------------

def _completed(stdout: str = "", stderr: str = "", rc: int = 0):
    return subprocess.CompletedProcess(args=["ssh"], returncode=rc, stdout=stdout, stderr=stderr)


def test_ssh_returns_stdout_with_no_reason():
    with patch("subprocess.run", return_value=_completed(stdout="k=v\n")):
        out, why = agent._ssh("host", "probe")
    assert out == "k=v\n"
    assert why == ""


def test_ssh_surfaces_stderr_when_there_is_no_output():
    # Regression: this used to collapse to a bare "unreachable", which made the
    # collector's own failures indistinguishable from a dead host.
    err = "kex_exchange_identification: Connection closed by remote host\n"
    with patch("subprocess.run", return_value=_completed(stderr=err, rc=255)):
        out, why = agent._ssh("host", "probe")
    assert out == ""
    assert "Connection closed by remote host" in why


def test_ssh_reports_exit_code_when_stderr_is_empty_too():
    with patch("subprocess.run", return_value=_completed(rc=1)):
        _, why = agent._ssh("host", "probe")
    assert "exit 1" in why


def test_ssh_reports_timeout_distinctly():
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="ssh", timeout=120)):
        out, why = agent._ssh("host", "probe", timeout=120)
    assert out == ""
    assert "timed out" in why


# ---- collect_host: a failed host still yields both slots -----------------------

def test_collect_host_retries_then_reports_the_real_reason():
    with patch.object(agent, "_ssh", return_value=("", "Permission denied (publickey)")) as m, \
         patch("time.sleep"):
        slots = agent.collect_host("macmini-m4-239.test.releng.mdc1.mozilla.com", {}, False)
    assert m.call_count == 2  # one retry, since a single ssh failure is usually transient
    assert [s["slot"] for s in slots] == [1, 2]
    for s in slots:
        assert s["hostname"] == "macmini-m4-239"
        assert "Permission denied" in s["agent_error"]


def test_collect_host_recovers_on_the_retry():
    # A transient first failure must not be reported: observed on m4-237 and m4-239,
    # both up 13 days and answering fine seconds later.
    responses = [("", "connection reset"), ("slot1_vm=sequoia-tester-1\nslot1_state=running\n", "")]
    with patch.object(agent, "_ssh", side_effect=responses), patch("time.sleep"):
        slots = agent.collect_host("macmini-m4-239.test", {}, False)
    assert all("agent_error" not in s for s in slots)
    assert slots[0]["vm_name"] == "sequoia-tester-1"


# ---- auth: prod is cert-only ----------------------------------------------------

def test_ssl_context_none_without_both_halves():
    assert agent._ssl_context("", "") is None
    assert agent._ssl_context("/only/cert", "") is None
    assert agent._ssl_context("", "/only/key") is None


def _args(**kw):
    import argparse
    base = dict(hangar_url="https://h", client_cert="", client_key="", token_env="TOK",
                concurrency=1, no_guests=True, dry_run=False)
    base.update(kw)
    return argparse.Namespace(**base)


def _capture_push(monkeypatch, args, token_value=""):
    """Run one sweep with the network stubbed; return the urllib Request it built."""
    sent = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"accepted": 2}'

    def fake_urlopen(req, timeout=None, context=None):
        sent["req"] = req
        sent["context"] = context
        return _Resp()

    monkeypatch.setenv("TOK", token_value)
    # Stub the TLS setup: load_cert_chain really opens the files, and these tests are
    # about which headers/URL get sent, not about certificate loading.
    monkeypatch.setattr(agent, "_ssl_context", lambda c, k: object() if (c and k) else None)
    monkeypatch.setattr(agent, "tc_workers", lambda: {})
    monkeypatch.setattr(agent, "collect_host", lambda h, tc, g: [{"hostname": h, "slot": 1}])
    monkeypatch.setattr(agent.urllib.request, "urlopen", fake_urlopen)
    agent.sweep(["host-a"], args)
    return sent


def test_cert_authed_agent_sends_no_token_header(monkeypatch):
    # reprovision-runner does the same: a cert-authed client omits the token entirely.
    sent = _capture_push(monkeypatch, _args(client_cert="/c", client_key="/k"), token_value="secret")
    assert "X-Reprovision-runner-token" not in sent["req"].headers
    assert "X-Reprovision-Runner-Token" not in sent["req"].headers


def test_token_used_when_no_cert(monkeypatch):
    sent = _capture_push(monkeypatch, _args(), token_value="secret")
    assert sent["req"].get_header("X-reprovision-runner-token") == "secret"


def test_push_targets_the_agent_endpoint(monkeypatch):
    sent = _capture_push(monkeypatch, _args())
    assert sent["req"].full_url == "https://h/api/tart-health/agent/push"
    assert sent["req"].get_method() == "POST"


def test_dry_run_does_not_push(monkeypatch, capsys):
    monkeypatch.setattr(agent, "tc_workers", lambda: {})
    monkeypatch.setattr(agent, "collect_host", lambda h, tc, g: [{"hostname": h, "slot": 1}])

    def boom(*a, **k):
        raise AssertionError("dry run must not open a connection")

    monkeypatch.setattr(agent.urllib.request, "urlopen", boom)
    assert agent.sweep(["host-a"], _args(dry_run=True)) == 1
    assert "host-a" in capsys.readouterr().out
