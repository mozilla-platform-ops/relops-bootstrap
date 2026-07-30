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
