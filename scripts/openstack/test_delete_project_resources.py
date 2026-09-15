"""Unit tests for the project-resource deletion plan."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import unittest
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
