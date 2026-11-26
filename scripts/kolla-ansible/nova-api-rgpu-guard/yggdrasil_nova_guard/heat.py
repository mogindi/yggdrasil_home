"""Read-only Heat checks used to authorize remote-model Nova operations."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlparse
from urllib.request import Request, urlopen


class HeatAuthorizationError(Exception):
    """The request cannot be proven to belong to an approved Heat stack."""


class HeatAuthorizationUnavailable(HeatAuthorizationError):
    """Heat could not be queried, so authorization fails closed."""


def is_remote_model_template(template: Any) -> bool:
    """Recognize the remote-model template used by the Yggdrasil catalog."""
    if not isinstance(template, dict):
        return False
    if isinstance(template.get("template"), dict):
        template = template["template"]
    resources = template.get("resources")
    parameters = template.get("parameters")
    if not isinstance(resources, dict) or not isinstance(parameters, dict):
        return False
    if not {"gpu_tier", "model_id", "model_name"}.issubset(parameters):
        return False

    server = resources.get("server") or {}
    deployment = resources.get("remote_model_lifecycle_deployment") or {}
    tunnel = resources.get("podman_create_container")
    server_properties = server.get("properties") or {}
    deployment_properties = deployment.get("properties") or {}
    return (
        str(server.get("type", "")).endswith("modules/compute/coreos_server.yaml")
        and str(deployment.get("type", "")).endswith(
            "modules/deployment/software_deployment.yaml"
        )
        and isinstance(tunnel, dict)
        and server_properties.get("rgpu_owner_stack_id")
        == {"get_param": "OS::stack_id"}
        and deployment_properties.get("actions") in (["CREATE"], ["CREATE", "DELETE"])
        and deployment_properties.get("server") == {"get_resource": "server"}
    )


def is_approved_template(template: Any, approved_templates: list[dict[str, Any]]) -> bool:
    """Require the full template to match a marketplace snapshot."""
    if not isinstance(template, dict):
        return False
    return any(template == approved for approved in approved_templates)


def has_approved_admin_key(
    stack: dict[str, Any], approved_templates: list[dict[str, Any]]
) -> bool:
    """Prevent a project user from replacing the platform VM SSH key."""
    parameters = stack.get("parameters") or {}
    actual_key = parameters.get("admin_ssh_public_key_blob")
    if not isinstance(actual_key, str) or not actual_key:
        return False
    for template in approved_templates:
        parameter = (template.get("parameters") or {}).get(
            "admin_ssh_public_key_blob"
        ) or {}
        approved_key = parameter.get("default")
        if isinstance(approved_key, str) and actual_key == approved_key:
            return True
    return False


class HeatReader:
    """Authenticate as the read-only RGPU Heat reader, scoped to one project."""

    def __init__(
        self,
        auth_url: str,
        heat_url: str,
        username: str,
        password: str,
        user_domain: str = "Default",
        project_domain: str = "Default",
        timeout: float = 5.0,
        approved_template_directory: str = "/etc/nova/yggdrasil-approved-templates",
    ) -> None:
        self.auth_url = auth_url.rstrip("/")
        self.heat_url = heat_url.rstrip("/")
        self.username = username
        self.password = password
        self.user_domain = user_domain
        self.project_domain = project_domain
        self.timeout = timeout
        self.approved_template_directory = Path(approved_template_directory)
        self._tokens: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()
        self._approved_templates: dict[str, list[dict[str, Any]]] = {}
        self._approved_templates_lock = threading.Lock()

    def _approved_templates_for(self, prefix: str) -> list[dict[str, Any]]:
        with self._approved_templates_lock:
            cached = self._approved_templates.get(prefix)
            if cached is not None:
                return cached

            try:
                import yaml

                paths = sorted(
                    self.approved_template_directory.glob(f"{prefix}*.yaml")
                )
                if not paths:
                    raise FileNotFoundError(
                        f"No approved Heat templates match {prefix}*.yaml"
                    )
                templates = []
                for path in paths:
                    template = yaml.safe_load(path.read_text())
                    if not isinstance(template, dict) or not isinstance(
                        template.get("resources"), dict
                    ):
                        raise ValueError(f"Invalid approved Heat template: {path.name}")
                    templates.append(template)
            except Exception as error:
                raise HeatAuthorizationUnavailable(
                    "Approved Heat templates are unavailable"
                ) from error

            self._approved_templates[prefix] = templates
            return templates

    def _token(self, project_id: str) -> str:
        now = time.time()
        with self._lock:
            cached = self._tokens.get(project_id)
            if cached and cached[1] > now + 60:
                return cached[0]

            payload = {
                "auth": {
                    "identity": {
                        "methods": ["password"],
                        "password": {
                            "user": {
                                "name": self.username,
                                "domain": {"name": self.user_domain},
                                "password": self.password,
                            }
                        },
                    },
                    "scope": {
                        "project": {
                            "id": project_id,
                            "domain": {"name": self.project_domain},
                        }
                    },
                }
            }
            request = Request(
                self.auth_url + "/auth/tokens",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                method="POST",
            )
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    token = response.headers.get("X-Subject-Token")
                    body = json.loads(response.read() or b"{}")
            except (HTTPError, URLError, TimeoutError, ValueError) as error:
                raise HeatAuthorizationUnavailable("Keystone reader authentication failed") from error
            if not token:
                raise HeatAuthorizationUnavailable("Keystone returned no reader token")
            expires_at = body.get("token", {}).get("expires_at", "")
            try:
                from datetime import datetime

                expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00")).timestamp()
            except (AttributeError, TypeError, ValueError):
                expiry = now + 300
            self._tokens[project_id] = (token, expiry)
            return token

    def _get(self, project_id: str, url: str) -> dict[str, Any]:
        token = self._token(project_id)
        request = Request(
            url,
            headers={"X-Auth-Token": token, "Accept": "application/json"},
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read() or b"{}")
        except (HTTPError, URLError, TimeoutError, ValueError) as error:
            raise HeatAuthorizationUnavailable("Heat ownership lookup failed") from error
        if not isinstance(body, dict):
            raise HeatAuthorizationUnavailable("Heat returned an invalid response")
        return body

    def _stack(self, project_id: str, stack_id: str) -> tuple[dict[str, Any], str]:
        url = f"{self.heat_url}/{quote(project_id, safe='')}/stacks/{quote(stack_id, safe='')}"
        body = self._get(project_id, url)
        stack = body.get("stack")
        if not isinstance(stack, dict) or str(stack.get("id", "")) != stack_id:
            raise HeatAuthorizationError("Heat stack id did not match the request")
        links = stack.get("links") or []
        self_link = next(
            (link.get("href") for link in links if link.get("rel") == "self"), None
        )
        if not self_link:
            raise HeatAuthorizationUnavailable("Heat stack response has no self link")
        resolved = urljoin(self.heat_url + "/", self_link)
        expected = urlparse(self.heat_url)
        actual = urlparse(resolved)
        if (actual.scheme, actual.netloc) != (expected.scheme, expected.netloc):
            raise HeatAuthorizationUnavailable("Heat returned an unexpected API link")
        return stack, resolved.rstrip("/")

    def _resource(self, project_id: str, stack_url: str, name: str) -> dict[str, Any]:
        body = self._get(project_id, stack_url + "/resources")
        resources = body.get("resources")
        if not isinstance(resources, list):
            raise HeatAuthorizationUnavailable("Heat returned an invalid resource list")
        resource = next(
            (
                item
                for item in resources
                if isinstance(item, dict)
                and str(item.get("resource_name", item.get("name", ""))) == name
            ),
            None,
        )
        if not isinstance(resource, dict):
            raise HeatAuthorizationError("Heat resource is missing")
        return resource

    def _template(self, project_id: str, stack_url: str) -> dict[str, Any]:
        body = self._get(project_id, stack_url + "/template")
        template = body.get("template", body)
        if not isinstance(template, dict):
            raise HeatAuthorizationError("Heat template is invalid")
        return template

    def validate_create(
        self,
        project_id: str,
        hints: dict[str, Any],
        server: dict[str, Any],
        fcos_image_id: str,
        fcos_image_name: str,
        rgpu_flavor_ids: set[str],
    ) -> bool:
        root_id = str(hints.get("heat_root_stack_id", ""))
        child_id = str(hints.get("heat_stack_id", ""))
        resource_name = str(hints.get("heat_resource_name", ""))
        flavor = server.get("flavorRef", server.get("flavor_ref", ""))
        image = server.get("imageRef", server.get("image_ref", ""))
        metadata = server.get("metadata") or {}
        if (
            not root_id
            or not child_id
            or resource_name != "server"
            or str(image) not in {fcos_image_id, fcos_image_name}
            or str(flavor) not in rgpu_flavor_ids
            or str(metadata.get("yggdrasil_rgpu_owner_stack_id", "")) != root_id
        ):
            return False

        root, root_url = self._stack(project_id, root_id)
        approved_model_templates = self._approved_templates_for("remote_model_")
        root_template = self._template(project_id, root_url)
        if (
            not is_remote_model_template(root_template)
            or not is_approved_template(root_template, approved_model_templates)
            or not has_approved_admin_key(root, approved_model_templates)
        ):
            return False
        root_server = self._resource(project_id, root_url, "server")
        if str(root_server.get("physical_resource_id", "")) != child_id:
            return False

        _child, child_url = self._stack(project_id, child_id)
        child_template = self._template(project_id, child_url)
        approved_coreos_templates = self._approved_templates_for("coreos_server_")
        if not is_approved_template(child_template, approved_coreos_templates):
            return False
        nova_server = (child_template.get("resources") or {}).get("server") or {}
        if str(nova_server.get("type", "")) != "OS::Nova::Server":
            return False
        nova_resource = self._resource(project_id, child_url, "server")
        return str(nova_resource.get("physical_resource_id", "")) in ("", "None")

    def validate_existing(self, project_id: str, root_id: str, server_id: str) -> bool:
        if not root_id or not server_id:
            return False
        root, root_url = self._stack(project_id, root_id)
        approved_model_templates = self._approved_templates_for("remote_model_")
        root_template = self._template(project_id, root_url)
        if (
            not is_remote_model_template(root_template)
            or not is_approved_template(root_template, approved_model_templates)
            or not has_approved_admin_key(root, approved_model_templates)
        ):
            return False
        root_server = self._resource(project_id, root_url, "server")
        child_id = str(root_server.get("physical_resource_id", ""))
        if not child_id:
            return False
        _child, child_url = self._stack(project_id, child_id)
        child_template = self._template(project_id, child_url)
        approved_coreos_templates = self._approved_templates_for("coreos_server_")
        if not is_approved_template(child_template, approved_coreos_templates):
            return False
        nova_server = (child_template.get("resources") or {}).get("server") or {}
        if str(nova_server.get("type", "")) != "OS::Nova::Server":
            return False
        nova_resource = self._resource(project_id, child_url, "server")
        return str(nova_resource.get("physical_resource_id", "")) == server_id
