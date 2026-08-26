#!/usr/bin/env python3
"""Unit tests for list-project-resources.py.

These tests use small fakes so they can run without an OpenStack cloud.  The
live SDK/client smoke test is intentionally separate: the command must fail
when a deployment advertises a service for which no client is installed.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
import unittest
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

    @mock.patch.object(inventory, "subprocess")
    def test_endpoint_command_is_the_primary_discovery_path(self, subprocess: mock.Mock) -> None:
        subprocess.run.return_value = types.SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "ID": "one",
                        "Region": "RegionOne",
                        "Service Name": "Nova",
                        "Service Type": "compute",
                        "Enabled": True,
                        "Interface": "public",
                        "URL": "https://nova.example/",
                    }
                ]
            ),
            stderr="",
        )
        endpoints = inventory.discover_endpoints("openstack")
        subprocess.run.assert_called_once_with(
            ["openstack", "endpoint", "list", "-f", "json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(endpoints[0].service_type, "compute")

    @mock.patch.object(inventory, "_run_endpoint_command", side_effect=FileNotFoundError("openstack"))
    def test_missing_cli_uses_sdk_catalog_fallback(self, _: mock.Mock) -> None:
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
        endpoints = inventory.discover_endpoints("missing-openstack", connection)
        self.assertEqual(endpoints[0].url, "https://nova.example/")

    @mock.patch.object(inventory, "subprocess")
    def test_endpoint_command_failure_is_not_hidden_by_catalog_fallback(
        self, subprocess: mock.Mock
    ) -> None:
        subprocess.run.return_value = types.SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="Unauthorized",
        )
        with self.assertRaisesRegex(inventory.EndpointDiscoveryError, "Unauthorized"):
            inventory.discover_endpoints("openstack", types.SimpleNamespace(service_catalog=[]))

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


class ProviderTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
