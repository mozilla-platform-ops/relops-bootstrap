"""Tests for workflow.resolve() — pool resolution + the `registered` flag that gates
whether the reprovision flow quarantines/drains (skip for hosts not in TC)."""

from __future__ import annotations

from unittest.mock import patch

from orchestrator import workflow


def test_resolve_registered_true_uses_found_pool():
    """Worker registered in the -staging pool → registered=True, that pool is used."""
    with patch("orchestrator.workflow.taskcluster.find_registered_pool",
               return_value="releng-hardware/gecko-t-osx-1500-m4-staging"), \
         patch("orchestrator.workflow.simplemdm.find_device_by_name", return_value={"id": 123}):
        ctx = workflow.resolve("macmini-m4-111")
    assert ctx.registered is True
    assert ctx.worker_pool_id == "releng-hardware/gecko-t-osx-1500-m4-staging"


def test_resolve_registered_false_falls_back_to_prod():
    """Worker registered in NO pool → registered=False, pool falls back to prod
    (so pool-scoped calls have a value, but reprovision() will skip quarantine/drain)."""
    with patch("orchestrator.workflow.taskcluster.find_registered_pool", return_value=None), \
         patch("orchestrator.workflow.simplemdm.find_device_by_name", return_value=None):
        ctx = workflow.resolve("macmini-m4-111")
    assert ctx.registered is False
    assert ctx.worker_pool_id == "releng-hardware/gecko-t-osx-1500-m4"


def test_hostcontext_defaults_registered_true():
    """Existing call sites that build a HostContext directly stay registered by default."""
    ctx = workflow.HostContext(hostname="macmini-m4-81", fqdn="x", role="r",
                               worker_pool_id="releng-hardware/gecko-t-osx-1500-m4")
    assert ctx.registered is True
