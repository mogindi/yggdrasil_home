"""Unit tests for the Heat recovery command; no cloud access is required."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).with_name("recover-heat-stack.py")
SPEC = importlib.util.spec_from_file_location("recover_heat_stack", SCRIPT)
assert SPEC and SPEC.loader
recovery = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = recovery
SPEC.loader.exec_module(recovery)

STACK_ID = "aad0e9d2-ac8d-4094-a484-6c09409e4b8f"
PROJECT_ID = "88a8b083-8f53-4961-a703-aa0dd42e7536"


def completed(argv, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def stack_json(status="CREATE_FAILED"):
    return json.dumps(
        {
            "id": STACK_ID,
            "project_id": PROJECT_ID,
            "stack_name": "recovery-test",
            "stack_status": status,
        }
    )


class Runner:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.environments = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        self.environments.append(kwargs.get("env", {}))
        if not self.responses:
            raise AssertionError(f"unexpected command: {argv}")
        expected, response = self.responses.pop(0)
        if expected is not None and not expected(argv):
            raise AssertionError(f"unexpected command: {argv}")
        return response(argv) if callable(response) else response


def operator(runner):
    return recovery.RecoveryOperator(
        environ={
            "OPENSTACK_BIN": "/mock/openstack",
            "HEAT_ADMIN_STACK_BIN": "/mock/heat-admin-stack.py",
            "LIST_PROJECT_RESOURCES_BIN": "/mock/list-project-resources.py",
            "KEYSTONE_CONTAINER_CLI": "/mock/docker",
            "KEYSTONE_CONTAINER": "keystone",
            "OS_USERNAME": "admin",
            "OS_PASSWORD": "test-password",
            "OS_USER_DOMAIN_NAME": "Default",
        },
        runner=runner,
    )


class RecoveryCommandTests(unittest.TestCase):
    def test_recover_restores_same_project_uuid_then_invokes_heat_cleanup(self):
        runner = Runner(
            [
                (lambda argv: argv[:2] == ["/mock/heat-admin-stack.py", "show"], lambda argv: completed(argv, stdout=stack_json())),
                (lambda argv: argv[:3] == ["/mock/openstack", "project", "show"], lambda argv: completed(argv, 1, stderr=f"No project with a name or ID of '{PROJECT_ID}' exists")),
                (lambda argv: argv[:3] == ["/mock/docker", "exec", "keystone"], lambda argv: completed(argv)),
                (lambda argv: argv[:3] == ["/mock/openstack", "project", "show"], lambda argv: completed(argv, stdout=json.dumps({"ID": PROJECT_ID}))),
                (lambda argv: argv[:2] == ["/mock/heat-admin-stack.py", "delete"], lambda argv: completed(argv, stdout="Stack cleanup complete\n")),
            ]
        )
        args = recovery.build_parser().parse_args(["recover", STACK_ID, "--timeout", "30"])

        result = recovery.run(args, operator(runner))

        self.assertEqual(result, 0)
        setup_call = runner.calls[2]
        self.assertEqual(
            setup_call,
            [
                "/mock/docker",
                "exec",
                "keystone",
                "keystone-manage",
                "project_setup",
                "--project-name",
                f"heat-recovery-{STACK_ID[:8]}",
                "--project-id",
                PROJECT_ID,
            ],
        )
        self.assertEqual(
            runner.calls[4],
            [
                "/mock/heat-admin-stack.py",
                "delete",
                STACK_ID,
                "--recover-stale-credentials",
                "--yes",
                "--timeout",
                "30",
            ],
        )
        self.assertFalse(any(call[-2:] == ["delete", PROJECT_ID] for call in runner.calls))

    def test_restore_only_does_not_delete_the_stack(self):
        runner = Runner(
            [
                (None, lambda argv: completed(argv, stdout=stack_json())),
                (None, lambda argv: completed(argv, stdout=json.dumps({"id": PROJECT_ID, "domain_id": "domain-id"}))),
            ]
        )
        args = recovery.build_parser().parse_args(["restore-only", STACK_ID])

        self.assertEqual(recovery.run(args, operator(runner)), 0)
        self.assertEqual(len(runner.calls), 2)

    def test_recover_is_noop_for_delete_complete_stack(self):
        runner = Runner([(None, lambda argv: completed(argv, stdout=stack_json("DELETE_COMPLETE")))])
        args = recovery.build_parser().parse_args(["recover", STACK_ID])

        self.assertEqual(recovery.run(args, operator(runner)), 0)
        self.assertEqual(len(runner.calls), 1)

    def test_delete_only_refuses_to_restore_a_missing_project(self):
        runner = Runner(
            [
                (None, lambda argv: completed(argv, stdout=stack_json())),
                (None, lambda argv: completed(argv, 1, stderr="No project with a name or ID exists")),
            ]
        )
        args = recovery.build_parser().parse_args(["delete-only", STACK_ID])

        with self.assertRaisesRegex(recovery.RecoveryError, "run restore-only first"):
            recovery.run(args, operator(runner))
        self.assertEqual(len(runner.calls), 2)

    def test_cleanup_refuses_to_delete_when_inventory_is_incomplete(self):
        runner = Runner(
            [
                (None, lambda argv: completed(argv, stdout=json.dumps({"id": PROJECT_ID, "domain_id": "domain-id"}))),
                (None, lambda argv: completed(argv, stdout=json.dumps({"id": "admin-user"}))),
                (None, lambda argv: completed(argv, stdout="[]")),
                (None, lambda argv: completed(argv)),
                (None, lambda argv: completed(argv, 2, stderr="strict inventory failed")),
                (None, lambda argv: completed(argv)),
            ]
        )

        with self.assertRaisesRegex(recovery.RecoveryError, "complete inventory"):
            operator(runner).cleanup_project(PROJECT_ID, confirmed=True)
        self.assertFalse(any("delete" in call for call in runner.calls))
        self.assertEqual(runner.calls[-1][1:3], ["role", "remove"])

    def test_cleanup_refuses_to_delete_when_inventory_contains_resources(self):
        inventory = json.dumps(
            {
                "project": {"id": PROJECT_ID},
                "resources": {"compute": {"server": [{"id": "vm-1"}]}},
            }
        )
        runner = Runner(
            [
                (None, lambda argv: completed(argv, stdout=json.dumps({"id": PROJECT_ID, "domain_id": "domain-id"}))),
                (None, lambda argv: completed(argv, stdout=json.dumps({"id": "admin-user"}))),
                (None, lambda argv: completed(argv, stdout="[]")),
                (None, lambda argv: completed(argv)),
                (None, lambda argv: completed(argv, stdout=inventory)),
                (None, lambda argv: completed(argv)),
            ]
        )

        with self.assertRaisesRegex(recovery.RecoveryError, "1 inventoried resource"):
            operator(runner).cleanup_project(PROJECT_ID, confirmed=True)
        self.assertFalse(any("delete" in call for call in runner.calls))

    def test_cleanup_refuses_unexpected_inventory_resource_shape(self):
        inventory = json.dumps(
            {
                "project": {"id": PROJECT_ID},
                "resources": {"compute": {"server": {"id": "vm-1"}}},
            }
        )
        runner = Runner(
            [
                (None, lambda argv: completed(argv, stdout=json.dumps({"id": PROJECT_ID, "domain_id": "domain-id"}))),
                (None, lambda argv: completed(argv, stdout=json.dumps({"id": "admin-user"}))),
                (None, lambda argv: completed(argv, stdout="[]")),
                (None, lambda argv: completed(argv)),
                (None, lambda argv: completed(argv, stdout=inventory)),
                (None, lambda argv: completed(argv)),
            ]
        )

        with self.assertRaisesRegex(recovery.RecoveryError, "unexpected record list"):
            operator(runner).cleanup_project(PROJECT_ID, confirmed=True)
        self.assertFalse(any("project" in call and "delete" in call for call in runner.calls))

    def test_cleanup_deletes_only_after_empty_inventory_and_no_heat_stacks(self):
        inventory = json.dumps({"project": {"id": PROJECT_ID}, "resources": {}})
        runner = Runner(
            [
                (None, lambda argv: completed(argv, stdout=json.dumps({"id": PROJECT_ID, "domain_id": "domain-id"}))),
                (None, lambda argv: completed(argv, stdout=json.dumps({"id": "admin-user"}))),
                (None, lambda argv: completed(argv, stdout="[]")),
                (None, lambda argv: completed(argv)),
                (None, lambda argv: completed(argv, stdout=inventory)),
                (None, lambda argv: completed(argv)),
                (None, lambda argv: completed(argv, stdout="[]")),
                (None, lambda argv: completed(argv)),
            ]
        )

        self.assertEqual(operator(runner).cleanup_project(PROJECT_ID, confirmed=True), 0)
        inventory_environment = runner.environments[4]
        self.assertEqual(inventory_environment["OS_PROJECT_ID"], PROJECT_ID)
        self.assertEqual(inventory_environment["OS_PROJECT_DOMAIN_ID"], "domain-id")
        self.assertNotIn("OS_SYSTEM_SCOPE", inventory_environment)
        self.assertEqual(runner.calls[6], ["/mock/heat-admin-stack.py", "list-project", PROJECT_ID])
        self.assertEqual(runner.calls[-1], ["/mock/openstack", "project", "delete", PROJECT_ID])

    def test_cleanup_requires_explicit_confirmation(self):
        runner = Runner([])

        with self.assertRaisesRegex(recovery.RecoveryError, "requires --yes"):
            operator(runner).cleanup_project(PROJECT_ID, confirmed=False)
        self.assertEqual(runner.calls, [])


class SystemScopeHelperTests(unittest.TestCase):
    def test_source_helper_clears_project_scope_and_sets_system_scope(self):
        helper = SCRIPT.with_name("admin-openrc-system-scope.sh")
        with tempfile.TemporaryDirectory() as temporary:
            rc = Path(temporary) / "admin-openrc.sh"
            rc.write_text(
                "export OS_AUTH_URL=https://identity.example/v3\n"
                "export OS_USERNAME=admin\n"
                "export OS_PASSWORD=secret-for-this-test-only\n"
                "export OS_PROJECT_NAME=demo\n"
                "export OS_PROJECT_DOMAIN_NAME=Default\n",
                encoding="utf-8",
            )
            command = (
                'source "$1" && '
                '[[ "$OS_SYSTEM_SCOPE" == all ]] && '
                '[[ -z "${OS_PROJECT_NAME+x}${OS_PROJECT_ID+x}${OS_PROJECT_DOMAIN_NAME+x}'
                '${OS_DOMAIN_NAME+x}${OS_TENANT_NAME+x}" ]] && '
                '[[ "$OS_USERNAME" == admin ]] && '
                '[[ -n "$OPENSTACK_BIN" && -n "$OPENSTACK_PYTHON" ]]'
            )
            result = subprocess.run(
                ["bash", "-c", command, "test", str(helper)],
                env={
                    **dict(os.environ),
                    "OPENSTACK_BIN": "",
                    "OPENSTACK_PYTHON": "",
                    "OPENSTACK_RC": str(rc),
                    "OPENSTACK_KOLLA_WORKSPACE": temporary,
                    "OS_PROJECT_ID": "stale-project",
                    "OS_TENANT_NAME": "stale-tenant",
                    "OS_DOMAIN_NAME": "stale-domain",
                },
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
