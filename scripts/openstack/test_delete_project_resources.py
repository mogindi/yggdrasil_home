"""Unit tests for the project-resource deletion plan."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).with_name("delete-project-resources.py")
SPEC = importlib.util.spec_from_file_location("project_resource_delete", SCRIPT)
assert SPEC and SPEC.loader
deleter = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = deleter
SPEC.loader.exec_module(deleter)


def report_with_resources(resources):
    return {
        "project": {"id": "project-1", "name": "Project 1"},
        "errors": [],
        "resources": resources,
    }


class PlanningTests(unittest.TestCase):
    def test_missing_resources_are_planned_in_dependency_order(self):
        report = report_with_resources(
            {
                "network": {
                    "security_group": [{"id": "sg-1"}],
                    "network": [{"id": "net-1"}],
                    "subnet": [{"id": "subnet-1"}],
                    "port": [{"id": "port-1"}],
                    "router": [{"id": "router-1"}],
                    "floating_ip": [{"id": "fip-1"}],
                },
                "compute": {"server": [{"id": "server-1"}]},
                "block-storage": {
                    "volume": [{"id": "volume-1"}],
                    "snapshot": [{"id": "snapshot-1"}],
                    "backup": [{"id": "backup-1"}],
                },
                "application-container": {"container": [{"uuid": "container-1"}]},
                "orchestration": {"stack": [{"id": "stack-1"}]},
                "workflow": {
                    "workflow": [{"id": "workflow-1"}],
                    "execution": [{"id": "execution-1"}],
                    "cron_trigger": [{"name": "trigger-1"}],
                },
                "identity": {"trust": [{"id": "trust-1"}]},
            }
        )

        operations, issues = deleter.plan_deletions(report)

        self.assertEqual(issues, [])
        self.assertEqual(
            [(item.resource.resource, item.resource.identifier) for item in operations],
            [
                ("cron_trigger", "trigger-1"),
                ("execution", "execution-1"),
                ("workflow", "workflow-1"),
                ("stack", "stack-1"),
                ("container", "container-1"),
                ("server", "server-1"),
                ("snapshot", "snapshot-1"),
                ("backup", "backup-1"),
                ("volume", "volume-1"),
                ("floating_ip", "fip-1"),
                ("router", "router-1"),
                ("port", "port-1"),
                ("subnet", "subnet-1"),
                ("network", "net-1"),
                ("security_group", "sg-1"),
                ("trust", "trust-1"),
            ],
        )

    def test_swift_object_delete_uses_the_inventory_container(self):
        report = report_with_resources(
            {
                "object-store": {
                    "object": [
                        {"name": "file.txt", "container": "uploads"},
                    ],
                    "container": [{"name": "uploads"}],
                }
            }
        )

        operations, issues = deleter.plan_deletions(report)

        self.assertEqual(issues, [])
        self.assertEqual(operations[0].argv, ("object", "delete", "uploads", "file.txt"))
        self.assertEqual(operations[1].argv, ("container", "delete", "-r", "uploads"))

    def test_mistral_commands_use_openstack_plugin_command_names(self):
        report = report_with_resources(
            {
                "workflow": {
                    "cron_trigger": [{"name": "trigger-1"}],
                    "action_execution": [{"id": "action-execution-1"}],
                    "execution": [{"id": "execution-1"}],
                    "workflow": [{"id": "workflow-1"}],
                }
            }
        )

        operations, issues = deleter.plan_deletions(report)

        self.assertEqual(issues, [])
        self.assertEqual(
            [operation.argv for operation in operations],
            [
                ("cron", "trigger", "delete", "trigger-1"),
                ("action", "execution", "delete", "action-execution-1"),
                ("workflow", "execution", "delete", "--force", "execution-1"),
                ("workflow", "delete", "workflow-1"),
            ],
        )

    def test_dns_children_are_deleted_before_the_zone(self):
        report = report_with_resources(
            {
                "designate": {
                    "zone": [{"id": "zone-1"}],
                    "recordsets": [{"id": "recordset-1", "zone_id": "zone-1"}],
                    "zone_share": [{"id": "share-1", "zone_id": "zone-1"}],
                }
            }
        )

        operations, issues = deleter.plan_deletions(report)

        self.assertEqual(issues, [])
        self.assertEqual(
            [operation.argv for operation in operations],
            [
                ("zone", "share", "delete", "zone-1", "share-1"),
                ("recordset", "delete", "zone-1", "recordset-1"),
                ("zone", "delete", "--hard-delete", "zone-1"),
            ],
        )

    def test_read_only_children_are_covered_by_the_parent(self):
        report = report_with_resources(
            {
                "application-container": {
                    "action": [{"id": "action-1"}],
                    "container": [{"uuid": "container-1"}],
                },
                "load-balancer": {
                    "listener": [{"id": "listener-1"}],
                    "load_balancer": [{"id": "lb-1"}],
                },
                "orchestration": {
                    "resource": [{"id": "stack-resource-1"}],
                    "stack": [{"id": "stack-1"}],
                },
                "compute": {
                    "server_action": [{"id": "server-action-1"}],
                    "server_interface": [{"id": "server-interface-1"}],
                    "server_ip": [{"id": "server-ip-1"}],
                    "server": [{"id": "server-1"}],
                },
            }
        )

        operations, issues = deleter.plan_deletions(report)

        self.assertEqual(issues, [])
        self.assertEqual(
            [(operation.resource.resource, operation.resource.identifier) for operation in operations],
            [
                ("stack", "stack-1"),
                ("container", "container-1"),
                ("load_balancer", "lb-1"),
                ("server", "server-1"),
            ],
        )

    def test_service_parents_follow_their_children(self):
        report = report_with_resources(
            {
                "container-infrastructure-management": {
                    "cluster": [{"id": "cluster-1"}],
                    "cluster_template": [{"id": "template-1"}],
                },
                "shared-file-system": {
                    "share": [{"id": "share-1"}],
                    "share_snapshot": [{"id": "share-snapshot-1"}],
                    "share_group": [{"id": "share-group-1"}],
                    "share_network": [{"id": "share-network-1"}],
                },
                "block-storage": {
                    "group": [{"id": "group-1"}],
                    "volume": [{"id": "volume-1"}],
                    "volume_transfer": [{"id": "transfer-1"}],
                },
            }
        )

        operations, issues = deleter.plan_deletions(report)

        self.assertEqual(issues, [])
        self.assertEqual(
            [(item.resource.resource, item.resource.identifier) for item in operations],
            [
                ("cluster", "cluster-1"),
                ("cluster_template", "template-1"),
                ("share_snapshot", "share-snapshot-1"),
                ("share", "share-1"),
                ("share_group", "share-group-1"),
                ("share_network", "share-network-1"),
                ("group", "group-1"),
                ("volume_transfer", "transfer-1"),
                ("volume", "volume-1"),
            ],
        )

    def test_project_record_is_not_a_delete_target(self):
        report = report_with_resources(
            {"identity": {"project": [{"id": "project-1", "name": "Project 1"}]}}
        )

        operations, issues = deleter.plan_deletions(report)

        self.assertEqual(operations, [])
        self.assertEqual(issues, [])

    def test_unknown_resource_refuses_the_whole_plan(self):
        report = report_with_resources(
            {"new-service": {"new-resource": [{"id": "resource-1"}]}}
        )

        operations, issues = deleter.plan_deletions(report)

        self.assertEqual(operations, [])
        self.assertIn("unsupported project resource: new-service/new_resource", issues)


class ExecutionTests(unittest.TestCase):
    def _run_project_cleanup(
        self,
        reports,
        arguments,
        *,
        openstack_exit=0,
    ):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            report_dir = temporary_path / "reports"
            report_dir.mkdir()
            for index, report in enumerate(reports):
                (report_dir / f"{index}.json").write_text(
                    json.dumps(report), encoding="utf-8"
                )

            inventory_script = temporary_path / "inventory.py"
            inventory_script.write_text(
                "#!/usr/bin/env python3\n"
                "import os, pathlib, sys\n"
                "counter = pathlib.Path(os.environ['MOCK_INVENTORY_COUNTER'])\n"
                "try:\n"
                "    index = int(counter.read_text(encoding='utf-8'))\n"
                "except FileNotFoundError:\n"
                "    index = 0\n"
                "with open(os.environ['MOCK_INVENTORY_ARGS'], 'a', encoding='utf-8') as handle:\n"
                "    handle.write(' '.join(sys.argv[1:]) + '\\n')\n"
                "report = pathlib.Path(os.environ['MOCK_REPORT_DIR']) / f'{index}.json'\n"
                "print(report.read_text(encoding='utf-8'))\n"
                "counter.write_text(str(index + 1), encoding='utf-8')\n",
                encoding="utf-8",
            )
            openstack_bin = temporary_path / "openstack"
            openstack_bin.write_text(
                "#!/usr/bin/env python3\n"
                "import os, sys\n"
                "with open(os.environ['MOCK_OPENSTACK_LOG'], 'a', encoding='utf-8') as handle:\n"
                "    handle.write(' '.join(sys.argv[1:]) + '\\n')\n"
                "raise SystemExit(int(os.environ['MOCK_OPENSTACK_EXIT']))\n",
                encoding="utf-8",
            )
            openstack_bin.chmod(openstack_bin.stat().st_mode | stat.S_IXUSR)

            inventory_args = temporary_path / "inventory-args.log"
            openstack_log = temporary_path / "openstack.log"
            environment = {
                "MOCK_REPORT_DIR": str(report_dir),
                "MOCK_INVENTORY_COUNTER": str(temporary_path / "inventory-count"),
                "MOCK_INVENTORY_ARGS": str(inventory_args),
                "MOCK_OPENSTACK_LOG": str(openstack_log),
                "MOCK_OPENSTACK_EXIT": str(openstack_exit),
            }
            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch.dict(os.environ, environment):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    result = deleter.main(
                        [
                            "--project",
                            "project-1",
                            "--inventory-script",
                            str(inventory_script),
                            "--openstack-bin",
                            str(openstack_bin),
                            *arguments,
                        ]
                    )
            return (
                result,
                stdout.getvalue(),
                stderr.getvalue(),
                openstack_log.read_text(encoding="utf-8")
                if openstack_log.exists()
                else "",
                inventory_args.read_text(encoding="utf-8")
                if inventory_args.exists()
                else "",
            )

    @staticmethod
    def _service_api_error():
        return {
            "service": "backup",
            "resource": "backups",
            "category": "service_api",
            "error": "Freezer API returned 500",
        }

    def test_service_api_inventory_errors_are_warnings_during_delete_and_verify(self):
        initial = report_with_resources(
            {"compute": {"server": [{"id": "server-1"}]}}
        )
        initial["errors"] = [self._service_api_error()]
        verified = report_with_resources({})
        verified["errors"] = [self._service_api_error()]

        result, stdout, stderr, commands, inventory_args = self._run_project_cleanup(
            [initial, verified],
            ["--allow-service-api-errors", "--yes", "--verify"],
        )

        self.assertEqual(result, 0, stderr)
        self.assertIn("WARNING: non-blocking service_api error", stderr)
        self.assertIn("WARNING: non-blocking service_api error during verification", stderr)
        self.assertIn("Verified that no project resources remain.", stdout)
        self.assertIn("server delete --wait server-1", commands)
        self.assertEqual(len(inventory_args.splitlines()), 2)
        self.assertNotIn("--strict", inventory_args)

    def test_non_api_inventory_errors_still_block_delete(self):
        report = report_with_resources(
            {"compute": {"server": [{"id": "server-1"}]}}
        )
        report["errors"] = [
            self._service_api_error(),
            {
                "service": "workflow",
                "resource": "(client)",
                "category": "inventory",
                "error": "No supported SDK client",
            },
        ]

        result, _stdout, stderr, commands, _inventory_args = self._run_project_cleanup(
            [report], ["--allow-service-api-errors", "--yes"]
        )

        self.assertEqual(result, 2)
        self.assertIn("WARNING: non-blocking service_api error", stderr)
        self.assertIn("workflow/(client): No supported SDK client", stderr)
        self.assertEqual(commands, "")

    def test_service_api_inventory_errors_remain_blocking_by_default(self):
        report = report_with_resources(
            {"compute": {"server": [{"id": "server-1"}]}}
        )
        report["errors"] = [self._service_api_error()]

        result, _stdout, stderr, commands, inventory_args = self._run_project_cleanup(
            [report], ["--yes"]
        )

        self.assertEqual(result, 2)
        self.assertIn("backup/backups: Freezer API returned 500", stderr)
        self.assertEqual(commands, "")
        self.assertIn("--strict", inventory_args)

    def test_resource_delete_api_failure_remains_blocking(self):
        report = report_with_resources(
            {"compute": {"server": [{"id": "server-1"}]}}
        )
        report["errors"] = [self._service_api_error()]

        result, _stdout, stderr, commands, inventory_args = self._run_project_cleanup(
            [report, report],
            ["--allow-service-api-errors", "--yes", "--verify"],
            openstack_exit=1,
        )

        self.assertEqual(result, 3)
        self.assertIn("ERROR: compute/server/server-1", stderr)
        self.assertIn("server delete --wait server-1", commands)
        self.assertEqual(len(inventory_args.splitlines()), 1)

    def test_missing_router_interface_is_treated_as_already_removed(self):
        self.assertTrue(
            deleter._missing_output(
                "Router router-1 does not have an interface with id port-1"
            )
        )

    @mock.patch.object(deleter.time, "sleep")
    @mock.patch.object(deleter, "subprocess")
    def test_heat_delete_wait_accepts_delete_complete_status(
        self, subprocess_module, sleep
    ):
        subprocess_module.run.side_effect = [
            mock.Mock(returncode=0, stdout="DELETE_IN_PROGRESS\n", stderr=""),
            mock.Mock(returncode=0, stdout="DELETE_COMPLETE\n", stderr=""),
        ]

        error = deleter.wait_for_stack_delete(
            "openstack", "stack-1", timeout=10, poll_seconds=1
        )

        self.assertIsNone(error)
        sleep.assert_called_once_with(1)

    @mock.patch.object(deleter, "subprocess")
    def test_router_ports_are_removed_before_router_delete(self, subprocess_module):
        subprocess_module.run.side_effect = [
            mock.Mock(returncode=0, stdout="port-1\nport-2\n", stderr=""),
            mock.Mock(returncode=0, stdout="", stderr=""),
            mock.Mock(returncode=0, stdout="", stderr=""),
            mock.Mock(returncode=0, stdout="", stderr=""),
            mock.Mock(returncode=0, stdout="", stderr=""),
        ]
        resource = deleter.Resource("network", "router", "router-1", {"id": "router-1"})

        errors = deleter.delete_router("openstack", resource)

        self.assertEqual(errors, [])
        self.assertEqual(
            [call.args[0] for call in subprocess_module.run.call_args_list],
            [
                ["openstack", "port", "list", "--router", "router-1", "-f", "value", "-c", "ID"],
                ["openstack", "router", "remove", "port", "router-1", "port-1"],
                ["openstack", "router", "remove", "port", "router-1", "port-2"],
                ["openstack", "router", "unset", "--external-gateway", "router-1"],
                ["openstack", "router", "delete", "router-1"],
            ],
        )

    def test_dry_run_does_not_execute_openstack(self):
        report = report_with_resources(
            {"orchestration": {"stack": [{"id": "stack-1"}]}}
        )
        with mock.patch.object(deleter, "execute_plan") as execute_plan:
            with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(report))):
                result = deleter.main(["-", "--openstack-bin", "fake-openstack"])

        self.assertEqual(result, 0)
        execute_plan.assert_not_called()

    def test_report_errors_are_rejected_before_deletion(self):
        report = report_with_resources(
            {"orchestration": {"stack": [{"id": "stack-1"}]}}
        )
        report["errors"] = [{"service": "workflow", "error": "listing failed"}]
        with mock.patch.object(deleter, "execute_plan") as execute_plan:
            with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(report))):
                result = deleter.main(["-", "--yes"])

        self.assertEqual(result, 2)
        execute_plan.assert_not_called()


if __name__ == "__main__":
    unittest.main()
