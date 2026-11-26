"""Unit coverage for the Nova API RGPU guard's tenant boundary."""

from __future__ import annotations

import io
import json
import sys
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


GUARD_ROOT = Path(__file__).resolve().parents[1] / "kolla-ansible" / "nova-api-rgpu-guard"
sys.path.insert(0, str(GUARD_ROOT))

from yggdrasil_nova_guard.heat import (  # noqa: E402
    has_approved_admin_key,
    is_approved_template,
    is_remote_model_template,
)
from yggdrasil_nova_guard.middleware import RgpuGuardMiddleware  # noqa: E402


CONFIG = {
    "enabled": "true",
    "rgpu_flavor_ids": "rgpu-t1-id,rgpu-t2-id",
    "rgpu_flavor_names": "t1.rgpu,t2.rgpu",
    "fcos_image_id": "fcos-id",
    "fcos_image_name": "fcos-image",
    "trusted_heat_header": "X-Yggdrasil-Heat-Authorization",
    "trusted_heat_secret": "test-secret",
    "reader_password_b64": "cGFzcw==",
    "keystone_url": "http://identity/v3",
    "heat_url": "http://orchestration/v1",
    "reader_username": "reader",
}


def wsgi_request(app, path, method="GET", body=None, context=None, trusted=False):
    payload = b"" if body is None else json.dumps(body).encode()
    environ = {
        "PATH_INFO": path,
        "REQUEST_METHOD": method,
        "QUERY_STRING": "",
        "CONTENT_LENGTH": str(len(payload)),
        "wsgi.input": io.BytesIO(payload),
        "nova.context": context or SimpleNamespace(project_id="project-a", roles=["member"]),
    }
    if trusted:
        environ["HTTP_X_YGGDRASIL_HEAT_AUTHORIZATION"] = "test-secret"
    captured = {}

    def start_response(status, headers, _exc_info=None):
        captured["status"] = status
        captured["headers"] = headers

    response = b"".join(app(environ, start_response))
    return captured["status"], json.loads(response) if response else {}, environ


class NovaGuardMiddlewareTest(unittest.TestCase):
    def setUp(self):
        self.downstream = Mock()

        def app(environ, start_response):
            result = self.downstream(environ)
            start_response(result[0], result[1])
            return [result[2]]

        self.app = app
        self.middleware = RgpuGuardMiddleware(app, CONFIG)

    @staticmethod
    def json_response(payload, status="200 OK"):
        return status, [("Content-Type", "application/json")], json.dumps(payload).encode()

    def test_flavor_catalog_hides_rgpu_flavors(self):
        self.downstream.return_value = self.json_response(
            {"flavors": [{"id": "normal", "name": "m1.small"}, {"id": "rgpu-t1-id", "name": "t1.rgpu"}]}
        )
        status, body, _ = wsgi_request(self.middleware, "/v2.1/flavors/detail")
        self.assertEqual(status, "200 OK")
        self.assertEqual([item["name"] for item in body["flavors"]], ["m1.small"])

    def test_flavor_catalog_also_hides_project_prefixed_compat_route(self):
        self.downstream.return_value = self.json_response(
            {"flavors": [{"id": "rgpu-t1-id", "name": "t1.rgpu"}]}
        )
        status, body, _ = wsgi_request(
            self.middleware, "/v2.1/project-a/flavors/detail"
        )
        self.assertEqual(status, "200 OK")
        self.assertEqual(body["flavors"], [])

    def test_flavor_detail_returns_not_found_for_id_and_name(self):
        for value in ("rgpu-t1-id", "t1.rgpu"):
            with self.subTest(value=value):
                status, body, _ = wsgi_request(
                    self.middleware, f"/v2.1/flavors/{value}"
                )
                self.assertEqual(status, "404 Not Found")
                self.assertIn("itemNotFound", body)
        self.downstream.assert_not_called()

    def test_project_admin_is_not_a_platform_admin(self):
        self.downstream.return_value = self.json_response(
            {"flavors": [{"id": "rgpu-t1-id", "name": "t1.rgpu"}]}
        )
        context = SimpleNamespace(project_id="project-a", roles=["admin"], system_scope=None)
        status, body, _ = wsgi_request(
            self.middleware, "/v2.1/flavors/detail", context=context
        )
        self.assertEqual(status, "200 OK")
        self.assertEqual(body["flavors"], [])

    def test_project_admin_cannot_create_an_rgpu_vm(self):
        context = SimpleNamespace(project_id="project-a", roles=["admin"], system_scope=None)
        status, body, _ = wsgi_request(
            self.middleware,
            "/v2.1/servers",
            method="POST",
            body={"server": {"flavorRef": "rgpu-t1-id"}},
            context=context,
        )
        self.assertEqual(status, "403 Forbidden")
        self.assertIn("forbidden", body)
        self.downstream.assert_not_called()

    def test_system_scoped_admin_can_see_rgpu_flavors(self):
        self.downstream.return_value = self.json_response(
            {"flavors": [{"id": "rgpu-t1-id", "name": "t1.rgpu"}]}
        )
        context = SimpleNamespace(project_id=None, roles=["admin"], system_scope="all")
        status, body, _ = wsgi_request(
            self.middleware, "/v2.1/flavors/detail", context=context
        )
        self.assertEqual(status, "200 OK")
        self.assertEqual(body["flavors"][0]["name"], "t1.rgpu")

    def test_tenant_direct_rgpu_create_is_denied_before_nova(self):
        status, body, _ = wsgi_request(
            self.middleware,
            "/v2.1/servers",
            method="POST",
            body={"server": {"flavorRef": "rgpu-t1-id"}},
        )
        self.assertEqual(status, "403 Forbidden")
        self.assertIn("forbidden", body)
        self.downstream.assert_not_called()

    def test_heat_create_is_allowed_only_after_stack_validation(self):
        self.downstream.return_value = self.json_response(
            {"server": {"id": "server-id"}}, status="202 Accepted"
        )
        self.middleware.heat.validate_create = Mock(return_value=True)
        status, body, _ = wsgi_request(
            self.middleware,
            "/v2.1/servers",
            method="POST",
            trusted=True,
            body={
                "server": {
                    "name": "qwen-remote-model-vm",
                    "flavorRef": "rgpu-t1-id",
                    "imageRef": "fcos-id",
                },
                "os:scheduler_hints": {"heat_root_stack_id": "root-id"},
            },
        )
        self.assertEqual(status, "202 Accepted")
        self.assertEqual(body["server"]["id"], "server-id")
        self.middleware.heat.validate_create.assert_called_once()

    def test_trusted_heat_create_fails_closed_when_stack_is_unapproved(self):
        self.middleware.heat.validate_create = Mock(return_value=False)
        status, body, _ = wsgi_request(
            self.middleware,
            "/v2.1/servers",
            method="POST",
            trusted=True,
            body={
                "server": {
                    "name": "other-vm",
                    "flavorRef": "rgpu-t1-id",
                    "imageRef": "fcos-id",
                },
                "os:scheduler_hints": {},
            },
        )
        self.assertEqual(status, "403 Forbidden")
        self.assertIn("forbidden", body)
        self.downstream.assert_not_called()

    def test_tenant_cannot_read_a_managed_vm(self):
        self.downstream.return_value = self.json_response(
            {
                "server": {
                    "id": "server-id",
                    "flavor": {"id": "rgpu-t1-id"},
                    "metadata": {},
                }
            }
        )
        status, body, _ = wsgi_request(
            self.middleware, "/v2.1/servers/server-id"
        )
        self.assertEqual(status, "404 Not Found")
        self.assertIn("itemNotFound", body)

    def test_tenant_list_hides_vm_tagged_with_owner_stack_metadata(self):
        self.downstream.return_value = self.json_response(
            {
                "servers": [
                    {"id": "normal", "metadata": {}},
                    {"id": "managed", "metadata": {"yggdrasil_rgpu_owner_stack_id": "stack-id"}},
                ]
            }
        )
        status, body, _ = wsgi_request(
            self.middleware, "/v2.1/servers/detail"
        )
        self.assertEqual(status, "200 OK")
        self.assertEqual([item["id"] for item in body["servers"]], ["normal"])

    def test_server_summary_hides_remote_model_vm_by_name(self):
        self.downstream.return_value = self.json_response(
            {
                "servers": [
                    {"id": "normal", "name": "ordinary-server"},
                    {"id": "managed", "name": "qwen-remote-model-vm"},
                ]
            }
        )
        status, body, _ = wsgi_request(self.middleware, "/v2.1/servers")
        self.assertEqual(status, "200 OK")
        self.assertEqual([item["id"] for item in body["servers"]], ["normal"])

    def test_tenant_cannot_manage_a_managed_vm(self):
        self.downstream.return_value = self.json_response(
            {
                "server": {
                    "id": "server-id",
                    "flavor": {"id": "rgpu-t1-id"},
                    "metadata": {},
                }
            }
        )
        status, body, _ = wsgi_request(
            self.middleware,
            "/v2.1/servers/server-id/action",
            method="POST",
            body={"os-stop": None},
        )
        self.assertEqual(status, "404 Not Found")
        self.assertIn("itemNotFound", body)

    def test_system_admin_bypasses_vm_hiding(self):
        self.downstream.return_value = self.json_response(
            {"server": {"id": "server-id", "flavor": {"id": "rgpu-t1-id"}}}
        )
        context = SimpleNamespace(project_id=None, roles=["admin"], system_scope="all")
        status, body, _ = wsgi_request(
            self.middleware,
            "/v2.1/servers/server-id",
            context=context,
        )
        self.assertEqual(status, "200 OK")
        self.assertEqual(body["server"]["id"], "server-id")


class HeatTemplateCheckTest(unittest.TestCase):
    def test_recognizes_only_the_expected_remote_model_template_shape(self):
        template = {
            "parameters": {"gpu_tier": {}, "model_id": {}, "model_name": {}},
            "resources": {
                "server": {
                    "type": "modules/compute/coreos_server.yaml",
                    "properties": {"rgpu_owner_stack_id": {"get_param": "OS::stack_id"}},
                },
                "remote_model_lifecycle_deployment": {
                    "type": "modules/deployment/software_deployment.yaml",
                    "properties": {"actions": ["CREATE", "DELETE"], "server": {"get_resource": "server"}},
                },
                "podman_create_container": {"type": "OS::Heat::SoftwareConfig"},
            },
        }
        self.assertTrue(is_remote_model_template(template))
        template["resources"].pop("podman_create_container")
        self.assertFalse(is_remote_model_template(template))

    def test_template_must_match_an_approved_marketplace_snapshot(self):
        approved = {
            "heat_template_version": "2018-08-31",
            "parameters": {"model_name": {"type": "string"}},
            "resources": {
                "remote_model_lifecycle_deployment": {
                    "properties": {"config_script": "approved script"}
                }
            },
        }
        self.assertTrue(is_approved_template(approved, [approved]))

        modified = deepcopy(approved)
        modified["resources"]["remote_model_lifecycle_deployment"]["properties"][
            "config_script"
        ] = "run attacker script"
        self.assertFalse(is_approved_template(modified, [approved]))

        with_extra_resource = deepcopy(approved)
        with_extra_resource["resources"]["rogue_server"] = {
            "type": "OS::Nova::Server"
        }
        self.assertFalse(is_approved_template(with_extra_resource, [approved]))

    def test_remote_model_stack_must_keep_the_platform_ssh_key(self):
        approved = {
            "parameters": {
                "admin_ssh_public_key_blob": {"default": "platform-public-key"}
            }
        }
        self.assertTrue(
            has_approved_admin_key(
                {"parameters": {"admin_ssh_public_key_blob": "platform-public-key"}},
                [approved],
            )
        )
        self.assertFalse(
            has_approved_admin_key(
                {"parameters": {"admin_ssh_public_key_blob": "tenant-public-key"}},
                [approved],
            )
        )


if __name__ == "__main__":
    unittest.main()
