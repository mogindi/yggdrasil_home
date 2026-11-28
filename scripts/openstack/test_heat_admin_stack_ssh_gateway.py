"""Tests for the restricted Heat cleanup SSH gateway."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).with_name("heat-admin-stack-ssh-gateway.py")
SPEC = importlib.util.spec_from_file_location("heat_admin_stack_ssh_gateway", SCRIPT)
assert SPEC and SPEC.loader
gateway = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gateway
SPEC.loader.exec_module(gateway)

PROJECT_ID = "88a8b083-8f53-4961-a703-aa0dd42e7536"
STACK_ID = "aad0e9d2-ac8d-4094-a484-6c09409e4b8f"


class GatewayTests(unittest.TestCase):
    @mock.patch.object(gateway.os, "geteuid", return_value=0)
    @mock.patch.object(gateway.Path, "is_file", return_value=True)
    @mock.patch.object(
        gateway.subprocess,
        "run",
        return_value=subprocess.CompletedProcess([], 0),
    )
    def test_gateway_forwards_scoped_delete_and_recovery_flags(
        self, run, _is_file, _geteuid
    ):
        result = gateway.main(
            {
                "SSH_ORIGINAL_COMMAND": f"delete {PROJECT_ID} {STACK_ID}",
            }
        )

        self.assertEqual(result, 0)
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ["bash", "-c"])
        self.assertIn(STACK_ID, command[2])
        self.assertIn(PROJECT_ID, command[2])
        self.assertIn("--recover-stale-credentials", command[2])

    @mock.patch.object(gateway.os, "geteuid", return_value=0)
    def test_gateway_rejects_unscoped_or_non_uuid_commands(self, _geteuid):
        for command in (
            f"delete {STACK_ID}",
            f"delete not-a-project {STACK_ID}",
            f"delete {PROJECT_ID} not-a-stack",
        ):
            with self.subTest(command=command):
                with mock.patch.object(gateway.subprocess, "run") as run:
                    result = gateway.main({"SSH_ORIGINAL_COMMAND": command})

                self.assertEqual(result, 2)
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
