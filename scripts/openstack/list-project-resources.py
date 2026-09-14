#!/usr/bin/env python3
"""List resources visible to an OpenStack project.

Endpoint discovery uses the authenticated openstacksdk Keystone catalog,
which is the Python equivalent of the core endpoint-list operation and does
not need service-specific CLI plugins.  The core OpenStack CLI is retained as
a catalog fallback. Resource enumeration is performed through Python SDKs:
openstacksdk proxies are used when available, with dynamically discovered
legacy client SDKs as a fallback.

The command produces a best-effort inventory by default.  Resource request
failures are included in the report and do not prevent other resources from
being listed.  Use ``--strict`` when an incomplete inventory should be a hard
failure.

The deployment-specific dashboard, metrics, and compatibility endpoints for
``panel``, ``LMS``, ``cloudformation``, and Gnocchi are intentionally excluded
because they are not part of this project-resource inventory.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import importlib
import importlib.metadata
import inspect
import itertools
import json
import logging
import os
import pkgutil
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable, Mapping
from typing import Any


class InventoryError(RuntimeError):
    """Base class for errors that should be shown without a traceback."""


class UnsupportedClientError(InventoryError):
    """Raised when an endpoint has no usable Python client."""


class EndpointDiscoveryError(InventoryError):
    """Raised when the Keystone endpoint catalog cannot be read."""


@dataclasses.dataclass(frozen=True)
class Endpoint:
    endpoint_id: str
    region: str
    service_name: str
    raw_service_type: str
    service_type: str
    interface: str
    url: str
    enabled: bool = True


@dataclasses.dataclass
class Project:
    project_id: str
    name: str
    raw: Any


@dataclasses.dataclass
class ResourceSpec:
    name: str
    resource_class: type
    placeholders: tuple[str, ...]


def _provider_error(resource_type: str, message: Any) -> dict[str, str]:
    return {"resource": resource_type, "error": _text(message)}


@contextlib.contextmanager
def _suppress_error_logs(enabled: bool) -> Any:
    if not enabled:
        yield
        return
    previous_disable_level = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(previous_disable_level)


# These catalog services are enabled in the deployment but are not included
# in the project-resource inventory.  Match the service type and name so
# aliases such as an uppercase ``LMS`` or a Gnocchi ``metric`` type remain
# excluded if service-type canonicalization changes.
IGNORED_SERVICE_TYPES = frozenset(
    {"cloudformation", "gnocchi", "lms", "metric", "panel"}
)


def _is_ignored_endpoint(endpoint: Endpoint) -> bool:
    return any(
        _text(value).strip().casefold() in IGNORED_SERVICE_TYPES
        for value in (
            endpoint.service_type,
            endpoint.raw_service_type,
            endpoint.service_name,
        )
    )


GLOBAL_RESOURCE_NAMES = {
    "account",
    "agent",
    "archive_policy",
    "archive_policy_rule",
    "availability_zone",
    "availability_zone_profile",
    "capabilities",
    "cluster_template",  # A template is listed below only when it has project ownership.
    "conductor",
    "datastore",
    "datastore_version",
    "domain",
    "endpoint",
    "extension",
    "flavor",
    "flavor_access",
    "group",
    "group_type",
    "host",
    "hypervisor",
    "image_import",
    "info",
    "limit",
    "limits",
    "policy_type",
    "profile_type",
    "provider",
    "quota",
    "quota_class_set",
    "quota_set",
    "region",
    "resource_class",
    "resource_filter",
    "resource_provider",
    "role",
    "schema",
    "service",
    "service_status",
    "stats",
    "stats_pools",
    "task",
    "type",
    "user",
    "version",
    "volume_type",
    "volume_type_access",
    "zone_export",
}


GLOBAL_LEGACY_MANAGER_NAMES = {
    "archive_policies",
    "archive_policy_rules",
    "availability_zones",
    "capabilities",
    "datastores",
    "datastore_versions",
    "extensions",
    "flavors",
    "group_types",
    "hosts",
    "hypervisors",
    "images",
    "limits",
    "mservices",
    "policies",
    "policy_types",
    "profiles",
    "quotas",
    "regions",
    "registries",
    "resource_classes",
    "resource_filters",
    "resource_providers",
    "resource_type",
    "resource_types",
    "roles",
    "services",
    "stats",
    "tasks",
    "types",
    "users",
    "versions",
    "volume_type_accesses",
    "volume_types",
}


# These service collections are status/history views rather than deletable
# project resources.  Some deployed services expose them through a legacy
# client even when the endpoint returns 404 for the list call.
NON_DELETABLE_RESOURCE_NAMES = frozenset(
    {
        "network_ip_availability",
        "network_ip_availabilities",
    }
)
NON_DELETABLE_RESOURCE_NAMES_BY_SERVICE = {
    "application-container": frozenset(
        {
            "action",
            "actions",
            "container_action",
            "container_actions",
            "log",
            "logs",
            "stats",
        }
    ),
    "backup": frozenset({"action", "actions"}),
    "clustering": frozenset({"action", "event"}),
    "dns": frozenset(
        {
            "blacklist",
            "blacklists",
            "floatingip",
            "floatingips",
            "pool",
            "pools",
            "service_status",
            "service_statuses",
            "tld",
            "tlds",
            "tsigkey",
            "tsigkeys",
            "zone_export",
            "zone_exports",
            "zone_import",
            "zone_imports",
        }
    ),
    "image": frozenset({"image_member", "image_members", "member", "members"}),
    "load-balancer": frozenset(
        {
            "healthmonitor",
            "health_monitor",
            "listener",
            "member",
            "pool",
        }
    ),
    "orchestration": frozenset({"resource", "resources"}),
}


def _non_deletable_resource(service_type: str, resource_name: str) -> bool:
    return (
        resource_name in NON_DELETABLE_RESOURCE_NAMES
        or resource_name
        in NON_DELETABLE_RESOURCE_NAMES_BY_SERVICE.get(service_type, ())
    )


def _is_missing_resource_error(error: BaseException) -> bool:
    """Return whether a list failure means this API resource is unavailable."""

    text = _text(error).casefold()
    return bool(
        re.search(
            r"\b404\b|not found|could not be found|resource could not be found",
            text,
        )
    )


# Some APIs expose project-owned objects without project_id in the SDK
# resource model.  These names are only used after the request is made with a
# project-scoped connection; they are not a service allow-list.
PROJECT_SCOPED_EXCEPTIONS = {
    "application-container": {"container", "capsule"},
    "block-storage": {"group", "group_snapshot", "volume_transfer"},
    "container-infrastructure-management": {"cluster"},
    "compute": set(),
    "database": {"backup", "cluster", "configuration", "instance", "module"},
    "key-manager": {"container", "order", "secret"},
    "orchestration": {"stack"},
    "shared-file-system": {"share", "share_group", "share_snapshot", "user_message"},
    "workflow": {"execution", "workflow"},
}


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _field(record: Mapping[str, Any], *names: str) -> Any:
    """Get a field from CLI/catalog output, tolerating key casing."""

    lowered = {str(key).casefold(): value for key, value in record.items()}
    for name in names:
        if name in record:
            return record[name]
        value = lowered.get(name.casefold())
        if value is not None:
            return value
    return None


def _boolean(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return _text(value).strip().casefold() not in {"0", "false", "no", "disabled"}


def normalize_interface(value: Any) -> str:
    value = _text(value).strip().casefold()
    return {
        "publicurl": "public",
        "internalurl": "internal",
        "adminurl": "admin",
    }.get(value, value)


def canonical_service_type(value: Any) -> str:
    """Return Keystone's canonical service type when os-service-types knows it."""

    value = _text(value).strip()
    if not value:
        return value
    try:
        service_types = importlib.import_module("os_service_types").ServiceTypes()
        canonical = service_types.get_service_type(value)
        if canonical:
            return canonical
    except (ImportError, AttributeError, TypeError):
        pass
    return value.casefold()


def _endpoint_from_record(record: Mapping[str, Any]) -> Endpoint | None:
    raw_type = _field(record, "Service Type", "service_type", "type")
    url = _field(record, "URL", "url", "publicURL", "public_url")
    if not raw_type or not url:
        return None
    return Endpoint(
        endpoint_id=_text(_field(record, "ID", "id")),
        region=_text(_field(record, "Region", "region")),
        service_name=_text(_field(record, "Service Name", "service_name", "name")),
        raw_service_type=_text(raw_type),
        service_type=canonical_service_type(raw_type),
        interface=normalize_interface(_field(record, "Interface", "interface")),
        url=_text(url),
        enabled=_boolean(_field(record, "Enabled", "enabled")),
    )


def parse_endpoint_list_json(payload: str) -> list[Endpoint]:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise EndpointDiscoveryError(
            f"openstack endpoint list returned invalid JSON: {exc}"
        ) from exc
    if not isinstance(value, list):
        raise EndpointDiscoveryError("openstack endpoint list JSON was not an array")
    endpoints = [
        endpoint
        for record in value
        if isinstance(record, Mapping)
        for endpoint in [_endpoint_from_record(record)]
        if endpoint is not None and endpoint.enabled
    ]
    if not endpoints:
        raise EndpointDiscoveryError("openstack endpoint list returned no enabled endpoints")
    return endpoints


def _catalog_endpoints(catalog: Iterable[Mapping[str, Any]]) -> list[Endpoint]:
    """Convert openstacksdk's Keystone catalog shape into Endpoint objects."""

    endpoints: list[Endpoint] = []
    for service in catalog:
        if not isinstance(service, Mapping):
            continue
        raw_type = _field(service, "type", "service_type")
        if not raw_type:
            continue
        service_name = _text(_field(service, "name", "service_name"))
        service_id = _text(_field(service, "id", "service_id"))
        service_endpoints = service.get("endpoints", [])
        if isinstance(service_endpoints, Mapping):
            # Keystone v2-style catalog: publicURL/internalURL/adminURL.
            service_endpoints = [
                {
                    "interface": key,
                    "url": value,
                }
                for key, value in service_endpoints.items()
                if _text(key).casefold().endswith("url")
                or _text(key).casefold() in {"public", "internal", "admin"}
            ]
        for item in service_endpoints or []:
            if not isinstance(item, Mapping):
                continue
            interface = normalize_interface(_field(item, "interface", "Interface"))
            if not interface:
                for candidate in ("public", "internal", "admin"):
                    if _field(item, candidate, f"{candidate}URL", f"{candidate}_url"):
                        interface = candidate
                        break
            url = _field(item, "url", "URL")
            if not url:
                url = _field(item, f"{interface}URL", f"{interface}_url")
            if not url:
                continue
            endpoints.append(
                Endpoint(
                    endpoint_id=_text(_field(item, "id", "ID")) or service_id,
                    region=_text(_field(item, "region", "Region")),
                    service_name=service_name,
                    raw_service_type=_text(raw_type),
                    service_type=canonical_service_type(raw_type),
                    interface=interface,
                    url=_text(url),
                    enabled=_boolean(_field(item, "enabled", "Enabled")),
                )
            )
    endpoints = [endpoint for endpoint in endpoints if endpoint.enabled]
    if not endpoints:
        raise EndpointDiscoveryError("the Keystone service catalog has no enabled endpoints")
    return endpoints


def _run_endpoint_command(binary: str, timeout: int = 60) -> list[Endpoint]:
    try:
        completed = subprocess.run(
            [binary, "endpoint", "list", "-f", "json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise FileNotFoundError(binary) from exc
    except subprocess.TimeoutExpired as exc:
        raise EndpointDiscoveryError(
            f"{binary} endpoint list timed out after {timeout} seconds"
        ) from exc
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise EndpointDiscoveryError(
            f"{binary} endpoint list failed with exit code {completed.returncode}"
            + (f": {detail}" if detail else "")
        )
    return parse_endpoint_list_json(completed.stdout)


def discover_endpoints(connection: Any, binary: str = "openstack") -> list[Endpoint]:
    """Discover endpoints from openstacksdk, with the CLI as a fallback."""

    try:
        return _catalog_endpoints(connection.service_catalog)
    except InventoryError as catalog_error:
        catalog_failure = catalog_error
    except Exception as exc:
        catalog_failure = EndpointDiscoveryError(
            f"could not read the Keystone service catalog with openstacksdk: {exc}"
        )

    resolved_binary = shutil.which(binary) or binary
    try:
        return _run_endpoint_command(resolved_binary)
    except FileNotFoundError as cli_error:
        raise EndpointDiscoveryError(
            f"openstacksdk endpoint discovery failed: {catalog_failure}; "
            f"CLI fallback {binary!r} is unavailable"
        ) from cli_error
    except EndpointDiscoveryError as cli_error:
        raise EndpointDiscoveryError(
            f"openstacksdk endpoint discovery failed: {catalog_failure}; "
            f"CLI fallback also failed: {cli_error}"
        ) from cli_error


def select_endpoints(
    endpoints: Iterable[Endpoint],
    interface: str,
    region: str | None,
) -> list[Endpoint]:
    """Choose one endpoint per service type for the requested interface/region."""

    interface = normalize_interface(interface)
    candidates = [
        endpoint
        for endpoint in endpoints
        if endpoint.enabled and not _is_ignored_endpoint(endpoint)
    ]
    if region:
        candidates = [endpoint for endpoint in candidates if endpoint.region == region]
    if not candidates:
        suffix = f" in region {region!r}" if region else ""
        raise EndpointDiscoveryError(
            f"no enabled OpenStack endpoints match interface {interface!r}{suffix}"
        )

    selected: list[Endpoint] = []
    by_service: dict[str, list[Endpoint]] = {}
    for endpoint in candidates:
        by_service.setdefault(endpoint.service_type, []).append(endpoint)

    for service_type, service_endpoints in sorted(by_service.items()):
        matching = [endpoint for endpoint in service_endpoints if endpoint.interface == interface]
        if not matching:
            names = ", ".join(
                sorted({endpoint.service_name or endpoint.raw_service_type for endpoint in service_endpoints})
            )
            raise EndpointDiscoveryError(
                f"service {service_type!r} ({names}) has no {interface!r} endpoint"
            )

        def rank(endpoint: Endpoint) -> tuple[int, int, str, str]:
            version = re.search(r"v(\d+)$", endpoint.raw_service_type.casefold())
            return (
                0 if endpoint.raw_service_type.casefold() == service_type.casefold() else 1,
                -(int(version.group(1)) if version else 0),
                endpoint.raw_service_type,
                endpoint.url,
            )

        selected.append(sorted(matching, key=rank)[0])
    return selected


def _connect(interface: str, region: str | None) -> Any:
    try:
        openstack = importlib.import_module("openstack")
    except ImportError as exc:
        raise InventoryError(
            "openstacksdk is required for project resolution and resource listing"
        ) from exc
    kwargs: dict[str, Any] = {"strict_proxies": True, "interface": interface}
    if region:
        kwargs["region_name"] = region
    try:
        return openstack.connect(**kwargs)
    except Exception as exc:
        raise InventoryError(f"could not create the OpenStack SDK connection: {exc}") from exc


def resolve_project(connection: Any, value: str) -> Project:
    project = None
    try:
        project = connection.identity.get_project(value)
    except Exception:
        try:
            project = connection.identity.find_project(value, ignore_missing=False)
        except Exception as exc:
            raise InventoryError(f"could not resolve project {value!r}: {exc}") from exc
    if project is None:
        try:
            project = connection.identity.find_project(value, ignore_missing=False)
        except Exception as exc:
            raise InventoryError(f"could not resolve project {value!r}: {exc}") from exc
    project_id = _text(getattr(project, "id", None) or _value(project, "id"))
    if not project_id:
        raise InventoryError(f"project lookup for {value!r} returned no project ID")
    name = _text(getattr(project, "name", None) or _value(project, "name")) or value
    return Project(project_id=project_id, name=name, raw=project)


def _value(resource: Any, key: str, default: Any = None) -> Any:
    if isinstance(resource, Mapping):
        return resource.get(key, default)
    return getattr(resource, key, default)


def _resource_to_dict(resource: Any) -> dict[str, Any]:
    if isinstance(resource, Mapping):
        return {str(key): _jsonable(value) for key, value in resource.items()}
    to_dict = getattr(resource, "to_dict", None)
    if callable(to_dict):
        for kwargs in (
            {"ignore_none": False, "original_names": False},
            {"ignore_none": False},
            {},
        ):
            try:
                result = to_dict(**kwargs)
                if isinstance(result, Mapping):
                    return {str(key): _jsonable(value) for key, value in result.items()}
            except TypeError:
                continue
    info = getattr(resource, "_info", None)
    if isinstance(info, Mapping):
        return {str(key): _jsonable(value) for key, value in info.items()}
    if hasattr(resource, "__dict__"):
        return {
            str(key): _jsonable(value)
            for key, value in vars(resource).items()
            if not key.startswith("_")
        }
    return {"value": _jsonable(resource)}


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except TypeError:
            pass
    return _resource_to_dict(value)


def _connection_as_project(connection: Any, project_id: str) -> Any:
    try:
        # openstacksdk's connect_as_project treats a string as a project name;
        # passing a UUID through connect_as therefore avoids an accidental
        # name lookup and preserves the requested project scope.
        connect_as = getattr(connection, "connect_as", None)
        if callable(connect_as):
            return connect_as(project_id=project_id)
        return connection.connect_as_project(project_id)
    except Exception as exc:
        raise InventoryError(
            f"could not create a connection scoped to project {project_id!r}: {exc}"
        ) from exc


def _service_descriptions() -> list[Any]:
    try:
        mixin_module = importlib.import_module("openstack._services_mixin")
        description_type = importlib.import_module(
            "openstack.service_description"
        ).ServiceDescription
    except ImportError:
        return []
    descriptions = []
    seen: set[int] = set()
    for value in vars(mixin_module.ServicesMixin).values():
        if isinstance(value, description_type) and id(value) not in seen:
            descriptions.append(value)
            seen.add(id(value))
    return descriptions


def _description_matches(description: Any, endpoint: Endpoint) -> bool:
    candidates = {
        _text(getattr(description, "service_type", "")),
        canonical_service_type(getattr(description, "service_type", "")),
    }
    all_types = getattr(description, "all_types", ()) or ()
    candidates.update(_text(value) for value in all_types)
    return any(
        canonical_service_type(value) == endpoint.service_type
        or value.casefold() == endpoint.raw_service_type.casefold()
        for value in candidates
        if value
    )


def _resource_placeholders(resource_class: type) -> tuple[str, ...]:
    base_path = _text(getattr(resource_class, "base_path", ""))
    return tuple(dict.fromkeys(re.findall(r"%\(([^)]+)\)s", base_path)))


def _registry(proxy: Any) -> dict[str, type]:
    registry = getattr(proxy, "_resource_registry", None)
    if not isinstance(registry, Mapping):
        return {}
    return {
        _text(name): resource_class
        for name, resource_class in registry.items()
        if isinstance(resource_class, type)
        and getattr(resource_class, "allow_list", True)
    }


def _query_mapping(resource_class: type) -> set[str]:
    mapping = getattr(getattr(resource_class, "_query_mapping", None), "_mapping", {})
    return set(mapping) if isinstance(mapping, Mapping) else set()


def _has_project_field(resource_class: type) -> bool:
    return bool(
        _query_mapping(resource_class).intersection({"project_id", "tenant_id", "owner", "project"})
        or any(
            hasattr(resource_class, field)
            for field in ("project_id", "tenant_id", "owner", "project")
        )
    )


def _normalized_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _parent_name(placeholder: str) -> str:
    value = placeholder
    for suffix in ("_id", "_name"):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
            break
    return _normalized_name(value)


def _required_parameters(signature: inspect.Signature) -> list[inspect.Parameter]:
    return [
        parameter
        for parameter in signature.parameters.values()
        if parameter.default is parameter.empty
        and parameter.kind
        not in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD)
    ]


def _resource_attr(resource: Any, name: str) -> Any:
    value = _value(resource, name)
    if value is not None:
        return value
    if name.endswith("_id"):
        return _value(resource, "id")
    if name.endswith("_name"):
        return _value(resource, "name") or _value(resource, "id")
    if name in {"container", "queue_name", "stack_name", "namespace_name"}:
        return _value(resource, "name") or _value(resource, "id")
    return None


def _link_project_value(resource: Any) -> Any:
    links = _value(resource, "links")
    if isinstance(links, Mapping):
        links = [links]
    if not isinstance(links, Iterable) or isinstance(links, (str, bytes)):
        return None
    project_pattern = re.compile(
        r"/v\d+(?:\.\d+)?/([0-9a-f]{16,}(?:-[0-9a-f-]+)?)(?:/|$)",
        re.IGNORECASE,
    )
    for link in links:
        href = _value(link, "href")
        match = project_pattern.search(_text(href)) if href else None
        if match:
            return match.group(1)
    return None


def _project_value(resource: Any) -> Any:
    # Heat's ``user_project_id`` is the private stack-user project, not the
    # project that owns the stack. Prefer the explicit/location project first
    # and only use user_project_id as a fallback for APIs without that field.
    for field in ("project_id", "tenant_id", "tenant", "owner"):
        value = _value(resource, field)
        if value is not None:
            return value
    value = _link_project_value(resource)
    if value is not None:
        return value
    project = _value(resource, "project")
    if project is not None:
        for field in ("id", "project_id", "tenant_id"):
            value = _value(project, field)
            if value is not None:
                return value
    location = _value(resource, "location")
    location_project = _value(location, "project") if location is not None else None
    if location_project is not None:
        for field in ("id", "project_id", "tenant_id"):
            value = _value(location_project, field)
            if value is not None:
                return value
    value = _value(resource, "user_project_id")
    if value is not None:
        return value
    return None


def _belongs_to_project(resource: Any, project_id: str) -> bool:
    value = _project_value(resource)
    return value is None or _text(value) == project_id


def _filter_related_resources(
    service_type: str,
    resource_name: str,
    objects: list[Any],
    parents: Mapping[str, list[Any]],
) -> list[Any]:
    # Mistral exposes workflow executions through an administrator-wide
    # collection even when the connection is project scoped.  An execution
    # has no reliable project field, so retain only executions belonging to a
    # workflow already owned by the requested project.
    if service_type == "workflow" and resource_name in {
        "execution",
        "workflow_execution",
    }:
        workflows = parents.get("workflow", [])
        workflow_ids = {
            _text(_value(workflow, "id"))
            for workflow in workflows
            if _value(workflow, "id") is not None
        }
        return [
            obj
            for obj in objects
            if _text(_value(obj, "workflow_id")) in workflow_ids
        ]
    return objects


def _list_all(proxy: Any, resource_class: type, **kwargs: Any) -> list[Any]:
    list_method = getattr(proxy, "_list", None)
    if not callable(list_method):
        raise UnsupportedClientError(
            f"SDK proxy {type(proxy).__name__} has no generic list method"
        )
    try:
        result = list_method(resource_class, **kwargs)
        return list(result) if result is not None else []
    except Exception as exc:
        name = _text(getattr(resource_class, "resource_key", "")) or resource_class.__name__
        raise InventoryError(f"listing SDK resource {name!r} failed: {exc}") from exc


class BuiltinSDKProvider:
    """Provider backed by one openstacksdk service proxy."""

    def __init__(self, endpoint: Endpoint, connection: Any, description: Any):
        self.endpoint = endpoint
        self.connection = connection
        self.description = description
        self.name = "openstacksdk"
        self.errors: list[dict[str, str]] = []
        supported_versions = getattr(description, "supported_versions", {})
        if not supported_versions:
            raise UnsupportedClientError(
                f"service {endpoint.service_type!r} has an openstacksdk descriptor but no supported resource version"
            )
        try:
            module = importlib.import_module(description.__class__.__module__)
            if module is None:
                raise ImportError("service descriptor module was not importable")
            for proxy_class in supported_versions.values():
                proxy_module_name = _text(getattr(proxy_class, "__module__", ""))
                if proxy_module_name:
                    importlib.import_module(proxy_module_name)
            self.proxy = description.__get__(connection, type(connection))
        except Exception as exc:
            raise InventoryError(
                f"could not initialize the openstacksdk client for endpoint "
                f"{endpoint.service_type!r} ({endpoint.url}): {exc}"
            ) from exc
        self.resources = _registry(self.proxy)
        if not self.resources:
            raise UnsupportedClientError(
                f"openstacksdk has no listable resource registry for service {endpoint.service_type!r}"
            )

    def _project_kwargs(self, resource_class: type, project_id: str) -> dict[str, Any]:
        query = _query_mapping(resource_class)
        for key in ("project_id", "tenant_id", "owner", "project"):
            if key in query:
                return {key: project_id}
        return {}

    def _direct_specs(self) -> list[ResourceSpec]:
        specs = []
        exceptions = PROJECT_SCOPED_EXCEPTIONS.get(self.endpoint.service_type, set())
        for name, resource_class in self.resources.items():
            if name in GLOBAL_RESOURCE_NAMES and name not in exceptions:
                continue
            if _non_deletable_resource(self.endpoint.service_type, name):
                continue
            placeholders = _resource_placeholders(resource_class)
            if placeholders:
                continue
            if _has_project_field(resource_class) or name in exceptions:
                specs.append(ResourceSpec(name, resource_class, placeholders))
        return sorted(specs, key=lambda spec: spec.name)

    def _nested_specs(self) -> list[ResourceSpec]:
        specs = []
        exceptions = PROJECT_SCOPED_EXCEPTIONS.get(self.endpoint.service_type, set())
        for name, resource_class in self.resources.items():
            if name in GLOBAL_RESOURCE_NAMES and name not in exceptions:
                continue
            if _non_deletable_resource(self.endpoint.service_type, name):
                continue
            placeholders = _resource_placeholders(resource_class)
            if self.endpoint.service_type == "object-store" and name == "object":
                # Objects are listed in the container-aware block above.
                continue
            if placeholders:
                specs.append(ResourceSpec(name, resource_class, placeholders))
        return sorted(specs, key=lambda spec: (len(spec.placeholders), spec.name))

    def _list_spec(self, spec: ResourceSpec, project_id: str, parents: Mapping[str, list[Any]]) -> list[Any]:
        if not spec.placeholders:
            objects = _list_all(
                self.proxy,
                spec.resource_class,
                **self._project_kwargs(spec.resource_class, project_id),
            )
            return [obj for obj in objects if _belongs_to_project(obj, project_id)]

        parent_options: list[list[Any]] = []
        for placeholder in spec.placeholders:
            matches = [
                values
                for name, values in parents.items()
                if _normalized_name(name) == _parent_name(placeholder)
            ]
            if not matches:
                return []
            parent_options.append(matches[0])
        if any(not values for values in parent_options):
            return []

        objects: list[Any] = []
        seen_kwargs: set[tuple[tuple[str, str], ...]] = set()
        for combination in itertools.product(*parent_options):
            kwargs = {
                placeholder: _resource_attr(parent, placeholder)
                for placeholder, parent in zip(spec.placeholders, combination)
            }
            if any(value is None for value in kwargs.values()):
                continue
            kwargs.update(self._project_kwargs(spec.resource_class, project_id))
            key = tuple(sorted((key, _text(value)) for key, value in kwargs.items()))
            if key in seen_kwargs:
                continue
            seen_kwargs.add(key)
            objects.extend(_list_all(self.proxy, spec.resource_class, **kwargs))
        return [obj for obj in objects if _belongs_to_project(obj, project_id)]

    def collect(
        self, project: Project, strict: bool = True
    ) -> dict[str, list[dict[str, Any]]]:
        self.errors = []
        result: dict[str, list[dict[str, Any]]] = {}
        objects_by_resource: dict[str, list[Any]] = {}

        # A project is itself an identity resource, but listing all projects
        # would violate the requested project scope.  Include the resolved one.
        if self.endpoint.service_type == "identity":
            result["project"] = [_resource_to_dict(project.raw)]
            objects_by_resource["project"] = [project.raw]

        for spec in self._direct_specs():
            try:
                objects = self._list_spec(spec, project.project_id, objects_by_resource)
            except InventoryError as exc:
                if not _is_missing_resource_error(exc):
                    self.errors.append(_provider_error(spec.name, exc))
                if strict and not _is_missing_resource_error(exc):
                    raise
                objects = []
            objects_by_resource[spec.name] = objects
            result[spec.name] = [_resource_to_dict(obj) for obj in objects]

        for name, objects in list(objects_by_resource.items()):
            filtered = _filter_related_resources(
                self.endpoint.service_type,
                name,
                objects,
                objects_by_resource,
            )
            objects_by_resource[name] = filtered
            if name in result:
                result[name] = [_resource_to_dict(obj) for obj in filtered]

        # Object-store objects are nested below containers and are not safely
        # discoverable from the generic registry without a container value.
        if self.endpoint.service_type == "object-store":
            container_class = self.resources.get("container")
            object_class = self.resources.get("object")
            if not container_class or not object_class:
                exc = UnsupportedClientError(
                    "openstacksdk object-store proxy does not expose both container and object resources"
                )
                self.errors.append(_provider_error("object-store", exc))
                if strict:
                    raise exc
            else:
                containers = objects_by_resource.get("container")
                if containers is None:
                    try:
                        containers = _list_all(self.proxy, container_class)
                    except InventoryError as exc:
                        if not _is_missing_resource_error(exc):
                            self.errors.append(_provider_error("container", exc))
                        if strict and not _is_missing_resource_error(exc):
                            raise
                        containers = []
                    objects_by_resource["container"] = containers
                    result["container"] = [_resource_to_dict(obj) for obj in containers]
                all_objects: list[dict[str, Any]] = []
                for container in containers:
                    container_name = _resource_attr(container, "container")
                    if container_name is None:
                        continue
                    try:
                        for obj in _list_all(
                            self.proxy,
                            object_class,
                            container=container_name,
                        ):
                            serialized = _resource_to_dict(obj)
                            serialized.setdefault("container", container_name)
                            all_objects.append(serialized)
                    except InventoryError as exc:
                        if not _is_missing_resource_error(exc):
                            self.errors.append(_provider_error("object", exc))
                        if strict and not _is_missing_resource_error(exc):
                            raise
                result["object"] = all_objects

        pending = self._nested_specs()
        while pending:
            progressed = False
            remaining: list[ResourceSpec] = []
            for spec in pending:
                if not all(
                    any(
                        _normalized_name(name) == _parent_name(placeholder)
                        and values
                        for name, values in objects_by_resource.items()
                    )
                    for placeholder in spec.placeholders
                ):
                    remaining.append(spec)
                    continue
                try:
                    objects = self._list_spec(spec, project.project_id, objects_by_resource)
                except InventoryError as exc:
                    if not _is_missing_resource_error(exc):
                        self.errors.append(_provider_error(spec.name, exc))
                    if strict and not _is_missing_resource_error(exc):
                        raise
                    objects = []
                objects_by_resource[spec.name] = objects
                result[spec.name] = [_resource_to_dict(obj) for obj in objects]
                progressed = True
            if not progressed:
                break
            pending = remaining
        return result


class LegacyContext:
    """Small compatibility context accepted by openstackclient plugins."""

    def __init__(
        self,
        connection: Any,
        endpoint: Endpoint,
        api_name: str,
        api_version: str,
    ):
        self.session = connection.session
        self.auth = getattr(self.session, "auth", None)
        self.region_name = endpoint.region
        self.interface = endpoint.interface
        self._interface = endpoint.interface
        self._region_name = endpoint.region
        self._api_version = {api_name: api_version}
        self._insecure = False
        self.verify = getattr(self.session, "verify", True)
        self.cert = None
        self.cacert = None
        self._cacert = None
        self.endpoint = endpoint.url
        self._cli_options = type("CLIOptions", (), {"debug": False})()

    def get_endpoint_for_service_type(self, service_type: str, **_: Any) -> str:
        return self.endpoint

    def get_configuration(self) -> dict[str, Any]:
        return {}

    def setup_auth(self) -> None:
        return None


def _entry_points(group: str) -> list[Any]:
    entries = importlib.metadata.entry_points()
    if hasattr(entries, "select"):
        return list(entries.select(group=group))
    if isinstance(entries, Mapping):
        return list(entries.get(group, ()))
    return [entry for entry in entries if getattr(entry, "group", None) == group]


def _legacy_root(module: Any) -> str:
    root = _text(getattr(module, "__name__", "")).split(".")[0]
    return re.sub(r"client$", "", root.casefold())


def _legacy_score(endpoint: Endpoint, module: Any) -> int:
    api_name = _text(getattr(module, "API_NAME", ""))
    root = _legacy_root(module)
    score = 0
    if api_name and canonical_service_type(api_name) == endpoint.service_type:
        score = max(score, 100)
    if api_name.casefold() == endpoint.raw_service_type.casefold():
        score = max(score, 110)
    service_name = endpoint.service_name.casefold().removesuffix("v2").removesuffix("v3")
    if root and root == service_name:
        score = max(score, 90)
    try:
        project_name = importlib.import_module("os_service_types").ServiceTypes().get_project_name(
            endpoint.raw_service_type
        )
        if project_name and root == _text(project_name).casefold():
            score = max(score, 80)
    except (ImportError, AttributeError, TypeError):
        pass
    return score


def _plugin_api_version(module: Any) -> str:
    versions = getattr(module, "API_VERSIONS", {})
    option = _text(getattr(module, "API_VERSION_OPTION", ""))
    if option:
        environment_key = option.upper().replace("-", "_")
        configured = os.environ.get(environment_key)
        if configured:
            return configured
    default_version = _text(getattr(module, "DEFAULT_API_VERSION", ""))
    if default_version:
        return default_version
    if isinstance(versions, Mapping) and versions:
        def version_key(value: str) -> tuple[tuple[int, Any], ...]:
            return tuple(
                (0, int(part)) if part.isdigit() else (1, part)
                for part in re.split(r"[.]", value)
            )

        return sorted(
            (_text(version) for version in versions), key=version_key
        )[-1]
    return "1"


class LegacySDKProvider:
    """Provider backed by a dynamically discovered legacy Python client."""

    def __init__(self, endpoint: Endpoint, connection: Any, entry: Any, module: Any):
        self.endpoint = endpoint
        self.connection = connection
        self.entry = entry
        self.module = module
        self.name = f"{module.__name__} (legacy SDK)"
        self.errors: list[dict[str, str]] = []
        self._unlisted_nested_managers: list[str] = []
        make_client = getattr(module, "make_client", None)
        if not callable(make_client):
            raise UnsupportedClientError(
                f"legacy client module {module.__name__!r} has no make_client function"
            )
        try:
            self.client = make_client(
                LegacyContext(
                    connection,
                    endpoint,
                    _text(getattr(module, "API_NAME", endpoint.raw_service_type)),
                    _plugin_api_version(module),
                )
            )
        except Exception as exc:
            raise InventoryError(
                f"could not initialize legacy SDK {module.__name__!r} for "
                f"endpoint {endpoint.service_type!r} ({endpoint.url}): {exc}"
            ) from exc

    @classmethod
    def from_client(
        cls, endpoint: Endpoint, connection: Any, module: Any, client: Any
    ) -> "LegacySDKProvider":
        provider = cls.__new__(cls)
        provider.endpoint = endpoint
        provider.connection = connection
        provider.entry = None
        provider.module = module
        provider.name = f"{module.__name__} (Python SDK)"
        provider.client = client
        provider.errors = []
        provider._unlisted_nested_managers = []
        return provider

    def _managers(self) -> list[tuple[str, Any, inspect.Signature]]:
        managers: list[tuple[str, Any, inspect.Signature]] = []
        self._nested_managers: list[tuple[str, Any, inspect.Signature]] = []
        for name in sorted(dir(self.client)):
            if (
                name.startswith("_")
                or name in GLOBAL_RESOURCE_NAMES
                or name in GLOBAL_LEGACY_MANAGER_NAMES
                or _non_deletable_resource(self.endpoint.service_type, name)
            ):
                continue
            manager = getattr(self.client, name, None)
            list_method = getattr(manager, "list", None)
            if not callable(list_method):
                continue
            try:
                signature = inspect.signature(list_method)
            except (TypeError, ValueError):
                signature = inspect.Signature()
            required = _required_parameters(signature)
            if required:
                # Keep nested collections for the generic parent traversal
                # performed after direct project resources are listed.
                self._nested_managers.append((name, manager, signature))
                continue
            managers.append((name, manager, signature))
        return managers

    @staticmethod
    def _parent_values(
        parameter_name: str, objects_by_manager: Mapping[str, list[Any]]
    ) -> list[Any] | None:
        parent = parameter_name
        for suffix in ("_id", "_name"):
            if parent.endswith(suffix):
                parent = parent[: -len(suffix)]
                break
        normalized_parent = _normalized_name(parent)
        for manager_name, values in objects_by_manager.items():
            normalized_manager = _normalized_name(manager_name)
            if normalized_manager == normalized_parent or normalized_manager.rstrip("s") == normalized_parent:
                return values
        return None

    @staticmethod
    def _parent_argument(resource: Any, parameter_name: str) -> Any:
        if parameter_name.endswith("_name") or parameter_name in {
            "container",
            "queue_name",
            "stack_name",
            "namespace_name",
        }:
            return _value(resource, "name") or _value(resource, "id")
        # Legacy managers such as zun's actions.list(container) use an ID
        # even though the argument does not end in _id.
        return _value(resource, "id") or _value(resource, "uuid")

    @staticmethod
    def _kwargs(signature: inspect.Signature, project_id: str) -> dict[str, Any]:
        names = set(signature.parameters)
        kwargs: dict[str, Any] = {}
        for key in ("project_id", "tenant_id", "project"):
            if key in names:
                kwargs[key] = project_id
                break
        if "filters" in names and not kwargs:
            kwargs["filters"] = {"project_id": project_id}
        for key in ("all_projects", "all_tenants", "all_projects_mode"):
            if key in names:
                kwargs[key] = False
        return kwargs

    def collect(
        self, project: Project, strict: bool = True
    ) -> dict[str, list[dict[str, Any]]]:
        self.errors = []
        managers = self._managers()
        nested_managers = getattr(self, "_nested_managers", [])
        if not managers:
            if not nested_managers:
                exc = UnsupportedClientError(
                    f"legacy SDK {self.module.__name__!r} exposes no listable project resources"
                )
                self.errors.append(_provider_error("(provider)", exc))
                if strict:
                    raise exc
        result: dict[str, list[dict[str, Any]]] = {}
        objects_by_manager: dict[str, list[Any]] = {}
        for name, manager, signature in managers:
            kwargs = self._kwargs(signature, project.project_id)
            try:
                values = manager.list(**kwargs)
                values = [] if values is None else list(values)
            except Exception as exc:
                error = InventoryError(
                    f"listing legacy SDK resource {name!r} for service "
                    f"{self.endpoint.service_type!r} failed: {exc}"
                )
                if not _is_missing_resource_error(error):
                    self.errors.append(_provider_error(name, error))
                if strict and not _is_missing_resource_error(error):
                    raise error from exc
                objects_by_manager[name] = []
                continue
            values = [value for value in values if _belongs_to_project(value, project.project_id)]
            objects_by_manager[name] = values
            if values:
                result[name] = [_resource_to_dict(value) for value in values]

        for name, values in list(objects_by_manager.items()):
            objects_by_manager[name] = _filter_related_resources(
                self.endpoint.service_type,
                name,
                values,
                objects_by_manager,
            )
            if name in result:
                result[name] = [
                    _resource_to_dict(value) for value in objects_by_manager[name]
                ]

        unlisted_nested: list[str] = []
        for name, manager, signature in nested_managers:
            required = _required_parameters(signature)
            parent_options = [
                self._parent_values(parameter.name, objects_by_manager)
                for parameter in required
            ]
            if any(values is None for values in parent_options):
                unlisted_nested.append(name)
                continue
            if any(not values for values in parent_options):
                objects_by_manager[name] = []
                continue

            nested_values: list[Any] = []
            for combination in itertools.product(*parent_options):
                arguments = [
                    self._parent_argument(resource, parameter.name)
                    for parameter, resource in zip(required, combination)
                ]
                if any(value is None for value in arguments):
                    unlisted_nested.append(name)
                    continue
                args = []
                kwargs = {}
                for parameter, value in zip(required, arguments):
                    if parameter.kind == parameter.KEYWORD_ONLY:
                        kwargs[parameter.name] = value
                    else:
                        args.append(value)
                try:
                    values = manager.list(*args, **kwargs)
                    if values:
                        if (
                            self.endpoint.service_type == "object-store"
                            and _normalized_name(name) in {"object", "objects"}
                        ):
                            container_name = self._parent_argument(
                                combination[0], required[0].name
                            )
                            for value in values:
                                serialized = _resource_to_dict(value)
                                serialized.setdefault("container", container_name)
                                nested_values.append(serialized)
                        else:
                            nested_values.extend(values)
                except Exception as exc:
                    error = InventoryError(
                        f"listing nested legacy SDK resource {name!r} for service "
                        f"{self.endpoint.service_type!r} failed: {exc}"
                    )
                    if not _is_missing_resource_error(error):
                        self.errors.append(_provider_error(name, error))
                    if strict and not _is_missing_resource_error(error):
                        raise error from exc
            objects_by_manager[name] = nested_values
            if nested_values:
                result[name] = [_resource_to_dict(value) for value in nested_values]

        if unlisted_nested:
            error = UnsupportedClientError(
                f"legacy SDK {self.module.__name__!r} exposes nested list resources "
                f"that could not be enumerated: {', '.join(sorted(set(unlisted_nested)))}"
            )
            for name in sorted(set(unlisted_nested)):
                self.errors.append(_provider_error(name, error))
            if strict:
                raise error
        return result


def _legacy_provider(endpoint: Endpoint, connection: Any) -> LegacySDKProvider | None:
    candidates: list[tuple[int, Any, Any]] = []
    for entry in _entry_points("openstack.cli.extension"):
        try:
            module = entry.load()
        except Exception:
            continue
        score = _legacy_score(endpoint, module)
        if score:
            candidates.append((score, entry, module))
    if not candidates:
        return None
    _, entry, module = sorted(
        candidates,
        key=lambda item: (-item[0], item[2].__name__),
    )[0]
    return LegacySDKProvider(endpoint, connection, entry, module)


def _client_package_candidates(endpoint: Endpoint) -> list[str]:
    seeds = {
        endpoint.service_name,
        endpoint.raw_service_type,
        endpoint.service_type,
    }
    try:
        project_name = importlib.import_module("os_service_types").ServiceTypes().get_project_name(
            endpoint.raw_service_type
        )
        if project_name:
            seeds.add(_text(project_name))
    except (ImportError, AttributeError, TypeError):
        pass

    candidates: list[str] = []
    for seed in seeds:
        value = re.sub(r"[^a-z0-9]", "", _text(seed).casefold())
        value = re.sub(r"v\d+$", "", value)
        if value:
            candidate = f"{value}client"
            if candidate not in candidates:
                candidates.append(candidate)
    return sorted(candidates)


def _generic_client_classes(endpoint: Endpoint) -> list[tuple[Any, Any]]:
    """Find conventional Client classes without requiring an OSC plugin."""

    classes: list[tuple[Any, Any]] = []
    seen: set[tuple[str, str]] = set()
    for package_name in _client_package_candidates(endpoint):
        try:
            if importlib.util.find_spec(package_name) is None:
                continue
            package = importlib.import_module(package_name)
        except (ImportError, ModuleNotFoundError, ValueError):
            continue
        module_names = [package_name]
        if hasattr(package, "__path__"):
            module_names.extend(
                name
                for _, name, _ in pkgutil.walk_packages(
                    package.__path__, package.__name__ + "."
                )
                if name.rsplit(".", 1)[-1] == "client"
            )
        module_names = sorted(
            set(module_names),
            key=lambda name: (
                -int(version.group(1))
                if (version := re.search(r"\.v(\d+)(?:\.|$)", name))
                else 1,
                name,
            ),
        )
        for module_name in module_names:
            try:
                module = importlib.import_module(module_name)
            except (ImportError, ModuleNotFoundError):
                continue
            client_class = getattr(module, "Client", None)
            if not isinstance(client_class, type):
                continue
            key = (module.__name__, client_class.__name__)
            if key not in seen:
                classes.append((module, client_class))
                seen.add(key)
    return classes


def _generic_client_provider(endpoint: Endpoint, connection: Any) -> LegacySDKProvider | None:
    for module, client_class in _generic_client_classes(endpoint):
        attempts = [
            {
                "session": connection.session,
                "region_name": endpoint.region,
                "interface": endpoint.interface,
                "service_type": endpoint.raw_service_type,
            },
            {
                "session": connection.session,
                "region_name": endpoint.region,
                "endpoint_type": endpoint.interface,
                "service_type": endpoint.raw_service_type,
            },
            {
                "session": connection.session,
                "region_name": endpoint.region,
                "endpoint": endpoint.url,
            },
            {"session": connection.session, "endpoint": endpoint.url},
            {"session": connection.session},
        ]
        last_type_error: TypeError | None = None
        for kwargs in attempts:
            try:
                client = client_class(**kwargs)
            except TypeError as exc:
                # Constructor keyword differences are expected across the
                # legacy clients; try the next conventional signature.
                last_type_error = exc
                continue
            except Exception as exc:
                raise InventoryError(
                    f"could not initialize Python SDK {module.__name__!r} for "
                    f"endpoint {endpoint.service_type!r} ({endpoint.url}): {exc}"
                ) from exc
            return LegacySDKProvider.from_client(endpoint, connection, module, client)
        if last_type_error is not None:
            continue
    return None


def resolve_provider(endpoint: Endpoint, connection: Any) -> Any:
    descriptions = [
        description
        for description in _service_descriptions()
        if _description_matches(description, endpoint)
    ]
    # An actual openstacksdk descriptor wins over a legacy CLI plugin.  The
    # descriptor is authoritative for service integrations maintained there.
    supported = [
        description
        for description in descriptions
        if getattr(description, "supported_versions", {})
    ]
    sdk_error: InventoryError | None = None
    for description in supported:
        try:
            return BuiltinSDKProvider(endpoint, connection, description)
        except InventoryError as exc:
            # Some releases advertise a service descriptor but do not ship a
            # usable version for the endpoint.  Give the installed legacy
            # client a chance before reporting the service as unavailable.
            sdk_error = exc

    provider = _legacy_provider(endpoint, connection)
    if provider is not None:
        return provider
    if sdk_error is not None:
        raise sdk_error
    provider = _generic_client_provider(endpoint, connection)
    if provider is not None:
        return provider
    attempted = (
        "openstacksdk service descriptors, installed openstack.cli.extension clients, "
        "and conventional Python Client modules"
    )
    raise UnsupportedClientError(
        f"endpoint {endpoint.service_type!r} ({endpoint.raw_service_type!r}, "
        f"service name {endpoint.service_name!r}) has no list-capable Python client; "
        f"checked {attempted}"
    )


def _as_project_connection(connection: Any, project: Project) -> Any:
    current_project = getattr(connection, "current_project_id", None)
    if current_project and _text(current_project) == project.project_id:
        return connection
    return _connection_as_project(connection, project.project_id)


def inventory(
    endpoints: list[Endpoint],
    connection: Any,
    project: Project,
    strict: bool = True,
) -> dict[str, Any]:
    endpoints = [endpoint for endpoint in endpoints if not _is_ignored_endpoint(endpoint)]
    target_connection = _as_project_connection(connection, project)

    # Resolve every selected endpoint before making resource calls.  A missing
    # client is retained as a report error in best-effort mode so other
    # endpoints can still be inventoried.
    providers: list[tuple[Endpoint, Any]] = []
    errors: list[dict[str, str]] = []
    for endpoint in endpoints:
        try:
            providers.append((endpoint, resolve_provider(endpoint, target_connection)))
        except InventoryError as exc:
            errors.append(
                {
                    "service": endpoint.service_type,
                    "resource": "(client)",
                    "error": (
                        f"{endpoint.service_type} ({endpoint.raw_service_type}, "
                        f"{endpoint.url}): {exc}"
                    ),
                }
            )
    if errors and strict:
        raise InventoryError(
            "service client preflight failed; no resources were listed:\n- "
            + "\n- ".join(issue["error"] for issue in errors)
        )

    resources: dict[str, Any] = {}
    provider_metadata: dict[str, Any] = {}
    for endpoint, provider in providers:
        try:
            collected = provider.collect(project, strict=strict)
        except Exception as exc:
            if strict:
                raise
            errors.append(
                {
                    "service": endpoint.service_type,
                    "resource": "(provider)",
                    "error": _text(exc),
                }
            )
            collected = {}
        resources[endpoint.service_type] = collected
        provider_metadata[endpoint.service_type] = {
            "provider": provider.name,
            "service_name": endpoint.service_name,
            "raw_service_type": endpoint.raw_service_type,
            "interface": endpoint.interface,
            "region": endpoint.region,
            "url": endpoint.url,
        }
        for issue in getattr(provider, "errors", ()):
            errors.append(
                {
                    "service": endpoint.service_type,
                    "resource": issue["resource"],
                    "error": issue["error"],
                }
            )
    return {
        "project": {"id": project.project_id, "name": project.name},
        "endpoints": [dataclasses.asdict(endpoint) for endpoint in endpoints],
        "providers": provider_metadata,
        "resources": resources,
        "errors": errors,
    }


def _short(value: Any, width: int = 60) -> str:
    value = _text(value)
    if len(value) <= width:
        return value
    return value[: width - 1] + "…"


def render_table(report: Mapping[str, Any]) -> str:
    lines = [
        f"Project: {report['project']['name']} ({report['project']['id']})",
        "",
    ]
    resources = report.get("resources", {})
    if not any(
        objects
        for service_resources in resources.values()
        for objects in service_resources.values()
    ):
        lines.append("No project resources found.")
    else:
        for service_type in sorted(resources):
            provider = report.get("providers", {}).get(service_type, {})
            lines.append(
                f"{service_type} ({provider.get('provider', 'unknown')})"
            )
            for resource_type in sorted(resources[service_type]):
                objects = resources[service_type][resource_type]
                lines.append(f"  {resource_type}: {len(objects)}")
                if not objects:
                    continue
                rows = []
                for obj in objects:
                    rows.append(
                        [
                            _short(obj.get("id", obj.get("uuid", ""))),
                            _short(obj.get("name", obj.get("display_name", "")), 40),
                            _short(obj.get("status", obj.get("state", "")), 20),
                        ]
                    )
                lines.append("    ID                                      NAME                                      STATUS")
                for row in rows:
                    lines.append(f"    {row[0]:<40}  {row[1]:<40}  {row[2]}")
            lines.append("")
    errors = report.get("errors", [])
    if errors:
        lines.extend(("Errors:",))
        for issue in errors:
            lines.append(
                f"  {issue.get('service', 'unknown')} / "
                f"{issue.get('resource', 'unknown')}: {issue.get('error', '')}"
            )
    return "\n".join(lines).rstrip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="List all project-visible OpenStack resources using Python SDKs."
    )
    parser.add_argument(
        "--project",
        required=True,
        help="Project ID or project name.",
    )
    parser.add_argument(
        "--format",
        choices=("table", "json"),
        default="table",
        help="Output format (default: table).",
    )
    parser.add_argument(
        "--interface",
        default=os.environ.get("OS_INTERFACE", "public"),
        help="Keystone endpoint interface (default: OS_INTERFACE or public).",
    )
    parser.add_argument(
        "--region",
        default=os.environ.get("OS_REGION_NAME"),
        help="Limit discovery to this region (default: OS_REGION_NAME).",
    )
    parser.add_argument(
        "--openstack-bin",
        default=os.environ.get("OPENSTACK_BIN", "openstack"),
        help="Core OpenStack executable used as the endpoint-discovery fallback.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail instead of returning a partial inventory when a client or resource request fails.",
    )
    parser.add_argument(
        "--hide-errors",
        action="store_true",
        help="Hide client/resource errors and SDK error logs from the output.",
    )
    parser.add_argument("--debug", action="store_true", help="Show a traceback on failure.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        with _suppress_error_logs(args.hide_errors):
            # Use the SDK's authenticated Keystone catalog first.  The core
            # CLI remains an equivalent fallback for unusual SDK/catalog
            # failures.
            connection = _connect(args.interface, args.region)
            endpoints = discover_endpoints(connection, args.openstack_bin)
            endpoints = select_endpoints(endpoints, args.interface, args.region)
            project = resolve_project(connection, args.project)
            report = inventory(endpoints, connection, project, strict=args.strict)
        has_errors = bool(report.get("errors"))
        if args.hide_errors:
            report = {
                key: value for key, value in report.items() if key != "errors"
            }
        if args.format == "json":
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(render_table(report))
        return 2 if args.strict and has_errors else 0
    except InventoryError as exc:
        if args.debug:
            raise
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # Keep unexpected SDK incompatibilities actionable.
        if args.debug:
            raise
        print(f"error: unexpected inventory failure: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
