#!/usr/bin/env python3
"""Unit tests for list-project-resources.py.

These tests use small fakes so they can run without an OpenStack cloud.  The
live SDK/client smoke test is intentionally separate: the command must fail
when a deployment advertises a service for which no client is installed.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).with_name("list-project-resources.py")
SPEC = importlib.util.spec_from_file_location("project_resource_inventory", SCRIPT)
assert SPEC and SPEC.loader
inventory = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = inventory
SPEC.loader.exec_module(inventory)


class EndpointTests(unittest.TestCase):
    def test_parse_endpoint_list_json_ignores_disabled_rows(self) -> None:
        payload = json.dumps(
            [
                {
                    "ID": "one",
                    "Region": "RegionOne",
                    "Service Name": "Nova",
                    "Service Type": "compute",
                    "Enabled": True,
                    "Interface": "public",
                    "URL": "https://nova.example/",
                },
                {
                    "ID": "two",
                    "Region": "RegionOne",
                    "Service Name": "Disabled",
                    "Service Type": "disabled-service",
                    "Enabled": False,
                    "Interface": "public",
                    "URL": "https://disabled.example/",
                },
            ]
        )
        endpoints = inventory.parse_endpoint_list_json(payload)
        self.assertEqual([endpoint.service_type for endpoint in endpoints], ["compute"])

    @mock.patch.object(inventory, "_run_endpoint_command")
    def test_sdk_catalog_is_the_primary_discovery_path(self, run_command: mock.Mock) -> None:
        connection = types.SimpleNamespace(
            service_catalog=[
                {
                    "id": "nova-service",
                    "name": "nova",
                    "type": "compute",
                    "endpoints": [
                        {
                            "id": "endpoint",
                            "region": "RegionOne",
                            "interface": "public",
                            "url": "https://nova.example/",
                        }
                    ],
                }
            ]
        )
        endpoints = inventory.discover_endpoints(connection, "openstack")
        run_command.assert_not_called()
        self.assertEqual(endpoints[0].url, "https://nova.example/")

    @mock.patch.object(inventory, "_run_endpoint_command", side_effect=FileNotFoundError("openstack"))
    def test_missing_cli_is_only_used_after_sdk_catalog_failure(self, _: mock.Mock) -> None:
        connection = types.SimpleNamespace(service_catalog=[])
        with self.assertRaisesRegex(inventory.EndpointDiscoveryError, "unavailable"):
            inventory.discover_endpoints(connection, "missing-openstack")

    @mock.patch.object(inventory, "_run_endpoint_command")
    def test_cli_fallback_is_used_when_sdk_catalog_fails(self, run_command: mock.Mock) -> None:
        run_command.return_value = [
            inventory.Endpoint(
                "one", "RegionOne", "Nova", "compute", "compute", "public", "https://nova.example/"
            )
        ]
        endpoints = inventory.discover_endpoints(types.SimpleNamespace(service_catalog=[]), "openstack")
        run_command.assert_called_once_with("openstack")
        self.assertEqual(endpoints[0].service_type, "compute")

    @mock.patch.object(inventory, "_run_endpoint_command", side_effect=inventory.EndpointDiscoveryError("Unauthorized"))
    def test_endpoint_command_failure_is_not_hidden_by_catalog_failure(
        self, _: mock.Mock
    ) -> None:
        with self.assertRaisesRegex(inventory.EndpointDiscoveryError, "Unauthorized"):
            inventory.discover_endpoints(types.SimpleNamespace(service_catalog=[]), "openstack")

    def test_alias_selection_prefers_canonical_or_newest_version(self) -> None:
        endpoints = [
            inventory.Endpoint(
                "old", "RegionOne", "manila", "share", "shared-file-system", "public", "old"
            ),
            inventory.Endpoint(
                "new", "RegionOne", "manila", "sharev2", "shared-file-system", "public", "new"
            ),
        ]
        selected = inventory.select_endpoints(endpoints, "public", "RegionOne")
        self.assertEqual([endpoint.url for endpoint in selected], ["new"])

    def test_ignored_service_types_are_not_selected(self) -> None:
        endpoints = [
            inventory.Endpoint(
                "heat-cfn",
                "RegionOne",
                "heat-cfn",
                "cloudformation",
                "cloudformation",
                "public",
                "https://heat-cfn.example/",
            ),
            inventory.Endpoint(
                "venus",
                "RegionOne",
                "venus",
                "LMS",
                "lms",
                "public",
                "https://venus.example/",
            ),
            inventory.Endpoint(
                "skyline",
                "RegionOne",
                "skyline",
                "panel",
                "panel",
                "public",
                "https://skyline.example/",
            ),
            inventory.Endpoint(
                "gnocchi",
                "RegionOne",
                "Gnocchi",
                "metric",
                "metric",
                "public",
                "https://gnocchi.example/",
            ),
            inventory.Endpoint(
                "compute",
                "RegionOne",
                "nova",
                "compute",
                "compute",
                "public",
                "https://nova.example/",
            ),
        ]

        selected = inventory.select_endpoints(endpoints, "public", "RegionOne")

        self.assertEqual([endpoint.service_type for endpoint in selected], ["compute"])


class ProviderTests(unittest.TestCase):
    def test_project_connection_uses_project_id_not_project_name(self) -> None:
        connection = mock.Mock()
        connection.connect_as.return_value = "scoped"
        self.assertEqual(
            inventory._connection_as_project(connection, "project-1"),
            "scoped",
        )
        connection.connect_as.assert_called_once_with(project_id="project-1")

    def test_project_filter_uses_user_project_id_when_present(self) -> None:
        self.assertTrue(
            inventory._belongs_to_project(
                {"user_project_id": "project-1"}, "project-1"
            )
        )
        self.assertFalse(
            inventory._belongs_to_project(
                {"user_project_id": "project-2"}, "project-1"
            )
        )

    def test_project_filter_uses_nested_location_project(self) -> None:
        resource = {"location": {"project": {"id": "project-2"}}}
        self.assertFalse(inventory._belongs_to_project(resource, "project-1"))

    def test_location_project_precedes_heat_stack_user_project(self) -> None:
        resource = {
            "location": {"project": {"id": "project-1"}},
            "user_project_id": "heat-stack-user-project",
        }
        self.assertTrue(inventory._belongs_to_project(resource, "project-1"))

    def test_project_filter_uses_heat_stack_link_project(self) -> None:
        resource = {
            "links": [
                {
                    "href": "http://heat.example/v1/22222222222222222222222222222222/stacks/name/id",
                    "rel": "self",
                }
            ],
            "location": {"project": {"id": "11111111111111111111111111111111"}},
        }
        self.assertFalse(
            inventory._belongs_to_project(
                resource, "11111111111111111111111111111111"
            )
        )

    def test_workflow_executions_are_limited_to_project_workflows(self) -> None:
        workflows = [{"id": "workflow-1"}]
        executions = [
            {"id": "execution-1", "workflow_id": "workflow-1"},
            {"id": "execution-2", "workflow_id": "workflow-2"},
        ]
        filtered = inventory._filter_related_resources(
            "workflow", "execution", executions, {"workflow": workflows}
        )
        self.assertEqual([item["id"] for item in filtered], ["execution-1"])

    def test_service_descriptor_does_not_match_unrelated_endpoint(self) -> None:
        description = types.SimpleNamespace(service_type="compute", all_types=())
        endpoint = inventory.Endpoint(
            "network", "RegionOne", "neutron", "network", "network", "public", "network"
        )
        self.assertFalse(inventory._description_matches(description, endpoint))

    def test_builtin_provider_consumes_the_sdk_pagination_iterator(self) -> None:
        class QueryMapping:
            _mapping = {"project_id": "project_id"}

        class Widget:
            allow_list = True
            base_path = "/widgets"
            resource_key = "widgets"
            _query_mapping = QueryMapping()

        class Proxy:
            _resource_registry = {"widget": Widget}

            def __init__(self) -> None:
                self.calls = []

            def _list(self, resource_class, **kwargs):
                self.calls.append((resource_class, kwargs))
                yield {"id": "widget-1", "project_id": "project-1"}

        proxy = Proxy()

        class Description:
            service_type = "compute"
            supported_versions = {"2": object}
            all_types = ()

            def __get__(self, connection, owner):
                return proxy

        # The provider checks that the descriptor module is importable.
        Description.__module__ = "json"
        endpoint = inventory.Endpoint(
            "compute", "RegionOne", "nova", "compute", "compute", "public", "compute"
        )
        provider = inventory.BuiltinSDKProvider(endpoint, object(), Description())
        result = provider.collect(inventory.Project("project-1", "Project", {"id": "project-1"}))
        self.assertEqual(result["widget"][0]["id"], "widget-1")
        self.assertEqual(proxy.calls[0][1], {"project_id": "project-1"})

    def test_builtin_provider_reports_list_failures(self) -> None:
        class QueryMapping:
            _mapping = {"project_id": "project_id"}

        class Widget:
            allow_list = True
            base_path = "/widgets"
            resource_key = "widgets"
            _query_mapping = QueryMapping()

        class Proxy:
            _resource_registry = {"widget": Widget}

            def _list(self, resource_class, **kwargs):
                raise RuntimeError("service returned 503")

        class Description:
            service_type = "compute"
            supported_versions = {"2": object}
            all_types = ()

            def __get__(self, connection, owner):
                return Proxy()

        Description.__module__ = "json"
        endpoint = inventory.Endpoint(
            "compute", "RegionOne", "nova", "compute", "compute", "public", "compute"
        )
        provider = inventory.BuiltinSDKProvider(endpoint, object(), Description())
        with self.assertRaisesRegex(inventory.InventoryError, "widget"):
            provider.collect(inventory.Project("project-1", "Project", {"id": "project-1"}))

    def test_legacy_provider_lists_manager_with_project_scope(self) -> None:
        class Manager:
            def list(self, project_id=None, all_projects=True):
                self.arguments = (project_id, all_projects)
                return [{"id": "legacy-1", "project_id": project_id}]

        provider = inventory.LegacySDKProvider.__new__(inventory.LegacySDKProvider)
        provider.endpoint = inventory.Endpoint(
            "legacy", "RegionOne", "legacy", "legacy", "legacy", "public", "legacy"
        )
        provider.module = types.SimpleNamespace(__name__="legacyclient.osc")
        provider.name = "legacyclient.osc (legacy SDK)"
        provider.client = types.SimpleNamespace(resources=Manager())
        result = provider.collect(inventory.Project("project-1", "Project", {}))
        self.assertEqual(result["resources"][0]["project_id"], "project-1")
        self.assertEqual(provider.client.resources.arguments, ("project-1", False))

    def test_legacy_provider_traverses_nested_manager_collections(self) -> None:
        class ContainerManager:
            def list(self, all_projects=False):
                return [{"id": "container-1", "project_id": "project-1"}]

        class ActionManager:
            def list(self, container):
                return [{"id": f"action-for-{container}"}]

        provider = inventory.LegacySDKProvider.__new__(inventory.LegacySDKProvider)
        provider.endpoint = inventory.Endpoint(
            "legacy", "RegionOne", "legacy", "legacy", "legacy", "public", "legacy"
        )
        provider.module = types.SimpleNamespace(__name__="legacyclient.osc")
        provider.name = "legacyclient.osc (legacy SDK)"
        provider.client = types.SimpleNamespace(
            containers=ContainerManager(), actions=ActionManager()
        )
        result = provider.collect(inventory.Project("project-1", "Project", {}))
        self.assertEqual(result["containers"][0]["id"], "container-1")
        self.assertEqual(result["actions"][0]["id"], "action-for-container-1")

    def test_legacy_object_inventory_preserves_container_name(self) -> None:
        class ContainerManager:
            def list(self, all_projects=False):
                return [{"name": "uploads", "project_id": "project-1"}]

        class ObjectManager:
            def list(self, container):
                return [{"name": "file.txt"}]

        provider = inventory.LegacySDKProvider.__new__(inventory.LegacySDKProvider)
        provider.endpoint = inventory.Endpoint(
            "swift", "RegionOne", "swift", "object-store", "object-store", "public", "swift"
        )
        provider.module = types.SimpleNamespace(__name__="swiftclient")
        provider.name = "swiftclient (legacy SDK)"
        provider.client = types.SimpleNamespace(
            containers=ContainerManager(), objects=ObjectManager()
        )

        result = provider.collect(inventory.Project("project-1", "Project", {}))

        self.assertEqual(result["objects"][0]["container"], "uploads")

    def test_legacy_provider_continues_after_manager_failure(self) -> None:
        class WorkingManager:
            def list(self):
                return [{"id": "backup-1", "project_id": "project-1"}]

        class BrokenManager:
            def list(self):
                raise RuntimeError("Service Unavailable")

        provider = inventory.LegacySDKProvider.__new__(inventory.LegacySDKProvider)
        provider.endpoint = inventory.Endpoint(
            "backup", "RegionOne", "freezer", "backup", "backup", "public", "backup"
        )
        provider.module = types.SimpleNamespace(__name__="freezerclient.client")
        provider.name = "freezerclient.client (Python SDK)"
        provider.client = types.SimpleNamespace(
            backups=WorkingManager(), sessions=BrokenManager()
        )

        result = provider.collect(
            inventory.Project("project-1", "Project", {}), strict=False
        )

        self.assertEqual(result["backups"][0]["id"], "backup-1")
        self.assertEqual(provider.errors[0]["resource"], "sessions")
        self.assertIn("Service Unavailable", provider.errors[0]["error"])

    def test_legacy_provider_ignores_an_unavailable_optional_collection(self) -> None:
        class MissingManager:
            def list(self):
                raise RuntimeError("NotFoundException: 404 resource could not be found")

        provider = inventory.LegacySDKProvider.__new__(inventory.LegacySDKProvider)
        provider.endpoint = inventory.Endpoint(
            "missing", "RegionOne", "missing", "missing", "missing", "public", "missing"
        )
        provider.module = types.SimpleNamespace(__name__="missingclient")
        provider.name = "missingclient (Python SDK)"
        provider.client = types.SimpleNamespace(resources=MissingManager())

        result = provider.collect(inventory.Project("project-1", "Project", {}))

        self.assertEqual(result, {})
        self.assertEqual(provider.errors, [])

    @mock.patch.object(inventory, "_generic_client_classes")
    def test_conventional_client_module_is_used_without_an_osc_plugin(
        self, client_classes: mock.Mock
    ) -> None:
        class ResourceManager:
            def list(self):
                return [{"id": "sdk-1", "project_id": "project-1"}]

        class Client:
            def __init__(self, **kwargs):
                self.resources = ResourceManager()

        module = types.SimpleNamespace(__name__="newserviceclient.client")
        client_classes.return_value = [(module, Client)]
        connection = types.SimpleNamespace(session=object())
        endpoint = inventory.Endpoint(
            "new", "RegionOne", "newservice", "newservice", "newservice", "public", "new"
        )
        provider = inventory._generic_client_provider(endpoint, connection)
        self.assertIsNotNone(provider)
        result = provider.collect(inventory.Project("project-1", "Project", {}))
        self.assertEqual(result["resources"][0]["id"], "sdk-1")


class InventoryTests(unittest.TestCase):
    def test_all_clients_are_preflighted_before_any_resource_is_listed(self) -> None:
        endpoints = [
            inventory.Endpoint("one", "RegionOne", "one", "one", "one", "public", "one"),
            inventory.Endpoint("two", "RegionOne", "two", "two", "two", "public", "two"),
        ]
        connection = types.SimpleNamespace(current_project_id="project-1")
        project = inventory.Project("project-1", "Project", {})
        first_provider = types.SimpleNamespace(name="first", collect=mock.Mock())

        def resolve(endpoint, target):
            if endpoint.service_type == "two":
                raise inventory.UnsupportedClientError("missing client")
            return first_provider

        with mock.patch.object(inventory, "resolve_provider", side_effect=resolve):
            with self.assertRaisesRegex(inventory.InventoryError, "no resources were listed"):
                inventory.inventory(endpoints, connection, project)
        first_provider.collect.assert_not_called()

    def test_best_effort_inventory_keeps_resources_and_reports_preflight_errors(self) -> None:
        endpoints = [
            inventory.Endpoint("one", "RegionOne", "one", "one", "one", "public", "one"),
            inventory.Endpoint("two", "RegionOne", "two", "two", "two", "public", "two"),
        ]
        connection = types.SimpleNamespace(current_project_id="project-1")
        project = inventory.Project("project-1", "Project", {})
        first_provider = types.SimpleNamespace(
            name="first",
            errors=[],
            collect=mock.Mock(return_value={"resources": [{"id": "resource-1"}]}),
        )

        def resolve(endpoint, target):
            if endpoint.service_type == "two":
                raise inventory.UnsupportedClientError("missing client")
            return first_provider

        with mock.patch.object(inventory, "resolve_provider", side_effect=resolve):
            report = inventory.inventory(endpoints, connection, project, strict=False)

        self.assertEqual(report["resources"]["one"]["resources"][0]["id"], "resource-1")
        self.assertEqual(report["errors"][0]["service"], "two")
        self.assertEqual(report["errors"][0]["resource"], "(client)")

    def test_render_table_shows_resource_errors(self) -> None:
        output = inventory.render_table(
            {
                "project": {"id": "project-1", "name": "Project"},
                "resources": {"backup": {"backups": []}},
                "providers": {"backup": {"provider": "freezerclient"}},
                "errors": [
                    {
                        "service": "backup",
                        "resource": "actions",
                        "error": "Error 404: Not Found",
                    }
                ],
            }
        )

        self.assertIn("backup / actions: Error 404: Not Found", output)

    def test_hide_errors_removes_errors_from_json_output(self) -> None:
        report = {
            "project": {"id": "project-1", "name": "Project"},
            "endpoints": [],
            "providers": {},
            "resources": {},
            "errors": [
                {
                    "service": "backup",
                    "resource": "actions",
                    "error": "Error 404: Not Found",
                }
            ],
        }
        output = io.StringIO()

        with mock.patch.object(inventory, "_connect", return_value=object()), \
             mock.patch.object(inventory, "discover_endpoints", return_value=[]), \
             mock.patch.object(inventory, "select_endpoints", return_value=[]), \
             mock.patch.object(
                 inventory,
                 "resolve_project",
                 return_value=inventory.Project("project-1", "Project", {}),
             ), \
             mock.patch.object(inventory, "inventory", return_value=report):
            with redirect_stdout(output):
                status = inventory.main(
                    ["--project", "admin", "--format", "json", "--hide-errors"]
                )

        self.assertEqual(status, 0)
        self.assertNotIn("errors", json.loads(output.getvalue()))


if __name__ == "__main__":
    unittest.main()
