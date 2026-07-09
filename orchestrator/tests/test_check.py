"""`reprovision check` — read-only preflight that confirms every credential resolves."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from orchestrator import workflow
from orchestrator.errors import ReprovisionError


def test_check_passes_when_all_resolve():
    with patch("orchestrator.workflow.ssh_admin_password", return_value="pw"), \
         patch("orchestrator.workflow.ssh_admin_key", return_value="key"), \
         patch("orchestrator.workflow.simplemdm_api_key", return_value="mdm"), \
         patch("orchestrator.workflow.tc_credentials", return_value=("cid", "tok")):
        workflow.check()  # no raise


def test_check_raises_when_a_required_secret_fails():
    def boom():
        raise ReprovisionError("not authorized — ask a RelOps admin to add you to the 'RelOps' vault")

    with patch("orchestrator.workflow.ssh_admin_password", side_effect=boom), \
         patch("orchestrator.workflow.ssh_admin_key", return_value="key"), \
         patch("orchestrator.workflow.simplemdm_api_key", return_value="mdm"), \
         patch("orchestrator.workflow.tc_credentials", return_value=("cid", "tok")):
        with pytest.raises(ReprovisionError, match="didn't resolve"):
            workflow.check()


def test_check_tolerates_missing_tc_creds():
    # TC creds are optional (only needed for quarantine/drain) — empty must not fail the check.
    with patch("orchestrator.workflow.ssh_admin_password", return_value="pw"), \
         patch("orchestrator.workflow.ssh_admin_key", return_value="key"), \
         patch("orchestrator.workflow.simplemdm_api_key", return_value="mdm"), \
         patch("orchestrator.workflow.tc_credentials", return_value=("", "")):
        workflow.check()  # no raise
